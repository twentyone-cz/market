import unittest
from unittest import mock

from common import fresh_db
import store
import vouchers


def paid_voucher_order(token="src", value=500, kind="voucher", days=0, qty=1):
    pid = store.add_product("V", "", value, kind=kind, days=days,
                            stock=-1, active=1)
    prod = store.get_product(pid)
    store.create_order(token, value * qty, None)
    store.add_item(token, prod, qty)
    store.mark_paid(token)
    vouchers.issue_for_order(token)
    return store.vouchers_for_order(token)


class Codes(unittest.TestCase):
    def test_generate_format_and_normalize(self):
        code = vouchers.generate_code()
        self.assertRegex(code, r"^JDNV-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertNotIn("O", code.replace("JDNV", ""))
        self.assertEqual(vouchers.normalize_code(code.lower().replace("-", " ")),
                         code)
        self.assertEqual(vouchers.normalize_code("kratke"), "")


class Issue(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_issue_per_unit_and_idempotent(self):
        rows = paid_voucher_order(qty=3)
        self.assertEqual(len(rows), 3)
        vouchers.issue_for_order("src")                      # podruhé nic
        self.assertEqual(len(store.vouchers_for_order("src")), 3)

    def test_days_enqueue_registration(self):
        paid_voucher_order(kind="days", days=30)
        pend = store.pending_redemptions()
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["days"], 30)

    def test_physical_items_no_codes(self):
        pid = store.add_product("H", "", 100, stock=1, active=1)
        store.create_order("o1", 100, "code", "balikovna")
        store.add_item("o1", store.get_product(pid), 1)
        store.mark_paid("o1")
        vouchers.issue_for_order("o1")
        self.assertEqual(store.vouchers_for_order("o1"), [])


class Discount(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_full_lifecycle(self):
        code = paid_voucher_order(value=500)[0]["code"]
        d, err = vouchers.voucher_discount(code, 300, "order2")
        self.assertEqual((d, err), (300, ""))                # strop = total
        # jednorázovost
        d2, err2 = vouchers.voucher_discount(code, 300, "order3")
        self.assertEqual(d2, 0)
        self.assertIn("uplatněn", err2)
        # release vrací kód do hry
        store.release_voucher("order2")
        d3, err3 = vouchers.voucher_discount(code, 1000, "order4")
        self.assertEqual((d3, err3), (500, ""))              # plná hodnota

    def test_invalid_inputs(self):
        self.assertEqual(vouchers.voucher_discount("blbost", 100, "o")[0], 0)
        code = vouchers.generate_code()
        self.assertIn("Neplatný", vouchers.voucher_discount(code, 100, "o")[1])

    def test_unpaid_source_rejected(self):
        pid = store.add_product("V", "", 500, kind="voucher", stock=-1, active=1)
        store.create_order("src", 500, None)
        store.add_item("src", store.get_product(pid), 1)
        # NEzaplaceno, ale kód uměle existuje
        store.add_voucher("JDNV-AAAA-BBBB-CCCC", "voucher", 500, 0, "src")
        _d, err = vouchers.voucher_discount("JDNV-AAAA-BBBB-CCCC", 100, "o2")
        self.assertIn("nezaplacené", err)

    def test_days_code_not_usable_as_credit(self):
        code = paid_voucher_order(kind="days", days=30)[0]["code"]
        d, _err = vouchers.voucher_discount(code, 100, "o2")
        self.assertEqual(d, 0)


class PushRedemptions(unittest.TestCase):
    def setUp(self):
        fresh_db()
        paid_voucher_order(kind="days", days=30)

    def test_waits_without_config(self):
        vouchers.push_redemptions()
        self.assertEqual(len(store.pending_redemptions()), 1)

    def test_success_marks_sent(self):
        store.set_setting("cockscale_api", "http://cs:8091")
        store.set_setting("cockscale_partner_secret", "s3cret")
        resp = mock.MagicMock()
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: False
        with mock.patch.object(vouchers.urllib.request, "urlopen",
                               return_value=resp) as m:
            vouchers.push_redemptions()
        self.assertEqual(store.pending_redemptions(), [])
        req = m.call_args[0][0]
        self.assertEqual(req.full_url, "http://cs:8091/partner/vouchers")
        self.assertIn("Bearer s3cret", req.get_header("Authorization"))

    def test_failure_bumps_attempts(self):
        store.set_setting("cockscale_api", "http://cs:8091")
        store.set_setting("cockscale_partner_secret", "s3cret")
        with mock.patch.object(vouchers.urllib.request, "urlopen",
                               side_effect=OSError("down")):
            vouchers.push_redemptions()
        pend = store.pending_redemptions()
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["attempts"], 1)


if __name__ == "__main__":
    unittest.main()

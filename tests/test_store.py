import time
import unittest

from common import fresh_db
import store


class ProductsStock(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_crud_and_listing(self):
        pid = store.add_product("Krabička", "popis", 1000, stock=3, active=1)
        self.assertEqual(store.get_product(pid)["name"], "Krabička")
        store.update_product(pid, price_sat=1500, active=0)
        self.assertEqual(store.get_product(pid)["price_sat"], 1500)
        self.assertEqual(store.list_products(), [])          # neaktivní
        self.assertEqual(len(store.list_products(False)), 1)
        store.update_product(pid, nonsense=1)                # ignorováno
        store.delete_product(pid)
        self.assertIsNone(store.get_product(pid))

    def test_reserve_all_or_nothing(self):
        a = store.get_product(store.add_product("A", "", 10, stock=2, active=1))
        b = store.get_product(store.add_product("B", "", 10, stock=0, active=1))
        ok, err = store.reserve_stock([(a, 1), (b, 1)])
        self.assertFalse(ok)
        self.assertIn("B", err)
        # nic se nestrhlo
        self.assertEqual(store.get_product(a["id"])["stock"], 2)

    def test_reserve_unlimited_and_return(self):
        a = store.get_product(store.add_product("A", "", 10, stock=2, active=1))
        d = store.get_product(store.add_product("D", "", 10, stock=-1, active=1))
        ok, _ = store.reserve_stock([(a, 2), (d, 5)])
        self.assertTrue(ok)
        self.assertEqual(store.get_product(a["id"])["stock"], 0)
        self.assertEqual(store.get_product(d["id"])["stock"], -1)
        store.create_order("tok1", 70, None)
        store.add_item("tok1", a, 2)
        store.add_item("tok1", d, 5)
        store.return_stock("tok1")
        self.assertEqual(store.get_product(a["id"])["stock"], 2)
        self.assertEqual(store.get_product(d["id"])["stock"], -1)

    def test_inactive_rejected(self):
        a = store.get_product(store.add_product("A", "", 10, stock=5, active=0))
        ok, _ = store.reserve_stock([(a, 1)])
        self.assertFalse(ok)


class OrdersPayments(unittest.TestCase):
    def setUp(self):
        fresh_db()
        self.p = store.get_product(
            store.add_product("P", "", 100, stock=5, active=1))
        store.create_order("tok", 200, "code", "balikovna", "12345678", None)
        store.add_item("tok", self.p, 2)

    def test_mark_paid_idempotent(self):
        self.assertTrue(store.mark_paid("tok"))
        self.assertFalse(store.mark_paid("tok"))     # podruhé už ne
        self.assertEqual(store.get_order("tok")["status"], "paid")

    def test_settle_idempotent(self):
        store.add_payment("h1", "tok", 200, "lnbc1")
        self.assertTrue(store.settle_payment("h1"))
        self.assertFalse(store.settle_payment("h1"))

    def test_expired_candidates_and_pending(self):
        self.assertEqual(store.expired_candidates(3600), [])
        store._execute("UPDATE orders SET created_at = created_at - 7200")
        self.assertEqual(len(store.expired_candidates(3600)), 1)
        store.add_payment("h1", "tok", 200, "lnbc1")
        self.assertEqual(len(store.pending_payments(86400)), 1)
        store.settle_payment("h1")
        self.assertEqual(store.pending_payments(86400), [])

    def test_wipe(self):
        store.mark_paid("tok")
        store.mark_done("tok")
        self.assertEqual(store.wipe_candidates(0)[0]["token"], "tok")
        store.wipe_delivery("tok")
        row = store.get_order("tok")
        self.assertEqual(row["wiped"], 1)
        self.assertIsNone(row["ship_code"])
        self.assertIsNone(row["point_id"])
        self.assertIsNone(row["note"])

    def test_ship_code_roundtrip(self):
        self.assertEqual(store.get_order("tok")["carrier"], "balikovna")
        store.set_ship_code("tok", "99887766")
        self.assertEqual(store.get_order("tok")["ship_code"], "99887766")
        self.assertEqual(store.wipe_candidates(0), [])

    def test_wipe_waits(self):
        store.mark_paid("tok")
        store.mark_done("tok")
        self.assertEqual(store.wipe_candidates(3600), [])   # ještě ne


class Settings(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_settings_and_stats(self):
        self.assertIsNone(store.get_setting("x"))
        store.set_setting("x", "1")
        store.set_setting("x", "2")                          # upsert
        self.assertEqual(store.get_setting("x"), "2")
        store.bump_stat("s", 5)
        store.bump_stat("s", 2)
        self.assertEqual(store.get_stat("s"), 7)


if __name__ == "__main__":
    unittest.main()

"""E2E přes HTTP: reálný ThreadingHTTPServer s dočasnou DB a fake platebním
backendem — katalog, checkout (validace, doprava, vouchery), platba+poll,
lifecycle (expirace, reconcile), admin (auth + akce + nastavení)."""

import base64
import http.client
import json
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from unittest import mock

from common import fresh_db, FakeManager
import store
import vouchers
import app
import admin


def start_web():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


class Client:
    def __init__(self, port):
        self.port = port

    def req(self, method, path, form=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = urllib.parse.urlencode(form).encode() if form else None
        h = dict(headers or {})
        if body:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read().decode()
        conn.close()
        return resp.status, dict(resp.getheaders()), data

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def post(self, path, form, **kw):
        return self.req("POST", path, form=form, **kw)


def seed_shop():
    box = store.get_product(store.add_product(
        "Krabicka", "set", 1000, stock=3, active=1))
    vou = store.get_product(store.add_product(
        "Kredit", "darek", 400, kind="voucher", stock=-1, active=1))
    day = store.get_product(store.add_product(
        "Dny30", "sit", 200, kind="days", days=30, stock=-1, active=1))
    return box, vou, day


def checkout_form(items, **extra):
    form = {"items": json.dumps(items)}
    form.update(extra)
    return form


DELIV = dict(delivery="code", carrier="balikovna")


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fresh_db()
        app.manager = FakeManager()
        cls.srv, port = start_web()
        cls.c = Client(port)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        # čistá DB pro každý test (server drží jen handler, stav je ve store)
        fresh_db()
        app.manager = FakeManager()
        app.limiter = app.RateLimiter(capacity=1000, refill_per_s=1000)
        self.box, self.vou, self.day = seed_shop()

    # -- katalog / košík / statika --

    def test_catalog_lists_active(self):
        st, _h, body = self.c.get("/")
        self.assertEqual(st, 200)
        self.assertIn("Krabicka", body)
        store.update_product(self.box["id"], active=0)
        _st, _h, body = self.c.get("/")
        self.assertNotIn("Krabicka", body)

    def test_kosik_and_static(self):
        st, _h, body = self.c.get("/kosik")
        self.assertEqual(st, 200)
        self.assertIn("Doručení", body)
        st, h, _b = self.c.get("/static/style.css")
        self.assertEqual(st, 200)
        self.assertIn("text/css", h["Content-Type"])
        st, _h, _b = self.c.get("/static/../app.py")
        self.assertEqual(st, 404)

    # -- checkout validace --

    def test_checkout_invalid_items(self):
        for items in ("nejson", "[]", json.dumps([{"id": 1, "qty": 0}]),
                      json.dumps([{"id": 999, "qty": 1}])):
            st, _h, _b = self.c.post("/checkout", {"items": items})
            self.assertEqual(st, 400)

    def test_physical_requires_delivery(self):
        st, _h, body = self.c.post("/checkout", checkout_form(
            [{"id": self.box["id"], "qty": 1}]))
        self.assertEqual(st, 400)
        self.assertIn("doručení", body.lower())
        # neznámý dopravce (Packeta/DPD schválně nenabízíme)
        st, _h, body = self.c.post("/checkout", checkout_form(
            [{"id": self.box["id"], "qty": 1}], delivery="code",
            carrier="packeta"))
        self.assertEqual(st, 400)
        self.assertIn("dopravce", body.lower())
        # nesmyslný podací kód
        st, _h, _b = self.c.post("/checkout", checkout_form(
            [{"id": self.box["id"], "qty": 1}], delivery="code",
            carrier="ppl", ship_code="x"))
        self.assertEqual(st, 400)

    def test_personal_delivery_needs_nothing(self):
        st, h, _b = self.c.post("/checkout", checkout_form(
            [{"id": self.box["id"], "qty": 1}], delivery="personal"))
        self.assertEqual(st, 303)
        order = store.get_order(self._order_token(h["Location"]))
        self.assertEqual(order["delivery"], "personal")
        self.assertIsNone(order["carrier"])

    def test_digital_only_needs_no_delivery(self):
        st, h, _b = self.c.post("/checkout", checkout_form(
            [{"id": self.vou["id"], "qty": 1}]))
        self.assertEqual(st, 303)
        self.assertIn("/o/", h["Location"])

    def test_soldout(self):
        store.update_product(self.box["id"], stock=0)
        st, _h, body = self.c.post("/checkout", checkout_form(
            [{"id": self.box["id"], "qty": 1}], **DELIV))
        self.assertEqual(st, 400)
        self.assertIn("skladem", body)

    def test_backend_down_rolls_back(self):
        app.manager.backend.fail_create = True
        st, _h, body = self.c.post("/checkout", checkout_form(
            [{"id": self.box["id"], "qty": 2}], **DELIV))
        self.assertEqual(st, 400)
        self.assertIn("nedostupné", body)
        self.assertEqual(store.get_product(self.box["id"])["stock"], 3)

    # -- platba end-to-end --

    def _order_token(self, location):
        return location.rsplit("/", 1)[1]

    def test_pay_flow_with_poll_and_codes(self):
        st, h, _b = self.c.post("/checkout", checkout_form(
            [{"id": self.box["id"], "qty": 1}, {"id": self.day["id"], "qty": 2}],
            **DELIV))
        self.assertEqual(st, 303)
        token = self._order_token(h["Location"])
        # sklad rezervovaný, objednávka new s invoice
        self.assertEqual(store.get_product(self.box["id"])["stock"], 2)
        _st, _h, body = self.c.get("/o/" + token)
        self.assertIn("lnbc_test_", body)
        st, _h, body = self.c.get("/pay/poll?t=" + token)
        self.assertEqual(json.loads(body), {"paid": False})
        # zaplatit → poll True → kódy vydané, dny ve frontě
        app.manager.backend.pay(store.get_order(token)["payment_hash"])
        _st, _h, body = self.c.get("/pay/poll?t=" + token)
        self.assertEqual(json.loads(body), {"paid": True})
        order = store.get_order(token)
        self.assertEqual(order["status"], "paid")
        codes = store.vouchers_for_order(token)
        self.assertEqual(len(codes), 2)                     # 2× dny, krabička ne
        self.assertEqual(len(store.pending_redemptions()), 2)
        _st, _h, body = self.c.get("/o/" + token)
        self.assertIn(codes[0]["code"], body)
        self.assertIn("zaplaceno", body)
        # poll idempotence — druhé volání nic nerozbije
        _st, _h, body = self.c.get("/pay/poll?t=" + token)
        self.assertEqual(json.loads(body), {"paid": True})
        self.assertEqual(len(store.vouchers_for_order(token)), 2)

    def test_voucher_full_coverage_skips_invoice(self):
        # 1) koupit kredit 400
        st, h, _b = self.c.post("/checkout", checkout_form(
            [{"id": self.vou["id"], "qty": 1}]))
        src = self._order_token(h["Location"])
        app.manager.backend.pay(store.get_order(src)["payment_hash"])
        self.c.get("/pay/poll?t=" + src)
        code = store.vouchers_for_order(src)[0]["code"]
        # 2) objednávka za 200 plně krytá kreditem → rovnou zaplaceno
        st, h, _b = self.c.post("/checkout", checkout_form(
            [{"id": self.day["id"], "qty": 1}], voucher=code))
        self.assertEqual(st, 303)
        token = self._order_token(h["Location"])
        order = store.get_order(token)
        self.assertEqual(order["status"], "paid")
        self.assertEqual(order["total_sat"], 0)
        self.assertIsNone(order["payment_hash"])
        # kredit je spotřebovaný
        st, _h, body = self.c.post("/checkout", checkout_form(
            [{"id": self.day["id"], "qty": 1}], voucher=code))
        self.assertEqual(st, 400)
        self.assertIn("uplatněn", body)

    def test_voucher_partial_discount(self):
        st, h, _b = self.c.post("/checkout", checkout_form(
            [{"id": self.vou["id"], "qty": 1}]))
        src = self._order_token(h["Location"])
        app.manager.backend.pay(store.get_order(src)["payment_hash"])
        self.c.get("/pay/poll?t=" + src)
        code = store.vouchers_for_order(src)[0]["code"]
        st, h, _b = self.c.post("/checkout", checkout_form(
            [{"id": self.box["id"], "qty": 1}], voucher=code, **DELIV))
        token = self._order_token(h["Location"])
        self.assertEqual(store.get_order(token)["total_sat"], 600)  # 1000-400

    def test_ship_code_at_checkout(self):
        st, h, _b = self.c.post("/checkout", checkout_form(
            [{"id": self.box["id"], "qty": 1}], delivery="code",
            carrier="balikovna", ship_code="1234-5678"))
        self.assertEqual(st, 303)
        order = store.get_order(self._order_token(h["Location"]))
        self.assertEqual(order["ship_code"], "12345678")   # normalizováno
        self.assertEqual(order["carrier"], "balikovna")

    def test_ship_code_added_later(self):
        st, h, _b = self.c.post("/checkout", checkout_form(
            [{"id": self.box["id"], "qty": 1}], **DELIV))
        token = self._order_token(h["Location"])
        # před zaplacením kód nejde uložit
        st, _h, _b = self.c.post("/o/%s/kod" % token, {"ship_code": "12345678"})
        self.assertEqual(st, 400)
        app.manager.backend.pay(store.get_order(token)["payment_hash"])
        self.c.get("/pay/poll?t=" + token)
        # zaplaceno → stránka vyzve k zadání kódu
        _st, _h, body = self.c.get("/o/" + token)
        self.assertIn("podací kód", body.lower())
        # neplatný kód
        st, _h, _b = self.c.post("/o/%s/kod" % token, {"ship_code": "abc"})
        self.assertEqual(st, 400)
        # platný kód
        st, _h, _b = self.c.post("/o/%s/kod" % token, {"ship_code": "8765 4321"})
        self.assertEqual(st, 303)
        self.assertEqual(store.get_order(token)["ship_code"], "87654321")
        _st, _h, body = self.c.get("/o/" + token)
        self.assertIn("87654321", body)

    def test_ship_code_unknown_order(self):
        st, _h, _b = self.c.post("/o/neexistuje/kod", {"ship_code": "12345678"})
        self.assertEqual(st, 404)

    # -- lifecycle --

    def test_expiry_returns_stock_and_voucher(self):
        st, h, _b = self.c.post("/checkout", checkout_form(
            [{"id": self.vou["id"], "qty": 1}]))
        src = self._order_token(h["Location"])
        app.manager.backend.pay(store.get_order(src)["payment_hash"])
        self.c.get("/pay/poll?t=" + src)
        code = store.vouchers_for_order(src)[0]["code"]
        st, h, _b = self.c.post("/checkout", checkout_form(
            [{"id": self.box["id"], "qty": 2}], voucher=code, **DELIV))
        token = self._order_token(h["Location"])
        self.assertEqual(store.get_product(self.box["id"])["stock"], 1)
        store._execute("UPDATE orders SET created_at = created_at - 99999"
                       " WHERE token=?", (token,))
        app.lifecycle_tick()
        self.assertEqual(store.get_order(token)["status"], "expired")
        self.assertEqual(store.get_product(self.box["id"])["stock"], 3)
        # voucher je zase použitelný
        d, err = vouchers.voucher_discount(code, 100, "dalsi")
        self.assertEqual((d, err), (100, ""))

    def test_reconcile_pays_closed_tab(self):
        st, h, _b = self.c.post("/checkout", checkout_form(
            [{"id": self.day["id"], "qty": 1}]))
        token = self._order_token(h["Location"])
        app.manager.backend.pay(store.get_order(token)["payment_hash"])
        app.lifecycle_tick()                       # žádný prohlížeč nepolluje
        self.assertEqual(store.get_order(token)["status"], "paid")
        self.assertEqual(len(store.vouchers_for_order(token)), 1)

    def test_expiry_last_chance_pays(self):
        """Zaplaceno těsně před expirací se zavřenou záložkou → paid, ne expired."""
        st, h, _b = self.c.post("/checkout", checkout_form(
            [{"id": self.day["id"], "qty": 1}]))
        token = self._order_token(h["Location"])
        store._execute("UPDATE orders SET created_at = created_at - 99999"
                       " WHERE token=?", (token,))
        app.manager.backend.pay(store.get_order(token)["payment_hash"])
        app.lifecycle_tick()
        self.assertEqual(store.get_order(token)["status"], "paid")

    # -- captcha brána --

    def test_captcha_gate(self):
        with mock.patch.object(app.captcha, "verify", return_value=False), \
             mock.patch.object(app.captcha, "enabled", return_value=True):
            st, _h, body = self.c.post("/checkout", checkout_form(
                [{"id": self.day["id"], "qty": 1}]))
        self.assertEqual(st, 400)
        self.assertIn("Ověření", body)

    # -- rate limit --

    def test_rate_limit(self):
        app.limiter = app.RateLimiter(capacity=2, refill_per_s=0.001)
        results = [self.c.post("/checkout", {"items": "x"})[0] for _ in range(4)]
        self.assertIn(429, results)

    # -- BASE_PATH --

    def test_base_path_prefix(self):
        old = app.BASE
        app.BASE = "/obchod"
        try:
            st, _h, body = self.c.get("/obchod/")
            self.assertEqual(st, 200)
            self.assertIn('href="/obchod/kosik"', body)
        finally:
            app.BASE = old

    def test_order_not_found(self):
        st, _h, _b = self.c.get("/o/neexistuje")
        self.assertEqual(st, 404)


class AdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fresh_db()
        admin.ADMIN_PASSWORD = "test-heslo-123"
        admin.ADMIN_PORT = 0
        srv = admin.start(manager=FakeManager(),
                          cancel_order=app.cancel_order)
        cls.srv = srv
        cls.c = Client(srv.server_address[1])
        cls.auth = {"Authorization": "Basic " + base64.b64encode(
            b"x:test-heslo-123").decode()}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        fresh_db()
        self.box, self.vou, self.day = seed_shop()

    def test_auth_required(self):
        st, _h, _b = self.c.get("/")
        self.assertEqual(st, 401)
        st, _h, _b = self.c.get("/", headers={"Authorization": "Basic " +
                                base64.b64encode(b"x:spatne").decode()})
        self.assertEqual(st, 401)
        st, _h, _b = self.c.get("/", headers=self.auth)
        self.assertEqual(st, 200)

    def test_order_actions(self):
        store.create_order("tok", 100, "code", "balikovna", "12345678")
        store.add_item("tok", self.box, 1)
        store.mark_paid("tok")
        st, _h, _b = self.c.post("/order", {"token": "tok", "action": "shipped"},
                                 headers=self.auth)
        self.assertEqual(st, 303)
        row = store.get_order("tok")
        self.assertEqual((row["status"], row["ship_code"]),
                         ("shipped", "12345678"))
        self.c.post("/order", {"token": "tok", "action": "done"},
                    headers=self.auth)
        self.assertEqual(store.get_order("tok")["status"], "done")
        self.c.post("/order", {"token": "tok", "action": "wipe"},
                    headers=self.auth)
        self.assertEqual(store.get_order("tok")["wiped"], 1)

    def test_shipped_requires_customer_code(self):
        store.create_order("bezkodu", 100, "code", "balikovna")
        store.add_item("bezkodu", self.box, 1)
        store.mark_paid("bezkodu")
        st, _h, body = self.c.post("/order", {"token": "bezkodu",
                                              "action": "shipped"},
                                   headers=self.auth)
        self.assertEqual(st, 400)
        self.assertIn("podací kód", body)
        self.assertEqual(store.get_order("bezkodu")["status"], "paid")

    def test_order_cancel_returns_stock(self):
        ok, _ = store.reserve_stock([(self.box, 2)])
        self.assertTrue(ok)
        store.create_order("tok", 2000, "code", "balikovna", "11223344")
        store.add_item("tok", self.box, 2)
        self.c.post("/order", {"token": "tok", "action": "cancel"},
                    headers=self.auth)
        self.assertEqual(store.get_order("tok")["status"], "cancelled")
        self.assertEqual(store.get_product(self.box["id"])["stock"], 3)

    def test_product_crud_and_settings(self):
        st, _h, _b = self.c.post("/product", {
            "id": "0", "name": "Nový", "descr": "d", "price_sat": "123",
            "kind": "physical", "days": "0", "stock": "5", "action": "save"},
            headers=self.auth)
        self.assertEqual(st, 303)
        prods = store.list_products(active_only=False)
        new = [p for p in prods if p["name"] == "Nový"][0]
        self.assertEqual((new["price_sat"], new["active"]), (123, 0))
        self.c.post("/product", {
            "id": str(new["id"]), "name": "Nový", "descr": "d",
            "price_sat": "200", "kind": "physical", "days": "0",
            "stock": "5", "active": "1", "action": "save"}, headers=self.auth)
        self.assertEqual(store.get_product(new["id"])["active"], 1)
        # settings: prázdný secret = beze změny
        store.set_setting("lnbits_invoice_key", "puvodni")
        self.c.post("/settings", {"lnbits_url": "http://ln:5000",
                                  "lnbits_invoice_key": ""}, headers=self.auth)
        self.assertEqual(store.get_setting("lnbits_invoice_key"), "puvodni")
        self.assertEqual(store.get_setting("lnbits_url"), "http://ln:5000")

    def test_password_change(self):
        self.c.post("/password", {"current": "test-heslo-123",
                                  "new1": "nove-heslo-8", "new2": "nove-heslo-8"},
                    headers=self.auth)
        self.assertTrue(admin.check_password("nove-heslo-8"))
        self.assertFalse(admin.check_password("test-heslo-123"))
        store.set_setting("admin_password_hash", "")  # necháme env pro další testy
        store._execute("DELETE FROM settings WHERE key='admin_password_hash'")


if __name__ == "__main__":
    unittest.main()

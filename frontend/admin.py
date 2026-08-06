"""Admin obchodu — objednávky, produkty, nastavení. Interní port (default
8094), NIKDY veřejně. Basic auth (PBKDF2 vzor z CockScale adminu); bez hesla
se server vůbec nespustí. Nastavení platí okamžitě (settings v DB)."""

import base64
import hashlib
import hmac
import html
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import captcha
import payments
import store

ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "8094"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

_deps = {}

SETTING_FIELDS = (
    ("order_ttl_min", "ORDER_TTL_MIN", "Platnost nezaplacené objednávky (min)"),
    ("wipe_days", "WIPE_DAYS", "Smazat doručovací údaje po dokončení (dny)"),
)

_PBKDF2_ITERATIONS = 200_000


def setting(key, env, fallback=""):
    return store.get_setting(key) or os.environ.get(env, fallback)


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt,
                                 _PBKDF2_ITERATIONS)
    return "pbkdf2$%d$%s$%s" % (_PBKDF2_ITERATIONS, salt.hex(), digest.hex())


def check_password(password):
    stored = store.get_setting("admin_password_hash")
    if stored:
        try:
            _, iters, salt_hex, digest_hex = stored.split("$")
            digest = hashlib.pbkdf2_hmac(
                "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
            return hmac.compare_digest(digest.hex(), digest_hex)
        except (ValueError, TypeError):
            return False
    return bool(ADMIN_PASSWORD) and hmac.compare_digest(password, ADMIN_PASSWORD)


def fmt_ts(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "—"


def fmt_sat(n):
    return format(int(n), ",").replace(",", " ")


def page(title, body, msg=""):
    banner = '<p class="msg">%s</p>' % html.escape(msg) if msg else ""
    return """<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s — obchod admin</title><style>
:root { --bg:#fbf9f5; --surface:#fff; --fg:#1a1714; --muted:#7c7568;
  --line:#ece6da; --accent:#f7931a; --ok:#2f9e44; --danger:#e03131; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#08080b; --surface:#131319; --fg:#f4f4f5; --muted:#a1a1aa;
    --line:#ffffff1a; color-scheme:dark; } }
* { box-sizing:border-box }
body { margin:0; font-family:system-ui,sans-serif; background:var(--bg);
  color:var(--fg); line-height:1.55 }
header { padding:.8rem 1.25rem; border-bottom:1px solid var(--line);
  background:var(--surface); display:flex; gap:1.2rem; position:sticky; top:0 }
header a { color:var(--fg); text-decoration:none; font-weight:600 }
main { max-width:70rem; margin:0 auto; padding:1.5rem 1.25rem }
h1 { font-size:1.5rem } h2 { font-size:1.1rem; margin-top:1.8rem }
table { border-collapse:collapse; width:100%%; margin:.8rem 0; font-size:.88rem;
  background:var(--surface); border:1px solid var(--line) }
td,th { text-align:left; padding:.45rem .6rem; border-bottom:1px solid var(--line);
  vertical-align:top }
th { color:var(--muted); font-size:.72rem; text-transform:uppercase }
.mono { font-family:ui-monospace,monospace }
.ok { color:var(--ok); font-weight:600 } .warn { color:var(--accent); font-weight:600 }
.bad { color:var(--danger); font-weight:600 }
button { background:var(--accent); color:#fff; border:0; padding:.45rem .9rem;
  border-radius:8px; font-weight:700; cursor:pointer }
button.small { padding:.2rem .5rem; font-size:.76rem; background:var(--surface);
  color:var(--fg); border:1px solid var(--line) }
button.danger { background:var(--surface); color:var(--danger);
  border:1px solid var(--danger) }
input[type=text],input[type=password],input[type=number],select,textarea {
  background:var(--surface); color:var(--fg); border:1px solid var(--line);
  border-radius:8px; padding:.5rem .65rem; width:100%%; max-width:26rem }
input.tiny { width:6rem } label { display:block; margin:.7rem 0 .2rem;
  font-size:.83rem; color:var(--muted) }
.msg { background:var(--surface); border-left:3px solid var(--accent);
  padding:.7rem 1rem }
form.inline { display:inline }
fieldset { border:1px solid var(--line); border-radius:10px; margin:1rem 0;
  padding:.4rem 1.2rem 1.2rem; background:var(--surface) }
legend { color:var(--muted); font-size:.83rem }
</style></head><body>
<header><a href="/">Objednávky</a><a href="/products">Produkty</a>
<a href="/settings">Nastavení</a></header>
<main>%s%s</main></body></html>""" % (html.escape(title), banner, body)


STATUS_BADGE = {
    "new": '<span class="warn">čeká na platbu</span>',
    "paid": '<span class="ok">zaplaceno</span>',
    "shipped": "odesláno", "done": "hotovo",
    "cancelled": '<span class="bad">zrušeno</span>',
    "expired": '<span class="muted">vypršelo</span>',
    "refund": '<span class="bad">K VRÁCENÍ PENĚZ</span>',
}


def orders_body(manager):
    try:
        backend = '<span class="ok">LNbits</span>'
        manager.current()
    except payments.PaymentError:
        backend = ('<span class="warn">LNbits nenastaveno — zákazníci '
                   "nemůžou platit (Nastavení)</span>")
    rows = ""
    for o in store.list_orders(200):
        items = store.get_items(o["token"])
        itxt = "; ".join("%d× %s" % (i["qty"], i["name"]) for i in items)
        carrier_name = {"balikovna": "Balíkovna", "ppl": "PPL"}.get(
            o["carrier"] or "", o["carrier"] or "?")
        if o["delivery"] == "code":
            deliv = "%s → napsat na krabici: %s" % (
                carrier_name,
                ('<b class="mono">%s</b>' % html.escape(o["ship_code"]))
                if o["ship_code"] else '<span class="warn">ČEKÁME NA KÓD</span>')
        else:
            deliv = {"personal": "osobně", None: "digitální",
                     "point": "legacy výdejna %s" % (o["point_id"] or ""),
                     "anon": "legacy anon %s" % (o["point_id"] or ""),
                     }.get(o["delivery"], o["delivery"] or "")
        if o["wiped"]:
            deliv = "(údaje smazány)"
        actions = '<form class="inline" method="post" action="/order">' \
                  '<input type="hidden" name="token" value="%s">' % o["token"]
        if o["status"] == "paid":
            actions += '<button class="small" name="action" value="shipped">odesláno</button>'
        if o["status"] in ("paid", "shipped"):
            actions += '<button class="small" name="action" value="done">hotovo</button>'
        if o["status"] == "new":
            actions += '<button class="small danger" name="action" value="cancel">zrušit</button>'
        if o["status"] in ("paid", "shipped"):
            actions += ('<button class="small danger" name="action" value="refund"'
                        ' onclick="return confirm(\'Označit k vrácení peněz?'
                        ' Sklad se vrátí zpět.\')">k vrácení</button>')
        if not o["wiped"] and o["status"] in ("done", "cancelled", "expired",
                                              "refund"):
            actions += '<button class="small danger" name="action" value="wipe">smazat údaje</button>'
        actions += "</form>"
        note = (" · pozn: %s" % o["note"]) if o["note"] else ""
        rows += ("<tr><td class=\"mono\">%s</td><td>%s</td><td>%s</td>"
                 "<td>%s sat</td><td>%s%s</td><td>%s</td></tr>") % (
            o["token"][:8], fmt_ts(o["created_at"]),
            STATUS_BADGE.get(o["status"], o["status"]),
            fmt_sat(o["total_sat"]),
            html.escape(itxt) + html.escape(note), " · " + deliv,  # deliv už escapované
            actions)
    pending = len(store.pending_redemptions(100))
    stuck = len(store.stuck_redemptions())
    queue = ""
    if pending:
        queue += (' · <span class="warn">%d kódů čeká na registraci'
                  ' u sítě</span>' % pending)
    if stuck:
        queue += (' · <span class="bad">%d kódů se registrovat NEPODAŘILO'
                  ' — vyřeš ručně</span>' % stuck)
    return """<h1>Objednávky</h1>
<p>platby: %s · objednávek celkem: %d · tržby: %s sat%s</p>
<table><tr><th>obj.</th><th>kdy</th><th>stav</th><th>částka</th>
<th>obsah / doručení</th><th>akce</th></tr>%s</table>""" % (
        backend, store.get_stat("stat_orders"), fmt_sat(store.get_stat("stat_sat")),
        queue,
        rows or "<tr><td colspan=6>žádné</td></tr>")


PRODUCT_IMG_DIR = os.path.join(os.path.dirname(__file__), "static", "produkty")


def product_images():
    """Obrázky nahrané do static/produkty/ — nabídnou se v administraci."""
    try:
        names = sorted(n for n in os.listdir(PRODUCT_IMG_DIR)
                       if os.path.splitext(n)[1].lower()
                       in (".jpg", ".jpeg", ".png", ".webp"))
    except OSError:
        names = []
    return names


def image_select(name, current):
    opts = '<option value="">— bez obrázku —</option>'
    for img in product_images():
        opts += '<option value="%s"%s>%s</option>' % (
            html.escape(img), " selected" if img == current else "",
            html.escape(img))
    return '<select name="%s">%s</select>' % (name, opts)


def products_body(msg=""):
    rows = ""
    for p in store.list_products(active_only=False):
        rows += """<tr><form method="post" action="/product">
<input type="hidden" name="id" value="%d">
<td><input type="text" name="name" value="%s"></td>
<td><input type="text" name="descr" value="%s" style="max-width:20rem"></td>
<td><input type="number" name="price_sat" value="%d" class="tiny"></td>
<td><select name="kind"><option value="physical"%s>fyzické</option>
<option value="voucher"%s>kredit</option><option value="days"%s>dny sítě</option></select>
<input type="number" name="days" value="%d" class="tiny" title="dny (jen druh dny)"></td>
<td><input type="number" name="stock" value="%d" class="tiny" title="-1 = neomezeně"></td>
<td>%s</td>
<td><select name="active"><option value="1"%s>ANO</option><option value="0"%s>ne</option></select></td>
<td><button class="small" name="action" value="save">uložit</button>
<button class="small danger" name="action" value="delete"
 onclick="return confirm('Smazat produkt?')">smazat</button></td></form></tr>""" % (
            p["id"], html.escape(p["name"]), html.escape(p["descr"]),
            p["price_sat"],
            " selected" if p["kind"] == "physical" else "",
            " selected" if p["kind"] == "voucher" else "",
            " selected" if p["kind"] == "days" else "",
            p["days"], p["stock"], image_select("image", p["image"] or ""),
            " selected" if p["active"] else "", "" if p["active"] else " selected")
    return """<h1>Produkty</h1>
<p class="msg">Ceny v sat. Sklad -1 = neomezeně (digitální). Druh „kredit" =
dárkový kód do obchodu, „dny sítě" = kód na dny privátní sítě (vyplň počet dnů).
Obrázky se nahrávají do <code>frontend/static/produkty/</code> v repozitáři;
tady se jen vybírají.</p>
<table><tr><th>název</th><th>popis</th><th>cena</th><th>druh</th><th>sklad</th>
<th>obrázek</th><th>aktivní</th><th></th></tr>%s</table>
<h2>Nový produkt</h2>
<form method="post" action="/product"><input type="hidden" name="id" value="0">
<label>Název</label><input type="text" name="name">
<label>Popis</label><input type="text" name="descr">
<label>Cena (sat)</label><input type="number" name="price_sat" value="0">
<label>Druh</label><select name="kind"><option value="physical">fyzické</option>
<option value="voucher">kredit</option><option value="days">dny sítě</option></select>
<label>Dny (jen druh „dny sítě")</label><input type="number" name="days" value="0">
<label>Sklad (-1 = neomezeně)</label><input type="number" name="stock" value="0">
<label>Obrázek</label>%s
<p><button name="action" value="save">Přidat (neaktivní)</button></p></form>""" % (
        rows or "<tr><td colspan=8>žádné</td></tr>", image_select("image", ""))


def settings_body(msg=""):
    lnbits_url = setting("lnbits_url", "LNBITS_URL")
    has_key = bool(setting("lnbits_invoice_key", "LNBITS_INVOICE_KEY"))
    fields = ""
    for key, env, label in SETTING_FIELDS:
        fields += '<label>%s</label><input type="number" name="%s" value="%s">' % (
            html.escape(label), key, html.escape(setting(key, env, "")))
    return """<h1>Nastavení</h1>
<form method="post" action="/settings">
<fieldset><legend>Platby (LNbits — ODDĚLENÁ peněženka „obchod")</legend>
<label>LNbits API URL</label>
<input type="text" name="lnbits_url" value="%s" placeholder="https://…">
<label>Invoice key %s — nikdy admin key!</label>
<input type="password" name="lnbits_invoice_key" placeholder="%s">
<p><button type="submit" formaction="/settings/test" class="small">Otestovat LNbits</button></p>
</fieldset>
<fieldset><legend>Captcha (Compute Captcha)</legend>
<label>API URL</label>
<input type="text" name="captcha_api" value="%s" placeholder="https://captcha.qr6.eu">
<label>Site key (pk_…)</label>
<input type="text" name="captcha_site_key" value="%s" placeholder="pk_…">
<label>Site secret (ssk_…) %s</label>
<input type="password" name="captcha_site_secret" placeholder="%s">
<p><button type="submit" formaction="/settings/captcha-test" class="small">Otestovat captcha</button></p>
</fieldset>
<fieldset><legend>Registrace dárkových dnů (CockScale)</legend>
<label>CockScale API URL (interní)</label>
<input type="text" name="cockscale_api" value="%s" placeholder="http://frontend:8091">
<label>Partner secret %s</label>
<input type="password" name="cockscale_partner_secret" placeholder="%s">
</fieldset>
<fieldset><legend>Objednávky</legend>%s</fieldset>
<button type="submit">Uložit</button>
</form>
<form method="post" action="/password">
<fieldset><legend>Změna hesla admina</legend>
<label>Současné heslo</label><input type="password" name="current">
<label>Nové heslo (min. 8)</label><input type="password" name="new1">
<label>Znovu</label><input type="password" name="new2">
<p><button type="submit">Změnit heslo</button></p>
</fieldset></form>""" % (
        html.escape(lnbits_url),
        '<span class="ok">(nastaven)</span>' if has_key else '<span class="warn">(nenastaven)</span>',
        "beze změny" if has_key else "vlož invoice key",
        html.escape(setting("captcha_api", "CAPTCHA_API", captcha.DEFAULT_API)),
        html.escape(setting("captcha_site_key", "CAPTCHA_SITE_KEY")),
        '<span class="ok">(nastaven)</span>' if setting(
            "captcha_site_secret", "CAPTCHA_SITE_SECRET") else '<span class="warn">(nenastaven)</span>',
        "beze změny" if setting("captcha_site_secret", "CAPTCHA_SITE_SECRET")
        else "vlož site secret",
        html.escape(setting("cockscale_api", "COCKSCALE_API")),
        '<span class="ok">(nastaven)</span>' if setting(
            "cockscale_partner_secret", "COCKSCALE_PARTNER_SECRET")
        else '<span class="warn">(nenastaven)</span>',
        "beze změny" if setting("cockscale_partner_secret",
                                "COCKSCALE_PARTNER_SECRET") else "vlož secret",
        fields)


class Handler(BaseHTTPRequestHandler):
    server_version = "ObchodAdmin"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body, extra=None):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, target):
        self.send_response(303)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authed(self):
        header = self.headers.get("Authorization") or ""
        if header.startswith("Basic "):
            try:
                _, _, password = base64.b64decode(header[6:]).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                password = ""
            if password and check_password(password):
                return True
        self._send(401, "auth required",
                   {"WWW-Authenticate": 'Basic realm="obchod admin"'})
        return False

    def _form(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 65536)).decode(errors="replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def do_GET(self):
        if not self._authed():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self._send(200, page("Objednávky",
                                        orders_body(_deps["manager"])))
        if path == "/products":
            return self._send(200, page("Produkty", products_body()))
        if path == "/settings":
            return self._send(200, page("Nastavení", settings_body()))
        self._send(404, page("404", "<h1>404</h1>"))

    def do_POST(self):
        if not self._authed():
            return
        path = urllib.parse.urlparse(self.path).path
        form = self._form()
        if path == "/order":
            return self.post_order(form)
        if path == "/product":
            return self.post_product(form)
        if path == "/settings":
            return self.post_settings(form)
        if path == "/settings/test":
            return self.post_settings_test(form)
        if path == "/settings/captcha-test":
            return self.post_captcha_test(form)
        if path == "/password":
            return self.post_password(form)
        self._send(404, page("404", "<h1>404</h1>"))

    def post_order(self, form):
        token = form.get("token", "")
        action = form.get("action", "")
        order = store.get_order(token)
        if not order:
            return self._send(404, page("Chyba", "<h1>Objednávka nenalezena</h1>"))
        if action == "shipped" and order["status"] == "paid":
            if order["delivery"] == "code" and not order["ship_code"]:
                return self._send(400, page("Chyba",
                    "<h1>Chybí podací kód</h1><p>Zákazník ho ještě neposlal —"
                    " bez něj zásilku nelze podat.</p>"))
            store.set_status(token, "shipped")
        elif action == "done" and order["status"] in ("paid", "shipped"):
            store.mark_done(token)
        elif action == "cancel" and order["status"] == "new":
            _deps["cancel_order"](order)
        elif action == "refund" and order["status"] in ("paid", "shipped"):
            # zaplacenou objednávku nelze „zrušit" — peníze už dorazily.
            # Vrátí se sklad a označí se, že čeká na vrácení peněz.
            store.return_stock(token)
            store.set_status(token, "refund", expect=("paid", "shipped"))
        elif action == "wipe" and order["status"] in ("done", "cancelled",
                                                      "expired", "refund"):
            # dřív bez kontroly stavu: wipe na zaplacené objednávce smazal
            # podací kód a zákazníkovi zmizel formulář na jeho doplnění
            store.wipe_delivery(token)
        else:
            return self._send(400, page("Chyba", "<h1>Neplatná akce</h1>"))
        self._redirect("/")

    def post_product(self, form):
        try:
            pid = int(form.get("id", "0"))
            fields = dict(
                name=form.get("name", "").strip()[:100],
                descr=form.get("descr", "").strip()[:500],
                price_sat=max(0, int(form.get("price_sat", "0"))),
                kind=form.get("kind", "physical"),
                days=max(0, int(form.get("days", "0"))),
                stock=max(-1, int(form.get("stock", "0"))),
                image=form.get("image", "").strip(),
            )
            assert fields["kind"] in ("physical", "voucher", "days")
            assert fields["name"]
            assert fields["image"] in ("",) + tuple(product_images())
        except (ValueError, AssertionError):
            return self._send(400, page("Produkty",
                                        products_body("Neplatné hodnoty.")))
        # dny sítě se doručují přes partnerský endpoint — bez nastavení by
        # zákazník dostal kód, který nikde neplatí
        if (fields["kind"] == "days" and form.get("active") == "1"
                and not (store.get_setting("cockscale_api")
                         and store.get_setting("cockscale_partner_secret"))):
            return self._send(400, page("Produkty", products_body(
                "Dny sítě nejde aktivovat: chybí adresa a klíč partnerského "
                "rozhraní sítě (Nastavení). Kódy by se neregistrovaly.")))
        if form.get("action") == "delete" and pid:
            store.delete_product(pid)
        elif pid:
            fields["active"] = 1 if form.get("active") == "1" else 0
            store.update_product(pid, **fields)
        else:
            store.add_product(active=0, **fields)
        self._redirect("/products")

    def post_settings(self, form):
        for key in ("lnbits_url", "captcha_api", "captcha_site_key",
                    "cockscale_api"):
            store.set_setting(key, form.get(key, "").strip())
        for key in ("lnbits_invoice_key", "captcha_site_secret",
                    "cockscale_partner_secret"):
            val = form.get(key, "").strip()
            if val:  # prázdné pole = beze změny
                store.set_setting(key, val)
        for key, _env, _label in SETTING_FIELDS:
            value = form.get(key, "").strip()
            if value:
                try:
                    store.set_setting(key, int(value))
                except ValueError:
                    pass
        self._send(200, page("Nastavení", settings_body(),
                             msg="Uloženo — platí okamžitě."))

    def post_settings_test(self, form):
        url = form.get("lnbits_url", "").strip() or setting("lnbits_url", "LNBITS_URL")
        key = form.get("lnbits_invoice_key", "").strip() or setting(
            "lnbits_invoice_key", "LNBITS_INVOICE_KEY")
        if not url or not key:
            return self._send(200, page("Nastavení", settings_body(),
                                        msg="Chybí URL nebo invoice key."))
        try:
            info = payments.test_lnbits(url, key)
            msg = "LNbits OK — peněženka „%s“, zůstatek %d sat." % (
                info.get("name", "?"), int(info.get("balance", 0)) // 1000)
        except payments.PaymentError as e:
            msg = "LNbits test selhal: %s" % e
        self._send(200, page("Nastavení", settings_body(), msg=msg))

    def post_captcha_test(self, form):
        api = form.get("captcha_api", "").strip() or setting(
            "captcha_api", "CAPTCHA_API", captcha.DEFAULT_API)
        secret = form.get("captcha_site_secret", "").strip() or setting(
            "captcha_site_secret", "CAPTCHA_SITE_SECRET")
        _ok, msg = captcha.selftest(api, secret)
        self._send(200, page("Nastavení", settings_body(), msg=msg))

    def post_password(self, form):
        current = form.get("current", "")
        new1, new2 = form.get("new1", ""), form.get("new2", "")
        if not check_password(current):
            msg = "Současné heslo nesedí — nic se nezměnilo."
        elif len(new1) < 8:
            msg = "Nové heslo musí mít aspoň 8 znaků."
        elif new1 != new2:
            msg = "Nová hesla se neshodují."
        else:
            store.set_setting("admin_password_hash", hash_password(new1))
            msg = "Heslo změněno."
        self._send(200, page("Nastavení", settings_body(), msg=msg))


def start(**deps):
    if not ADMIN_PASSWORD and not store.get_setting("admin_password_hash"):
        print("admin UI vypnuto (žádné heslo — nastav ADMIN_PASSWORD)")
        return None
    _deps.update(deps)
    server = ThreadingHTTPServer(("0.0.0.0", ADMIN_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("obchod admin na portu %d" % ADMIN_PORT)
    return server

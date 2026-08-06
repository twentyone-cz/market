"""Obchod — primitivní e-shop s Lightning platbou (Phone21).

Čistá Python stdlib (žádný pip). Vzory převzaty z CockScale/frontend:
ThreadingHTTPServer, settings v DB (admin UI, env jen default), LNbits
invoice-only backend, Compute Captcha, rate limit v RAM, žádné logování IP.

Zásady:
  - žádné účty: objednávka = tajný token v URL (secrets.token_urlsafe)
  - košík žije jen v prohlížeči (localStorage); server ceny/dostupnost
    VŽDY přepočítá z DB — klientovi se nevěří
  - doručovací údaje se po dokončení mažou (lifecycle wipe)
  - BASE_PATH: aplikace umí běžet pod prefixem (např. /obchod za nginx)
"""

import html
import json
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import captcha
import payments
import store
import vouchers

WEB_PORT = int(os.environ.get("WEB_PORT", "8093"))
BASE = os.environ.get("BASE_PATH", "").rstrip("/")
LIFECYCLE_INTERVAL = int(os.environ.get("LIFECYCLE_INTERVAL", "60"))
NET_DASHBOARD = os.environ.get("NET_DASHBOARD_URL", "https://phone.twentyone.cz/app")
# Odkazy na zbytek webu (obchod běží pod stejnou doménou → cesty od kořene;
# pro samostatný běh jde přepsat na absolutní URL přes env).
SITE_HOME = os.environ.get("SITE_HOME_URL", "/")
SITE_DOCS = os.environ.get("SITE_DOCS_URL", "/instalace")
SITE_ACCOUNT = os.environ.get("SITE_ACCOUNT_URL", "/app")

manager = payments.PaymentManager(store)


def u(path):
    """Odkaz s BASE_PATH prefixem — všechny interní URL jdou tudy."""
    return BASE + path


def cfg_int(key, env, default):
    raw = store.get_setting(key) or os.environ.get(env, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def order_ttl():
    return cfg_int("order_ttl_min", "ORDER_TTL_MIN", 30) * 60


def wipe_after():
    return cfg_int("wipe_days", "WIPE_DAYS", 14) * 86400


# --- rate limit (token bucket per IP, jen RAM — IP se nikam neloguje) --------

class RateLimiter:
    def __init__(self, capacity=10, refill_per_s=0.5):
        self.capacity = capacity
        self.refill = refill_per_s
        self.buckets = {}
        self.lock = threading.Lock()

    def allow(self, ip):
        now = time.monotonic()
        with self.lock:
            tokens, last = self.buckets.get(ip, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill)
            if tokens < 1:
                self.buckets[ip] = (tokens, now)
                return False
            self.buckets[ip] = (tokens - 1, now)
            if len(self.buckets) > 10000:
                self.buckets.clear()
            return True


limiter = RateLimiter()


# --- byznys logika -----------------------------------------------------------

def apply_settlement(token):
    """Po zaplacení: přechod paid + vydání digitálních kódů. Idempotentní
    (mark_paid vrací True jen napoprvé)."""
    order = store.get_order(token)
    if not order:
        return
    if store.mark_paid(token):
        store.consume_voucher(token)
        vouchers.issue_for_order(token)
        store.bump_stat("stat_orders", 1)
        store.bump_stat("stat_sat", order["total_sat"])


def hash_state(payment_hash):
    """„paid" / „unpaid" / „unknown" podle platební brány. Rozlišení je
    zásadní: výpadek brány nesmí vypadat jako nezaplaceno (jinak by lifecycle
    zrušil i objednávky, které se právě platí)."""
    if not payment_hash:
        return "unpaid"
    try:
        return "paid" if manager.current().is_paid(payment_hash) else "unpaid"
    except payments.PaymentError:
        return "unknown"


def payment_state(order):
    """Stav platby objednávky; u už uzavřených se řídí stavem objednávky."""
    if order["status"] in ("paid", "shipped", "done"):
        return "paid"
    if order["status"] != "new":
        return "unpaid"
    return hash_state(order["payment_hash"])


def check_order_paid(order):
    """Ověří platbu u LNbits; při zaplacení settlene a aplikuje.
    Vrací True, když je objednávka zaplacená (teď či dříve)."""
    state = payment_state(order)
    if state != "paid":
        return False
    if order["status"] != "new":
        return True
    store.settle_payment(order["payment_hash"])
    apply_settlement(order["token"])
    return True


def expire_order(order):
    _close_unpaid(order["token"], "expired")


def cancel_order(order):
    _close_unpaid(order["token"], "cancelled")


def _close_unpaid(token, status):
    """Uzavře nezaplacenou objednávku. Stav se mění jako první a podmíněně —
    kdo prohraje závod (objednávka se mezitím zaplatila), nesmí vrátit sklad
    ani uvolnit už spotřebovaný dárkový kredit."""
    if not store.set_status(token, status, expect="new"):
        return False
    store.return_stock(token)
    store.release_voucher(token)
    return True


def lifecycle_tick():
    for o in store.expired_candidates(order_ttl()):
        # poslední šance: mohla být zaplacená se zavřenou záložkou.
        # Při „unknown" (brána nedostupná) se NERUŠÍ — zkusí se příště.
        if payment_state(o) == "unpaid":
            expire_order(o)
        else:
            check_order_paid(o)
    for p in store.pending_payments(86400):
        o = store.get_order(p["order_token"])
        if not o:
            continue
        if o["status"] == "new":
            check_order_paid(o)
        else:
            recover_late_payment(o, p)
    for o in store.wipe_candidates(wipe_after()):
        store.wipe_delivery(o["token"])
    vouchers.push_redemptions()


def recover_late_payment(order, payment):
    """Platba dorazila až po expiraci/zrušení objednávky. Peníze jsou na
    peněžence, takže objednávku nelze nechat ležet: když je zboží pořád
    skladem, obnoví se; jinak jde do stavu „k vrácení" a řeší se ručně."""
    if hash_state(payment["payment_hash"]) != "paid":
        return
    if not store.settle_payment(payment["payment_hash"]):
        return
    token = order["token"]
    items = [(store.get_product(i["product_id"]), i["qty"])
             for i in store.get_items(token)]
    items = [(p, q) for p, q in items if p]
    ok, _reason = store.reserve_stock(items) if items else (True, "")
    if ok and store.set_status(token, "paid", expect=("expired", "cancelled")):
        store.set_paid_at(token)
        store.consume_voucher(token)
        vouchers.issue_for_order(token)
        store.bump_stat("stat_orders", 1)
        store.bump_stat("stat_sat", order["total_sat"])
        return
    if ok:
        store.return_stock(token)
    # zboží už není — objednávka zůstane uzavřená a platba se ukáže
    # v administraci jako „dorazila k uzavřené objednávce" (řeší se ručně)


def lifecycle_loop():
    while True:
        time.sleep(LIFECYCLE_INTERVAL)
        try:
            lifecycle_tick()
        except Exception:
            pass  # smyčka nesmí umřít; nelogujeme (soukromí)


# --- HTML --------------------------------------------------------------------

def fmt_sat(n):
    return format(int(n), ",").replace(",", " ")


def page(title, body, extra=""):
    return """<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s — obchod Phone21</title>
<link rel="stylesheet" href="%s">
<style>
/* Doplňky ke sdílenému stylu: formuláře (style.css dává inputům width:100%%,
   což u radiobuttonů rozhází layout) */
fieldset { border:1px solid var(--line); border-radius:var(--radius-sm);
  padding:.4rem 1rem 1rem; margin:1.2rem 0; background:var(--surface); }
legend { color:var(--muted); font-size:.85rem; padding:0 .35rem; }
label { display:block; margin:.8rem 0 .25rem; color:var(--muted); font-size:.85rem; }
input[type=radio], input[type=checkbox] { width:auto; margin:0; }
label.opt { display:flex; align-items:flex-start; gap:.6rem; margin:.5rem 0;
  padding:.65rem .8rem; border:1px solid var(--line); border-radius:var(--radius-sm);
  color:var(--fg); font-size:.95rem; cursor:pointer; }
label.opt:hover { border-color:var(--accent); }
label.opt input { margin-top:.3rem; flex:none; }
label.opt b { display:block; }
label.opt span { display:block; color:var(--muted); font-size:.85rem; }
.sub { margin:.4rem 0 0; padding:.2rem 0 0 .2rem; }
.sub input[type=text] { max-width:28rem; }
</style>
<script src="%s"></script>
<script>
/* košík: jen localStorage, server o něm neví až do checkoutu */
function cart(){try{return JSON.parse(localStorage.getItem("obchod_cart")||"[]")}catch(e){return[]}}
function cartSave(c){localStorage.setItem("obchod_cart",JSON.stringify(c));cartBadge();}
function cartAdd(id){var c=cart(),f=c.find(function(i){return i.id===id});
 if(f){f.qty=Math.min(99,f.qty+1)}else{c.push({id:id,qty:1})}cartSave(c);}
function cartDel(id){cartSave(cart().filter(function(i){return i.id!==id}));}
function cartQty(id,q){var c=cart(),f=c.find(function(i){return i.id===id});
 if(f){f.qty=Math.max(1,Math.min(99,q|0));cartSave(c);}}
function cartBadge(){var n=cart().reduce(function(s,i){return s+i.qty},0);
 var el=document.getElementById("cartn");if(el)el.textContent=n?(" ("+n+")"):"";}
document.addEventListener("DOMContentLoaded",cartBadge);
</script></head>
<body>
<header class="site-header"><div class="container">
<a class="brand" href="%s">Phone<em>21</em></a>
<nav class="main"><a href="%s">Nabídka</a><a href="%s">Košík<span id="cartn"></span></a>
<a href="%s">Návod</a><a href="%s">Můj účet</a></nav>
</div></header>
<main class="container">%s</main>
<footer><a href="%s">Phone21</a> · <a href="%s">návod</a> ·
žádné účty · platba Lightningem · doručovací údaje se po vyřízení mažou</footer>
%s</body></html>""" % (
        html.escape(title), u("/static/style.css"), u("/static/qrcode.min.js"),
        SITE_HOME, u("/"), u("/kosik"), SITE_DOCS, SITE_ACCOUNT, body,
        SITE_HOME, SITE_DOCS, extra)


KIND_LABEL = {"physical": "", "voucher": "digitální — dárkový kredit",
              "days": "digitální — dny privátní sítě"}

# Dopravci, u kterých podmínky výslovně připouštějí podání cizí osobou na kód
# (Balíkovna: „Poskytovatel neověřuje totožnost podavatele"; PPL: „SMART PIN
# je přenositelný"). Packeta/DPD schválně NE — obsluha tam tiskne štítek se
# jménem zákazníka, takže by model ztratil smysl.
CARRIERS = {
    "balikovna": ("Balíkovna (Česká pošta)", "podací kód (8 číslic)",
                  "AlzaBox / Penguin Box, kód platí 7 dní"),
    "ppl": ("PPL Balík pro Tebe", "SMART PIN",
            "ParcelBox, PIN platí 15 dní, hodnota do 5 000 Kč"),
}


def normalize_ship_code(raw):
    """Podací kód: 6–14 alfanumerických znaků, bez mezer a pomlček."""
    code = "".join(c for c in raw.upper() if c.isalnum())
    return code if 6 <= len(code) <= 14 else ""

STATUS_LABEL = {
    "new": "čeká na platbu", "paid": "zaplaceno — chystá se k odeslání",
    "shipped": "odesláno", "done": "dokončeno",
    "cancelled": "zrušeno", "expired": "vypršelo (nezaplaceno)",
}


def katalog_body(products):
    if not products:
        return ("<h1>Obchod</h1><p class='muted'>Zrovna tu nic není — "
                "zkus to později.</p>")
    cards = ""
    for p in products:
        badge = ('<span class="badge">%s</span>' % KIND_LABEL[p["kind"]]
                 ) if p["kind"] != "physical" else ""
        soldout = (p["stock"] == 0)
        btn = ("<button disabled>vyprodáno</button>" if soldout else
               '<button onclick="cartAdd(%d)">Do košíku</button>' % p["id"])
        img = ""
        if p["image"]:
            # odkaz na plnou velikost — v kartě jsou popisky u dílů nečitelné
            src = u("/static/produkty/" + p["image"])
            img = ('<a href="%s" target="_blank" rel="noopener">'
                   '<img class="pimg" src="%s" alt="%s" loading="lazy"></a>'
                   % (src, src, html.escape(p["name"])))
        cards += """<div class="card">%s<h2 style="margin-top:0">%s</h2>%s
<p class="muted small">%s</p>
<div class="cardfoot"><span class="price">%s sat</span>%s</div></div>""" % (
            img, html.escape(p["name"]), badge, html.escape(p["descr"]),
            fmt_sat(p["price_sat"]), btn)
    return """<section class="hero"><h1>Obchod</h1>
<p class="lead">Hardware pro vlastní Phone21 a dárky do privátní sítě.
Platba Lightningem, bez účtů. Přepravu si objednáš u dopravce sám (sem se
vkládá jen podací kód), takže se o tobě nikdo nedozví jméno ani adresu.</p></section>
<div class="grid">%s</div>""" % cards


def kosik_body(products, msg=""):
    pmap = {p["id"]: {"name": p["name"], "price": p["price_sat"],
                      "kind": p["kind"]} for p in products}
    widget = captcha.widget_html(store, "obchodCaptchaPass") \
        if captcha.enabled(store) else ""
    banner = '<p class="msg">%s</p>' % html.escape(msg) if msg else ""
    return """%s<h1>Košík</h1>
<div id="empty" class="muted" style="display:none">Košík je prázdný —
<a href="%s">vybrat něco v nabídce</a>.</div>
<table id="ctab" style="display:none"><thead>
<tr><th>položka</th><th>ks</th><th>cena</th><th></th></tr></thead>
<tbody id="crows"></tbody>
<tfoot><tr><th colspan="2">celkem</th><th id="ctotal"></th><th></th></tr></tfoot>
</table>
<form id="cform" method="post" action="%s" style="display:none">
<input type="hidden" name="items" id="items">
<input type="hidden" name="captcha_token" id="captcha_token">
<fieldset><legend>Dárkový kód (nepovinné)</legend>
<input type="text" name="voucher" placeholder="JDNV-XXXX-XXXX-XXXX"
 style="max-width:22rem" autocomplete="off"></fieldset>
<fieldset id="delivery"><legend>Doručení</legend>
<label class="opt"><input type="radio" name="delivery" value="code" checked>
<span><b>Přepravu si objednáš sám</b>Zaplatíš ji u dopravce a sem vložíš jen
podací kód — nikdo se o tobě nedozví nic (ani jméno, ani kam to jde).</span></label>
<label class="opt"><input type="radio" name="delivery" value="personal">
<span><b>Osobní předání</b>Po domluvě (komunita, meetupy).</span></label>
<div id="d-code" class="sub">
<label>Dopravce</label>
<select name="carrier" style="max-width:28rem">
<option value="balikovna">Balíkovna (Česká pošta) — jen ČR</option>
<option value="ppl">PPL Balík pro Tebe — ČR i EU</option>
</select>
<label>Podací kód (můžeš doplnit i po zaplacení na stránce objednávky —
odkaz na ni si ale ulož, jiný přístup k objednávce není)</label>
<input type="text" name="ship_code" placeholder="8 číslic / SMART PIN"
 autocomplete="off">
<div class="small muted">
<p><b>Jak na to:</b> u dopravce si objednej a zaplať přepravu, jako
<b>odesílatele i adresáta vyplň sebe</b> a vyber výdejní box. Dostaneš
podací kód — ten vlož sem. Napíše se na krabici a zásilka půjde do boxu;
potvrzení o podání a sledování přijde na tvůj e-mail.</p>
<p><b>Balíkovna:</b> podání do AlzaBoxu / Penguin Boxu, kód platí 7 dní,
do 15 kg. <b>PPL:</b> podání do ParcelBoxu, SMART PIN platí 15 dní,
hodnota zásilky do 5 000 Kč. Přepravu nikdo odsud neplatí ani nereklamuje —
smlouvu s dopravcem máš ty (proto o tobě nejsou žádné údaje).</p>
</div>
</div>
</fieldset>
<fieldset><legend>Poznámka (nepovinné)</legend>
<input type="text" name="note" style="max-width:28rem">
</fieldset>
%s
<button type="submit" id="paybtn">Zaplatit Lightningem</button>
</form>
<script>
var PRODUCTS=%s;
function render(){var c=cart().filter(function(i){return PRODUCTS[i.id]});
 cartSave(c);var rows="",total=0,physical=false;
 c.forEach(function(i){var p=PRODUCTS[i.id];total+=p.price*i.qty;
  if(p.kind==="physical")physical=true;
  rows+='<tr><td>'+esc(p.name)+'</td><td><input type="number" min="1" max="99" value="'
   +i.qty+'" style="width:4.5rem" onchange="cartQty('+i.id+',this.value);render()"></td><td>'
   +fmt(p.price*i.qty)+' sat</td><td><button type="button" class="small" '
   +'onclick="cartDel('+i.id+');render()">✕</button></td></tr>';});
 document.getElementById("crows").innerHTML=rows;
 document.getElementById("ctotal").textContent=fmt(total)+" sat";
 document.getElementById("empty").style.display=c.length?"none":"";
 document.getElementById("ctab").style.display=c.length?"":"none";
 document.getElementById("cform").style.display=c.length?"":"none";
 document.getElementById("delivery").style.display=physical?"":"none";
}
function esc(s){var d=document.createElement("div");d.textContent=s;return d.innerHTML;}
function fmt(n){return String(n).replace(/\\B(?=(\\d{3})+(?!\\d))/g," ");}
document.querySelectorAll('input[name=delivery]').forEach(function(r){
 r.addEventListener("change",function(){
  document.getElementById("d-code").style.display=this.value==="code"?"":"none";});});
var CAPTCHA=%s;
function obchodCaptchaPass(token){document.getElementById("captcha_token").value=token;}
document.getElementById("cform").addEventListener("submit",function(e){
 document.getElementById("items").value=JSON.stringify(cart());
 if(CAPTCHA && !document.getElementById("captcha_token").value){
  e.preventDefault();alert("Počkej prosím na dokončení ověření (pár vteřin).");}
});
document.addEventListener("DOMContentLoaded",render);
</script>""" % (banner, u("/"), u("/checkout"), widget,
                json.dumps(pmap), "true" if captcha.enabled(store) else "false")


def order_body(order, items, codes):
    token = order["token"]
    st = order["status"]
    rows = "".join(
        "<tr><td>%s</td><td>%d×</td><td>%s sat</td></tr>" % (
            html.escape(i["name"]), i["qty"], fmt_sat(i["price_sat"] * i["qty"]))
        for i in items)
    discount = order["discount_sat"] or 0
    if discount:
        rows += ('<tr><td>dárkový kredit</td><td></td><td>-%s sat</td></tr>'
                 % fmt_sat(discount))
    body = """<h1>Objednávka <span class="mono">%s</span></h1>
<p>Stav: <b>%s</b></p>
<table><tr><th>položka</th><th></th><th></th></tr>%s
<tr><th colspan="2">k úhradě</th><th>%s sat</th></tr></table>
<div class="linkbox">
<b>Ulož si odkaz na tuhle objednávku.</b> Je jediný přístup k ní — účty
nejsou a jinak se k jejímu stavu ani k doplnění podacího kódu nedostaneš.
<div class="linkrow">
<input id="ordurl" readonly onclick="this.select()">
<button type="button" id="ordcopy">Zkopírovat</button>
</div>
<span class="small muted" id="ordmsg"></span>
</div>""" % (
        html.escape(token[:8]), STATUS_LABEL.get(st, st), rows,
        fmt_sat(order["total_sat"]))

    extra = ""
    if st == "new":
        pay = store.get_payment(order["payment_hash"]) if order["payment_hash"] else None
        if pay:
            mins = max(0, (order["created_at"] + order_ttl()) - int(time.time())) // 60
            body += """<h2>Zaplať Lightningem</h2>
<div id="qr" class="qr"></div>
<p class="mono" style="word-break:break-all">%s</p>
<p class="small muted">Objednávka čeká %d min, pak se ruší a sklad
se uvolní. Po zaplacení se stránka sama obnoví.</p>""" % (
                html.escape(pay["bolt11"]), mins)
            extra = """<script>
new QRCode(document.getElementById("qr"), {text: %s, width: 260, height: 260,
  correctLevel: QRCode.CorrectLevel.M});
(function poll(){fetch(%s).then(function(r){return r.json()})
 .then(function(d){if(d.paid){location.reload()}else{setTimeout(poll,2000)}})
 .catch(function(){setTimeout(poll,4000)});})();
</script>""" % (json.dumps("lightning:" + pay["bolt11"]),
                json.dumps(u("/pay/poll?t=") + token))
    else:
        if codes:
            code_rows = ""
            for c in codes:
                if c["kind"] == "days":
                    note = ('dny privátní sítě — uplatníš na '
                            '<a href="%s">svém účtu sítě</a>' %
                            html.escape(NET_DASHBOARD))
                else:
                    note = "dárkový kredit obchodu — zadává se v košíku"
                code_rows += '<tr><td class="mono">%s</td><td>%s</td></tr>' % (
                    html.escape(c["code"]), note)
            body += ("<h2>Tvoje kódy</h2><table><tr><th>kód</th><th>k čemu</th>"
                     "</tr>%s</table>" % code_rows)
        if order["delivery"] == "code" and not order["wiped"]:
            name, code_label, note = CARRIERS.get(
                order["carrier"] or "", ("dopravce", "podací kód", ""))
            if order["ship_code"]:
                body += ("<h2>Doprava</h2><p>%s — %s: <b class='mono'>%s</b>."
                         " Kód se napíše na krabici a zásilka se podá; potvrzení ti "
                         "přijde od dopravce na e-mail.</p>" % (
                             html.escape(name), html.escape(code_label),
                             html.escape(order["ship_code"])))
            elif order["status"] in ("paid", "shipped"):
                body += ("""<h2>Podací kód</h2>
<p>U dopravce <b>%s</b> si objednej a zaplať přepravu (jako odesílatele
i adresáta vyplň sebe, vyber výdejní box) — %s. Pak sem vlož %s:</p>
<form class="inline" method="post" action="%s">
<input type="text" name="ship_code" placeholder="%s" autocomplete="off"
 style="max-width:16rem" required>
<button type="submit">Uložit kód</button></form>
<p class="small muted">Bez kódu se zásilka podat nedá — a víc není potřeba.
Vlož ho až ve chvíli, kdy ho máš: jeho platnost běží
od chvíle, kdy ti ho dopravce vystaví.</p>""" % (
                    html.escape(name), html.escape(note),
                    html.escape(code_label), u("/o/%s/kod" % token),
                    html.escape(code_label)))
        elif order["delivery"] == "personal" and not order["wiped"]:
            body += ("<h2>Osobní předání</h2><p>Domluva probíhá přes komunitu"
                     " — ozvi se tam, kde jsi o Phone21 slyšel (sraz,"
                     " skupina). Stačí ukázat tuhle stránku, podle ní se objednávka"
                     " najde; nic dalšího potřeba není.</p>")
        elif order["delivery"] in ("point", "anon") and not order["wiped"]:
            # legacy objednávky ze starého modelu doručení
            body += ("<p class='small muted'>Doručení: %s %s</p>" % (
                html.escape(order["delivery"]),
                html.escape(order["point_id"] or "")))

    # odkaz doplní prohlížeč — server nezná doménu, pod kterou běží
    extra += """<script>
(function(){
  try { localStorage.removeItem("obchod_cart"); } catch (e) {}
  var f = document.getElementById("ordurl");
  var b = document.getElementById("ordcopy");
  var m = document.getElementById("ordmsg");
  if (!f || !b) { return; }
  f.value = location.href;
  b.addEventListener("click", function(){
    var done = function(){ m.textContent = "Zkopírováno."; };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(f.value).then(done, fallback);
    } else { fallback(); }
    function fallback(){
      f.select();
      try { document.execCommand("copy"); done(); }
      catch (e) { m.textContent = "Zkopíruj odkaz ručně z pole výše."; }
    }
  });
})();
</script>"""
    return page("Objednávka", body, extra)


# --- HTTP handler ------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "Obchod"
    protocol_version = "HTTP/1.1"
    _body = None

    def log_message(self, fmt, *args):
        pass  # nic — ani IP, ani cesty s tokeny

    def _send(self, status, body, ctype="text/html; charset=utf-8", extra=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj), "application/json")

    def _redirect(self, target):
        self._send(303, "", extra={"Location": target})

    def _read_body(self):
        if self._body is None:
            length = int(self.headers.get("Content-Length") or 0)
            self._body = self.rfile.read(min(length, 65536)) if length else b""
        return self._body

    def _form(self):
        raw = self._read_body().decode(errors="replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def _client_ip(self):
        real = (self.headers.get("X-Real-IP") or "").strip()
        return real or self.client_address[0]

    def _path(self):
        """Cesta bez BASE_PATH prefixu (nginx prefix nestrhává)."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if BASE and path.startswith(BASE):
            path = path[len(BASE):] or "/"
        return path, urllib.parse.parse_qs(parsed.query)

    def do_GET(self):
        self._body = b""
        path, qs = self._path()
        if path == "/":
            return self._send(200, page("Nabídka",
                                        katalog_body(store.list_products())))
        if path == "/kosik":
            return self._send(200, page("Košík",
                                        kosik_body(store.list_products())))
        if path.startswith("/o/"):
            return self.get_order(path[3:])
        if path == "/pay/poll":
            return self.get_pay_poll(qs)
        if path.startswith("/static/"):
            return self.get_static(path)
        self._send(404, page("404", "<h1>404</h1>"))

    def do_POST(self):
        self._body = None
        path, _qs = self._path()
        if path == "/checkout":
            if not limiter.allow(self._client_ip()):
                self._read_body()
                return self._send(429, page("Zpomal",
                    "<h1>Moc požadavků</h1><p>Zkus to za chvíli.</p>"))
            return self.post_checkout(self._form())
        if path.startswith("/o/") and path.endswith("/kod"):
            return self.post_ship_code(path[3:-4], self._form())
        self._read_body()
        self._send(404, page("404", "<h1>404</h1>"))

    # -- stránky --

    def get_order(self, token):
        token = "".join(c for c in token if c.isalnum() or c in "-_")
        order = store.get_order(token)
        if not order:
            return self._send(404, page("404", "<h1>Objednávka nenalezena</h1>"))
        if order["status"] == "new":
            check_order_paid(order)
            order = store.get_order(token)
        codes = store.vouchers_for_order(token)
        self._send(200, order_body(order, store.get_items(token), codes))

    def post_ship_code(self, token, form):
        """Zákazník doplní podací kód po zaplacení (dokud není odesláno)."""
        token = "".join(c for c in token if c.isalnum() or c in "-_")
        order = store.get_order(token)
        if not order or order["delivery"] != "code":
            return self._send(404, page("404", "<h1>Objednávka nenalezena</h1>"))
        if order["status"] not in ("paid", "shipped"):
            return self._send(400, page("Objednávka",
                "<h1>Kód zatím nejde uložit</h1><p>Objednávka musí být "
                "nejdřív zaplacená.</p>"))
        code = normalize_ship_code(form.get("ship_code", ""))
        if not code:
            return self._send(400, page("Objednávka",
                "<h1>Neplatný kód</h1><p>Podací kód má 6–14 znaků "
                "(číslice/písmena). <a href=\"%s\">Zpět na objednávku</a></p>"
                % u("/o/" + token)))
        store.set_ship_code(token, code)
        self._redirect(u("/o/" + token))

    def get_pay_poll(self, qs):
        token = (qs.get("t") or [""])[0]
        order = store.get_order(token)
        if not order:
            return self._json({"paid": False}, 404)
        self._json({"paid": check_order_paid(order)})

    def get_static(self, path):
        rel = path[len("/static/"):]
        # jen kořen static/ a jeden povolený podadresář — žádné procházení stromem
        parts = rel.split("/")
        if len(parts) > 2 or (len(parts) == 2 and parts[0] != "produkty"):
            return self._send(404, "not found", "text/plain")
        name = os.path.basename(parts[-1])
        fpath = os.path.join(os.path.dirname(__file__), "static", *parts[:-1], name)
        ctypes = {".css": "text/css", ".js": "application/javascript",
                  ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".png": "image/png", ".webp": "image/webp"}
        ext = os.path.splitext(name)[1].lower()
        if ext not in ctypes or not os.path.isfile(fpath):
            return self._send(404, "not found", "text/plain")
        with open(fpath, "rb") as f:
            self._send(200, f.read(), ctypes[ext],
                       {"Cache-Control": "max-age=3600"})

    # -- checkout --

    def post_checkout(self, form):
        # 1. položky (max 20, qty 1..99, jen aktivní produkty)
        try:
            raw = json.loads(form.get("items", ""))
            assert isinstance(raw, list) and 0 < len(raw) <= 20
            wanted = []
            seen = set()
            for it in raw:
                pid, qty = int(it["id"]), int(it["qty"])
                assert 1 <= qty <= 99 and pid not in seen
                seen.add(pid)
                wanted.append((pid, qty))
        except (ValueError, TypeError, KeyError, AssertionError):
            return self._kosik_err("Neplatný obsah košíku.")
        pairs = []
        for pid, qty in wanted:
            prod = store.get_product(pid)
            if not prod or not prod["active"]:
                return self._kosik_err("Některý produkt už není v nabídce — "
                                       "zkontroluj košík.")
            pairs.append((prod, qty))

        # 2. doručení (jen když je v košíku fyzická položka). Přepravu si
        # objednává zákazník sám — potřeba je jen napsat kód na krabici,
        # takže se o něm neukládá žádný osobní údaj.
        physical = any(p["kind"] == "physical" for p, _q in pairs)
        delivery = carrier = ship_code = None
        if physical:
            delivery = form.get("delivery", "")
            if delivery == "code":
                carrier = form.get("carrier", "")
                if carrier not in CARRIERS:
                    return self._kosik_err("Vyber dopravce.")
                ship_code = normalize_ship_code(form.get("ship_code", ""))
                if form.get("ship_code", "").strip() and not ship_code:
                    return self._kosik_err("Podací kód vypadá divně — zkontroluj "
                                           "ho, nebo ho doplň později u objednávky.")
            elif delivery != "personal":
                return self._kosik_err("Vyber způsob doručení.")
        note = form.get("note", "").strip()[:500] or None

        # 3. captcha (ekonomická bariéra před vystavením invoice)
        if not captcha.verify(store, form.get("captcha_token", "")):
            return self._kosik_err("Ověření prohlížeče neprošlo — zkus to znovu.")

        # 4. ceny VÝHRADNĚ z DB
        total = sum(p["price_sat"] * q for p, q in pairs)
        token = secrets.token_urlsafe(16)

        # 5. dárkový kód (rezervace — při jakékoli další chybě se vrací)
        discount = 0
        if form.get("voucher", "").strip():
            discount, err = vouchers.voucher_discount(
                form["voucher"], total, token)
            if err:
                return self._kosik_err("Dárkový kód: %s" % err)

        # 6. sklad (atomicky vše, nebo nic)
        ok, err = store.reserve_stock(pairs)
        if not ok:
            store.release_voucher(token)
            return self._kosik_err(err)

        to_pay = total - discount
        store.create_order(token, to_pay, delivery, carrier, ship_code, note)
        if discount:
            store.set_discount(token, discount)
        for prod, qty in pairs:
            store.add_item(token, prod, qty)

        # 7. plně pokryto kreditem → rovnou zaplaceno, žádná invoice
        if to_pay == 0:
            apply_settlement(token)
            return self._redirect(u("/o/") + token)

        # 8. LN invoice
        try:
            inv = manager.current().create_invoice(
                to_pay, "obchod %s" % token[:8], expiry_seconds=order_ttl())
        except payments.PaymentError:
            store.return_stock(token)
            store.release_voucher(token)
            store.set_status(token, "cancelled")
            return self._kosik_err("Platby jsou dočasně nedostupné — "
                                   "zkus to prosím později.")
        store.add_payment(inv.payment_hash, token, to_pay, inv.bolt11)
        store.set_order_payment(token, inv.payment_hash)
        self._redirect(u("/o/") + token)

    def _kosik_err(self, msg):
        self._send(400, page("Košík", kosik_body(store.list_products(), msg)))


# --- start -------------------------------------------------------------------

SEED_PRODUCTS = (
    # (name, descr, price_sat, kind, days, stock, image)  — vše NEaktivní,
    # ceny jsou placeholder; zapíná a ladí se v admin UI.
    ("Phone21 miniserver", "Kompletní set: miniserver s nahraným systémem "
     "+ USB modem. Zapojíš SIM a jedeš.", 2_500_000, "physical", 0, 0,
     "set.jpg"),
    ("USB LTE modem", "Kompatibilní modem pro vlastní stavbu (návod na webu).",
     900_000, "physical", 0, 0, "modem.jpg"),
    ("Dárkový kredit obchodu", "Kód na nákup čehokoli tady — dárek bez "
     "vyzvídání adresy.", 100_000, "voucher", 0, -1, ""),
    ("Dárkové dny privátní sítě (30)", "Kód na 30 dní privátní sítě — "
     "obdarovaný ho uplatní na svém účtu.", 12_000, "days", 30, -1, ""),
)


def seed_products():
    if store.list_products(active_only=False):
        return
    for name, descr, price, kind, days, stock, image in SEED_PRODUCTS:
        store.add_product(name, descr, price, kind, days, stock, active=0,
                          image=image)


def main():
    store.connect()
    seed_products()
    import admin
    admin.start(manager=manager, cancel_order=cancel_order)
    threading.Thread(target=lifecycle_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Handler)
    print("obchod na portu %d (BASE_PATH=%r)" % (WEB_PORT, BASE), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

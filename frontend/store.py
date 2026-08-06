"""SQLite úložiště obchodu (WAL). Drží se minimum dat: doručovací údaje se
po dokončení objednávky mažou (wipe), časy jsou unixové vteřiny (int).
Vzor převzat z CockScale/frontend/store.py (modulový singleton + lock)."""

import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("OBCHOD_DB", "/var/lib/obchod/obchod.db")

_lock = threading.Lock()
_conn = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  name      TEXT NOT NULL,
  descr     TEXT NOT NULL DEFAULT '',
  price_sat INTEGER NOT NULL,
  kind      TEXT NOT NULL DEFAULT 'physical',  -- physical | voucher | days
  days      INTEGER NOT NULL DEFAULT 0,        -- jen kind=days
  stock     INTEGER NOT NULL DEFAULT 0,        -- -1 = neomezeně (digitální)
  active    INTEGER NOT NULL DEFAULT 0,
  sort      INTEGER NOT NULL DEFAULT 100
);
CREATE TABLE IF NOT EXISTS orders(
  token         TEXT PRIMARY KEY,
  created_at    INTEGER NOT NULL,
  status        TEXT NOT NULL DEFAULT 'new',
  -- new | paid | shipped | done | cancelled | expired
  total_sat     INTEGER NOT NULL,
  delivery      TEXT,            -- code | personal | NULL (jen digitální)
  carrier       TEXT,            -- balikovna | ppl (u delivery=code)
  ship_code     TEXT,            -- podací kód od zákazníka (my ho píšeme na krabici)
  point_id      TEXT,            -- legacy (staré objednávky point/anon)
  recip_name    TEXT,            -- legacy
  recip_contact TEXT,            -- legacy
  note          TEXT,
  payment_hash  TEXT,
  pickup_code   TEXT,
  paid_at       INTEGER,
  done_at       INTEGER,
  wiped         INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS order_items(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  order_token TEXT NOT NULL,
  product_id  INTEGER NOT NULL,
  name        TEXT NOT NULL,     -- denormalizace: historie přežije úpravy produktu
  kind        TEXT NOT NULL,
  days        INTEGER NOT NULL DEFAULT 0,
  qty         INTEGER NOT NULL,
  price_sat   INTEGER NOT NULL   -- cena za kus v okamžiku objednávky
);
CREATE TABLE IF NOT EXISTS payments(
  payment_hash TEXT PRIMARY KEY,
  order_token  TEXT NOT NULL,
  amount_sat   INTEGER NOT NULL,
  bolt11       TEXT NOT NULL,
  created_at   INTEGER NOT NULL,
  settled_at   INTEGER
);
CREATE TABLE IF NOT EXISTS vouchers(
  code            TEXT PRIMARY KEY,
  kind            TEXT NOT NULL,          -- voucher (kredit obchodu) | days
  value_sat       INTEGER NOT NULL DEFAULT 0,
  days            INTEGER NOT NULL DEFAULT 0,
  src_order       TEXT NOT NULL,          -- ze které objednávky vznikl
  created_at      INTEGER NOT NULL,
  redeemed_order  TEXT,                   -- kde byl uplatněn (rezervace)
  redeemed_at     INTEGER
);
CREATE TABLE IF NOT EXISTS redemption_queue(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  code       TEXT NOT NULL,
  days       INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  sent_at    INTEGER,
  attempts   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings(
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def connect(path=None):
    """Otevře (a případně založí) databázi. Volat jednou při startu."""
    global _conn
    path = path or DB_PATH
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(_SCHEMA)
    _migrate()
    _conn.commit()
    return _conn


# Přidávané sloupce (schéma se vyvíjí, data ne). CREATE TABLE IF NOT EXISTS
# existující tabulku nezmění, takže nové sloupce doplňujeme ručně.
_ADDED_COLUMNS = (
    ("orders", "carrier", "TEXT"),
    ("orders", "ship_code", "TEXT"),
    ("products", "image", "TEXT"),  # jméno souboru ve static/produkty/
    ("vouchers", "value_left", "INTEGER"),   # zbývající kredit
    ("vouchers", "reserved_sat", "INTEGER"), # rezervováno na rozpracovanou obj.
    ("orders", "discount_sat", "INTEGER"),   # kolik pokryl dárkový kredit
)


def _migrate():
    for table, column, ctype in _ADDED_COLUMNS:
        cols = [r["name"] for r in
                _conn.execute("PRAGMA table_info(%s)" % table).fetchall()]
        if column not in cols:
            _conn.execute("ALTER TABLE %s ADD COLUMN %s %s"
                          % (table, column, ctype))
    # dřív se kredit utrácel celý najednou; starým kódům dopočítáme zůstatek
    _conn.execute(
        "UPDATE vouchers SET value_left = CASE WHEN redeemed_at IS NULL"
        " THEN value_sat ELSE 0 END WHERE value_left IS NULL")
    _conn.execute("UPDATE vouchers SET reserved_sat = 0"
                  " WHERE reserved_sat IS NULL")
    _conn.execute("UPDATE orders SET discount_sat = 0"
                  " WHERE discount_sat IS NULL")


def _execute(sql, params=()):
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur


def query(sql, params=()):
    with _lock:
        return _conn.execute(sql, params).fetchall()


def query_one(sql, params=()):
    with _lock:
        return _conn.execute(sql, params).fetchone()


# --- settings (runtime konfigurace z admin UI; env je jen výchozí hodnota) ---

def get_setting(key, default=None):
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key, value):
    _execute(
        "INSERT INTO settings(key, value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def bump_stat(key, amount=1):
    with _lock:
        cur = _conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        total = int(cur["value"]) + amount if cur else amount
        _conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(total)),
        )
        _conn.commit()
        return total


def get_stat(key):
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


# --- products ----------------------------------------------------------------

def add_product(name, descr, price_sat, kind="physical", days=0, stock=0,
                active=0, sort=100, image=""):
    cur = _execute(
        "INSERT INTO products(name, descr, price_sat, kind, days, stock,"
        " active, sort, image) VALUES(?,?,?,?,?,?,?,?,?)",
        (name, descr, price_sat, kind, days, stock, active, sort, image),
    )
    return cur.lastrowid


def update_product(pid, **fields):
    allowed = {"name", "descr", "price_sat", "kind", "days", "stock",
               "active", "sort", "image"}
    keys = [k for k in fields if k in allowed]
    if not keys:
        return
    sql = "UPDATE products SET " + ", ".join("%s=?" % k for k in keys) + " WHERE id=?"
    _execute(sql, tuple(fields[k] for k in keys) + (pid,))


def delete_product(pid):
    _execute("DELETE FROM products WHERE id=?", (pid,))


def get_product(pid):
    return query_one("SELECT * FROM products WHERE id=?", (pid,))


def list_products(active_only=True):
    if active_only:
        return query("SELECT * FROM products WHERE active=1 ORDER BY sort, id")
    return query("SELECT * FROM products ORDER BY sort, id")


def reserve_stock(items):
    """Atomicky zarezervuje sklad pro [(product_row, qty), ...] — všechno,
    nebo nic. Vrací (True, "") nebo (False, "důvod"). stock=-1 = neomezeně."""
    with _lock:
        for prod, qty in items:
            row = _conn.execute(
                "SELECT stock, active FROM products WHERE id=?", (prod["id"],)
            ).fetchone()
            if not row or not row["active"]:
                return False, "Produkt „%s“ už není v nabídce." % prod["name"]
            if row["stock"] != -1 and row["stock"] < qty:
                return False, "Produkt „%s“ už není skladem." % prod["name"]
        for prod, qty in items:
            _conn.execute(
                "UPDATE products SET stock = stock - ? WHERE id=? AND stock != -1",
                (qty, prod["id"]),
            )
        _conn.commit()
        return True, ""


def return_stock(order_token):
    """Vrátí rezervovaný sklad položek objednávky (expirace/zrušení)."""
    with _lock:
        items = _conn.execute(
            "SELECT product_id, qty FROM order_items WHERE order_token=?",
            (order_token,),
        ).fetchall()
        for it in items:
            _conn.execute(
                "UPDATE products SET stock = stock + ? WHERE id=? AND stock != -1",
                (it["qty"], it["product_id"]),
            )
        _conn.commit()


# --- orders ------------------------------------------------------------------

def create_order(token, total_sat, delivery, carrier=None, ship_code=None,
                 note=None):
    _execute(
        "INSERT INTO orders(token, created_at, status, total_sat, delivery,"
        " carrier, ship_code, note) VALUES(?,?,?,?,?,?,?,?)",
        (token, int(time.time()), "new", total_sat, delivery, carrier,
         ship_code, note),
    )


def set_ship_code(token, code):
    """Podací kód od zákazníka (píšeme ho na krabici). Jde doplnit i později
    na stránce objednávky — dokud není odesláno."""
    _execute("UPDATE orders SET ship_code=? WHERE token=?", (code, token))


def add_item(order_token, product, qty):
    _execute(
        "INSERT INTO order_items(order_token, product_id, name, kind, days,"
        " qty, price_sat) VALUES(?,?,?,?,?,?,?)",
        (order_token, product["id"], product["name"], product["kind"],
         product["days"], qty, product["price_sat"]),
    )


def get_order(token):
    return query_one("SELECT * FROM orders WHERE token=?", (token,))


def get_items(token):
    return query("SELECT * FROM order_items WHERE order_token=? ORDER BY id",
                 (token,))


def set_status(token, status, expect=None):
    """Změna stavu. S `expect` je to CAS — projde jen z očekávaného stavu
    (jinak by pomalý lifecycle přepsal objednávku, která se mezitím
    zaplatila). Vrací True, když se stav opravdu změnil."""
    if expect is None:
        cur = _execute("UPDATE orders SET status=? WHERE token=?", (status, token))
        return cur.rowcount == 1
    if isinstance(expect, str):
        expect = (expect,)
    placeholders = ",".join("?" * len(expect))
    cur = _execute(
        "UPDATE orders SET status=? WHERE token=? AND status IN (%s)" % placeholders,
        (status, token) + tuple(expect),
    )
    return cur.rowcount == 1


def mark_paid(token):
    """Přechod new→paid; vrací True jen při prvním úspěchu (idempotence)."""
    cur = _execute(
        "UPDATE orders SET status='paid', paid_at=? WHERE token=? AND status='new'",
        (int(time.time()), token),
    )
    return cur.rowcount == 1


def set_discount(token, amount_sat):
    _execute("UPDATE orders SET discount_sat=? WHERE token=?",
             (int(amount_sat), token))


def set_paid_at(token):
    """Doplní čas platby u objednávky, která se zaplatila mimo new→paid
    (pozdní platba po expiraci)."""
    _execute("UPDATE orders SET paid_at=? WHERE token=? AND paid_at IS NULL",
             (int(time.time()), token))


def mark_done(token):
    _execute(
        "UPDATE orders SET status='done', done_at=? WHERE token=?",
        (int(time.time()), token),
    )


def set_order_payment(token, payment_hash):
    _execute("UPDATE orders SET payment_hash=? WHERE token=?",
             (payment_hash, token))


def set_pickup_code(token, code):
    _execute("UPDATE orders SET pickup_code=? WHERE token=?", (code, token))


def list_orders(limit=100):
    return query("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?",
                 (limit,))


def expired_candidates(ttl_seconds):
    return query(
        "SELECT * FROM orders WHERE status='new' AND created_at < ?",
        (int(time.time()) - ttl_seconds,),
    )


def wipe_candidates(after_seconds):
    return query(
        "SELECT * FROM orders WHERE status IN ('done','cancelled','expired')"
        " AND wiped=0 AND coalesce(done_at, created_at) + ? <= ?",
        (after_seconds, int(time.time())),
    )


def wipe_delivery(token):
    """Smaže doručovací/kontaktní údaje — z objednávky zbyde jen účetní stopa."""
    _execute(
        "UPDATE orders SET point_id=NULL, recip_name=NULL, recip_contact=NULL,"
        " note=NULL, pickup_code=NULL, ship_code=NULL, wiped=1 WHERE token=?",
        (token,),
    )


# --- payments ----------------------------------------------------------------

def add_payment(payment_hash, order_token, amount_sat, bolt11):
    _execute(
        "INSERT INTO payments(payment_hash, order_token, amount_sat, bolt11,"
        " created_at) VALUES(?,?,?,?,?)",
        (payment_hash, order_token, amount_sat, bolt11, int(time.time())),
    )


def get_payment(payment_hash):
    return query_one("SELECT * FROM payments WHERE payment_hash=?",
                     (payment_hash,))


def settle_payment(payment_hash):
    """Idempotentní settled — True jen napoprvé (souběžný polling/reconcile)."""
    cur = _execute(
        "UPDATE payments SET settled_at=? WHERE payment_hash=? AND settled_at IS NULL",
        (int(time.time()), payment_hash),
    )
    return cur.rowcount == 1


def pending_payments(max_age_seconds):
    """Nesettled platby objednávek, které ještě nejsou uzavřené — včetně
    expirovaných, protože faktura mohla dojít až po expiraci."""
    return query(
        "SELECT p.* FROM payments p JOIN orders o ON o.token = p.order_token"
        " WHERE p.settled_at IS NULL"
        " AND o.status IN ('new','expired','cancelled')"
        " AND p.created_at > ?",
        (int(time.time()) - max_age_seconds,),
    )


def payments_on_closed_orders():
    """Platby, které dorazily k objednávce, co se už nedala obnovit. Peníze
    na peněžence jsou — ať to obsluha aspoň vidí."""
    return query(
        "SELECT p.* FROM payments p JOIN orders o ON o.token = p.order_token"
        " WHERE p.settled_at IS NOT NULL AND o.status IN ('expired','cancelled')"
        " ORDER BY p.created_at DESC")


# --- vouchers ----------------------------------------------------------------

def add_voucher(code, kind, value_sat, days, src_order):
    _execute(
        "INSERT INTO vouchers(code, kind, value_sat, days, src_order,"
        " created_at, value_left, reserved_sat) VALUES(?,?,?,?,?,?,?,0)",
        (code, kind, value_sat, days, src_order, int(time.time()), value_sat),
    )


def get_voucher(code):
    return query_one("SELECT * FROM vouchers WHERE code=?", (code,))


def vouchers_for_order(src_order):
    return query("SELECT * FROM vouchers WHERE src_order=? ORDER BY code",
                 (src_order,))


def reserve_voucher(code, order_token, amount_sat):
    """Atomická rezervace části store-kreditu pro objednávku (jen
    kind=voucher). Kredit se nespotřebuje celý — zbytek zůstává na kódu
    a jde utratit příště."""
    cur = _execute(
        "UPDATE vouchers SET redeemed_order=?, reserved_sat=?"
        " WHERE code=? AND kind='voucher' AND redeemed_order IS NULL"
        " AND value_left >= ?",
        (order_token, int(amount_sat), code, int(amount_sat)),
    )
    return cur.rowcount == 1


def consume_voucher(order_token):
    """Objednávka je zaplacená → rezervovaný kredit se opravdu odečte."""
    _execute(
        "UPDATE vouchers SET value_left = value_left - reserved_sat,"
        " reserved_sat = 0, redeemed_order = NULL,"
        " redeemed_at = CASE WHEN value_left - reserved_sat <= 0"
        "                    THEN ? ELSE redeemed_at END"
        " WHERE redeemed_order=?",
        (int(time.time()), order_token),
    )


def release_voucher(order_token):
    """Vrátí rezervovaný kredit expirované/zrušené objednávky."""
    _execute(
        "UPDATE vouchers SET redeemed_order=NULL, reserved_sat=0"
        " WHERE redeemed_order=?",
        (order_token,),
    )


# --- redemption queue (registrace dnů u CockScale) ---------------------------

def enqueue_redemption(code, days):
    _execute(
        "INSERT INTO redemption_queue(code, days, created_at) VALUES(?,?,?)",
        (code, days, int(time.time())),
    )


MAX_REDEMPTION_ATTEMPTS = 20


def pending_redemptions(limit=10, include_stuck=False):
    """Fronta registrací. Trvale selhávající položky se po
    MAX_REDEMPTION_ATTEMPTS pokusech přeskakují, aby neblokovaly novější
    kódy (dřív stačilo deset zaseknutých na začátku fronty)."""
    if include_stuck:
        return query(
            "SELECT * FROM redemption_queue WHERE sent_at IS NULL"
            " ORDER BY id LIMIT ?", (limit,))
    return query(
        "SELECT * FROM redemption_queue WHERE sent_at IS NULL AND attempts < ?"
        " ORDER BY id LIMIT ?",
        (MAX_REDEMPTION_ATTEMPTS, limit),
    )


def stuck_redemptions():
    """Kódy, které se doručit nepodařilo — admin je musí vyřešit ručně."""
    return query(
        "SELECT * FROM redemption_queue WHERE sent_at IS NULL AND attempts >= ?"
        " ORDER BY id", (MAX_REDEMPTION_ATTEMPTS,),
    )


def mark_redemption_sent(rid):
    _execute("UPDATE redemption_queue SET sent_at=? WHERE id=?",
             (int(time.time()), rid))


def bump_redemption_attempts(rid):
    _execute("UPDATE redemption_queue SET attempts = attempts + 1 WHERE id=?",
             (rid,))

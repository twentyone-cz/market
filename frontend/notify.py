"""Upozornění obsluze. Jediný kanál je Nostr (šifrovaná zpráva na vlastní
klíč) — obchod nemá provozovatele s e-mailem a zákazníkovi se nic neposílá.

Odesílá se na pozadí a chyby se jen vrací; objednávka nesmí spadnout kvůli
tomu, že je relay nedostupný.
"""

import os
import threading

import nostr
import store

DEFAULT_RELAYS = "wss://relay.damus.io, wss://nos.lol"


def _setting(key, env, fallback=""):
    return store.get_setting(key) or os.environ.get(env, fallback)


def configured():
    return bool(_setting("nostr_to", "NOSTR_TO")
                and _setting("nostr_sk", "NOSTR_SK"))


def send(text, to=None, sk=None, relays=None):
    """Synchronní odeslání — vrací (ok, chyba). Používá test v administraci."""
    to = to or _setting("nostr_to", "NOSTR_TO")
    sk = sk or _setting("nostr_sk", "NOSTR_SK")
    relays = relays or _setting("nostr_relays", "NOSTR_RELAYS", DEFAULT_RELAYS)
    if not (to and sk):
        return False, "Nostr není nastavený."
    try:
        return nostr.send_dm(sk, to, relays.split(","), text)
    except Exception as e:
        return False, str(e)[:160]


def send_async(text):
    """Upozornění z běhu obchodu: nikdy nečeká a nikdy nevyhodí výjimku."""
    if not configured():
        return
    threading.Thread(target=send, args=(text,), daemon=True).start()


def order_paid(order, items):
    """Zaplacená objednávka. Do zprávy jde jen to, co obsluha potřebuje —
    žádné doručovací údaje."""
    lines = ["Zaplacená objednávka %s" % order["token"][:8]]
    for item in items:
        lines.append("- %dx %s" % (item["qty"], item["name"]))
    lines.append("%d sat" % order["total_sat"])
    if order["delivery"] == "code":
        lines.append("Doprava: %s" % (
            "podací kód dodán" if order["ship_code"] else "ČEKÁ SE NA PODACÍ KÓD"))
    elif order["delivery"] == "personal":
        lines.append("Doprava: osobní předání")
    send_async("\n".join(lines))


def ship_code_arrived(order):
    send_async("Objednávka %s: dorazil podací kód %s — dá se podat."
               % (order["token"][:8], order["ship_code"]))

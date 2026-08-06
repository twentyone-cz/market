"""Automatické vydávání kódů po zaplacení — žádné ruční poolování.

Dva druhy digitálního zboží:
  - kind=voucher: dárkový kredit obchodu (value_sat) — uplatní se v košíku
    jako sleva; jednorázovost hlídá store.reserve_voucher (PK + NULL check).
  - kind=days: dárkové dny privátní sítě — kód se po vydání registruje
    u CockScale (redemption_queue, lifecycle retry); uplatnění probíhá
    v CockScale dashboardu, obchod do toho dál nevstupuje.

Kódy jsou náhodné (secrets), formát JDNV-XXXX-XXXX-XXXX (bez O/0/I/1)."""

import json
import secrets
import urllib.error
import urllib.request

import store

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # bez záměnitelných znaků


def generate_code(prefix="JDNV"):
    body = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return "%s-%s-%s-%s" % (prefix, body[0:4], body[4:8], body[8:12])


def normalize_code(raw):
    """Uživatelský vstup: velká písmena, pomlčky/mezery volitelné."""
    s = "".join(c for c in raw.upper() if c.isalnum())
    if len(s) != 16:
        return ""
    return "%s-%s-%s-%s" % (s[0:4], s[4:8], s[8:12], s[12:16])


def issue_for_order(token):
    """Po zaplacení objednávky vygeneruje kódy pro digitální položky
    (po kusech) a dny zařadí do registrační fronty. Idempotentní —
    když už kódy existují, nic nepřidává."""
    existing = store.vouchers_for_order(token)
    have = {}
    for v in existing:
        have[v["kind"]] = have.get(v["kind"], 0) + 1
    for item in store.get_items(token):
        if item["kind"] not in ("voucher", "days"):
            continue
        # dopočítáváme podle druhu, ne „existuje aspoň jeden" — pád uprostřed
        # vydávání jinak nechal část kódů navždy nevydaných
        missing = item["qty"] - have.get(item["kind"], 0)
        have[item["kind"]] = max(0, have.get(item["kind"], 0) - item["qty"])
        for _ in range(max(0, missing)):
            code = generate_code()
            store.add_voucher(code, item["kind"], item["price_sat"],
                              item["days"], token)
            if item["kind"] == "days":
                store.enqueue_redemption(code, item["days"])


def voucher_discount(code, total_sat, order_token):
    """Pokusí se uplatnit store-kredit: vrací (sleva_sat, chyba)."""
    norm = normalize_code(code)
    if not norm:
        return 0, "Kód nemá správný formát."
    row = store.get_voucher(norm)
    if row and row["kind"] == "days":
        return 0, ("Tenhle kód je na dny privátní sítě — uplatní se na účtu"
                   " sítě, ne tady v obchodě.")
    if not row or row["kind"] != "voucher":
        return 0, "Neplatný kód."
    src = store.get_order(row["src_order"])
    if not src or src["status"] not in ("paid", "shipped", "done"):
        return 0, "Kód pochází z nezaplacené objednávky."
    left = row["value_left"] if row["value_left"] is not None else row["value_sat"]
    if left <= 0:
        return 0, "Kód už byl vyčerpaný."
    amount = min(left, total_sat)
    if not store.reserve_voucher(norm, order_token, amount):
        return 0, "Kód už byl uplatněn."
    return amount, ""


def push_redemptions():
    """Lifecycle: odešle čekající registrace dnů do CockScale.
    Kontrakt viz CockScale/docs/obchod-vouchery-handoff.md; dokud endpoint
    není nasazený nebo není nastaven secret, fronta trpělivě čeká."""
    api = store.get_setting("cockscale_api", "")
    secret = store.get_setting("cockscale_partner_secret", "")
    if not api or not secret:
        return
    for row in store.pending_redemptions():
        payload = json.dumps({"code": row["code"], "days": row["days"]}).encode()
        req = urllib.request.Request(
            api.rstrip("/") + "/partner/vouchers", data=payload, method="POST",
            headers={"Authorization": "Bearer " + secret,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    store.mark_redemption_sent(row["id"])
                    continue
        except (urllib.error.URLError, OSError, ValueError):
            pass
        store.bump_redemption_attempts(row["id"])

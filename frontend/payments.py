"""Platební vrstva — LNbits (jediný backend; žádný dev/fake režim).

Memo NIKDY nesmí obsahovat nic, co identifikuje zákazníka — jen generický
text typu „cockscale 30d".
"""

import json
import os
import urllib.error
import urllib.request


class PaymentError(Exception):
    pass


class Invoice:
    def __init__(self, bolt11, payment_hash):
        self.bolt11 = bolt11
        self.payment_hash = payment_hash


class PaymentBackend:
    def create_invoice(self, amount_sat: int, memo: str) -> Invoice:
        raise NotImplementedError

    def is_paid(self, payment_hash: str) -> bool:
        raise NotImplementedError


class LNbitsBackend(PaymentBackend):
    """LNbits v1.4.x: POST /api/v1/payments (X-Api-Key = invoice key, ne admin!),
    ověření pollingem GET /api/v1/payments/{hash}."""

    def __init__(self, url, invoice_key, timeout=15):
        self.url = url.rstrip("/")
        self.invoice_key = invoice_key
        self.timeout = timeout

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.url + path,
            data=data,
            method=method,
            headers={"X-Api-Key": self.invoice_key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise PaymentError("LNbits %s %s -> %d" % (method, path, e.code)) from e
        except OSError as e:
            raise PaymentError("LNbits %s %s -> %s" % (method, path, e)) from e

    def create_invoice(self, amount_sat, memo):
        resp = self._req(
            "POST",
            "/api/v1/payments",
            {"out": False, "amount": amount_sat, "memo": memo, "unit": "sat"},
        )
        bolt11 = resp.get("bolt11") or resp.get("payment_request")
        payment_hash = resp.get("payment_hash")
        if not bolt11 or not payment_hash:
            raise PaymentError("LNbits: odpověď bez bolt11/payment_hash")
        return Invoice(bolt11, payment_hash)

    def is_paid(self, payment_hash):
        try:
            resp = self._req("GET", "/api/v1/payments/%s" % payment_hash)
        except PaymentError:
            return False
        # v1.4 vrací {"paid": bool, ...} příp. {"details": {...}, "paid": ...}
        return bool(resp.get("paid"))


class PaymentManager:
    """Drží aktivní platební backend podle runtime nastavení (admin UI →
    settings v DB); env proměnné jsou jen výchozí hodnoty. Změna LNbits
    údajů se projeví bez restartu.

    Jiný backend než LNbits neexistuje — dokud není nakonfigurovaný, platby
    prostě nejdou (žádný „fake" režim, který by mohl omylem běžet v ostrém
    provozu a rozdávat předplatné zdarma)."""

    def __init__(self, store):
        self.store = store
        self._backend = None

    def _creds(self):
        url = self.store.get_setting("lnbits_url", os.environ.get("LNBITS_URL", ""))
        key = self.store.get_setting(
            "lnbits_invoice_key", os.environ.get("LNBITS_INVOICE_KEY", ""))
        return (url or "").rstrip("/"), key or ""

    def configured(self):
        url, key = self._creds()
        return bool(url and key)

    def current(self):
        url, key = self._creds()
        if not (url and key):
            raise PaymentError("LNbits není nakonfigurováno (URL + invoice key)")
        if self._backend is None or (self._backend.url, self._backend.invoice_key) != (url, key):
            self._backend = LNbitsBackend(url, key)
        return self._backend


def test_lnbits(url, invoice_key, timeout=10):
    """Ověří spojení a invoice key: GET /api/v1/wallet → {name, balance(msat)}.
    Při chybě vyhodí PaymentError."""
    req = urllib.request.Request(
        url.rstrip("/") + "/api/v1/wallet",
        headers={"X-Api-Key": invoice_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise PaymentError("LNbits odpověděl %d (špatný klíč nebo URL?)" % e.code) from e
    except OSError as e:
        raise PaymentError("LNbits nedostupný: %s" % e) from e
    if "name" not in data:
        raise PaymentError("neočekávaná odpověď LNbits: %r" % data)
    return data

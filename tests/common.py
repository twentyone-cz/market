"""Společné vybavení testů: čistá DB v tempu + import frontend modulů."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "frontend"))

import store  # noqa: E402


def fresh_db():
    """Nová prázdná databáze (store je modulový singleton)."""
    path = os.path.join(tempfile.mkdtemp(prefix="obchod-test-"), "test.db")
    store.connect(path)
    return path


class FakeInvoice:
    def __init__(self, bolt11, payment_hash):
        self.bolt11 = bolt11
        self.payment_hash = payment_hash


class FakeBackend:
    """Platební backend pro testy: invoice v RAM, platí se voláním pay()."""

    def __init__(self):
        self.n = 0
        self.paid = set()
        self.fail_create = False

    def create_invoice(self, amount_sat, memo):
        import payments
        if self.fail_create:
            raise payments.PaymentError("test: backend down")
        self.n += 1
        h = "hash%04d" % self.n
        return FakeInvoice("lnbc_test_%s_%d" % (h, amount_sat), h)

    def is_paid(self, payment_hash):
        return payment_hash in self.paid

    def pay(self, payment_hash):
        self.paid.add(payment_hash)


class FakeManager:
    def __init__(self):
        self.backend = FakeBackend()

    def current(self):
        return self.backend

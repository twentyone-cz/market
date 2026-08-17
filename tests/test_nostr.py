"""Nostr: podpis proti oficiálním vektorům BIP-340, NIP-04 tam a zpět,
bech32 klíče a websocket rámec. Kryptografii píšeme sami, takže se musí
dát ověřit.
"""

import base64
import json
import subprocess
import unittest

import nostr


def _verify(pubkey_hex, msg32, sig_hex):
    """Ověření podpisu podle BIP-340 (v produkci nepotřebujeme, tady ano)."""
    px = int(pubkey_hex, 16)
    r = int(sig_hex[:64], 16)
    s = int(sig_hex[64:], 16)
    if r >= nostr.P or s >= nostr.N:
        return False
    point = nostr._lift_x(px)
    e = int.from_bytes(
        nostr._tagged_hash(
            "BIP0340/challenge",
            r.to_bytes(32, "big") + px.to_bytes(32, "big") + msg32), "big") % nostr.N
    r_point = nostr._add(nostr._mul(nostr.G, s),
                         nostr._mul(point, nostr.N - e))
    if r_point is None or r_point[1] % 2 != 0:
        return False
    return r_point[0] == r


class Bip340(unittest.TestCase):
    # vektory z BIP-340 (index 0, 1, 2 — secret key cases)
    VECTORS = [
        ("0000000000000000000000000000000000000000000000000000000000000003",
         "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9",
         "0000000000000000000000000000000000000000000000000000000000000000"),
        ("B7E151628AED2A6ABF7158809CF4F3C762E7160F38B4DA56A784D9045190CFEF",
         "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
         "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89"),
        ("C90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B14E5C9",
         "DD308AFEC5777E13121FA72B9CC1B7CC0139715309B086C960E18FD969774EB8",
         "7E2D58D8B3BCDF1ABADEC7829054F90DDA9805AAB56C77333024B9D0A508B75C"),
    ]

    def test_pubkey_matches_vectors(self):
        for sk, pk, _msg in self.VECTORS:
            self.assertEqual(nostr.pubkey_hex(sk).upper(), pk)

    def test_signature_verifies(self):
        for sk, pk, msg in self.VECTORS:
            sig = nostr.schnorr_sign(bytes.fromhex(msg), sk)
            self.assertTrue(_verify(pk, bytes.fromhex(msg), sig),
                            "podpis neprošel pro %s" % pk)

    def test_signature_rejects_tampering(self):
        sk, pk, msg = self.VECTORS[1]
        sig = nostr.schnorr_sign(bytes.fromhex(msg), sk)
        other = bytes.fromhex(self.VECTORS[2][2])
        self.assertFalse(_verify(pk, other, sig))


class Keys(unittest.TestCase):
    def test_bech32_and_hex(self):
        hex_key = "b7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef"
        self.assertEqual(nostr.to_hex_key(hex_key), hex_key)
        self.assertEqual(nostr.to_hex_key(hex_key.upper()), hex_key)
        # nsec téhož klíče (bech32)
        nsec = ("nsec1k7g23v230d9x40m3tzqfeafnca3ww9s7w95mftf4uym3q4rya7ls6zwtyt")
        self.assertEqual(len(nostr.to_hex_key(nsec)), 64)

    def test_rejects_nonsense(self):
        for bad in ("", "abc", "npub1"):
            with self.assertRaises(Exception):
                nostr.to_hex_key(bad)


class Nip04(unittest.TestCase):
    SK = "b7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef"
    RECIPIENT_SK = "c90fdaa22168c234c4c6628b80dc1cd129024e088a67cc74020bbea63b14e5c9"

    def test_roundtrip(self):
        to_pub = nostr.pubkey_hex(self.RECIPIENT_SK)
        payload = nostr.nip04_encrypt("nová objednávka: 1 000 sat", self.SK, to_pub)
        blob, iv_b64 = payload.split("?iv=")
        # příjemce odvodí stejný sdílený klíč z opačné strany
        shared = nostr._shared_x(self.RECIPIENT_SK, nostr.pubkey_hex(self.SK))
        proc = subprocess.run(
            ["openssl", "enc", "-d", "-aes-256-cbc", "-K", shared.hex(),
             "-iv", base64.b64decode(iv_b64).hex()],
            input=base64.b64decode(blob), capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.decode(), "nová objednávka: 1 000 sat")

    def test_event_is_signed_and_addressed(self):
        to_pub = nostr.pubkey_hex(self.RECIPIENT_SK)
        event = nostr.build_dm(self.SK, to_pub, "test")
        self.assertEqual(event["kind"], 4)
        self.assertEqual(event["tags"], [["p", to_pub]])
        self.assertTrue(_verify(event["pubkey"], bytes.fromhex(event["id"]),
                                event["sig"]))


class WebSocketFrame(unittest.TestCase):
    def test_frame_is_masked_and_decodable(self):
        import socket as sock_mod
        a, b = sock_mod.socketpair()
        try:
            nostr._ws_send_text(a, '["EVENT",{}]')
            data = b.recv(4096)
        finally:
            a.close()
            b.close()
        self.assertEqual(data[0], 0x81)          # FIN + text
        length = data[1] & 0x7F
        self.assertTrue(data[1] & 0x80, "klient musí maskovat")
        mask = data[2:6]
        payload = bytes(c ^ mask[i % 4] for i, c in enumerate(data[6:6 + length]))
        self.assertEqual(json.loads(payload.decode())[0], "EVENT")


if __name__ == "__main__":
    unittest.main()

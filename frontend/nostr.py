"""Nostr: šifrovaná zpráva o objednávce (NIP-04) přímo z obchodu.

Čistá stdlib jako zbytek projektu, takže podpis (BIP-340 Schnorr), ECDH
a WebSocket klient jsou tady; symetrickou šifru dělá systémový openssl.
Odesílání je vždy „best effort" — obchod kvůli oznámení nikdy nespadne
a nic o objednávce se nikam neloguje.
"""

import base64
import hashlib
import json
import os
import secrets
import socket
import ssl
import struct
import subprocess
import time
import urllib.parse

# --- secp256k1 ---------------------------------------------------------------

P = 2 ** 256 - 2 ** 32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _inv(a, m=P):
    return pow(a, m - 2, m)


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    (x1, y1), (x2, y2) = p1, p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if p1 == p2:
        lam = 3 * x1 * x1 * _inv(2 * y1) % P
    else:
        lam = (y2 - y1) * _inv(x2 - x1) % P
    x3 = (lam * lam - x1 - x2) % P
    return (x3, (lam * (x1 - x3) - y1) % P)


def _mul(point, scalar):
    result, addend = None, point
    while scalar:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    return result


def _lift_x(x):
    """Bod na křivce s daným x a sudým y (x-only klíče podle BIP-340)."""
    if x >= P:
        raise ValueError("x mimo pole")
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if pow(y, 2, P) != y_sq:
        raise ValueError("bod není na křivce")
    return (x, y if y % 2 == 0 else P - y)


def _tagged_hash(tag, msg):
    h = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(h + h + msg).digest()


def pubkey_hex(privkey_hex):
    d = int(privkey_hex, 16)
    if not 1 <= d < N:
        raise ValueError("neplatný privátní klíč")
    return "%064x" % _mul(G, d)[0]


def schnorr_sign(msg32, privkey_hex):
    """BIP-340. Ověřeno oficiálními testovacími vektory (tests/test_nostr.py)."""
    d0 = int(privkey_hex, 16)
    if not 1 <= d0 < N:
        raise ValueError("neplatný privátní klíč")
    point = _mul(G, d0)
    d = d0 if point[1] % 2 == 0 else N - d0
    px = point[0]
    aux = secrets.token_bytes(32)
    t = d ^ int.from_bytes(_tagged_hash("BIP0340/aux", aux), "big")
    rand = _tagged_hash("BIP0340/nonce",
                        t.to_bytes(32, "big") + px.to_bytes(32, "big") + msg32)
    k0 = int.from_bytes(rand, "big") % N
    if k0 == 0:
        raise ValueError("nonce = 0")
    r_point = _mul(G, k0)
    k = k0 if r_point[1] % 2 == 0 else N - k0
    rx = r_point[0]
    e = int.from_bytes(
        _tagged_hash("BIP0340/challenge",
                     rx.to_bytes(32, "big") + px.to_bytes(32, "big") + msg32),
        "big") % N
    return (rx.to_bytes(32, "big") + ((k + e * d) % N).to_bytes(32, "big")).hex()


# --- bech32 (npub/nsec) ------------------------------------------------------

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_decode(text):
    text = text.strip().lower()
    if "1" not in text:
        raise ValueError("není bech32")
    hrp, data_part = text.rsplit("1", 1)
    data = [_CHARSET.index(c) for c in data_part]
    bits, value, out = 0, 0, []
    for d in data[:-6]:                      # posledních 6 znaků je kontrolní součet
        value = (value << 5) | d
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((value >> bits) & 0xFF)
    return hrp, bytes(out)


def to_hex_key(text):
    """Přijme hex i bech32 (npub…/nsec…) a vrátí 64 znaků hex."""
    text = (text or "").strip()
    if len(text) == 64 and all(c in "0123456789abcdefABCDEF" for c in text):
        return text.lower()
    hrp, raw = _bech32_decode(text)
    if hrp not in ("npub", "nsec") or len(raw) != 32:
        raise ValueError("čekám npub/nsec nebo hex")
    return raw.hex()


def _bech32_polymod(values):
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if b >> i & 1 else 0
    return chk


def _bech32_encode(hrp, raw):
    """Protějšek _bech32_decode, s kontrolním součtem (BIP-173)."""
    data, acc, bits = [], 0, 0
    for byte in raw:
        acc = acc << 8 | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            data.append(acc >> bits & 31)
    if bits:
        data.append(acc << (5 - bits) & 31)
    hrpx = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    chk = _bech32_polymod(hrpx + data + [0] * 6) ^ 1
    data += [chk >> 5 * (5 - i) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in data)


def npub_of(key_text):
    """Veřejná identita (npub…) k privátnímu klíči (nsec… nebo hex)."""
    return _bech32_encode("npub",
                          bytes.fromhex(pubkey_hex(to_hex_key(key_text))))


def generate_keypair():
    """Nový privátní klíč, vrací (nsec, npub). Klíč se nikam neloguje."""
    while True:  # mimo rozsah křivky padne ~2^-128, ale kontrola je zadarmo
        raw = secrets.token_bytes(32)
        if 1 <= int.from_bytes(raw, "big") < N:
            return _bech32_encode("nsec", raw), npub_of(raw.hex())


# --- NIP-04 ------------------------------------------------------------------

def _shared_x(privkey_hex, pubkey_hex_x):
    point = _mul(_lift_x(int(pubkey_hex_x, 16)), int(privkey_hex, 16))
    return point[0].to_bytes(32, "big")


def nip04_encrypt(plaintext, privkey_hex, recipient_hex):
    """AES-256-CBC systémovým openssl — vlastní implementaci šifry si
    do peněžní aplikace psát nebudeme."""
    key = _shared_x(privkey_hex, recipient_hex)
    iv = secrets.token_bytes(16)
    proc = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc",
         "-K", key.hex(), "-iv", iv.hex()],
        input=plaintext.encode(), capture_output=True, timeout=10)
    if proc.returncode != 0:
        raise RuntimeError("openssl: %s" % proc.stderr.decode()[:120])
    return "%s?iv=%s" % (base64.b64encode(proc.stdout).decode(),
                         base64.b64encode(iv).decode())


# --- událost -----------------------------------------------------------------

def build_dm(privkey_hex, recipient_hex, text):
    pub = pubkey_hex(privkey_hex)
    event = {
        "pubkey": pub,
        "created_at": int(time.time()),
        "kind": 4,
        "tags": [["p", recipient_hex]],
        "content": nip04_encrypt(text, privkey_hex, recipient_hex),
    }
    serialized = json.dumps(
        [0, event["pubkey"], event["created_at"], event["kind"],
         event["tags"], event["content"]],
        separators=(",", ":"), ensure_ascii=False).encode()
    event["id"] = hashlib.sha256(serialized).hexdigest()
    event["sig"] = schnorr_sign(bytes.fromhex(event["id"]), privkey_hex)
    return event


# --- minimální WebSocket klient ---------------------------------------------

def _ws_connect(url, timeout=10):
    parts = urllib.parse.urlparse(url)
    secure = parts.scheme == "wss"
    host = parts.hostname
    port = parts.port or (443 if secure else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    sock = socket.create_connection((host, port), timeout=timeout)
    if secure:
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    sock.sendall((
        "GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, key)).encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(1024)
        if not chunk:
            raise OSError("relay zavřel spojení")
        head += chunk
    if b"101" not in head.split(b"\r\n", 1)[0]:
        raise OSError("relay neumí websocket: %s" % head.split(b"\r\n", 1)[0])
    return sock


def _ws_send_text(sock, text):
    payload = text.encode()
    header = bytearray([0x81])              # FIN + text frame
    mask = secrets.token_bytes(4)
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _ws_read_text(sock, timeout):
    """Jeden textový rámec od serveru (server nemaskuje)."""
    sock.settimeout(timeout)

    def read(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise OSError("relay zavřel spojení")
            buf += chunk
        return buf

    while True:
        head = read(2)
        opcode = head[0] & 0x0F
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", read(8))[0]
        mask = read(4) if head[1] & 0x80 else b""
        payload = read(length)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x1:
            return payload.decode("utf-8", "replace")
        if opcode == 0x8:
            raise OSError("relay ukončil spojení")
        # ping/pong a binární rámce přeskakujeme


def publish(relay_url, event, timeout=10):
    """Pošle událost na relay a POČKÁ na potvrzení. Relay může podpis
    odmítnout — „odesláno" samo o sobě nic neznamená.
    Vrací (True, "") nebo (False, důvod)."""
    sock = None
    try:
        sock = _ws_connect(relay_url, timeout)
        _ws_send_text(sock, json.dumps(["EVENT", event], separators=(",", ":")))
        deadline = time.time() + timeout
        while time.time() < deadline:
            reply = json.loads(_ws_read_text(sock, timeout))
            if not (isinstance(reply, list) and len(reply) >= 3):
                continue
            if reply[0] == "OK" and reply[1] == event["id"]:
                if reply[2] is True:
                    return True, ""
                return False, "relay odmítl: %s" % (
                    reply[3] if len(reply) > 3 else "bez důvodu")
            if reply[0] == "NOTICE":
                return False, "relay: %s" % str(reply[1])[:80]
        return False, "relay nepotvrdil přijetí"
    except Exception as e:                  # relay je cizí server, spolehnout se nedá
        return False, str(e)[:120]
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def send_dm(privkey, recipient, relays, text):
    """Publikuje zprávu na VŠECHNY relaye; úspěch = aspoň jedna přijala.

    Dřív se končilo prvním úspěchem — DM pak ležela jen na jednom relayi,
    a když to nebyl žádný z těch, kde příjemce čte (NIP-65), nikdy ji
    neviděl (přesně to se stalo 2026-08-18). U pošty nestačí „někde
    uložena": musí být všude, kde se příjemce může dívat."""
    priv = to_hex_key(privkey)
    to = to_hex_key(recipient)
    event = build_dm(priv, to, text)
    accepted, errors = 0, []
    for relay in relays:
        ok, err = publish(relay.strip(), event)
        if ok:
            accepted += 1
        else:
            errors.append("%s: %s" % (relay.strip(), err))
    if accepted:
        return True, "; ".join(errors)  # dílčí výpadky jen informativně
    return False, "; ".join(errors)

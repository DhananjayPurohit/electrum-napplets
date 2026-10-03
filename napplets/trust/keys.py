#!/usr/bin/env python3
"""Demo identities for the trust-vendor napplets: secp256k1 + npub (bech32).

Why this exists: the napplets need fixed, reproducible identities so that the
same actor is the same npub on a phone and inside Electrum. The ring bundle
(`trust-ring.bundle.js`) exports `prove`/`verifyProof` but not key generation,
and `secp256k1` is not exposed to the page, so the keys are derived here at
build time and baked into each napplet as constants.

The derivation is the team's own convention (see
`upstream/facilitator-roster.json` -> keyDerivation):

    secretKey = sha256(PREFIX + idx)
    publicKey = secp256k1.getPublicKey(secretKey, true)   # 33-byte compressed

`--verify` recomputes that roster's eight pubkeys from its documented prefix and
compares them byte for byte: if this file's secp256k1 is wrong, the check fails.
That is the only reason to trust the arithmetic below.

It also implements NPUB (bech32, BIP-173 charset) so an actor's identity is the
npub form of the same key the ring signature is made with.

    python3 napplets/trust/keys.py --verify
    python3 napplets/trust/keys.py --roster            # JSON for the build
"""

from __future__ import annotations

import argparse
import hashlib
import json

# ── secp256k1, minimally ─────────────────────────────────────────────────────
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)


def inv(a: int, m: int = P) -> int:
    return pow(a, m - 2, m)


def add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0] and (p[1] + q[1]) % P == 0:
        return None
    if p == q:
        lam = 3 * p[0] * p[0] * inv(2 * p[1]) % P
    else:
        lam = (q[1] - p[1]) * inv(q[0] - p[0]) % P
    x = (lam * lam - p[0] - q[0]) % P
    return (x, (lam * (p[0] - x) - p[1]) % P)


def mul(k: int, point=G):
    out = None
    while k:
        if k & 1:
            out = add(out, point)
        point = add(point, point)
        k >>= 1
    return out


def pubkey(secret: int, compressed: bool = True) -> bytes:
    x, y = mul(secret % N)
    return bytes([2 + (y & 1)]) + x.to_bytes(32, "big") if compressed else x.to_bytes(32, "big")


def even_y_secret(secret: int) -> int:
    """A secret whose public key has even y, so x-only == compressed[1:].

    Nostr's npub is the x-only key. Flipping to the negated secret gives the
    same x with even y, which is what NIP-01 means by that public key.
    """
    x, y = mul(secret % N)
    return secret if y % 2 == 0 else (N - secret) % N


# ── bech32 (BIP-173) / npub ──────────────────────────────────────────────────
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def bech32_polymod(values):
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            if (top >> i) & 1:
                chk ^= gen[i]
    return chk


def hrp_expand(hrp: str):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def convertbits(data, frombits: int, tobits: int, pad: bool = True):
    acc = bits = 0
    out = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if pad and bits:
        out.append((acc << (tobits - bits)) & maxv)
    return out


def bech32_encode(hrp: str, data: list[int]) -> str:
    values = hrp_expand(hrp) + data
    polymod = bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[d] for d in data + checksum)


def bech32_decode(text: str):
    pos = text.rfind("1")
    if pos < 1 or pos + 7 > len(text):
        raise ValueError("not bech32")
    hrp, body = text[:pos], text[pos + 1:]
    data = [CHARSET.find(c) for c in body]
    if -1 in data:
        raise ValueError("bad bech32 character")
    if bech32_polymod(hrp_expand(hrp) + data) != 1:
        raise ValueError("bad bech32 checksum")
    return hrp, data[:-6]


def to_npub(x_only: bytes) -> str:
    if len(x_only) != 32:
        raise ValueError("npub carries a 32-byte x-only key")
    return bech32_encode("npub", convertbits(list(x_only), 8, 5))


def from_npub(npub: str) -> bytes:
    hrp, data = bech32_decode(npub)
    if hrp != "npub":
        raise ValueError(f"expected an npub, got {hrp!r}")
    return bytes(convertbits(data, 5, 8, False))


# ── identities ───────────────────────────────────────────────────────────────
# Our demo roster. Prefix is ours; idx is positional and stable.
PREFIX = "ptc++-finals/trust-vendor/"

# Vendors in the customer's pinned set (MIN_RING_SIZE in the ring bundle is 4,
# so the set has to hold at least four keys for a proof to verify).
VENDORS = [
    ("alice", 1, "on stage · in the set"),
    ("bob", 2, "on stage · in the set"),
    ("carol", 3, "not on stage · in the set"),
    ("dave", 4, "not on stage · in the set"),
]
OUTSIDERS = [("malice", 5, "on stage · NOT in the set")]
CUSTOMERS = [("charlie", 6, "the customer")]


def identity(prefix: str, idx: int) -> dict:
    digest = hashlib.sha256(f"{prefix}{idx}".encode()).digest()
    secret = int.from_bytes(digest, "big") % N
    if secret == 0:
        raise ValueError("degenerate secret")
    secret = even_y_secret(secret)
    compressed = pubkey(secret)
    x_only = compressed[1:]
    return {
        "idx": idx,
        "secretKey": secret.to_bytes(32, "big").hex(),
        "publicKey": compressed.hex(),
        "xOnly": x_only.hex(),
        "npub": to_npub(x_only),
    }


def roster() -> dict:
    def entry(row):
        name, idx, note = row
        return {"name": name, "note": note, **identity(PREFIX, idx)}

    vendors = [entry(r) for r in VENDORS]
    return {
        "setId": "ptc-fininals-burger-vendors",
        "description": "Burgermeister Berlin vendors — finals demo set",
        "publishedAt": "2026-10-03T09:00:00Z",
        "prefix": PREFIX,
        "vendors": vendors,
        "outsiders": [entry(r) for r in OUTSIDERS],
        "customers": [entry(r) for r in CUSTOMERS],
        # The pin is over the four vendor keys only: that is the set Charlie chose.
        "membersFrom": "vendors",
    }


def verify() -> int:
    """Recompute the upstream roster's keys from its documented derivation."""
    upstream_prefix = "mcp-cashu-pizza-demo/"
    known = {
        0: "02af621be286642f62661ac6505c0c36b143ce22e78f100b90f8456de49036d966",
        1: "02b2b881fd9e01bf37f9be7935abc1e8edf422d5b9939952e06e5aeafea2c674f7",
        2: "038cc0a77c922931ca1149276615b35740bcb8980c9ad5c1d7089c15db7c4d7ef4",
        3: "03109eca059ca1c33b5d5537a5f8c298dc563d52ace9b8713ecefb71e63bd6e300",
        4: "037cf88ce7940811adf8da82f8ab4bd371bb47fe460ef845c92215d30b6d9bb4f6",
        5: "0288951d0792fd8b5266b8e43d4317cce80d10760414400ded424bcfc064007885",
        6: "02fe682fd9e90a0badc6773ccecc41c6e4f664b2556ed3dac6155d40281f279d81",
        7: "034382b46ed32e3351beee6fbe8fe78836e733a13db058143460e0f60024f1b5b2",
    }
    ok = True
    for idx, want in known.items():
        # upstream does not force even y, so compare the raw compressed key
        secret = int.from_bytes(hashlib.sha256(f"{upstream_prefix}{idx}".encode()).digest(), "big") % N
        got = pubkey(secret).hex()
        if got != want:
            ok = False
            print(f"FAIL idx {idx}\n  want {want}\n  got  {got}")
        else:
            print(f"ok   idx {idx}  {want[:16]}…  (secret {secret.to_bytes(32, 'big').hex()[:16]}…)")

    # npub: decode a known-good pair (the handover post and the nsite host)
    for npub in (
        "npub1ftjlarsn0k4g5wmxnjcae48u2nl20vfu2lf3rjdqrht89h9z0fhsah7hqu",
        "npub1g8s0xjxk3ewl39t45sshjznlhfznpaxmrt2facjsh90uh95qv9wqa6ajwk",
    ):
        try:
            raw = from_npub(npub)
            round_trip = to_npub(raw)
            good = round_trip == npub and len(raw) == 32
            print(("ok   " if good else "FAIL ") + f"npub {npub[:20]}… -> {raw.hex()[:16]}…")
            ok = ok and good
        except ValueError as exc:
            ok = False
            print(f"FAIL npub {npub[:20]}…: {exc}")

    print("VERIFIED" if ok else "FAILED")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--verify", action="store_true", help="check this file's secp256k1 + bech32")
    parser.add_argument("--roster", action="store_true", help="print the demo roster as JSON")
    args = parser.parse_args()
    if args.verify:
        return verify()
    print(json.dumps(roster(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

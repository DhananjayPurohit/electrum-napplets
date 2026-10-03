# Demo runbook — ring-vetted vendors, ordered and paid from Electrum

Four people on stage, three minutes. A customer pins a set of vendors; the order
opens to the whole set; each vendor proves *"I am a member of this set"* with a
real ring signature over that exact order, without revealing **which** member;
the cheapest verified quote wins; the customer pays in Electrum; the vendor
places the venue order.

Nothing in this flow is a mock except where it says so. The ring signature is
real (`napplets/upstream/trust-ring.bundle.js`, pinned by sha256), the identities
are real npubs (derived by `napplets/trust/keys.py`), the payment goes through
Electrum's own confirmation dialog, and the venue order goes to the team's live
bridge.

## The cast (these are stable — the same npub on every device)

| actor | role | npub |
|---|---|---|
| **charlie** | customer — pins the set, pays | `npub1ywevn5eprtnftln56quc6mj9usu7524lfcn66nvpfdpeg5qnsvasxtytyp` |
| **alice** | vendor — **in** the set | `npub1af83djx4szgukge52cfx2f2vcxsl2kqedptzcx7nvnyjc7055lnqghttvm` |
| **bob** | vendor — **in** the set | `npub1wwdv77wwn5jtyqthpjycpjl6akl5c8eh003tjv4pg2upcpcq4trsv3gkez` |
| **malice** | vendor — **not** in the set | `npub1vqwx4pkf9q9wrsudhupkykn03s2xz692laqpevrn2uwsdzlv0uzs4cve0a` |

The pinned set also contains two vendors who never appear on stage
(`carol`, `dave`) — the ring bundle's `MIN_RING_SIZE` is 4, so a set of four keys
is the smallest set whose proof verifies, and an anonymity set of four is a
better story than one of two. Their npubs are in the set list on the customer's
screen.

Every key is derived as `sha256("ptc++-finals/trust-vendor/" + idx)`, so
**there is no private key to leak** — they are worthless by construction, and
`python3 napplets/trust/keys.py --verify` proves the arithmetic against the
pizza roster's eight known public keys.

## Setup, once

```bash
./setup.sh                       # Electrum 4.8.2 from source + Qt WebEngine
python3 napplets/build-trust.py  # builds the four napplets, merges the catalog
```

`build-trust.py --no-hub` builds for the in-host-only topology (no phone).
`--hub ws://<lan-ip>:8787` builds for the phone, with that origin allowed by CSP.

## Topology A — everyone in one Electrum (the safe default)

All four napplets sit side by side in the **Napplets** tab and talk over NAP-INC.
No network, no hub, nothing to go wrong beyond Electrum itself.

```bash
./tests/run.sh trust     # the whole flow, headless, ~40 s
./run.sh                 # then: Napplets tab, four panes
```

Order of operations on stage:

1. **charlie** — "Open this order to my pinned set" (the order goes to the set, not a person)
2. **alice** and **bob** — "Prove I'm in the set & quote" (2 of 4 members answer)
3. **malice** — "Prove I'm in the set & quote" → **REFUSED**, twice over: she cannot
   build a proof for a set she is not in, and she also shouts a *cheaper* price with
   no proof attached, which charlie's verifier refuses on screen. That is the abuse
   beat, and it happens in front of the audience without a slide.
4. **charlie** — "Accept the best quote" → **Pay with Electrum** → the wallet's own
   dialog (site, amount, description, **No** is the default button) → preimage.
5. **alice** — "Place the venue order" → the bridge returns an order id + a bolt11
   invoice → "Poll status" twice → the venue order number.

## Topology B — charlie on the phone, the vendors in Electrum

```bash
python3 napplets/trust/hub.py --stub          # rehearsals: fixture venue, offline
python3 napplets/trust/hub.py                 # showtime: proxies the live bridge
```

The hub serves the same four napplet files standalone (`http://<lan-ip>:8787/charlie`,
`/alice`, `/bob`, `/malice`) and relays the same message envelopes addressed by npub.
Actors in Electrum keep using NAP-INC *and* mirror to the hub, so a phone and the
wallet interoperate on the same identities. Long-poll HTTP, standard library only —
no websocket dependency to install on someone else's laptop.

On the phone charlie pays through **WebLN** if the browser has a wallet; inside
Electrum he pays through the napplet `wallet` domain. Same call, same invoice.

```bash
node tests/phone/trust-phone-flow.cjs   # both transports, four contexts, real proofs
```

## The venue rail

```bash
python3 tests/smoke_bridge.py                 # demo mode, zero real venue calls
python3 tests/smoke_bridge.py --stub          # offline, against the hub's fixture
```

The bridge lifecycle is `awaiting_payment -> pending -> paid` in demo mode; live
mode creates a real kitchen ticket and ends at a venue order number.

## Ordering a REAL burger (human-gated, do not rehearse this casually)

Live mode creates a real kitchen ticket, and the card step stays in a human hand.
The harness refuses to run without both flags, tells you when to complete the
checkout, and restores demo mode afterwards:

```bash
python3 tests/smoke_bridge.py --live --venue 8613S3X --sku sim-0001 \
    --ready --confirm 8613S3X
```

It flips the bridge to live, creates the order, **stops and waits for you to
complete the Mollie checkout in a browser**, then polls until the venue order
number appears. Have a named human with the card next to the machine before you
run it. (`POST /mode {"mode":"demo"}` puts the bridge back.)

## Before you walk on stage

```bash
python3 tests/smoke_bridge.py     # the bridge answers, demo mode, order lifecycle
./tests/run.sh trust              # the whole in-host flow, 37 checks
node tests/phone/trust-phone-flow.cjs
pkill -f "[t]rust/hub.py"         # a leftover hub holds port 8787 and serves stale files
```

## Known gaps, stated plainly

- **The invoice is a stand-in in demo mode.** It is a well-formed bolt11 string
  and the UI labels it `STAND-IN`; it is not payable. The real invoice is the
  venue bridge's own (`bolt11` from `POST /provider/orders`) — in live mode that
  is a real invoice and Electrum can pay it.
- **The vendor's margin is not settled anywhere.** The quote is venue price + 8%
  and the ledger shows it; no rail moves it. The customer pays the venue's
  invoice, so nobody is extending credit.
- **The bridge does not verify the ring** and does not claim to. The gate lives in
  the customer's verifier (ring ⊆ pinned set, order-bound, fresh key image). When
  the bridge's `funded -> submitted` seam grows an attestation check, that is
  where it belongs.
- **Napplets load as `local · unsigned`.** There is no signed NIP-5A manifest and
  no Blossom hash check yet; the shell labels them accordingly, and the payment
  dialog names the napplet and its sha256.
- **Their pizza suite has 3 pre-existing failures** in the testnut mint-settlement
  leg (`mint quote paid`, `buyer minted the ecash`, `facilitator redeemed the
  token`). They fail identically on a clean checkout without any of this work.
- **`carol` and `dave` have no napplet.** They exist only as keys in the pinned
  set. Give them files with `build-trust.py` if the story needs a fifth actor.

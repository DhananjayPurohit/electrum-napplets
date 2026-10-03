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

The pinned set also contains two vendors who never appear at all (`carol`,
`dave`) — the ring bundle's `MIN_RING_SIZE` is 4, so a set of four keys is the
smallest set whose proof verifies, and an anonymity set of four is a better
story than one of two. Their npubs are in the set list on the customer's screen.

**Electrum shows exactly TWO panes: `Customer` (charlie) and `Facilitator`
(alice).** The other actors are still built and served standalone by the hub —
the phone flow and the tests drive all four — but they are not in the catalog,
so they never clutter the wallet. The outsider case is a test, not a pane.

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

1. **Customer pane** — "Open this order to my pinned set". The order goes to the
   set, not to a person; the customer never picks a counterparty.
2. **Facilitator pane** — ONE click: "Prove I'm in the set & quote". The quote
   message carries the ring proof; there is no separate proving step.
3. **Customer pane** — "Accept the best quote" → **Pay with Electrum** → the
   wallet's own dialog (site, amount, description, **No** is the default button)
   → preimage.
4. **Both panes** — the UI places the venue order itself through the venue API
   and publishes "Burger purchased successfully ✓ venue order …", so the
   facilitator screen ends on an announcement, not a console.

A vendor whose key is not in the pinned set resolves nothing: it does not build a
proof, does not quote and does not send a refusal. That is covered by
`node tests/phone/trust-phone-flow.cjs` (four actors) rather than by a pane.

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
./tests/run.sh trust              # the whole in-host flow
node tests/phone/trust-phone-flow.cjs
pkill -f "[t]rust/hub.py"         # a leftover hub holds port 8787 and serves stale files
```

## If the customer's pane says "Refused"

A refusal is the verifier working — it is the whole point of the demo — but the
pane now shows the verifier's own reason and the one action that clears it. The
reason strings are the pinned bundle's, verbatim:

| What the pane says | What it means | What to do |
|---|---|---|
| `key image already used for this order` | That member already answered **this** order. The key image belongs to the member, not the order, so one proof per member per order is all the verifier accepts. | Nothing to fix: the customer presses **Open this order to my pinned set** again. A fresh order id is a fresh scope, and the flow runs again in the same session. |
| `order expired at …` | The order was opened more than the validity window ago. | Open a fresh order. The window is 240 min (`CONFIG.orderWindowMin`); it exists so a rehearsal-to-stage gap cannot expire the order mid-demo. |
| `proof's pinned set does not match the verifier's pin` | The two panes are pinned to **different set versions** — almost always one pane left over from an older build. | Rebuild both panes and reload: `python3 napplets/build-trust.py --only-trust`. The pane also prints `their set … ≠ yours …` so this is visible at a glance. |
| `ring contains a key outside the pinned trust set` | The prover is not a member of the customer's set (this is Malice, refused). | Expected. Malice is the villain; the pane showing this is the demo working. |
| `LSAG signature verification failed` | The proof does not verify against **this** order — usually a proof from an earlier order. | Open a fresh order and quote again. |
| `ring size N is below minimum 4` / `duplicate keys` | Malformed or too-small ring. | Rebuild the panes; the roster must hold the four keys. |

Two rules that prevent almost every refusal:

1. **The facilitator's button is one answer per order.** Pressing it twice is safe
   now — the pane says "already answered this order" and emits nothing — but the
   customer must open a fresh order to run the flow again.
2. **Start the demo from a fresh order**, not from a pane that was left open
   during the previous rehearsal. One click on the customer's button is the reset.

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

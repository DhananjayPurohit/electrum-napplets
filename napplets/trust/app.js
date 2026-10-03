// Trust-vendor napplets for the Electrum napplet shell (NIP-5D).
//
// One file, two roles, chosen by the baked config below. The customer pins a set
// of vendor npubs; the order opens to the whole set; each vendor proves "I am a
// member of this set" with a real ring signature over that exact order, without
// revealing which member; the cheapest valid quote wins. A vendor outside the set
// is refused — twice: the prover cannot build a proof for a set it is not in, and
// the customer's verifier rejects any ring that is not a subset of the pinned set.
//
// Everything here is real: key derivation happens in keys.py and is checked
// against the upstream roster, the ring signature is `__trustRing.prove` /
// `verifyProof` from the pinned bundle, and the payment goes through the host's
// own wallet dialog (`window.napplet.wallet.pay`). The only stand-ins are labelled
// STAND-IN in the UI and in the state object.
//
// Transports: NAP-INC topics inside the shell (all actors in one Electrum), and a
// small npub-addressed WebSocket hub for actors on other devices (a phone). When
// both are present messages are mirrored and de-duplicated by id, so a phone and
// Electrum interoperate on the same npubs.
(() => {
  "use strict";

  const CONFIG = __TRUST_CONFIG__;              // baked by napplets/build-trust.py
  const T = window.__trustRing;
  if (!T) throw new Error("trust-ring bundle missing");
  if (!CONFIG || !CONFIG.actor) throw new Error("baked config missing");

  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const hexToBytes = (h) => Uint8Array.from(h.match(/../g) || [], (b) => parseInt(b, 16));
  const short = (s, n = 8) => (s ? String(s).slice(0, n) + "…" : "—");
  const sats = (n) => Number(n).toLocaleString("en-US");
  // A refusal is the verifier working correctly, but on stage the pane must also
  // say what to do next. Every reason verifyProof can return has exactly one
  // remedy; the reason string is the bundle's own, matched here verbatim.
  function refusalAdvice(reason) {
    const r = String(reason || "");
    if (r.includes("order expired")) return "press “Open this order to my pinned set” again — it mints a fresh order";
    if (r.includes("key image already used")) return "this order was already resolved — open a fresh order and quote again";
    if (r.includes("proof's pinned set does not match")) return "that pane is pinned to a different set version — rebuild both panes: python3 napplets/build-trust.py --only-trust";
    if (r.includes("outside the pinned trust set")) return "that vendor is not a member of your set";
    if (r.includes("LSAG signature verification failed")) return "the proof does not verify against this exact order";
    if (r.includes("duplicate keys")) return "the ring is malformed (duplicate keys)";
    if (r.includes("below minimum")) return "the ring is smaller than the minimum you accept";
    return "check that both panes were built from the same roster";
  }
  // A host that never answers must fail loudly, not hang: on stage a stuck
  // promise looks identical to a slow one, and the demo has 3 minutes.
  const withTimeout = (promise, ms, message) => Promise.race([
    promise, new Promise((_, reject) => setTimeout(() => reject(new Error(message)), ms)),
  ]);
  // Ring proofs carry Uint8Arrays (ring keys, c0, responses, key image). JSON
  // would turn those into {"0":2,...}, so they are tagged on the way out and
  // revived on the way in — the same convention as napplets/inc-channel.js.
  const encode = (value) => JSON.parse(JSON.stringify(value, (key, v) =>
    (v instanceof Uint8Array ? { __u8: Array.from(v, (b) => b.toString(16).padStart(2, "0")).join("") } : v)));
  const decode = (value) => JSON.parse(JSON.stringify(value), (key, v) =>
    (v && typeof v === "object" && typeof v.__u8 === "string" && Object.keys(v).length === 1
      ? Uint8Array.from(v.__u8.match(/../g) || [], (b) => parseInt(b, 16)) : v));

  // ── the identities ─────────────────────────────────────────────────────────
  const ME = CONFIG.actor;                       // {name, role, npub, publicKey, secretKey}
  const VENDORS = CONFIG.roster.vendors;         // the four keys in the pinned set
  const ORDER_ID = CONFIG.orderId || "BM-4471";
  const VENUE_SKU = CONFIG.sku || "sim-1001";
  const VENUE_PRICE = CONFIG.venuePriceSats || 676;
  const MARGIN_CAP = CONFIG.marginCap ?? 0.10;
  // The verifier refuses any order whose `expiresAt` has passed, so the window has
  // to survive a rehearsal-to-stage gap. Measured 2026-10-03: a pane left open for
  // a rehearsal refused the proof with "order expired at …" once the demo started.
  // The window is freshness policy, not the security boundary — the pin, the ring
  // and the one-use-per-order key image are.
  const ORDER_WINDOW_MIN = CONFIG.orderWindowMin ?? 240;
  const HUB = CONFIG.hub || null;
  const HUB_HTTP = CONFIG.hubHttp || null;

  // The trust set is the four vendor keys. Charlie pins it; everyone else is given
  // the same object by the ORDER message (and re-checks it against the pin).
  function trustSet() {
    return {
      setId: CONFIG.roster.setId,
      description: CONFIG.roster.description,
      publishedAt: CONFIG.roster.publishedAt,
      members: VENDORS.map((v) => ({
        publicKey: hexToBytes(v.publicKey),
        label: v.name,
        npub: v.npub,
        tier: "demo",
        basis: "derived-demo-key",
        expiresAt: "2027-12-31T23:59:59Z",
      })),
    };
  }
  const SET = trustSet();
  const PIN = T.pinTrustSet(SET);
  const MEMBER_HEX = new Set(SET.members.map((m) => T.toHex(m.publicKey)));
  const MY_INDEX = SET.members.findIndex((m) => T.toHex(m.publicKey) === ME.publicKey);
  const AM_MEMBER = MY_INDEX >= 0;

  // ── state, exposed for the smoke tests (real return values, never page text) ─
  const state = (window.__trust = {
    actor: ME.name, role: ME.role, npub: ME.npub, amMember: AM_MEMBER,
    roster: { setId: CONFIG.roster.setId, vendors: VENDORS.map((v) => ({ name: v.name, npub: v.npub })) },
    transport: null, peers: [], order: null, pin: PIN.member || CONFIG.pin,
    sent: [], received: [], proof: null, proofs: [], verdicts: {},
    answered: {}, alreadyAnswered: null,
    errors: [],
    menu: null, menuError: null, venueName: null, item: null,
    announcement: null,
    quotes: [], accepted: null, paid: null, walletError: null,
    venue: { orderId: null, status: null, invoice: null, venueOrderNumber: null, rail: null },
    refused: null, checks: [], error: null, log: [],
  });
  state.pin = T.pinTrustSet(SET);

  const log = (line) => {
    state.log.push(line);
    const box = $("#log");
    if (box) {
      box.textContent = line;
      box.classList.add("flash");
      setTimeout(() => box.classList.remove("flash"), 400);
    }
  };
  const render = () => (ME.role === "customer" ? renderCustomer() : renderVendor());

  // ── transport: NAP-INC in the shell, npub hub elsewhere, mirrored when both ──
  const TOPIC = CONFIG.topic || "trust-vendor/orders";

  function createTransport() {
    const transports = [];
    const seen = new Set();

    const deliver = (raw, via) => {
      const envelope = decode(raw);                          // revive Uint8Arrays
      if (!envelope || !envelope.id || seen.has(envelope.id)) return;
      if (envelope.from === ME.npub) return;                 // ignore our own echo
      if (envelope.to && envelope.to !== ME.npub && envelope.to !== "*") return;
      seen.add(envelope.id);
      state.received.push({ via, from: envelope.from, kind: envelope.kind, to: envelope.to });
      log(`← ${short(envelope.from, 14)} ${envelope.kind}`);
      try {
        onMessage(envelope);
      } catch (err) {
        // A handler bug must never look like a transport failure.
        state.errors.push(`${envelope.kind}: ${err.message}`);
        log(`handler error on ${envelope.kind}: ${err.message}`);
      }
    };

    const send = (kind, body, to = "*") => {
      const envelope = {
        id: `m${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`,
        from: ME.npub, fromName: ME.name, to, kind, ts: Date.now(), body,
      };
      seen.add(envelope.id);
      state.sent.push({ id: envelope.id, kind, to, ts: envelope.ts });
      const wire = encode(envelope);
      for (const t of transports) t.send(wire);
      log(`→ ${to === "*" ? "the set" : short(to, 14)} ${kind}`);
      return envelope;
    };

    if (window.napplet && window.napplet.inc) {
      window.napplet.inc.on(TOPIC, (event) => {
        try { deliver(event.payload, "inc"); } catch (err) { state.errors.push(`inc: ${err.message}`); }
      });
      transports.push({ name: "inc", send: (e) => window.napplet.inc.emit(TOPIC, e) });
    }
    // Plain HTTP long-poll on purpose: no websocket dependency, it goes through
    // anything, and a demo does not need sub-100 ms latency. Actors on other
    // devices (a phone) use this; actors in the same shell also use NAP-INC.
    if (HUB_HTTP) {
      const queue = [];
      let cursor = 0;
      const flush = async () => {
        while (queue.length) {
          const batch = queue.splice(0, queue.length);
          try {
            await fetch(`${HUB_HTTP}/send`, {
              method: "POST", headers: { "content-type": "application/json" },
              body: JSON.stringify({ from: ME.npub, messages: batch }),
            });
          } catch { /* hub gone; in-host messages still work */ }
        }
      };
      const poll = async () => {
        for (;;) {
          try {
            const res = await fetch(`${HUB_HTTP}/recv?npub=${encodeURIComponent(ME.npub)}&since=${cursor}`);
            const json = await res.json();
            cursor = json.cursor ?? cursor;
            if (!state.peers.includes("hub")) { state.peers.push("hub"); log(`hub connected ${HUB_HTTP}`); render(); }
            for (const envelope of json.messages || []) deliver(envelope, "hub");
          } catch {
            if (state.peers.includes("hub")) {
              state.peers = state.peers.filter((p) => p !== "hub");
              log("hub unreachable (in-host messages still work)");
              render();
            }
            await new Promise((r) => setTimeout(r, 1500));
          }
        }
      };
      transports.push({ name: "hub", send: (e) => { queue.push(e); void flush(); } });
      void poll();
    }
    state.transport = transports.map((t) => t.name).join("+") || "none";
    return { send, close: () => {} };
  }

  // ── messages ───────────────────────────────────────────────────────────────
  function onMessage(envelope) {
    const { kind, body } = envelope;
    if (kind === "order.open") return onOrder(envelope);
    if (kind === "quote") return onQuote(envelope);
    if (kind === "verdict") return onVerdict(envelope);
    if (kind === "venue.status") return onVenueStatus(envelope);
    if (kind === "announce") {
      state.announcement = body.text;
      if (body.venue) Object.assign(state.venue, body.venue);
      return render();
    }
  }

  function orderOf(body) {
    return {
      orderId: body.order.orderId,
      amount: String(body.order.amount),
      currency: "sats",
      clientId: body.order.clientId,
      expiresAt: body.order.expiresAt,
      pin: body.pin,
    };
  }

  // ── customer ───────────────────────────────────────────────────────────────
  // The item and its price come from the venue API's own menu, and the fiat order
  // is created FIRST so the invoice the customer pays is the venue's real one —
  // the same call that places the order. No placeholder content, no stand-in.
  async function openOrder(transport, item) {
    const chosen = item || state.item || defaultItem();
    state.item = chosen;
    await venueOrder(transport, null);
    // Every open mints a NEW order id. The verifier's replay guard is keyed on
    // (orderId, setId), so reusing an id makes a second run look like a replay of
    // the first and gets refused — "key image already used for this order". A
    // fresh id per open gives each attempt its own scope, so the demo can be run
    // again, and again, in the same session.
    const runOrdinal = (window.__trustRun = (window.__trustRun || 0) + 1);
    const order = {
      orderId: `${ORDER_ID}-${runOrdinal.toString(36)}`,
      amount: String(state.venue.amountSats || chosen.priceSats),
      currency: "sats",
      clientId: ME.npub,
      expiresAt: new Date(Date.now() + ORDER_WINDOW_MIN * 60e3).toISOString(),
      pin: PIN,
      venue: {
        name: state.venueName || "Burgermeister Mehringdamm",
        table: (CONFIG.venue && CONFIG.venue.table) || "8613S3X",
        sku: chosen.sku, item: chosen.name,
      },
      invoice: state.venue.invoice || null,
      marginCap: MARGIN_CAP,
    };
    state.order = order;
    transport.send("order.open", { order, pin: PIN, trustSet: SET, setMembers: VENDORS.map((v) => v.npub) });
    render();
  }

  function defaultItem() {
    const items = (state.menu && state.menu.items) || [];
    return items.find((i) => String(i.category || "").toLowerCase() === "burger") || items[0]
      || { sku: VENUE_SKU, name: "Burger", priceSats: VENUE_PRICE, price: null };
  }

  // The menu the pane shows is the venue API's own — the same source the fiat
  // order is placed against, so what the audience reads is what gets ordered.
  async function loadMenu() {
    if (!HUB_HTTP) return;
    try {
      const res = await fetch(`${HUB_HTTP}/provider/menu`);
      const json = await res.json();
      if (json && json.items && json.items.length) {
        state.menu = { venue: json.venue || null, items: json.items,
                       mode: json.mode || null, source: json.source || "bridge" };
        state.venueName = (json.venue && json.venue.name) || state.venueName;
      } else {
        state.menuError = json && json.error ? json.error : "empty menu";
      }
    } catch (err) {
      state.menuError = err.message;
    }
    render();
  }

  function onOrder(envelope) {
    if (ME.role !== "vendor") return;
    state.order = envelope.body.order;
    render();
  }

  function onQuote(envelope) {
    if (ME.role !== "customer") return;
    const { proof, quote } = envelope.body;
    // verify against OUR pinned set — never theirs
    // A transport may hand us the same proof twice (inc mirrored onto the hub, or a
    // retry). The envelope-id guard catches most of it; a byte-identical proof under a
    // new id is still the same resolution attempt, so treat it as a duplicate rather
    // than letting the replay guard refuse it and alarm the operator.
    const fingerprint = proof ? `${JSON.stringify(proof.ring)}|${JSON.stringify(proof.signature)}|${quote && quote.sats}` : null;
    if (fingerprint && state.proofs.some((p) => p.fingerprint === fingerprint)) {
      log(`ignored a repeat delivery of ${short(envelope.from, 14)}'s proof (identical signature)`);
      return render();
    }
    const seen = T.createSeenSet();
    if (!proof) {
      const verdict = { ok: false, reason: "no ring proof attached — nothing to verify against your pinned set" };
      state.proofs.push({ from: envelope.from, proof: null, quote, verdict, ringHex: [], checks: [] });
      state.verdicts[envelope.from] = { ok: false, reason: verdict.reason, ringHex: [] };
      log(`REFUSED ${short(envelope.from, 14)}: no proof`);
      return render();
    }
    for (const previous of state.proofs) {
      try { T.verifyProof(previous.proof, SET, PIN, seen); } catch { /* ignore */ }
    }
    const verdict = T.verifyProof(proof, SET, PIN, seen);
    const ringHex = (proof.ring || []).map((k) => T.toHex(k));
    const subset = ringHex.length > 0 && ringHex.every((k) => MEMBER_HEX.has(k));
    const bound = proof.order || {};
    const checks = [
      { label: `ring is a subset of your pinned set (${ringHex.length} of ${MEMBER_HEX.size} keys)`, ok: subset },
      { label: `ring size ${ringHex.length} ≥ minimum ${T.MIN_RING_SIZE}`, ok: ringHex.length >= T.MIN_RING_SIZE },
      { label: `proof binds order #${bound.orderId} · ${sats(bound.amount)} sats`, ok: bound.orderId === state.order.orderId && String(bound.amount) === String(state.order.amount) },
      { label: "proof is bound to the pinned set version", ok: proof.pin && proof.pin.contentHash === PIN.contentHash },
      { label: "key image is fresh — one use per order", ok: verdict.ok },
      { label: `quote is within your ${Math.round(MARGIN_CAP * 100)}% ceiling`, ok: Number(quote.sats) <= Math.ceil(VENUE_PRICE * (1 + MARGIN_CAP)) },
    ];
    state.proofs.push({ from: envelope.from, proof, quote, verdict, ringHex, checks, fingerprint });
    state.verdicts[envelope.from] = { ok: verdict.ok, reason: verdict.reason || null, ringHex };
    if (!verdict.ok) log(`REFUSED ${short(envelope.from, 14)}: ${verdict.reason}`);
    render();
  }

  function onVerdict(envelope) {
    if (ME.role !== "vendor") return;
    state.refused = envelope.body.ok ? null : envelope.body.reason;
    render();
  }

  function onVenueStatus(envelope) {
    if (envelope.body.quote) {
      state.quotes.push(envelope.body);
    }
    Object.assign(state.venue, envelope.body.venue || {});
    render();
  }

  // the customer takes the cheapest verified quote; first valid proof wins a tie
  function bestQuote() {
    const ok = state.proofs.filter((p) => p.verdict.ok);
    if (!ok.length) return null;
    return ok.slice().sort((a, b) => Number(a.quote.sats) - Number(b.quote.sats))[0];
  }

  // ── vendor: prove membership of a set we may not be in ─────────────────────
  // ONE action. The quote IS the proof-bearing message; there is no separate
  // "prove" step and no second button. A vendor whose key is not in the pinned
  // set has nothing to resolve here — it just never acts, and never answers.
  function proveAndQuote(transport) {
    const order = state.order;
    if (!order || !AM_MEMBER) return;
    // ONE answer per order. The verifier's guard is one key image per order, and the
    // key image is a property of the member, not of the order — so a second proof for
    // the same order is refused by design. A second press therefore must not emit a
    // doomed proof; it says what to do instead (the customer opens a fresh order).
    if (state.answered[order.orderId]) {
      state.alreadyAnswered = order.orderId;
      log(`already quoted ${order.orderId} — the customer opens a fresh order to run again`);
      render();
      return;
    }
    state.refused = null;
    state.alreadyAnswered = null;
    try {
      const signerKey = { secretKey: hexToBytes(ME.secretKey), publicKey: hexToBytes(ME.publicKey) };
      const ringIndices = SET.members.map((_, i) => i);      // the whole pinned set
      const proof = T.prove(SET, signerKey, ringIndices, order);
      const base = Number(order.amount) || VENUE_PRICE;      // the API's price for this item
      const quoteSats = base + Math.max(1, Math.round(base * 0.08));
      state.proof = proof;
      const quote = {
        npub: ME.npub, name: ME.name, sats: quoteSats,
        invoice: state.venue.invoice || CONFIG.standInInvoice || null,
        invoiceKind: state.venue.invoice ? "venue-bolt11" : "STAND-IN",
      };
      state.quote = quote;
      state.answered[order.orderId] = true;
      transport.send("quote", { proof, quote }, order.clientId);
      render();
    } catch (err) {
      state.errors.push(`prove: ${err.message}`);
      log(`cannot quote: ${err.message}`);
      render();
    }
  }

  // ── the venue leg (bridge), through the hub proxy when it is there ─────────
  // The bridge's real shape: POST /provider/orders {venue, items:[{sku, qty}]}
  // -> {id, bolt11, amountSats}; then GET /provider/orders/<id> until the venue
  // order number appears.
  async function venueOrder(transport, quote) {
    const url = `${CONFIG.hubHttp || ""}/provider/orders`;
    const payload = {
      venue: (CONFIG.venue && CONFIG.venue.table) || "8613S3X",
      items: [{ sku: (state.item && state.item.sku) || VENUE_SKU, qty: 1 }],
    };
    try {
      const res = await fetch(url, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload),
      });
      const json = await res.json();
      if (json.error) throw new Error(json.error);
      state.venue = {
        orderId: json.id || json.order || json.orderId,
        status: json.status || "awaiting_payment",
        invoice: json.bolt11 || json.invoice || null,
        amountSats: json.amountSats ?? null, rail: json.rail || null,
        venueOrderNumber: json.venueOrderNumber || null, source: json.source || "bridge",
      };
      if (state.order) state.order.invoice = state.venue.invoice;
    } catch (err) {
      state.venue = { orderId: null, status: "unavailable", invoice: null, rail: null,
                      venueOrderNumber: null, error: err.message };
    }
    transport.send("venue.status", { venue: state.venue, quote }, "*");
    render();
    return state.venue;
  }

  async function pollVenue(transport) {
    if (!state.venue.orderId || !CONFIG.hubHttp) return;
    try {
      const res = await fetch(`${CONFIG.hubHttp}/provider/orders/${state.venue.orderId}`);
      const json = await res.json();
      Object.assign(state.venue, {
        status: json.status || state.venue.status,
        venueOrderNumber: json.venueOrderNumber || state.venue.venueOrderNumber,
      });
      transport.send("venue.status", { venue: state.venue }, "*");
    } catch (err) {
      state.venue.error = err.message;
    }
    render();
  }

  // ── the wallet (customer only; the host shows its own dialog) ──────────────
  // Two ways to pay, same call: the host's napplet wallet (Electrum shows its
  // own dialog) or WebLN, so the same napplet also pays on a phone that has a
  // WebLN wallet. Neither path is a fallback for the other's failure.
  async function pay(invoice) {
    state.walletError = null;
    const host = window.napplet && window.napplet.wallet && window.napplet.wallet.pay;
    const webln = window.webln && typeof window.webln.sendPayment === "function" ? window.webln : null;
    if (!invoice) {
      state.walletError = "no invoice to pay yet";
      return render();
    }
    if (!host && !webln) {
      state.walletError = "no wallet in this host — open this in Electrum (or a WebLN wallet) to pay";
      return render();
    }
    try {
      const result = host
        ? await withTimeout(window.napplet.wallet.pay(invoice), 40000, "the host wallet did not answer (no dialog?)")
        : await webln.sendPayment(invoice);
      state.paid = { invoice, preimage: result && result.preimage, via: host ? "napplet-wallet" : "webln" };
      log(`wallet: paid via ${state.paid.via}`);
      void settleVenue();
    } catch (err) {
      state.walletError = err.message || String(err);
      log(`wallet: ${state.walletError}`);
    }
    render();
  }

  // The UI places the order itself, so the burger really gets ordered: the venue
  // API creates it, the poll follows it, and the result is announced to the set.
  // Exactly ONE placer (the customer's UI) so live mode can never open two
  // kitchen tickets for one payment.
  async function settleVenue() {
    if (!state.venue.orderId) {          // normally created when the order opened
      await venueOrder(transport, state.accepted && state.accepted.quote);
      render();
    }
    for (let i = 0; i < 8 && !state.venue.venueOrderNumber; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      await pollVenue(transport);
      if (state.venue.venueOrderNumber) break;
    }
    const what = (state.item && state.item.name) || "Burger";
    state.announcement = state.venue.venueOrderNumber
      ? `${what} purchased successfully ✓ venue order ${state.venue.venueOrderNumber}`
      : `Venue order ${state.venue.orderId || "?"} · ${state.venue.status}`;
    transport.send("announce", { text: state.announcement, venue: state.venue }, "*");
    render();
  }

  // ── rendering ──────────────────────────────────────────────────────────────
  const STATUS = ["ORDER_OPEN", "QUOTES_IN", "ACCEPTED", "LN_PAID", "VENUE_SUBMITTED", "COMPLETE"];

  function stateStrip(current) {
    const strip = el("div", "strip");
    for (const name of STATUS) {
      const at = STATUS.indexOf(name), now = STATUS.indexOf(current);
      strip.appendChild(el("span", "step" + (at < now ? " done" : at === now ? " now" : ""), name));
    }
    return strip;
  }

  function currentState() {
    if (state.paid) return state.venue.venueOrderNumber ? "COMPLETE" : "LN_PAID";
    if (state.accepted) return "ACCEPTED";
    if (state.proofs.length) return "QUOTES_IN";
    return "ORDER_OPEN";
  }

  function card(title, sub) {
    const box = el("div", "card");
    box.appendChild(el("div", "title", title));
    if (sub) box.appendChild(el("div", "sub", sub));
    return box;
  }

  function renderCustomer() {
    const root = $("#app");
    root.innerHTML = "";
    root.appendChild(stateStrip(currentState()));
    // A refused proof is the one thing that must never be quiet: show the bundle's
    // own reason at the top of the pane, with the single action that clears it.
    const refused = state.proofs.filter((p) => !p.verdict.ok);
    for (const r of refused) {
      const banner = card(`Refused — ${short(r.from, 22)}`, r.quote ? `${sats(r.quote.sats)} sats offered` : "");
      banner.classList.add("bad");
      banner.appendChild(el("div", "err", r.verdict.reason || "invalid proof"));
      banner.appendChild(el("div", "note", refusalAdvice(r.verdict.reason)));
      root.appendChild(banner);
    }

    const who = el("div", "who");
    who.appendChild(el("span", "pill", `customer · ${ME.name}`));
    who.appendChild(el("span", "mono", short(ME.npub, 20)));
    root.appendChild(who);

    const venue = card(`${state.venueName || "Burgermeister Mehringdamm"} · Tafel 1`,
      `menu read live from the venue API${state.menu ? ` (${state.menu.mode || "?"} · ${state.menu.source})` : ""}`
      + ` · ceiling +${Math.round(MARGIN_CAP * 100)}%`);
    if (state.menuError) venue.appendChild(el("div", "err", `menu: ${state.menuError}`));
    if (state.menu) {
      const rows = el("div", "rows");
      for (const it of state.menu.items.slice(0, 6)) {
        const row = el("div", "row" + (state.item && state.item.sku === it.sku ? " best" : ""));
        row.appendChild(el("span", "grow", `${it.name}${it.category ? " · " + it.category : ""}`));
        if (it.price) row.appendChild(el("span", "mono", `€${it.price}`));
        row.appendChild(el("span", "mono", `${sats(it.priceSats)} sats`));
        row.addEventListener("click", () => { state.item = it; render(); });
        rows.appendChild(row);
      }
      venue.appendChild(rows);
    }
    if (state.item) {
      venue.appendChild(el("div", "mono", `chosen: ${state.item.name} · ${sats(state.item.priceSats)} sats`));
    }
    if (!state.order) {
      const button = el("button", "cta", "Open this order to my pinned set");
      button.addEventListener("click", () => openOrder(transport));
      venue.appendChild(button);
    } else {
      venue.appendChild(el("div", "mono",
        `order #${state.order.orderId} · ${sats(state.order.amount)} sats · you chose a set, not a vendor`));
      if (state.order.invoice) {
        venue.appendChild(el("div", "mono", `venue invoice ${short(state.order.invoice, 22)}…`));
      }
    }
    root.appendChild(venue);

    const set = card("Your pinned trust set", `${VENDORS.length} keys · ${short(PIN.contentHash, 10)}`);
    const list = el("div", "rows");
    for (const v of VENDORS) {
      const row = el("div", "row");
      row.appendChild(el("span", "mono grow", short(v.npub, 26)));
      row.appendChild(el("span", "tag", state.verdicts[v.npub] ? (state.verdicts[v.npub].ok ? "proved" : "refused") : "invited"));
      list.appendChild(row);
    }
    set.appendChild(list);
    root.appendChild(set);

    const quotes = card("Quotes — verified against your set", state.proofs.length ? "" : "waiting for the set to answer");
    const rows = el("div", "rows");
    for (const p of state.proofs) {
      const row = el("div", "row" + (p.verdict.ok ? "" : " bad"));
      row.appendChild(el("span", "mono grow", short(p.from, 22)));
      row.appendChild(el("span", "tag " + (p.verdict.ok ? "ok" : "no"), p.verdict.ok ? "verified" : "refused"));
      row.appendChild(el("span", "mono", `${sats(p.quote.sats)} sats`));
      rows.appendChild(row);
      if (!p.verdict.ok) {
        row.appendChild(el("div", "note", p.verdict.reason || ""));
        const theirs = p.proof && p.proof.pin && p.proof.pin.contentHash;
        if (theirs && theirs !== PIN.contentHash) {
          row.appendChild(el("div", "err", `their set ${short(theirs, 10)} ≠ yours ${short(PIN.contentHash, 10)}`));
        }
      }
    }
    quotes.appendChild(rows);
    const best = bestQuote();
    if (best && !state.accepted) {
      const button = el("button", "cta", `Accept the best quote — ${sats(best.quote.sats)} sats`);
      button.addEventListener("click", () => { state.accepted = best; render(); });
      quotes.appendChild(button);
    }
    root.appendChild(quotes);

    if (state.accepted) {
      const pay = card("Pay", "the wallet confirms every payment itself");
      pay.appendChild(el("div", "mono", `to ${short(state.accepted.from, 22)} · ${sats(state.accepted.quote.sats)} sats`));
      pay.appendChild(el("div", "note", state.accepted.quote.invoiceKind === "STAND-IN"
        ? "STAND-IN invoice — the flow is real, this payment is not bitcoin."
        : "real venue invoice"));
      const invoice = state.accepted.quote.invoice;
      if (state.paid) {
        pay.appendChild(el("div", "ok", `paid · preimage ${short(state.paid.preimage, 12)}`));
      } else if (invoice) {
        const button = el("button", "cta", "Pay with Electrum");
        button.addEventListener("click", () => pay(invoice));
        pay.appendChild(button);
      } else {
        pay.appendChild(el("div", "note", "no invoice yet — the vendor is paying the venue"));
      }
      if (state.walletError) pay.appendChild(el("div", "err", state.walletError));
      root.appendChild(pay);
    }

    const venueCard = card(state.announcement || "Venue order",
      state.venue.orderId
        ? `#${state.venue.orderId} · ${state.venue.status} · ordered by this UI through the venue API`
        : "placed automatically once the payment settles");
    if (state.venue.venueOrderNumber) venueCard.appendChild(el("div", "ok", `venue order ${state.venue.venueOrderNumber}`));
    if (state.venue.error) venueCard.appendChild(el("div", "err", state.venue.error));
    root.appendChild(venueCard);
    root.appendChild(logLine());
  }

  function renderVendor() {
    const root = $("#app");
    root.innerHTML = "";
    root.appendChild(stateStrip(currentState()));

    const who = el("div", "who");
    who.appendChild(el("span", "pill" + (AM_MEMBER ? "" : " bad"), `${AM_MEMBER ? "vendor" : "vendor (outside the set)"} · ${ME.name}`));
    who.appendChild(el("span", "mono", short(ME.npub, 20)));
    root.appendChild(who);

    const order = card("Open order", state.order ? `#${state.order.orderId} · invited by the set, not chosen by the customer` : "waiting for a customer");
    if (state.order) {
      order.appendChild(el("div", "mono",
        `${state.order.venue && state.order.venue.item ? state.order.venue.item + " · " : ""}`
        + `venue price ${sats(state.order.amount)} sats · ceiling +${Math.round(MARGIN_CAP * 100)}%`));
      if (AM_MEMBER) {
        // one button, one message: the quote carries the proof
        const button = el("button", "cta", "Prove I'm in the set & quote");
        button.addEventListener("click", () => proveAndQuote(transport));
        order.appendChild(button);
        if (state.alreadyAnswered === state.order.orderId) {
          order.appendChild(el("div", "note",
            "already answered this order — the verifier accepts one proof per member per order. "
            + "Nothing to press: the customer opens a fresh order to run the demo again."));
        }
      } else {
        order.appendChild(el("div", "note", "not in this customer's set — nothing to resolve, so this vendor never answers"));
      }
    }
    root.appendChild(order);

    if (state.proof) {
      const proof = card("Your proof", "the customer learns a member signed — never which one");
      proof.appendChild(el("div", "mono", `key image ${short(T.toHex(state.proof.signature.keyImage), 16)}`));
      proof.appendChild(el("div", "mono", `ring ${state.proof.ring.length} keys · anonymity set ${state.proof.anonymitySetSize}`));
      proof.appendChild(el("div", "mono", `order bound · ${sats(state.proof.order.amount)} sats`));
      proof.appendChild(el("div", "mono",
        `quote ${sats(state.quote ? state.quote.sats : VENUE_PRICE)} sats · proof and quote in one message`));
      root.appendChild(proof);
    }

    if (state.announcement) {
      const done = card("✅ " + state.announcement,
        state.venue.venueOrderNumber ? `venue order ${state.venue.venueOrderNumber}` : "");
      root.appendChild(done);
    }
    root.appendChild(logLine());
  }

  function logLine() {
    const box = el("div", "log");
    box.id = "log";
    box.textContent = state.log.slice(-1)[0] || "ready";
    return box;
  }

  const transport = createTransport();
  if (ME.role === "customer") void loadMenu();
  render();

  // test hooks: real state, and the two entry points the smoke tests click
  window.__trust.openOrder = () => openOrder(transport);
  window.__trust.proveAndQuote = () => proveAndQuote(transport);
  window.__trust.acceptBest = () => { state.accepted = bestQuote(); render(); };
  window.__trust.pay = () => pay(state.accepted?.quote?.invoice);
  window.__trust.placeVenueOrder = () => venueOrder(transport, state.quote);
  window.__trust.pollVenue = () => pollVenue(transport);
  window.__trust.setExternal = (order, invoice) => {      // used by the tests to inject a real invoice
    state.venue.invoice = invoice || state.venue.invoice;
    if (order) state.order = order;
    render();
  };
  window.__trust.ready = true;
})();

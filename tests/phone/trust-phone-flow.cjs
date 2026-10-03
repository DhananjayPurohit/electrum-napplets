// The phone path: the same four napplets, served standalone by the hub, talking
// over the hub instead of NAP-INC. This is the interoperability test — the
// identities are the same npubs, the messages are the same envelopes, and the
// only thing that changes is the transport.
//
// Run: node tests/phone/trust-phone-flow.cjs      (needs `playwright`; the repo
// is Python-only, so this is the one dev dependency, used for this test alone)
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '..', '..');
const PORT = 8787;
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = path.join(ROOT, 'tests', 'screenshots');
const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${!ok && detail ? `  (${detail})` : ''}`);
  if (!ok) failures.push(name);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(fn, timeout = 25000) {
  const start = Date.now();
  for (;;) {
    const value = await fn().catch(() => null);
    if (value) return value;
    if (Date.now() - start > timeout) return null;
    await sleep(200);
  }
}

async function main() {
  // a hub left over from a crashed run would silently serve stale napplets
  const squatter = await fetch(`${BASE}/state`).then(() => true).catch(() => false);
  if (squatter) {
    check('port 8787 is free (no leftover hub)', false, 'kill it first: pkill -f trust/hub.py');
    process.exit(1);
  }
  const hub = spawn('python3', [path.join(ROOT, 'napplets', 'trust', 'hub.py'), '--stub', '--port', String(PORT), '--quiet'], {
    cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'],
  });
  process.on('exit', () => { try { hub.kill('SIGKILL'); } catch { /* already gone */ } });
  hub.stderr.on('data', (d) => { const s = String(d).trim(); if (s && !s.includes('[hub]')) console.log('  hub:', s); });
  const ready = await waitFor(async () => {
    const res = await fetch(`${BASE}/state`);
    return res.ok;
  }, 15000);
  if (!ready) { check('hub started', false); process.exit(1); }
  check('hub started (venue stub)', true);

  let chromium;
  try { ({ chromium } = require('playwright')); }
  catch { check('playwright available', false, 'npm i -D playwright'); hub.kill(); process.exit(1); }

  let browser;
  try { browser = await chromium.launch(); }
  catch { browser = await chromium.launch({ channel: 'chrome' }); }

  fs.mkdirSync(SHOTS, { recursive: true });
  const pages = {};
  for (const actor of ['charlie', 'alice', 'bob', 'malice']) {
    const context = await browser.newContext({ viewport: { width: 460, height: 900 } });
    // stand in for the phone's WebLN wallet, so the payment leg is exercised here too
    await context.addInitScript(() => {
      window.__weblnPaid = [];
      window.webln = { sendPayment: async (invoice) => { window.__weblnPaid.push(invoice); return { preimage: 'cd'.repeat(32) }; } };
    });
    const page = await context.newPage();
    await page.goto(`${BASE}/${actor}`, { waitUntil: 'load' });
    await page.waitForFunction('window.__trust && window.__trust.ready === true', null, { timeout: 20000 });
    pages[actor] = page;
  }
  check('all four actors booted standalone (the phone build)', true);

  const transport = await pages.alice.evaluate('__trust.transport');
  check('phone actors use the hub transport, not NAP-INC', transport === 'hub', String(transport));
  const identity = await pages.charlie.evaluate('__trust.npub');
  check('identity is an npub on the phone too', String(identity).startsWith('npub1'), String(identity));

  // the customer opens the order; it reaches the vendors over the hub
  await pages.charlie.evaluate('__trust.openOrder()');
  const gotOrder = await waitFor(async () => pages.alice.evaluate('__trust.order && __trust.order.orderId'));
  check('order reached a vendor over the hub', gotOrder === 'BM-4471', String(gotOrder));

  await pages.alice.evaluate('__trust.proveAndQuote()');
  const v1 = await waitFor(async () => pages.charlie.evaluate('__trust.proofs.length >= 1 && JSON.stringify(__trust.proofs[0].verdict)'));
  check('member quote verified on the phone', String(v1).includes('"ok":true'), String(v1));

  await pages.bob.evaluate('__trust.proveAndQuote()');
  await waitFor(async () => pages.charlie.evaluate('__trust.proofs.length >= 2'));

  // a vendor outside the set has nothing to resolve: it never answers at all
  await pages.malice.evaluate('__trust.proveAndQuote()');
  await sleep(1200);
  const count = await pages.charlie.evaluate('__trust.proofs.length');
  check('the outsider never answers — nothing to resolve on the phone either', count === 2, String(count));

  await pages.charlie.evaluate('__trust.acceptBest()');
  const accepted = await pages.charlie.evaluate('JSON.stringify({sats: __trust.accepted.quote.sats, from: __trust.accepted.from})');
  check('cheapest verified quote accepted', String(accepted).includes('730'), String(accepted));

  // pay with the phone's WebLN wallet
  await pages.charlie.evaluate('__trust.pay()');
  const paid = await waitFor(async () => pages.charlie.evaluate('__trust.paid && __trust.paid.preimage'));
  check('paid on the phone through WebLN', Boolean(paid), String(paid));
  const via = await pages.charlie.evaluate('__trust.paid && __trust.paid.via');
  check('the phone payment reports which wallet it used', via === 'webln', String(via));
  await pages.charlie.screenshot({ path: path.join(SHOTS, 'trust_phone_customer.png') });

  // the customer's UI orders the burger itself, as soon as the payment settles
  const venueOrder = await waitFor(async () => pages.charlie.evaluate('__trust.venue.venueOrderNumber'), 45000);
  check('the phone UI ordered the burger through the API', venueOrder === '8613-S3X-0007', String(venueOrder));
  const announce = await waitFor(async () => pages.charlie.evaluate('__trust.announcement'));
  check('the phone UI announces the purchase', String(announce).includes('successfully'), String(announce));
  const vendorAnnounce = await waitFor(async () => pages.alice.evaluate('__trust.announcement'), 25000);
  check('the facilitator sees the announcement too', Boolean(vendorAnnounce), String(vendorAnnounce));
  await pages.alice.screenshot({ path: path.join(SHOTS, 'trust_phone_vendor.png') });

  // the relay saw the conversation, and both transports used the same envelopes
  const state = await (await fetch(`${BASE}/state`)).json();
  check('hub relayed the whole conversation', state.messages >= 5, `${state.messages} messages`);
  check('hub delivery counter advanced', state.delivered > 0, `${state.delivered} delivered`);

  await browser.close();
  hub.kill('SIGTERM');
  console.log(`\n${failures.length} failure(s)`);
  process.exit(failures.length ? 1 : 0);
}

main().catch((err) => { console.error('FAIL', err.message); process.exit(1); });

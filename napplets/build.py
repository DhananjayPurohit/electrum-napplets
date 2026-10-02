#!/usr/bin/env python3
"""Builds the pizza napplets from the upstream demo pages.

upstream/ holds the original buyer (order.html) and facilitator pages and their
bundles, pinned by sha256 below. Each page becomes one self-contained napplet
(NIP-5D: a single index.html, no network but the granted mint):

  - the two bundles and the roster are inlined
  - BroadcastChannel is replaced by NappletChannel (NAP-INC through the shell)
  - the mint is fixed at build time (a srcdoc napplet has no ?mint= query)
  - the buyer gets a "Pay with Electrum" button (window.napplet.wallet)

Output goes to ../browser/napplets/, where the Electrum plugin picks it up.

    python3 napplets/build.py                      # testnut (fake Lightning, auto-paid)
    python3 napplets/build.py --mint https://cdk-a056e0f.cashu.exchange   # signet
"""
import argparse
import hashlib
import json
import os
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
UPSTREAM = os.path.join(HERE, 'upstream')
OUT = os.path.join(HERE, '..', 'browser', 'napplets')

PINNED = {
    'order.html': '0dd6e5e430ec5765ddb35166a3034b00104caf8735f299d0a05be7902f7ba1c1',
    'facilitator.html': 'ced35e94212ac2a7d213d2ab5300ac4b235885fa34f02279d6ef7e61bfbc7a42',
    'trust-ring.bundle.js': '3b779e1156067be45baa340c52218c8cb4e73f79bd0114cee1595cd5869d1110',
    'cashu-paid-leg.bundle.js': '0f05aedac6239b69afe390650edd17f8dc380f1b81de7cd2e63d5b1bfbeee15f',
    'facilitator-roster.json': '0c8c87a0af3f5d931a6ec19e0bdad34e392fb59228f7ae9337d73f9b334a4ca2',
}
UPSTREAM_MINT = '"https://cdk-a056e0f.cashu.exchange"'
MINT_EXPR = f'new URLSearchParams(location.search).get("mint") || {UPSTREAM_MINT}'


def read(name: str, folder: str = UPSTREAM) -> str:
    with open(os.path.join(folder, name), 'rb') as f:
        data = f.read()
    if folder == UPSTREAM:
        digest = hashlib.sha256(data).hexdigest()
        if digest != PINNED[name]:
            raise SystemExit(f'{name} changed upstream (sha256 {digest}); review it and update PINNED')
    return data.decode('utf-8')


def replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f'expected exactly one {old[:60]!r}, found {count}')
    return text.replace(old, new)


def inline_scripts(page: str, mint: str, roster: str) -> str:
    for bundle in ('trust-ring.bundle.js', 'cashu-paid-leg.bundle.js'):
        page = replace_once(page, f'<script src="./{bundle}"></script>', f'<script>\n{read(bundle)}\n</script>')
    helpers = f'<script>\n{read("inc-channel.js", HERE)}\nconst ROSTER = {roster};\n</script>\n<script>\n(() => {{'
    page = replace_once(page, '<script>\n(() => {', helpers)
    page = replace_once(page, 'new BroadcastChannel("pizza-trust-demo")', 'NappletChannel("pizza-trust-demo")')
    return replace_once(page, MINT_EXPR, json.dumps(mint))


PAY_WITH_ELECTRUM = '''
  // ── pay the mint's invoice from the wallet hosting this napplet ─────────
  const walletButton = $("#pay-electrum");
  if (!(window.napplet && window.napplet.wallet)) walletButton.classList.add("hide");
  walletButton.addEventListener("click", async () => {
    if (!state.realQuote) return;
    walletButton.disabled = true;
    walletButton.textContent = "Waiting for Electrum…";
    $("#pay-electrum-error").textContent = "";
    try {
      const result = await window.napplet.wallet.pay(state.realQuote.request);
      state.walletPreimage = result.preimage;
      walletButton.textContent = "Paid with Electrum ✓";
      await checkMint();
    } catch (err) {
      state.walletError = err.message || String(err);
      walletButton.disabled = false;
      walletButton.textContent = "Pay with Electrum";
      $("#pay-electrum-error").textContent = state.walletError;
    }
  });
'''


def build_buyer(mint: str, roster: str) -> str:
    page = inline_scripts(read('order.html'), mint, roster)
    page = replace_once(page, 'await (await fetch("./facilitator-roster.json")).json()', 'ROSTER')
    page = replace_once(
        page,
        '<div class="addr" id="pay-invoice">—</div>',
        '<div class="addr" id="pay-invoice">—</div>\n'
        '          <button class="wide" id="pay-electrum" style="margin-top:8px">Pay with Electrum</button>\n'
        '          <div class="mono" id="pay-electrum-error" style="color:var(--err);margin-top:6px"></div>')
    return replace_once(page, '  $("#pay-recheck").addEventListener("click", checkMint);\n',
                        '  $("#pay-recheck").addEventListener("click", checkMint);\n' + PAY_WITH_ELECTRUM)


def build_facilitator(mint: str, roster: str) -> str:
    page = inline_scripts(read('facilitator.html'), mint, roster)
    return replace_once(page, 'const res = await fetch("./facilitator-roster.json");\n    roster = await res.json();',
                        'roster = ROSTER;')


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--mint', default='https://testnut.cashu.space')
    mint = parser.parse_args().mint.rstrip('/')
    parts = urlsplit(mint)
    if parts.scheme != 'https' or not parts.netloc:
        raise SystemExit('--mint must be an https:// URL')
    connect = [f'https://{parts.netloc}', f'wss://{parts.netloc}']
    roster = json.dumps(json.loads(read('facilitator-roster.json')))

    catalog = [
        {'dTag': 'pizza-buyer', 'title': 'Pizza order', 'file': 'pizza-buyer.html',
         'domains': ['inc', 'wallet'], 'connect': connect, 'html': build_buyer(mint, roster)},
        {'dTag': 'pizza-facilitator', 'title': 'Facilitator', 'file': 'pizza-facilitator.html',
         'domains': ['inc'], 'connect': connect, 'html': build_facilitator(mint, roster)},
    ]
    os.makedirs(OUT, exist_ok=True)
    for entry in catalog:
        with open(os.path.join(OUT, entry['file']), 'w', encoding='utf-8') as f:
            f.write(entry.pop('html'))
    with open(os.path.join(OUT, 'catalog.json'), 'w', encoding='utf-8') as f:
        json.dump({'mint': mint, 'napplets': catalog}, f, indent=2)
        f.write('\n')
    print(f'built {len(catalog)} napplets for {mint} into {os.path.normpath(OUT)}')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Builds the trust-vendor napplets (customer + the vendors on stage).

Same idea as `napplets/build.py`, for the ring/trust flow:

  - the pinned ring bundle is inlined (a srcdoc napplet has no network for scripts)
  - the demo identities are baked in, derived by `trust/keys.py` and reproducible,
    so an actor is the SAME npub in Electrum and on a phone
  - the hub URL (npub-addressed WebSocket relay for actors on other devices) is
    fixed at build time, because a srcdoc napplet has no ?query= to read

Output:
  browser/napplets/trust-charlie.html    customer  (gets `wallet`)
  browser/napplets/trust-alice.html      vendor in the set
  browser/napplets/trust-bob.html        vendor in the set
  browser/napplets/trust-malice.html     vendor NOT in the set (the abuse case)
and the four catalog entries are merged into browser/napplets/catalog.json.

    python3 napplets/build-trust.py --hub ws://127.0.0.1:8787
    python3 napplets/build-trust.py --no-hub          # in-host (NAP-INC) only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
TRUST = os.path.join(HERE, 'trust')
UPSTREAM = os.path.join(HERE, 'upstream')
OUT = os.path.join(HERE, '..', 'browser', 'napplets')

RING_BUNDLE = 'trust-ring.bundle.js'
RING_SHA256 = '3b779e1156067be45baa340c52218c8cb4e73f79bd0114cee1595cd5869d1110'

ON_STAGE = [
    ('charlie', 'customer', 'The customer'),
    ('alice', 'vendor', 'Vendor — in the pinned set'),
    ('bob', 'vendor', 'Vendor — in the pinned set'),
    ('malice', 'vendor', 'Vendor — NOT in the pinned set (the abuse case)'),
]

# Electrum shows TWO panes and nothing else: the customer, and one facilitator.
# The other actors are still built and served standalone by the hub (the phone
# demo and the tests drive four of them), but they are not in the catalog, so
# they never appear on the Electrum screen. Four panes of this much UI is
# clutter; the operator asked for two.
CATALOG = [('charlie', 'Customer'), ('alice', 'Facilitator')]

ORDER_ID = 'BM-4471'
SKU = 'sim-1001'
VENUE_PRICE_SATS = 676           # what the live bridge serves for sim-1001 (GET /provider/menu)
MARGIN_CAP = 0.10
VENUE = {'name': 'Burgermeister Mehringdamm', 'table': '8613S3X', 'sku': SKU}


def read(path: str) -> str:
    with open(path, 'rb') as f:
        data = f.read()
    return data.decode('utf-8')


def pinned_bundle() -> str:
    path = os.path.join(UPSTREAM, RING_BUNDLE)
    with open(path, 'rb') as f:
        data = f.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != RING_SHA256:
        raise SystemExit(f'{RING_BUNDLE} changed (sha256 {digest}); review it and update RING_SHA256')
    return data.decode('utf-8')


def connect_origins(hub: str | None, extra: list[str]) -> list[str]:
    origins = list(extra)
    if hub:
        parts = urllib.parse.urlsplit(hub)
        scheme = 'https' if parts.scheme == 'wss' else 'http'
        origins += [f'{scheme}://{parts.netloc}', f'ws://{parts.netloc}', f'wss://{parts.netloc}']
    # de-duplicate, keep order
    seen, out = set(), []
    for origin in origins:
        if origin not in seen:
            seen.add(origin)
            out.append(origin)
    return out


def config_for(name: str, role: str, roster: dict, hub: str | None, invoice: str) -> dict:
    everyone = roster['vendors'] + roster['outsiders'] + roster['customers']
    actor = next(e for e in everyone if e['name'] == name)
    vendors = [
        {'name': v['name'], 'npub': v['npub'], 'publicKey': v['publicKey'], 'note': v['note']}
        for v in roster['vendors']
    ]
    hub_http = None
    if hub:
        parts = urllib.parse.urlsplit(hub)
        scheme = 'https' if parts.scheme == 'wss' else 'http'
        hub_http = f'{scheme}://{parts.netloc}'
    return {
        'actor': {
            'name': actor['name'], 'role': role, 'npub': actor['npub'],
            'publicKey': actor['publicKey'], 'secretKey': actor['secretKey'],
        },
        'roster': {
            'setId': roster['setId'], 'description': roster['description'],
            'publishedAt': roster['publishedAt'], 'vendors': vendors,
        },
        'orderId': ORDER_ID, 'sku': SKU, 'venuePriceSats': VENUE_PRICE_SATS,
        'marginCap': MARGIN_CAP, 'venue': VENUE,
        'hub': hub, 'hubHttp': hub_http, 'topic': 'trust-vendor/orders',
        # a clearly labelled placeholder: the real invoice comes from the venue rail
        'standInInvoice': invoice,
    }


STANDIN = ('lnbc6760n1ptcfinalsstandin' + 'qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq'
           'qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq')


def build_page(config: dict, bundle: str, app: str, template: str) -> str:
    scripts = (
        '<script>\n'
        '// DEMO IDENTITIES. Every key here is derived from a public constant\n'
        '// (napplets/trust/keys.py, prefix "ptc++-finals/trust-vendor/") and is\n'
        '// worthless by construction. There is no secret in this file to leak:\n'
        '// `python3 napplets/trust/keys.py --verify` re-derives the team\'s own\n'
        '// roster from its published formula to check the arithmetic.\n'
        f'window.__TRUST_CONFIG__ = {json.dumps(config, separators=(",", ":"))};\n'
        '</script>\n'
        f'<script>\n{bundle}\n</script>\n'
        '<script>\n'
        f'window.__trustRing = __trustRing;\n{app}\n'
        '</script>\n'
    )
    if '__TRUST_SCRIPTS__' not in template:
        raise SystemExit('template is missing __TRUST_SCRIPTS__')
    return template.replace('__TRUST_SCRIPTS__', scripts)


def ensure_pizza_entries(catalog: dict) -> dict:
    """The pizza builder owns its own entries; keep them if they are already there."""
    have = {n['dTag'] for n in catalog.get('napplets', [])}
    if {'pizza-buyer', 'pizza-facilitator'} - have:
        print('catalog has no pizza napplets — running napplets/build.py first')
        subprocess.run([sys.executable, os.path.join(HERE, 'build.py')], check=True)
        with open(os.path.join(OUT, 'catalog.json'), encoding='utf-8') as f:
            catalog = json.load(f)
    return catalog


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--hub', default='ws://127.0.0.1:8787',
                        help='npub-addressed WebSocket hub (the phone / cross-device transport)')
    parser.add_argument('--no-hub', action='store_true', help='in-host NAP-INC only')
    parser.add_argument('--connect-extra', action='append', default=[],
                        help='extra connect-src origin, e.g. http://192.168.1.5:8788 (repeatable)')
    parser.add_argument('--out-dir', default=OUT)
    args = parser.parse_args()

    hub = None if args.no_hub else args.hub
    sys.path.insert(0, TRUST)
    from keys import roster as build_roster           # noqa: E402

    roster = build_roster()
    bundle = pinned_bundle()
    app = read(os.path.join(TRUST, 'app.js'))
    template = read(os.path.join(TRUST, 'index.template.html'))
    connect = connect_origins(hub, args.connect_extra)

    catalog_path = os.path.join(args.out_dir, 'catalog.json')
    try:
        with open(catalog_path, encoding='utf-8') as f:
            catalog = json.load(f)
    except FileNotFoundError:
        catalog = {'mint': 'https://testnut.cashu.space', 'napplets': []}
    catalog = ensure_pizza_entries(catalog)
    catalog['napplets'] = [n for n in catalog['napplets'] if not n['dTag'].startswith('trust-')]

    written = []
    titles = dict(CATALOG)
    for name, role, title in ON_STAGE:
        config = config_for(name, role, roster, hub, STANDIN)
        html = build_page(config, bundle, app, template)
        filename = f'trust-{name}.html'
        with open(os.path.join(args.out_dir, filename), 'w', encoding='utf-8') as f:
            f.write(html)
        written.append((filename, len(html), config['actor']['npub']))
        if name not in titles:
            continue                    # built for the phone and the tests, not for Electrum
        catalog['napplets'].append({
            'dTag': f'trust-{name}', 'title': titles[name], 'file': filename,
            'domains': ['inc', 'wallet'] if role == 'customer' else ['inc'],
            'connect': connect,
        })

    with open(catalog_path, 'w', encoding='utf-8') as f:
        json.dump(catalog, f, indent=2)
        f.write('\n')

    print(f'{len(written)} napplet(s) into {os.path.normpath(args.out_dir)}  (hub: {hub or "none"})')
    for filename, size, npub in written:
        print(f'  {filename:24s} {size:>7,} B   {npub}')
    print(f'  catalog.json             {len(catalog["napplets"])} napplets total')
    print('\nset:', roster['setId'], '| members:', ', '.join(v['name'] for v in roster['vendors']),
          '| outside:', ', '.join(o['name'] for o in roster['outsiders']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

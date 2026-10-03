#!/usr/bin/env python3
"""Smoke test for the venue bridge (Amperstrand's live order API).

Two modes, very different in what they touch:

  python3 tests/smoke_bridge.py                 # demo mode, zero real venue calls
  python3 tests/smoke_bridge.py --stub          # the local hub stub, offline
  python3 tests/smoke_bridge.py --live --venue 8613S3X --sku sim-1001 \\
      --ready --confirm 8613S3X                 # creates a REAL kitchen ticket

Demo mode is the default on the bridge and makes no venue calls by construction
(the bridge enforces that). It walks the whole order lifecycle:

  GET  /mode                         -> demo, venueAllowed
  GET  /provider/menu                -> items with sku + priceSats
  POST /provider/orders              -> order id + bolt11 invoice, awaiting_payment
  GET  /provider/orders/<id>         -> funded -> submitted -> venueOrderNumber

Live mode is human-gated on purpose: it flips the bridge to live, creates the
order, and then STOPS and tells you to complete the checkout in the browser. It
needs both `--ready` and `--confirm <venue>` so that it cannot be run by accident,
and it restores demo mode on the way out. The card stays in a human hand; nothing
here holds or reads a card.

Exit code 0 = all checks passed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BRIDGE = 'https://jacksonville-foods-relates-such.trycloudflare.com'
DEFAULT_VENUE = '8613S3X'
DEFAULT_SKU = 'sim-1001'
# The bridge's own vocabulary. Issue #2 described funded/submitted; the live API
# answers `paid` in demo mode, so the test accepts either and only insists that
# the order progresses out of awaiting_payment.
TERMINAL_STATES = {'paid', 'funded', 'submitted', 'fulfilled', 'completed', 'collected'}

failures: list[str] = []


def check(name: str, ok: bool, detail: str = '') -> bool:
    print(('PASS ' if ok else 'FAIL ') + name + (f'  ({detail})' if detail and not ok else ''), flush=True)
    if not ok:
        failures.append(name)
    return ok


def call(base: str, method: str, path: str, payload: dict | None = None, timeout: float = 30.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f'{base}{path}', data=data, method=method,
                                headers={'content-type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read().decode('utf-8', 'replace') or '{}')
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', 'replace')
        try:
            return exc.code, json.loads(body or '{}')
        except json.JSONDecodeError:
            return exc.code, {'error': body[:300]}
    except Exception as exc:                                     # noqa: BLE001
        return 0, {'error': str(exc)}


def demo_flow(base: str) -> None:
    print(f'reachability: GET {base}/mode', flush=True)
    status, mode = call(base, 'GET', '/mode')
    if not check('bridge /mode answers', status == 200 and 'mode' in mode, f'{status} {mode}'):
        return
    check('bridge reports demo mode by default', mode.get('mode') == 'demo', str(mode.get('mode')))
    check('the live-enabled venue is the one we expect',
          DEFAULT_VENUE in (mode.get('realOrderVenues') or []), str(mode.get('realOrderVenues')))

    status, menu = call(base, 'GET', '/provider/menu')
    if not check('bridge /provider/menu answers', status == 200 and menu.get('items'), f'{status}'):
        return
    items = menu['items']
    print(f'  venue: {menu.get("venue", {}).get("name")} · {len(items)} items', flush=True)
    check('menu items carry a sku and a price in sats',
          all(i.get('sku') and i.get('priceSats') for i in items),
          str([i.get('sku') for i in items[:3]]))
    item = next((i for i in items if i.get('category') == 'Burger'), items[0])
    print(f'  ordering: {item["sku"]} {item["name"]} · {item.get("price")} EUR / {item["priceSats"]} sats',
          flush=True)

    order_body = {'venue': DEFAULT_VENUE, 'items': [{'sku': item['sku'], 'qty': 1}]}
    status, created = call(base, 'POST', '/provider/orders', order_body)
    order_id = created.get('id') or created.get('order') or created.get('orderId')
    if not check('order created', status in (200, 201, 202) and bool(order_id), f'{status} {created}'):
        return
    check('order starts awaiting payment',
          created.get('status', 'awaiting_payment') == 'awaiting_payment', str(created.get('status')))
    invoice = created.get('bolt11') or created.get('invoice')
    check('the order carries a bolt11 invoice', bool(invoice) and str(invoice).startswith('ln'),
          str(invoice)[:24])
    check('the order amount matches the menu price', created.get('amountSats') == item['priceSats'],
          f'{created.get("amountSats")} vs {item["priceSats"]}')
    print(f'  order {order_id} · invoice {str(invoice)[:28]}…', flush=True)

    seen = [created.get('status')]
    order = created
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        status, order = call(base, 'GET', f'/provider/orders/{order_id}')
        state = order.get('status')
        if state and state not in seen:
            seen.append(state)
            print(f'  -> {state}', flush=True)
        if state in TERMINAL_STATES or order.get('venueOrderNumber'):
            break
        time.sleep(2)

    check('the order progressed out of awaiting_payment',
          any(s in TERMINAL_STATES for s in seen), ' -> '.join(str(s) for s in seen))
    check('the poll stayed in demo mode', order.get('mode', 'demo') == 'demo', str(order.get('mode')))
    print(f'  final: {" -> ".join(str(s) for s in seen)} · venue order {order.get("venueOrderNumber")}',
          flush=True)


def live_flow(base: str, venue: str, sku: str) -> int:
    print('=' * 72, flush=True)
    print('LIVE MODE — this creates a REAL kitchen ticket at a real venue.', flush=True)
    print('A named human must be ready to complete the Mollie checkout in a browser.', flush=True)
    print('=' * 72, flush=True)

    status, previous = call(base, 'GET', '/mode')
    print(f'restoring demo mode afterwards (was {previous.get("mode")!r})', flush=True)

    status, flip = call(base, 'POST', '/mode', {'mode': 'live'})
    if not check('bridge switched to live', status in (200, 201) and flip.get('mode') == 'live', str(flip)):
        call(base, 'POST', '/mode', {'mode': 'demo'})
        return 1

    status, created = call(base, 'POST', '/provider/orders',
                           {'venue': venue, 'items': [{'sku': sku, 'qty': 1}]})
    order_id = created.get('id') or created.get('order') or created.get('orderId')
    if not check('live order created', status in (200, 201, 202) and bool(order_id), f'{status} {created}'):
        call(base, 'POST', '/mode', {'mode': 'demo'})
        return 1
    print(f'  LIVE order {order_id} · invoice {str(created.get("bolt11") or created.get("invoice"))[:40]}…',
          flush=True)
    if created.get('error'):
        print(f'  bridge said: {created["error"]}', flush=True)

    print('\n>>> HUMAN STEP: open the checkout link and complete the card payment now.', flush=True)
    print('>>> The order is not submitted until the bridge sees it funded.\n', flush=True)
    input('Press Enter once the payment is done (or Ctrl-C to abandon)... ')

    order = created
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status, order = call(base, 'GET', f'/provider/orders/{order_id}')
        print(f'  status: {order.get("status")}', flush=True)
        if order.get('venueOrderNumber') or order.get('status') in TERMINAL_STATES | {'failed', 'cancelled'}:
            break
        time.sleep(3)

    check('live order submitted to the venue', bool(order.get('venueOrderNumber')),
          f'{order.get("status")} {order.get("error", "")}')
    print(f'  venue order number: {order.get("venueOrderNumber")}', flush=True)

    status, restored = call(base, 'POST', '/mode', {'mode': 'demo'})
    check('bridge restored to demo mode', restored.get('mode') == 'demo', str(restored))
    return 0 if not failures else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--bridge', default=DEFAULT_BRIDGE)
    parser.add_argument('--stub', action='store_true',
                        help='use the local hub stub on :8787 instead of the real bridge')
    parser.add_argument('--live', action='store_true', help='place a REAL order (human-gated)')
    parser.add_argument('--ready', action='store_true', help='required with --live')
    parser.add_argument('--confirm', default='', help='required with --live: the venue id, typed out')
    parser.add_argument('--venue', default=DEFAULT_VENUE)
    parser.add_argument('--sku', default=DEFAULT_SKU)
    args = parser.parse_args()

    base = 'http://127.0.0.1:8787' if args.stub else args.bridge.rstrip('/')

    if args.live:
        if not args.ready or args.confirm != args.venue:
            print('refusing --live without --ready and --confirm <venue id>', file=sys.stderr)
            return 2
        return live_flow(base, args.venue, args.sku)

    demo_flow(base)
    print(f'\n{len(failures)} failure(s)', flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())

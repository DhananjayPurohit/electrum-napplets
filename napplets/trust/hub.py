#!/usr/bin/env python3
"""The trust-vendor hub: npub-addressed message relay + venue-bridge proxy.

Why it exists: NAP-INC only reaches napplets inside one Electrum shell. The demo
also runs actors on a phone, so messages need a rendezvous that is addressed by
npub and reachable over plain HTTP. This is deliberately boring: standard library
only, no websocket dependency, no build step, and it can be running before the
audience sits down.

  GET  /                      a page listing the actors and their npubs
  GET  /<actor>               the built napplet, served standalone (the phone)
  POST /send                  {from, messages:[envelope, ...]}
  GET  /recv?npub=X&since=N   long-poll (≤25 s); returns messages addressed to X or "*"
  GET  /provider/menu         -> the venue bridge (CORS added)
  POST /provider/orders       -> the venue bridge
  GET  /provider/orders/<id>  -> the venue bridge
  GET  /state                 what the hub has relayed (for tests and the runbook)

The bridge is the team's live API. `--bridge <url>` points at it; `--stub` serves a
local fixture instead so tests and rehearsals never touch a real venue:

  python3 napplets/trust/hub.py --stub --port 8787
  python3 napplets/trust/hub.py --bridge https://jacksonville-foods-relates-such.trycloudflare.com

Stub mode marks every venue response `"source": "stub"`. Live mode never invents
anything: it passes through what the bridge says, including its mode and venue.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
NAPPETS = os.path.join(HERE, '..', '..', 'browser', 'napplets')

STUB_MENU = {
    'mode': 'stub', 'source': 'stub',
    'venue': {'name': 'Burgermeister SIM', 'table': 'SIMBURG1', 'currency': 'EUR'},
    'items': [
        {'id': 'sim-1001', 'name': 'SIM Hamburger', 'category': 'Burger', 'sku': 'sim-1001',
         'price': 4.37, 'priceType': 'FIAT', 'priceSats': 676, 'vatRate': 19},
        {'id': 'sim-2001', 'name': 'SIM Fries', 'category': 'Fries', 'sku': 'sim-2001',
         'price': 3.45, 'priceType': 'FIAT', 'priceSats': 533, 'vatRate': 19},
    ],
}

STUB_INVOICE = ('lnbc6760n1ptcfinalsstub' + 'q' * 120)


class Hub:
    """Messages + a venue-bridge passthrough. One lock, no cleverness."""

    def __init__(self, bridge: str | None, stub: bool):
        self.bridge = bridge
        self.stub = stub
        self.lock = threading.Condition()
        self.messages: list[dict] = []          # [{cursor, envelope}]
        self.orders: dict[str, dict] = {}
        self.polls = 0
        self.delivered = 0

    # ── messages ──────────────────────────────────────────────────────────────
    def send(self, envelopes: list[dict]) -> int:
        with self.lock:
            for envelope in envelopes:
                self.messages.append({'cursor': len(self.messages) + 1, 'envelope': envelope})
            self.lock.notify_all()
            return len(self.messages)

    def recv(self, npub: str, since: int, timeout: float = 25.0) -> dict:
        deadline = time.monotonic() + timeout
        with self.lock:
            while True:
                out = [
                    m['envelope'] for m in self.messages
                    if m['cursor'] > since
                    and m['envelope'].get('from') != npub
                    and m['envelope'].get('to') in (npub, '*', None)
                ]
                if out or time.monotonic() >= deadline:
                    self.polls += 1
                    self.delivered += len(out)
                    return {'cursor': len(self.messages), 'messages': out}
                self.lock.wait(min(1.0, max(0.05, deadline - time.monotonic())))

    # ── venue bridge ──────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, payload: dict | None) -> tuple[int, dict]:
        url = f'{self.bridge}{path}' if self.bridge else None
        if url:
            data = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(url, data=data, method=method,
                                         headers={'content-type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=25) as res:
                    body = res.read().decode('utf-8', 'replace')
                    return res.status, json.loads(body or '{}')
            except urllib.error.HTTPError as exc:
                body = exc.read().decode('utf-8', 'replace')
                try:
                    return exc.code, json.loads(body or '{}')
                except json.JSONDecodeError:
                    return exc.code, {'error': body[:300]}
            except Exception as exc:                       # noqa: BLE001
                return 502, {'error': f'bridge unreachable: {exc}'}
        return 0, {}

    def venue(self, method: str, path: str, payload: dict | None) -> tuple[int, dict]:
        if self.stub:
            return self._venue_stub(method, path, payload)
        status, json_body = self._request(method, path, payload)
        json_body.setdefault('source', 'bridge')
        return status, json_body

    def _venue_stub(self, method: str, path: str, payload: dict | None) -> tuple[int, dict]:
        if path == '/provider/menu':
            return 200, STUB_MENU
        if path == '/mode':
            return 200, {'mode': 'stub', 'source': 'stub', 'realOrderVenues': ['8613S3X'], 'venueAllowed': True}
        if path == '/provider/orders' and method == 'POST':
            body = payload or {}
            items = body.get('items') or []
            if not items:
                return 400, {'error': 'no items', 'source': 'stub'}
            order_id = body.get('orderId') or 'po-stub-0001'
            amount = sum(676 for _ in items)                       # the fixture price of sim-1001
            self.orders[order_id] = {
                'id': order_id, 'status': 'awaiting_payment', 'bolt11': STUB_INVOICE,
                'amountSats': amount, 'rail': 'stub', 'venue': body.get('venue'),
                'items': items, 'polls': 0, 'venueOrderNumber': None,
                'createdAt': time.time(), 'source': 'stub',
            }
            return 201, dict(self.orders[order_id])
        if path.startswith('/provider/orders/') and method == 'GET':
            order_id = path.rsplit('/', 1)[-1]
            order = self.orders.get(order_id)
            if not order:
                return 404, {'error': 'unknown order', 'source': 'stub'}
            order['polls'] += 1
            if order['polls'] >= 2:                          # funded, then submitted
                order['status'] = 'submitted'
                order['venueOrderNumber'] = '8613-S3X-0007'
            elif order['polls'] >= 1:
                order['status'] = 'funded'
            return 200, dict(order)
        return 404, {'error': f'no stub route for {method} {path}', 'source': 'stub'}


class Handler(BaseHTTPRequestHandler):
    server_version = 'trust-hub/1'
    hub: Hub = None            # set by main()

    def log_message(self, fmt, *args):                       # quieter, but visible
        sys.stderr.write(f'[hub] {self.address_string()} {fmt % args}\n')

    def _json(self, status: int, payload: dict | list) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(body)))
        self.send_header('access-control-allow-origin', '*')
        self.send_header('access-control-allow-headers', 'content-type')
        self.send_header('access-control-allow-methods', 'GET, POST, OPTIONS')
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header('content-type', ctype)
        self.send_header('content-length', str(len(body)))
        self.send_header('access-control-allow-origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get('content-length') or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b'{}')
        except json.JSONDecodeError:
            return {}

    # ── routes ────────────────────────────────────────────────────────────────
    def do_OPTIONS(self):                                    # noqa: N802
        self._json(204, {})

    def do_GET(self):                                        # noqa: N802
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        if path == '/recv':
            npub = (query.get('npub') or [''])[0]
            since = int((query.get('since') or ['0'])[0] or 0)
            return self._json(200, self.hub.recv(npub, since))
        if path == '/state':
            return self._json(200, {
                'messages': len(self.hub.messages), 'delivered': self.hub.delivered,
                'polls': self.hub.polls, 'orders': list(self.hub.orders.values()),
                'stub': self.hub.stub, 'bridge': self.hub.bridge,
            })
        if path in ('/provider/menu', '/mode') or path.startswith('/provider/orders/'):
            status, payload = self.hub.venue('GET', path, None)
            return self._json(status or 502, payload)
        if path == '/':
            return self._bytes(200, self._index().encode(), 'text/html; charset=utf-8')
        actor = path.strip('/')
        if actor and '/' not in actor and actor.replace('-', '').isalnum():
            file_path = os.path.join(NAPPETS, f'trust-{actor}.html')
            if os.path.exists(file_path):
                with open(file_path, 'rb') as f:
                    return self._bytes(200, f.read(), 'text/html; charset=utf-8')
        return self._json(404, {'error': 'not found'})

    def do_POST(self):                                       # noqa: N802
        path = urlsplit(self.path).path
        body = self._body()
        if path == '/send':
            messages = body.get('messages') or []
            count = self.hub.send([m for m in messages if isinstance(m, dict)])
            return self._json(200, {'ok': True, 'cursor': count})
        if path == '/provider/orders':
            status, payload = self.hub.venue('POST', path, body)
            return self._json(status or 502, payload)
        return self._json(404, {'error': 'not found'})

    def _index(self) -> str:
        actors = []
        for name in ('charlie', 'alice', 'bob', 'malice'):
            actors.append(f'<li><a href="/{name}">{name}</a> — open on the phone</li>')
        return (
            '<!doctype html><meta charset="utf-8"><title>trust-vendor hub</title>'
            '<style>body{background:#0f1115;color:#e6e9ef;font:14px system-ui;padding:24px}'
            'a{color:#f7931a}code{color:#9aa3b2}</style>'
            f'<h1>trust-vendor hub</h1>'
            f'<p>messages relays: <code>{len(self.hub.messages)}</code> · '
            f'venue: <code>{"stub" if self.hub.stub else self.hub.bridge}</code></p>'
            f'<ul>{"".join(actors)}</ul>'
            '<p>Electrum actors reach the same hub over <code>/send</code> + <code>/recv</code>; '
            'in-shell messages also go over NAP-INC.</p>'
        )


def serve(port: int, bridge: str | None, stub: bool):
    hub = Hub(bridge=bridge, stub=stub)
    Handler.hub = hub
    httpd = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return hub, httpd


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--bridge', default='https://jacksonville-foods-relates-such.trycloudflare.com',
                        help='the venue bridge base URL')
    parser.add_argument('--stub', action='store_true', help='serve the venue fixture locally')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args()
    hub, httpd = serve(args.port, None if args.stub else args.bridge, args.stub)
    where = 'stub' if args.stub else args.bridge
    print(f'hub on http://0.0.0.0:{args.port}  venue={where}')
    print(f'  actors: http://127.0.0.1:{args.port}/charlie  … /alice  … /bob  … /malice')
    try:
        while True:
            time.sleep(5)
            if not args.quiet:
                print(f'  relayed {len(hub.messages)} message(s), delivered {hub.delivered}', flush=True)
    except KeyboardInterrupt:
        print('\nstopping')
    finally:
        httpd.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

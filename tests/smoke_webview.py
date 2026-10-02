"""Drives the browser widget against a local fake shop, with a fake wallet.

Checks the three payment paths (WebLN "Pay now", lightning: link, L402 on
fetch() and on a page load) without Electrum or real sats.
Run with ./tests/run.sh
"""
import http.server
import os
import sys
import threading

from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401  (must come before QApplication)
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'browser'))
from webview import BrowserWidget, L402Tokens  # noqa: E402

PREIMAGE = 'ab' * 32
TOKEN = f'L402 mac123:{PREIMAGE}'
SCREENSHOTS = os.path.join(ROOT, 'tests', 'screenshots')

SHOP = b"""<!doctype html><html><body>
<h1>Fake shop</h1>
<button id="pay" onclick="webln.sendPayment('lnbc_webln').then(r => document.getElementById('out').textContent = 'paid ' + r.preimage)">Pay now</button>
<a id="link" href="lightning:lnbc_link">Pay with wallet</a>
<p id="out"></p>
</body></html>"""


class Shop(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, content_type='text/html', headers=()):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/shop':
            return self._send(200, SHOP)
        if self.path in ('/paid-api', '/paid-page'):
            if self.headers.get('Authorization') == TOKEN:
                if self.path == '/paid-api':
                    return self._send(200, b'{"menu": "pizza"}', 'application/json')
                return self._send(200, b'<h1 id="secret">Members only menu</h1>')
            challenge = 'L402 macaroon="mac123", invoice="lnbc_l402"'
            return self._send(402, b'Payment required', headers=[('WWW-Authenticate', challenge)])
        self._send(404, b'not found')


class FakeWallet:
    def __init__(self):
        self.paid = []

    def node_info(self):
        return {'node': {'alias': 'fake', 'pubkey': '00'}}

    def pay(self, origin, bolt11, purpose, done):
        self.paid.append((origin, bolt11, purpose))
        done({'preimage': PREIMAGE})


def main():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Shop)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'

    app = QApplication(sys.argv)
    wallet = FakeWallet()
    browser = BrowserWidget(
        wallet=wallet, tokens=L402Tokens(),
        inject_js=open(os.path.join(ROOT, 'browser', 'inject.js')).read(),
        start_html=open(os.path.join(ROOT, 'browser', 'start.html')).read())
    browser.resize(900, 600)
    browser.show()
    os.makedirs(SCREENSHOTS, exist_ok=True)

    failures = []
    steps = []

    def check(name, ok):
        print(('PASS ' if ok else 'FAIL ') + name)
        if not ok:
            failures.append(name)

    def js(code, then):
        browser.page.runJavaScript(code, 0, then)

    def later(fn, ms=1500):
        QTimer.singleShot(ms, fn)

    def next_step(*_):
        if steps:
            steps.pop(0)()
        else:
            server.shutdown()
            print(f'{len(failures)} failure(s)')
            app.exit(1 if failures else 0)

    def screenshot(name):
        browser.grab().save(os.path.join(SCREENSHOTS, name + '.png'))

    def start_page():
        later(lambda: js('typeof window.webln.sendPayment', lambda r: (
            check('start page has window.webln', r == 'function'), screenshot('start'), next_step())))

    def open_shop():
        browser.navigate(base + '/shop')
        later(next_step)

    def click_pay_now():
        js("document.getElementById('pay').click()", lambda _: later(lambda: js(
            "document.getElementById('out').textContent", lambda text: (
                check('Pay now -> wallet asked for lnbc_webln', ('http://127.0.0.1:%d' % server.server_port, 'lnbc_webln', None) in wallet.paid),
                check('Pay now -> page got the preimage', text == 'paid ' + PREIMAGE),
                screenshot('shop'), next_step()))))

    def click_lightning_link():
        js("document.getElementById('link').click()", lambda _: later(lambda: (
            check('lightning: link -> wallet asked for lnbc_link', any(p[1] == 'lnbc_link' for p in wallet.paid)),
            next_step())))

    def l402_fetch():
        js("fetch('/paid-api').then(r => r.json()).then(j => window.__menu = j.menu)", lambda _: later(lambda: js(
            'window.__menu', lambda menu: (
                check('L402 fetch -> wallet asked for lnbc_l402', any(p[1] == 'lnbc_l402' for p in wallet.paid)),
                check('L402 fetch -> retried with token and got the menu', menu == 'pizza'),
                next_step()))))

    def l402_page():
        wallet.paid.clear()
        browser.navigate(base + '/paid-page')
        later(lambda: js("document.getElementById('secret') && document.getElementById('secret').textContent",
                         lambda text: (
                             check('L402 page load -> paid', any(p[1] == 'lnbc_l402' for p in wallet.paid)),
                             check('L402 page load -> reloaded with token', text == 'Members only menu'),
                             screenshot('l402_page'), next_step())), 3000)

    steps.extend([start_page, open_shop, click_pay_now, click_lightning_link, l402_fetch, l402_page])
    browser.go_home()
    QTimer.singleShot(30_000, lambda: (print('FAIL timeout'), app.exit(2)))
    later(next_step, 1000)
    sys.exit(app.exec())


if __name__ == '__main__':
    main()

"""Runs the pizza napplets in the napplet shell and orders a pizza.

Fake wallet, real mint (whatever napplets/build.py built for; testnut by
default, which marks its invoices paid by itself). Checks the sandbox, the
handshake, the buyer <-> facilitator messages through the shell, the trust
verdict (impostor blocked, member accepted), "Pay with Electrum", and the
ecash hand-off. Run with ./tests/run.sh napplets
"""
import os
import sys

from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401  (must come before QApplication)
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'browser'))
from napplet_tab import NappletCatalog, NappletsWidget  # noqa: E402

SCREENSHOTS = os.path.join(ROOT, 'tests', 'screenshots')
failures = []


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (f'  ({detail})' if detail and not ok else ''), flush=True)
    if not ok:
        failures.append(name)


class FakeWallet:
    def __init__(self):
        self.paid = []

    def node_info(self):
        return {'node': {'alias': 'fake', 'pubkey': '00'}}

    def pay(self, origin, bolt11, purpose, done):
        self.paid.append((origin, bolt11, purpose))
        done({'preimage': '00' * 32})


def read_plugin_file(name):
    with open(os.path.join(ROOT, 'browser', name), 'rb') as f:
        return f.read()


def run(steps):
    """Drives a generator that yields ('js', frame, code) or ('sleep', ms)."""
    def advance(value=None):
        try:
            action = steps.send(value)
        except StopIteration:
            return
        if action[0] == 'js':
            action[1].runJavaScript(action[2], 0, advance)
        else:
            QTimer.singleShot(action[1], advance)
    advance()


def wait_for(frame, expression, timeout_ms=20_000):
    waited = 0
    while True:
        value = yield ('js', frame, expression)
        if value or waited >= timeout_ms:
            return value
        yield ('sleep', 250)
        waited += 250


def click(frame, selector):
    return ('js', frame, f'document.querySelector({selector!r}).click()')


def main():
    app = QApplication(sys.argv)
    wallet = FakeWallet()
    browser = NappletsWidget(
        wallet=wallet, catalog=NappletCatalog.load(read_plugin_file),
        bridge_js=read_plugin_file('napplet_bridge.js').decode())
    browser.resize(1400, 820)
    browser.show()  # the tab loads the shell when first shown
    os.makedirs(SCREENSHOTS, exist_ok=True)
    shot = lambda name: browser.grab().save(os.path.join(SCREENSHOTS, name + '.png'))

    def finish():
        print(f'{len(failures)} failure(s)', flush=True)
        app.exit(1 if failures else 0)

    def steps():
        yield ('sleep', 3000)
        # index by dTag, not by position: the catalog also carries the trust napplets
        tags = [n['dTag'] for n in NappletCatalog.load(read_plugin_file).listing()['napplets']]
        frames = browser.page.mainFrame().children()
        check('shell created one frame per catalog napplet', len(frames) == len(tags),
              f'{len(frames)} frames for {len(tags)} napplets')
        if len(frames) != len(tags):
            return finish()
        by_tag = dict(zip(tags, frames))
        if 'pizza-buyer' not in by_tag or 'pizza-facilitator' not in by_tag:
            check('pizza napplets are in the catalog', False, str(tags))
            return finish()
        buyer, fac = by_tag['pizza-buyer'], by_tag['pizza-facilitator']
        ok = yield from wait_for(buyer, 'typeof window.__buyer === "object" && window.__buyer.ready === true')
        check('buyer napplet loaded its roster', bool(ok))

        # sandbox and capabilities
        origin = yield ('js', buyer, 'window.origin')
        check('napplet has an opaque origin (no allow-same-origin)', origin == 'null', origin)
        storage = yield ('js', buyer, '(() => { try { localStorage.length; return "open" } catch (e) { return "blocked" } })()')
        check('napplet cannot use localStorage', storage == 'blocked', storage)
        caps = yield ('js', buyer, '[napplet.shell.supports("inc"), napplet.shell.supports("wallet"), typeof napplet.wallet]')
        check('buyer was granted inc + wallet', caps == [True, True, 'object'], str(caps))
        caps = yield ('js', fac, '[napplet.shell.supports("inc"), napplet.shell.supports("wallet"), typeof napplet.wallet]')
        check('facilitator was granted inc only (no wallet)', caps == [True, False, 'undefined'], str(caps))
        has_nostr = yield ('js', buyer, 'typeof window.nostr')
        check('no window.nostr in napplets', has_nostr == 'undefined', has_nostr)
        yield ('js', buyer, 'fetch("https://example.com/").then(() => window.__cspTest = "allowed", () => window.__cspTest = "blocked")')
        csp = yield from wait_for(buyer, 'window.__cspTest', 5000)
        check('CSP blocks hosts other than the mint', csp == 'blocked', csp)
        subtle = yield ('js', fac, 'typeof crypto.subtle')
        check('crypto.subtle available (secure context)', subtle == 'object', subtle)
        bridge = yield ('js', buyer, 'typeof window.__electrumNapplets + "/" + typeof window.webln + "/" + typeof window.qt')
        check('napplets see neither the shell bridge, webln nor the Qt channel',
              bridge == 'undefined/undefined/undefined', bridge)
        shot('napplets_loaded')

        # impostor: the facilitator is not in the buyer's trust set
        yield click(buyer, '#go-menu')
        yield click(buyer, '#go-choose')
        yield click(buyer, '[data-fac="outsider"]')
        yield click(buyer, '#go-vet')
        got = yield from wait_for(fac, 'window.__facilitator.order && window.__facilitator.order.orderId')
        check('ORDER reached the facilitator through the shell', got == 'PZ-4471', got)
        yield click(fac, '#prove')
        verdict = yield from wait_for(buyer, 'window.__buyer.verdict && JSON.stringify(window.__buyer.verdict)')
        check('impostor proof rejected by the buyer', bool(verdict) and '"ok":false' in verdict
              and 'outside the pinned trust set' in verdict, verdict)
        shot('napplets_impostor_blocked')

        # a vetted member
        yield click(buyer, '#retry')
        yield ('js', fac, 'const r = document.querySelector("#role"); r.value = "member:0"; r.dispatchEvent(new Event("change"))')
        yield ('js', buyer, 'window.__buyer.verdict = null')
        yield click(buyer, '[data-fac="0"]')
        yield click(buyer, '#go-vet')
        yield ('sleep', 1000)
        yield click(fac, '#prove')
        verdict = yield from wait_for(buyer, 'window.__buyer.verdict && JSON.stringify(window.__buyer.verdict)')
        check('member proof verified (ring-signature vetting)', bool(verdict) and '"ok":true' in verdict, verdict)
        yield click(buyer, '#go-pay')
        invoice = yield from wait_for(buyer, 'window.__buyer.realQuote && window.__buyer.realQuote.request')
        check('mint issued a Lightning invoice through the trust gate', bool(invoice) and invoice.startswith('ln'), invoice)
        shot('napplets_invoice')

        yield click(buyer, '#pay-electrum')
        yield ('sleep', 1000)
        check('"Pay with Electrum" reached the wallet with the mint invoice',
              len(wallet.paid) == 1 and wallet.paid[0][1] == invoice, str(wallet.paid))
        check('wallet saw which napplet asked', bool(wallet.paid) and wallet.paid[0][0] == 'Napplet "Pizza order"',
              str(wallet.paid[:1]))
        paid = yield from wait_for(buyer, 'window.__buyer.paymentConfirmed && window.__buyer.screen', 30_000)
        check('mint quote paid, order tracking shown', paid == 'track', paid)
        token = yield from wait_for(buyer, 'window.__buyer.token && window.__buyer.mintedTotal', 30_000)
        check('buyer minted the ecash', token == 27900, token)
        redeemed = yield from wait_for(fac, 'window.__facilitator.redeemedTotal', 30_000)
        check('facilitator redeemed the token handed over through the shell', bool(redeemed) and redeemed > 0, redeemed)
        shot('napplets_paid')
        finish()

    QTimer.singleShot(150_000, lambda: (print('FAIL timeout', flush=True), app.exit(2)))
    run(steps())
    sys.exit(app.exec())


if __name__ == '__main__':
    main()

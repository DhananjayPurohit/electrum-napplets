"""End-to-end smoke test for the trust napplets, in the real napplet shell.

Electrum shows two panes: the customer and one facilitator. This drives exactly
those two, in `NappletsWidget` (the widget the Napplets tab uses), with a fake
wallet and the hub's venue stub:

  two panes and no clutter · the customer has the wallet, the facilitator does
  not · the order opens to the pinned set · the facilitator proves and quotes in
  ONE action with a real ring signature · the customer verifies it against his
  own pinned set · he pays through the host wallet · the UI places the venue
  order itself and both panes announce it.

Everything asserted comes from `window.__trust`, which holds real return values,
never page text. The outsider case (a vendor who is not in the set) is not a
pane here — it is covered by tests/phone/trust-phone-flow.cjs, where all four
actors still exist. Run with ./tests/run.sh trust
"""
import os
import sys

from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401  (before QApplication)
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'browser'))
sys.path.insert(0, os.path.join(ROOT, 'napplets', 'trust'))
from napplet_tab import NappletCatalog, NappletsWidget  # noqa: E402
import hub as trust_hub                                # noqa: E402

SCREENSHOTS = os.path.join(ROOT, 'tests', 'screenshots')
PORT = 8787
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
        done({'preimage': 'ab' * 32})


def read_plugin_file(name):
    with open(os.path.join(ROOT, 'browser', name), 'rb') as f:
        return f.read()


def run(steps):
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


def wait_for(frame, expression, timeout_ms=30_000):
    waited = 0
    while True:
        value = yield ('js', frame, expression)
        if value or waited >= timeout_ms:
            return value
        yield ('sleep', 200)
        waited += 200


def js(frame, expression):
    return ('js', frame, expression)


def main():
    hub, httpd = trust_hub.serve(PORT, bridge=None, stub=True)
    print(f'hub (stub venue) on :{PORT}', flush=True)

    app = QApplication(sys.argv)
    wallet = FakeWallet()
    catalog = NappletCatalog.load(read_plugin_file)
    tags = [n['dTag'] for n in catalog.listing()['napplets']]
    print('catalog:', ', '.join(tags), flush=True)
    browser = NappletsWidget(wallet=wallet, catalog=catalog,
                             bridge_js=read_plugin_file('napplet_bridge.js').decode())
    browser.resize(1300, 820)
    browser.show()
    os.makedirs(SCREENSHOTS, exist_ok=True)
    shot = lambda name: browser.grab().save(os.path.join(SCREENSHOTS, name + '.png'))

    def finish():
        print(f'{len(failures)} failure(s)', flush=True)
        httpd.shutdown()
        app.exit(1 if failures else 0)

    def steps():
        yield ('sleep', 3500)

        # exactly two panes: the customer and the facilitator. Nothing else.
        check('the catalog is the customer and one facilitator',
              tags == ['trust-charlie', 'trust-alice'], str(tags))
        frames = browser.page.mainFrame().children()
        check('the shell shows exactly two panes (no clutter)', len(frames) == 2, f'{len(frames)} panes')
        if len(frames) != 2:
            return finish()
        charlie, alice = frames

        for tag, frame in (('customer', charlie), ('facilitator', alice)):
            ready = yield from wait_for(frame, 'window.__trust && window.__trust.ready === true')
            check(f'{tag} napplet booted', bool(ready))

        # the handshake must complete, or every later request queues forever
        handshake = {}
        for tag, frame in (('customer', charlie), ('facilitator', alice)):
            handshake[tag] = yield js(frame, 'JSON.stringify({inc: napplet.shell.supports("inc"),'
                                             ' wallet: napplet.shell.supports("wallet")})')
        print('  handshake:', handshake, flush=True)
        check('both panes completed the shell handshake',
              all('"inc":true' in v for v in handshake.values()), str(handshake))

        caps = yield js(charlie, '[napplet.shell.supports("inc"), typeof napplet.wallet, typeof napplet.wallet.pay]')
        check('the customer holds the wallet', caps == [True, 'object', 'function'], str(caps))
        caps = yield js(alice, '[napplet.shell.supports("inc"), typeof napplet.wallet]')
        check('the facilitator has no wallet at all', caps == [True, 'undefined'], str(caps))

        who = yield js(charlie, 'JSON.stringify([__trust.actor, __trust.role, __trust.npub.slice(0, 10)])')
        check('the customer pane is charlie, by npub', '"charlie"' in who and '"customer"' in who, who)
        who = yield js(alice, 'JSON.stringify([__trust.actor, __trust.role, __trust.amMember])')
        check('the facilitator pane is a member of the pinned set',
              '"alice"' in who and '"vendor"' in who and 'true' in who, who)
        size = yield js(alice, 'JSON.stringify(__trust.roster.vendors.length)')
        check('the pinned set still holds four keys (the ring minimum)', size == '4', size)

        # the order opens to the set
        yield js(charlie, '__trust.openOrder()')
        got = yield from wait_for(alice, 'window.__trust.order && window.__trust.order.orderId')
        check('the order reached the facilitator through the shell', got == 'BM-4471', str(got))
        amount = yield js(alice, 'String(__trust.order.amount)')
        check('the order carries the venue price from the bridge fixture', amount == '676', amount)
        shot('trust_1_order_open')

        # ONE action: the quote carries the proof
        yield js(alice, '__trust.proveAndQuote()')
        got = yield from wait_for(charlie, 'window.__trust.proofs.length >= 1')
        check('the customer received the quote', bool(got))
        verdict = yield js(charlie, 'JSON.stringify(__trust.proofs[0].verdict)')
        check('the quote verified against the pinned set', '"ok":true' in verdict, verdict)
        checks = yield js(charlie, 'JSON.stringify((__trust.proofs[0].checks || []).map(c => c.ok))')
        check('every verifier check passed (subset, ring size, order bound, pin, fresh)',
              checks.count('true') >= 5 and 'false' not in checks, checks)
        image = yield js(alice, 'window.__trust.proof && __trustRing.toHex(__trust.proof.signature.keyImage)')
        check('the facilitator produced a key image', bool(image) and len(str(image)) == 66, str(image)[:20])
        quote_sats = yield js(alice, 'String(__trust.quote.sats)')
        check('the proof and the quote left in one message', quote_sats == '730', quote_sats)
        shot('trust_2_quoted')

        # the customer takes it and pays through the host wallet
        yield js(charlie, '__trust.acceptBest()')
        accepted = yield js(charlie, 'JSON.stringify({sats: __trust.accepted.quote.sats, from: __trust.accepted.from})')
        alice_npub = yield js(alice, '__trust.npub')
        check('the verified quote was accepted', '"sats":730' in accepted and alice_npub in accepted, accepted)

        yield js(charlie, '__trust.pay()')
        yield ('sleep', 1200)
        check('the payment reached the wallet with the invoice',
              len(wallet.paid) == 1 and wallet.paid[0][1].startswith('lnbc'), str(wallet.paid[:1]))
        paid = yield from wait_for(charlie, 'window.__trust.paid && window.__trust.paid.preimage')
        check('the customer holds the preimage', bool(paid), str(paid))

        # the UI orders the burger itself, and both panes announce it
        created = yield from wait_for(charlie, 'window.__trust.venue.orderId', 30_000)
        check('the UI placed the venue order through the API', bool(created), str(created))
        got = yield from wait_for(charlie, 'window.__trust.venue.venueOrderNumber', 45_000)
        check('the venue confirmed the order number', got == '8613-S3X-0007', str(got))
        announce = yield from wait_for(charlie, 'window.__trust.announcement')
        check('the customer pane announces the purchase',
              bool(announce) and 'successfully' in announce, str(announce))
        vendor = yield from wait_for(alice, 'window.__trust.announcement', 25_000)
        check('the facilitator pane shows the announcement too', bool(vendor), str(vendor))
        shot('trust_3_purchased')

        state = hub.messages
        check('the hub relayed the same conversation (phone path)', len(state) > 0, f'{len(state)} messages')
        finish()

    QTimer.singleShot(180_000, lambda: (print('FAIL timeout', flush=True), httpd.shutdown(), app.exit(2)))
    run(steps())
    sys.exit(app.exec())


if __name__ == '__main__':
    main()

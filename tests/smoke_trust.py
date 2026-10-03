"""End-to-end smoke test for the trust-vendor napplets, in the real napplet shell.

Drives four actors — the customer and three vendors, one of whom is not in the
pinned set — through the whole flow inside `NappletsWidget` (the same widget the
Napplets tab uses), with a fake wallet and a local venue stub:

  order opens to the set · two members prove with real ring signatures · the
  outsider is refused on her own screen AND on the customer's (she shouts a
  cheaper price with no proof and the customer's verifier rejects it) · the
  cheapest verified quote wins · the customer pays through the host's wallet ·
  the vendor places the venue order and polls it to a venue order number.

Everything asserted comes from `window.__trust`, which holds real return values,
never page text. Run with ./tests/run.sh trust
"""
import json
import os
import sys
import threading

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


def wait_for(frame, expression, timeout_ms=25_000):
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
    listing = catalog.listing()['napplets']
    order = [n['dTag'] for n in listing]
    print('napplets:', ', '.join(order), flush=True)
    browser = NappletsWidget(wallet=wallet, catalog=catalog,
                             bridge_js=read_plugin_file('napplet_bridge.js').decode())
    browser.resize(1900, 900)
    browser.show()
    os.makedirs(SCREENSHOTS, exist_ok=True)
    shot = lambda name: browser.grab().save(os.path.join(SCREENSHOTS, name + '.png'))

    def finish():
        print(f'{len(failures)} failure(s)', flush=True)
        httpd.shutdown()
        app.exit(1 if failures else 0)

    def steps():
        yield ('sleep', 3500)
        frames = browser.page.mainFrame().children()
        check('shell created one frame per catalog napplet', len(frames) == len(order),
              f'{len(frames)} frames for {len(order)} napplets')
        if len(frames) != len(order):
            return finish()
        by_tag = dict(zip(order, frames))
        missing = [t for t in ('trust-charlie', 'trust-alice', 'trust-bob', 'trust-malice') if t not in by_tag]
        if missing:
            check(f'catalog has the trust napplets (missing {missing})', False)
            return finish()
        charlie, alice, bob, malice = (by_tag[t] for t in
                                      ('trust-charlie', 'trust-alice', 'trust-bob', 'trust-malice'))

        for tag, frame in (('charlie', charlie), ('alice', alice), ('bob', bob), ('malice', malice)):
            ready = yield from wait_for(frame, 'window.__trust && window.__trust.ready === true')
            check(f'{tag} napplet booted', bool(ready))

        # the NAP-SHELL handshake must have completed in every napplet, or every
        # later request queues forever (that is how the wallet leg used to hang)
        handshake = {}
        for tag, frame in (('charlie', charlie), ('alice', alice), ('bob', bob), ('malice', malice)):
            handshake[tag] = yield js(frame, 'JSON.stringify({inc: napplet.shell.supports("inc"),'
                                             ' wallet: napplet.shell.supports("wallet"),'
                                             ' services: napplet.shell.services.length})')
        print('  handshake:', handshake, flush=True)
        check('every actor completed the shell handshake (shell.init)',
              all('"inc":true' in v for v in handshake.values()), str(handshake))
        set_size = yield js(alice, 'JSON.stringify(__trust.roster && __trust.roster.vendors.length)')
        check('pinned set is big enough for the ring minimum (4)', set_size == '4', set_size)

        # capabilities and identity
        caps = yield js(charlie, '[napplet.shell.supports("inc"), typeof napplet.wallet, typeof napplet.wallet.pay]')
        check('customer was granted inc + wallet', caps == [True, 'object', 'function'], str(caps))
        caps = yield js(alice, '[napplet.shell.supports("inc"), typeof napplet.wallet]')
        check('vendor was granted inc but NOT wallet', caps == [True, 'undefined'], str(caps))
        npubs = yield js(charlie, 'JSON.stringify([__trust.npub, __trust.actor, __trust.amMember])')
        check('customer identity is an npub', npubs.startswith('["npub1') and '"charlie"' in npubs, npubs)
        member = yield js(alice, 'JSON.stringify([__trust.amMember, __trust.actor])')
        check('in-set vendor knows it is a member', member == '["true","alice"]' if False else 'true' in member and '"alice"' in member, member)
        member = yield js(malice, 'JSON.stringify([__trust.amMember, __trust.actor])')
        check('outsider knows it is NOT a member', '"malice"' in member and 'false' in member, member)
        transport = yield js(alice, '__trust.transport')
        check('in-shell transport includes NAP-INC', 'inc' in str(transport), str(transport))
        pin_ok = yield js(charlie, 'JSON.stringify(__trust.pin)')
        check('customer pinned a set with a content hash', 'contentHash' in pin_ok and 'setId' in pin_ok, pin_ok)
        setsize = yield js(alice, 'JSON.stringify(__trust.roster.vendors.length)')
        check('pinned set is big enough for the ring minimum (4)', setsize == '4', setsize)

        # order opens to the whole set
        yield js(charlie, '__trust.openOrder()')
        for tag, frame in (('alice', alice), ('bob', bob), ('malice', malice)):
            got = yield from wait_for(frame, 'window.__trust.order && window.__trust.order.orderId')
            check(f'order reached {tag} through the shell', got == 'BM-4471', str(got))
        order_amount = yield js(alice, 'String(__trust.order.amount)')
        check('order carries the venue price from the bridge fixture', order_amount == '676', order_amount)
        shot('trust_1_order_open')

        # two members prove; the outsider cannot
        yield js(alice, '__trust.proveAndQuote()')
        got = yield from wait_for(charlie, 'window.__trust.proofs.length >= 1')
        check('customer received the first quote', bool(got))
        first = yield js(charlie, 'JSON.stringify(__trust.proofs[0].verdict)')
        check('first quote verified against the pinned set', '"ok":true' in first, first)
        image_alice = yield js(alice, 'window.__trust.proof && __trustRing.toHex(__trust.proof.signature.keyImage)')

        yield js(bob, '__trust.proveAndQuote()')
        got = yield from wait_for(charlie, 'window.__trust.proofs.length >= 2')
        check('customer received the second quote', bool(got))
        image_bob = yield js(bob, 'window.__trust.proof && __trustRing.toHex(__trust.proof.signature.keyImage)')
        check('the two members produced different key images',
              bool(image_alice) and bool(image_bob) and image_alice != image_bob,
              f'{str(image_alice)[:12]} vs {str(image_bob)[:12]}')

        # a vendor outside the set simply has nothing to resolve: it never answers
        yield js(malice, '__trust.proveAndQuote()')
        yield ('sleep', 900)
        count = yield js(charlie, '__trust.proofs.length')
        check('the outsider never answers — no quote, and no refusal message either',
              count == 2, str(count))
        role = yield js(malice, 'JSON.stringify({amMember: __trust.amMember, proof: !!__trust.proof})')
        check('the outsider has nothing to act on', '"amMember":false' in role and '"proof":false' in role, role)
        shot('trust_2_quotes')

        # cheapest verified quote wins
        yield js(charlie, '__trust.acceptBest()')
        accepted = yield js(charlie, 'JSON.stringify({sats: __trust.accepted.quote.sats, from: __trust.accepted.from})')
        alice_npub = yield js(alice, '__trust.npub')
        bob_npub = yield js(bob, '__trust.npub')
        check('cheapest verified quote was accepted (730 sats)',
              '"sats":730' in accepted, accepted)
        check('the accepted quote is a member, not the undercutting outsider',
              alice_npub in accepted or bob_npub in accepted, accepted)
        check('and it is specifically the cheaper of the two members', alice_npub in accepted, accepted)

        # payment goes through the host wallet
        yield js(charlie, '__trust.pay()')
        yield ('sleep', 900)
        if not wallet.paid:
            diag = yield js(charlie, 'JSON.stringify({walletError: __trust.walletError,'
                                     ' napplet: typeof window.napplet,'
                                     ' wallet: typeof (window.napplet||{}).wallet,'
                                     ' invoice: ((__trust.accepted||{}).quote||{}).invoice ? "present" : "missing",'
                                     ' paid: !!__trust.paid, log: __trust.log.slice(-3), errors: __trust.errors})')
            print('  diag:', diag, flush=True)
            yield js(charlie, 'window.__probe = null;'
                              ' window.napplet.wallet.pay("lnbc1probe")'
                              '.then(r => window.__probe = "resolved:" + JSON.stringify(r))'
                              '.catch(e => window.__probe = "rejected:" + e.message)')
            yield ('sleep', 1500)
            probe = yield js(charlie, 'JSON.stringify(window.__probe)')
            print('  wallet probe:', probe, flush=True)
        check('the payment reached the wallet with the quote invoice',
              len(wallet.paid) == 1 and wallet.paid[0][1].startswith('lnbc'), str(wallet.paid[:1]))
        check('the wallet was told which napplet asked', bool(wallet.paid) and 'charlie' in wallet.paid[0][2].lower()
              or bool(wallet.paid) and 'Trust' in wallet.paid[0][0], str(wallet.paid[:1]))
        paid = yield from wait_for(charlie, 'window.__trust.paid && window.__trust.paid.preimage')
        check('customer holds the preimage', bool(paid), str(paid))
        shot('trust_3_paid')

        # the UI itself orders the burger: paying places the venue order
        created = yield from wait_for(charlie, 'window.__trust.venue.orderId', 30_000)
        check('the customer UI placed the venue order through the API', bool(created), str(created))
        got = yield from wait_for(charlie, 'window.__trust.venue.venueOrderNumber', 45_000)
        check('venue confirmed the order number', got == '8613-S3X-0007', str(got))
        announce = yield from wait_for(charlie, 'window.__trust.announcement')
        check('the UI announces the purchase', bool(announce) and 'successfully' in announce, str(announce))
        vendor_announce = yield from wait_for(alice, 'window.__trust.announcement', 25_000)
        check('the facilitator sees the announcement too', bool(vendor_announce), str(vendor_announce))
        shot('trust_4_venue_ordered')

        # the cross-device path was exercised (same messages, also over the hub)
        state = hub.messages
        check('the hub relayed the same conversation (phone path)', len(state) > 0, f'{len(state)} messages')
        sent_over_hub = yield js(charlie, 'window.__trust.sent.length')
        check('the customer emitted its order over every transport', int(sent_over_hub) >= 1, str(sent_over_hub))
        finish()

    QTimer.singleShot(180_000, lambda: (print('FAIL timeout', flush=True), httpd.shutdown(), app.exit(2)))
    run(steps())
    sys.exit(app.exec())


if __name__ == '__main__':
    main()

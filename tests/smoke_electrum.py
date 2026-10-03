"""Starts the real Electrum GUI with the Browser plugin and pays through it.

Uses a throwaway testnet wallet, offline. Lightning *sending* is stubbed,
since a fresh wallet has no channels; everything else is real: plugin
loading, the Browser tab, window.webln, the confirmation dialog, the reply
to the page. Run with ./tests/run.sh electrum
"""
import json
import os
import runpy
import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

import electrum.gui.qt as electrum_qt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCREENSHOTS = os.path.join(ROOT, 'tests', 'screenshots')
FAKE_PREIMAGE = b'\x11' * 32

failures = []


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (f'  ({detail})' if detail and not ok else ''), flush=True)
    if not ok:
        failures.append(name)


def drive(gui):
    window = gui.windows[0]
    tabs = window.tabs
    names = [tabs.tabText(i) for i in range(tabs.count())]
    check('Browser tab added to the wallet window', 'Browser' in names)
    if 'Browser' not in names:
        return finish()
    tabs.setCurrentIndex(names.index('Browser'))
    browser = tabs.widget(names.index('Browser'))

    wallet = window.wallet
    lnworker = wallet.lnworker
    key = wallet.create_request(amount_sat=21_000, message='1x Margherita pizza', exp_delay=3600, address=None)
    bolt11 = wallet.get_bolt11_invoice(wallet.get_request(key))

    async def fake_pay_invoice(invoice, **kwargs):
        return True, []
    lnworker.can_pay_invoice = lambda invoice: True
    lnworker.pay_invoice = fake_pay_invoice
    lnworker.get_preimage = lambda payment_hash: FAKE_PREIMAGE

    def pay_and_answer(answer, then):
        browser.page.runJavaScript(
            f"window.webln.sendPayment('{bolt11}')"
            ".then(r => window.__result = 'ok:' + r.preimage, e => window.__result = 'err:' + e.message)")

        def click():
            dialog = QApplication.activeModalWidget()
            if not isinstance(dialog, QMessageBox):
                check('confirmation dialog shown', False)
                return finish()
            text = dialog.text()
            check('dialog names the site, amount and item',
                  'Electrum start page' in text and '21' in text and 'Margherita' in text)
            dialog.grab().save(os.path.join(SCREENSHOTS, 'electrum_confirm.png'))
            dialog.button(answer).click()
            QTimer.singleShot(1500, lambda: browser.page.runJavaScript('window.__result', 0, then))
        QTimer.singleShot(1500, click)

    def after_yes(result):
        check('Yes -> page gets the preimage', result == 'ok:' + FAKE_PREIMAGE.hex())
        window.grab().save(os.path.join(SCREENSHOTS, 'electrum_paid.png'))
        pay_and_answer(QMessageBox.StandardButton.No, after_no)

    def after_no(result):
        check('No -> page gets "Payment declined"', result == 'err:Payment declined')
        names = [tabs.tabText(i) for i in range(tabs.count())]
        check('Napplets tab added to the wallet window', 'Napplets' in names)
        if 'Napplets' not in names:
            return finish()
        tabs.setCurrentIndex(names.index('Napplets'))  # first show loads the shell
        QTimer.singleShot(1000, lambda: napplet_pays(tabs.widget(names.index('Napplets')), 20))

    def napplet_pays(napplets, tries_left):
        # the tab opens on the first group only: the pizza buyer and facilitator
        tags = [n['dTag'] for n in napplets.catalog.listing(napplets.group)['napplets']]
        frames = napplets.page.mainFrame().children()
        if len(frames) != len(tags) and tries_left:
            return QTimer.singleShot(1000, lambda: napplet_pays(napplets, tries_left - 1))
        if len(frames) != len(tags) or tags != ['pizza-buyer', 'pizza-facilitator']:
            return napplets.page.runJavaScript("document.getElementById('log').textContent", 0, lambda status: (
                check('Napplets tab opens on just the pizza buyer + facilitator', False,
                      f'{len(frames)} frames for {tags}, shell says: {status}'),
                finish()))
        check('Napplets tab opens on just the pizza buyer + facilitator', True)
        buyer = dict(zip(tags, frames))['pizza-buyer']
        buyer.runJavaScript(
            f"napplet.wallet.pay('{bolt11}')"
            ".then(r => window.__result = 'ok:' + r.preimage, e => window.__result = 'err:' + e.message)", 0)

        def answer():
            dialog = QApplication.activeModalWidget()
            if not isinstance(dialog, QMessageBox):
                check('napplet payment shows the confirmation dialog', False)
                return finish()
            text = dialog.text()
            check('dialog names the napplet and its hash', 'Napplet "Pizza order"' in text and 'pizza-buyer' in text)
            dialog.grab().save(os.path.join(SCREENSHOTS, 'electrum_napplet_confirm.png'))
            dialog.button(QMessageBox.StandardButton.Yes).click()
            QTimer.singleShot(1500, lambda: buyer.runJavaScript('window.__result', 0, after_napplet))

        def after_napplet(result):
            check('napplet gets the preimage back', result == 'ok:' + FAKE_PREIMAGE.hex())
            window.grab().save(os.path.join(SCREENSHOTS, 'electrum_napplets.png'))
            ask_with_permission('napplet.wallet.makeInvoice({amount: 2100, memo: "Pizza tip"})', after_invoice)

        def ask_with_permission(expression, then):
            buyer.runJavaScript(f"{expression}.then(r => window.__result = JSON.stringify(r),"
                                " e => window.__result = 'err:' + e.message); window.__result = null", 0)

            def allow():
                dialog = QApplication.activeModalWidget()
                if not isinstance(dialog, QMessageBox):
                    check('napplet permission dialog shown', False)
                    return finish()
                check(f'permission dialog for {expression.split("(")[0]}', 'Napplet "Pizza order" asks to' in dialog.text(),
                      dialog.text())
                dialog.grab().save(os.path.join(SCREENSHOTS, 'electrum_napplet_permission.png'))
                dialog.button(QMessageBox.StandardButton.Yes).click()
                QTimer.singleShot(1000, lambda: buyer.runJavaScript('window.__result', 0, then))
            QTimer.singleShot(1500, allow)

        def after_invoice(result):
            invoice = json.loads(result) if result and result.startswith('{') else {}
            request = invoice.get('paymentHash') and window.wallet.get_request(invoice['paymentHash'])
            check('makeInvoice returns a real wallet invoice (testnet lntb...)',
                  invoice.get('paymentRequest', '').startswith('lntb') and bool(request)
                  and request.get_amount_sat() == 2100 and request.get_message() == 'Pizza tip', result)
            ask_with_permission('napplet.wallet.balance()', after_balance)

        def after_balance(result):
            balance = json.loads(result) if result and result.startswith('{') else {}
            check('balance returns the wallet\'s Lightning capacity',
                  set(balance) == {'canSendSats', 'canReceiveSats'}
                  and all(isinstance(v, int) for v in balance.values()), result)
            finish()

        QTimer.singleShot(1500, answer)

    window.grab().save(os.path.join(SCREENSHOTS, 'electrum_tab.png'))
    pay_and_answer(QMessageBox.StandardButton.Yes, after_yes)


def finish():
    print(f'{len(failures)} failure(s)', flush=True)
    os._exit(1 if failures else 0)


def main():
    os.makedirs(SCREENSHOTS, exist_ok=True)
    original_main = electrum_qt.ElectrumGui.main

    def main_with_driver(gui):
        QTimer.singleShot(8000, lambda: drive(gui))
        QTimer.singleShot(90_000, lambda: (print('FAIL timeout'), os._exit(2)))
        return original_main(gui)
    electrum_qt.ElectrumGui.main = main_with_driver

    sys.argv = ['run_electrum'] + sys.argv[1:]
    runpy.run_path(os.path.join(ROOT, 'electrum-src', 'run_electrum'), run_name='__main__')


if __name__ == '__main__':
    main()

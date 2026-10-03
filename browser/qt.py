import asyncio
from functools import partial
from typing import TYPE_CHECKING, Callable, Optional

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QLabel, QLineEdit, QVBoxLayout

from electrum.i18n import _
from electrum.invoices import Invoice
from electrum.lnutil import PaymentFeeBudget
from electrum.plugin import BasePlugin, hook
from electrum.util import InvoiceError, get_asyncio_loop
from electrum.gui.qt.util import Buttons, CancelButton, OkButton, WindowModalDialog, read_QIcon_from_bytes

# QtWebEngine can only be imported before the QApplication exists. Electrum loads
# enabled plugins first, so this works on startup, but not when the plugin is
# switched on in a running Electrum. In that case the tab asks for a restart.
try:
    from .webview import BrowserWidget, L402Tokens
    from .napplet_tab import NappletCatalog, NappletsWidget
except ImportError as e:
    BrowserWidget = L402Tokens = NappletCatalog = NappletsWidget = None
    WEBENGINE_ERROR = e
else:
    WEBENGINE_ERROR = None

if TYPE_CHECKING:
    from electrum.gui.qt.main_window import ElectrumWindow
    from electrum.wallet import Abstract_Wallet


class _GuiThread(QObject):
    """Runs callables on the GUI thread, from any thread."""
    _call = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self._call.connect(lambda fn: fn(), Qt.ConnectionType.QueuedConnection)

    def run(self, fn: Callable[[], None]) -> None:
        self._call.emit(fn)


class _WindowWallet:
    """What the browser in one Electrum window may do with that window's wallet."""

    def __init__(self, plugin: 'Plugin', window: 'ElectrumWindow'):
        self.plugin = plugin
        self.window = window

    def node_info(self) -> dict:
        lnworker = self.window.wallet.lnworker
        return {
            'node': {
                'alias': 'Electrum',
                'pubkey': lnworker.node_keypair.pubkey.hex() if lnworker else '',
            },
            'methods': ['getInfo', 'sendPayment'],
        }

    def allow(self, origin: str, what: str) -> bool:
        return self.window.question(
            '\n\n'.join([_('{} asks to {}.').format(origin, what), _('Allow this until Electrum is closed?')]),
            title=_('Napplet permission'))

    def make_invoice(self, amount_sat: int, memo: str) -> dict:
        wallet = self.window.wallet
        if not wallet.has_lightning():
            return {'error': _('This wallet does not have Lightning')}
        key = wallet.create_request(amount_sat=amount_sat, message=memo, exp_delay=3600, address=None)
        request = wallet.get_request(key)
        self.window.receive_tab.request_list.update()
        return {'paymentRequest': wallet.get_bolt11_invoice(request), 'paymentHash': request.rhash}

    def balance(self) -> dict:
        lnworker = self.window.wallet.lnworker
        if not lnworker:
            return {'error': _('This wallet does not have Lightning')}
        return {
            'canSendSats': int(lnworker.num_sats_can_send()),
            'canReceiveSats': int(lnworker.num_sats_can_receive()),
        }

    def pay(self, origin: str, bolt11: str, purpose: Optional[str], done: Callable[[dict], None]) -> None:
        window = self.window
        lnworker = window.wallet.lnworker
        try:
            invoice = Invoice.from_bech32(bolt11.strip())
        except InvoiceError as e:
            done({'error': f'Invalid Lightning invoice: {e}'})
            return
        error = None
        if not lnworker:
            error = _('This wallet does not have Lightning')
        elif invoice.get_amount_sat() is None:
            error = _('Invoices without an amount are not supported')
        elif invoice.has_expired():
            error = _('The invoice has expired')
        elif not lnworker.can_pay_invoice(invoice):
            error = _('Not enough Lightning balance. Your channels can send {}').format(
                window.format_amount_and_units(int(lnworker.num_sats_can_send())))
        if error:
            done({'error': error})
            return

        lines = [_('{} asks you to pay').format(origin), '', window.format_amount_and_units(invoice.get_amount_sat())]
        if invoice.get_message():
            lines += ['', invoice.get_message()]
        if purpose:
            lines += ['', purpose]
        lines += ['', _('Pay with Lightning?')]
        if not window.question('\n'.join(lines), title=_('Lightning payment')):
            done({'error': 'Payment declined'})
            return

        window.wallet.save_invoice(invoice)
        budget = PaymentFeeBudget.from_invoice_amount(
            config=self.plugin.config, invoice_amount_msat=invoice.get_amount_msat())

        async def pay() -> dict:
            success, log = await lnworker.pay_invoice(invoice, budget=budget)
            preimage = lnworker.get_preimage(bytes.fromhex(invoice.rhash))
            if success and preimage:
                return {'preimage': preimage.hex()}
            try:
                reason = log[-1].formatted_tuple()[2]
            except Exception:
                reason = _('no route found')
            return {'error': _('Payment failed: {}').format(reason)}

        def on_done(future):
            try:
                result = future.result()
            except Exception as e:
                result = {'error': _('Payment failed: {}').format(str(e) or repr(e))}
            self.plugin.gui_thread.run(partial(done, result))

        asyncio.run_coroutine_threadsafe(pay(), get_asyncio_loop()).add_done_callback(on_done)


class Plugin(BasePlugin):

    def __init__(self, parent, config, name):
        BasePlugin.__init__(self, parent, config, name)
        self.gui_thread = _GuiThread()
        self._tokens = L402Tokens() if WEBENGINE_ERROR is None else None
        self._tabs = {}  # type: dict[ElectrumWindow, list[QWidget]]
        self._napplets = None
        if WEBENGINE_ERROR is None:
            try:
                self._napplets = NappletCatalog.load(self.read_file)
            except Exception:
                self.logger.exception('no napplets loaded')

    @hook
    def load_wallet(self, wallet: 'Abstract_Wallet', window: 'ElectrumWindow'):
        if WEBENGINE_ERROR is not None:
            widget = QLabel('\n\n'.join([
                _('The browser could not start: {}').format(WEBENGINE_ERROR),
                _('If you just enabled this plugin, restart Electrum. '
                  'Otherwise install PyQt6-WebEngine and run Electrum from source.'),
            ]))
            widget.setWordWrap(True)
            widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        else:
            widget = BrowserWidget(
                wallet=_WindowWallet(self, window),
                tokens=self._tokens,
                inject_js=self.read_file('inject.js').decode('utf-8'),
                start_html=self.read_file('start.html').decode('utf-8'),
                home_url=self.config.BROWSER_HOME_URL,
            )
            widget.go_home()
        icon = read_QIcon_from_bytes(self.read_file('icon.png'))
        window.tabs.addTab(widget, icon, _('Browser'))
        self._tabs[window] = [widget]
        if self._napplets:
            napplets = NappletsWidget(
                wallet=_WindowWallet(self, window),
                catalog=self._napplets,
                bridge_js=self.read_file('napplet_bridge.js').decode('utf-8'),
                group=self._napplets.groups()[0]['key'],  # the first group, e.g. pizza buyer + facilitator
            )
            window.tabs.addTab(napplets, icon, _('Napplets'))
            self._tabs[window].append(napplets)

    @hook
    def on_close_window(self, window: 'ElectrumWindow'):
        self._tabs.pop(window, None)

    def on_close(self):
        for window, widgets in self._tabs.items():
            for widget in widgets:
                window.tabs.removeTab(window.tabs.indexOf(widget))
                widget.deleteLater()
        self._tabs.clear()

    def requires_settings(self) -> bool:
        return True

    def settings_dialog(self, window):
        d = WindowModalDialog(window, _('Browser settings'))
        vbox = QVBoxLayout(d)
        vbox.addWidget(QLabel(_('Home page (leave empty for the start page):')))
        home = QLineEdit(self.config.BROWSER_HOME_URL)
        home.setPlaceholderText('https://')
        home.setMinimumWidth(400)
        vbox.addWidget(home)
        vbox.addLayout(Buttons(CancelButton(d), OkButton(d)))
        if not d.exec():
            return
        self.config.BROWSER_HOME_URL = home.text().strip()
        for widgets in self._tabs.values():
            for widget in widgets:
                if WEBENGINE_ERROR is None and isinstance(widget, BrowserWidget):
                    widget.home_url = self.config.BROWSER_HOME_URL

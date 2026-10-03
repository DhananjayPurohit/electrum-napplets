"""The Napplets tab: a NIP-5D shell running sandboxed napplets inside Electrum.

The tab has its own web view, which only ever shows napplet_shell.html at
NAPPLET_SHELL_URL. The shell reaches the wallet through napplet_bridge.js and
`_NappletBridge` (QWebChannel); napplets reach the shell only through
postMessage from their sandboxed iframes.

Qt only, no Electrum imports. The wallet side is a `NappletWalletApi`,
implemented in qt.py.
"""
import hashlib
import json
from typing import Callable, Optional, Protocol

from PyQt6.QtCore import QFile, QIODevice, QObject, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineScript
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QVBoxLayout, QWidget

# .invalid never resolves; https makes the shell, and so its napplets, a secure context.
NAPPLET_SHELL_URL = 'https://napplets.electrum.invalid/'
MAX_INVOICE_SAT = 10_000_000


class NappletWalletApi(Protocol):
    def pay(self, origin: str, bolt11: str, purpose: Optional[str], done: Callable[[dict], None]) -> None:
        """Ask the user, pay, then call `done` on the GUI thread with {'preimage'} or {'error'}."""

    def allow(self, origin: str, what: str) -> bool:
        """Ask the user whether `origin` may `what` (e.g. 'create Lightning invoices')."""

    def make_invoice(self, amount_sat: int, memo: str) -> dict:
        """-> {'paymentRequest', 'paymentHash'} or {'error'}"""

    def balance(self) -> dict:
        """-> {'canSendSats', 'canReceiveSats'} or {'error'}"""


class NappletCatalog:
    """Local napplets the shell can run, from the plugin's napplets/ folder.

    Loaded from local files, so they are not checked against a signed
    NIP-5A manifest; the shell labels them "local · unsigned".

    Napplets that work together form a group (a catalog entry's "group", or
    else its dTag prefix: pizza-buyer -> "pizza"). The shell shows one group
    at a time.
    """
    GROUP_TITLES = {'pizza': 'Pizza order', 'trust': 'Trust vendors'}

    def __init__(self, shell_html: str, shim_js: str, napplets: list[dict]):
        self.shell_html = shell_html
        self.shim_js = shim_js
        self._napplets = {n['dTag']: n for n in napplets}

    @classmethod
    def load(cls, read_file: Callable[[str], bytes]) -> 'NappletCatalog':
        catalog = json.loads(read_file('napplets/catalog.json'))
        napplets = []
        for entry in catalog['napplets']:
            html = read_file('napplets/' + entry['file'])
            napplets.append({
                'dTag': entry['dTag'],
                'group': entry.get('group') or entry['dTag'].split('-')[0],
                'title': entry['title'],
                'domains': list(entry['domains']),
                'connect': list(entry['connect']),
                'sha256': hashlib.sha256(html).hexdigest(),
                'html': html.decode('utf-8'),
            })
        return cls(read_file('napplet_shell.html').decode('utf-8'),
                   read_file('napplet_shim.js').decode('utf-8'), napplets)

    def get(self, d_tag: str, group: str = None) -> Optional[dict]:
        napplet = self._napplets.get(d_tag)
        if napplet and group and napplet['group'] != group:
            return None
        return napplet

    def groups(self) -> list[dict]:
        """[{key, title, count}] in catalog order."""
        counts = {}
        for n in self._napplets.values():
            counts[n['group']] = counts.get(n['group'], 0) + 1
        return [{'key': key, 'title': self.GROUP_TITLES.get(key, key.capitalize()), 'count': count}
                for key, count in counts.items()]

    def listing(self, group: str = None) -> dict:
        """The napplets of `group` (all of them when None) and what the shell needs to run them."""
        fields = ('dTag', 'title', 'domains', 'connect', 'sha256')
        return {
            'napplets': [{k: n[k] for k in fields} for n in self._napplets.values()
                         if group is None or n['group'] == group],
            'groups': self.groups(),
            'group': group,
            'shim': self.shim_js,
        }


class _NappletBridge(QObject):
    """The `electrum` object napplet_bridge.js calls over QWebChannel."""
    resolved = pyqtSignal(str, str)  # request id, JSON result

    def __init__(self, tab: 'NappletsWidget'):
        super().__init__(tab)
        self._tab = tab

    def _reply(self, request_id: str) -> Callable[[dict], None]:
        return lambda result: self.resolved.emit(request_id, json.dumps(result))

    @pyqtSlot(str)
    def nappletList(self, request_id: str):
        self._tab.handle_list(self._reply(request_id))

    @pyqtSlot(str, str)
    def nappletSource(self, request_id: str, d_tag: str):
        self._tab.handle_source(d_tag, self._reply(request_id))

    @pyqtSlot(str, str)
    def nappletSelectGroup(self, request_id: str, group: str):
        self._tab.handle_select_group(group, self._reply(request_id))

    # These may open a modal dialog, so they start after the slot has returned.

    @pyqtSlot(str, str, str)
    def nappletPay(self, request_id: str, d_tag: str, bolt11: str):
        QTimer.singleShot(0, lambda: self._tab.handle_pay(d_tag, bolt11, self._reply(request_id)))

    @pyqtSlot(str, str, str, str)
    def nappletMakeInvoice(self, request_id: str, d_tag: str, amount_sat: str, memo: str):
        QTimer.singleShot(0, lambda: self._tab.handle_make_invoice(d_tag, amount_sat, memo, self._reply(request_id)))

    @pyqtSlot(str, str)
    def nappletBalance(self, request_id: str, d_tag: str):
        QTimer.singleShot(0, lambda: self._tab.handle_balance(d_tag, self._reply(request_id)))


def _qwebchannel_js() -> str:
    f = QFile(':/qtwebchannel/qwebchannel.js')
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError('qwebchannel.js not found in Qt resources')
    try:
        return bytes(f.readAll()).decode('utf-8')
    finally:
        f.close()


class NappletsWidget(QWidget):
    """Runs the napplets of one catalog group (`group`), or all of them when None."""

    def __init__(self, *, wallet: NappletWalletApi, catalog: NappletCatalog, bridge_js: str,
                 group: str = None, parent: QWidget = None):
        super().__init__(parent)
        self.wallet = wallet
        self.catalog = catalog
        self.group = group
        self._busy = False     # a dialog is open or a payment is in flight
        self._granted = set()  # (dTag, permission) the user allowed until Electrum closes
        self._loaded = False

        self.view = QWebEngineView(self)
        self.page = QWebEnginePage(self.view)
        self.view.setPage(self.page)
        main_world = QWebEngineScript.ScriptWorldId.MainWorld.value
        channel = QWebChannel(self.page)
        channel.registerObject('electrum', _NappletBridge(self))
        self.page.setWebChannel(channel, main_world)
        script = QWebEngineScript()
        script.setName('electrum-napplet-shell')
        script.setSourceCode(_qwebchannel_js() + '\n' + bridge_js)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(main_world)
        script.setRunsOnSubFrames(False)
        self.page.scripts().insert(script)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

    def showEvent(self, event):
        # start the napplets the first time the tab is shown, not at wallet load
        if not self._loaded:
            self.load_shell()
        super().showEvent(event)

    def load_shell(self) -> None:
        self._loaded = True
        self.view.setHtml(self.catalog.shell_html, QUrl(NAPPLET_SHELL_URL))

    def _from_shell(self, done: Callable[[dict], None]) -> bool:
        if self.view.url().toString() != NAPPLET_SHELL_URL:
            done({'error': 'Napplet calls are only accepted from the napplet shell'})
            return False
        return True

    def _napplet(self, d_tag: str, done: Callable[[dict], None]) -> Optional[dict]:
        if not self._from_shell(done):
            return None
        napplet = self.catalog.get(d_tag, self.group)  # only napplets of the group on screen
        if napplet is None:
            done({'error': f'Unknown napplet {d_tag!r}'})
        return napplet

    def handle_list(self, done: Callable[[dict], None]) -> None:
        if self._from_shell(done):
            done(self.catalog.listing(self.group))

    def handle_select_group(self, group: str, done: Callable[[dict], None]) -> None:
        if not self._from_shell(done):
            return
        if group not in {g['key'] for g in self.catalog.groups()}:
            done({'error': f'Unknown napplet group {group!r}'})
            return
        if self._busy:
            done({'error': 'Finish the wallet request first'})
            return
        self.group = group
        done({'group': group})
        QTimer.singleShot(0, self.load_shell)  # a fresh shell with only that group's napplets

    def handle_source(self, d_tag: str, done: Callable[[dict], None]) -> None:
        napplet = self._napplet(d_tag, done)
        if napplet:
            done({'html': napplet['html']})

    @staticmethod
    def _label(napplet: dict) -> str:
        return f'Napplet "{napplet["title"]}"'

    def _start(self, d_tag: str, done: Callable[[dict], None]) -> Optional[dict]:
        """The napplet, if the call may go ahead now; otherwise answers `done`."""
        napplet = self._napplet(d_tag, done)
        if napplet and self._busy:
            done({'error': 'Another wallet request is waiting for approval'})
            return None
        return napplet

    def _allowed(self, napplet: dict, permission: str, what: str) -> bool:
        key = (napplet['dTag'], permission)
        if key in self._granted:
            return True
        self._busy = True
        try:
            allowed = self.wallet.allow(self._label(napplet), what)
        finally:
            self._busy = False
        if allowed:
            self._granted.add(key)
        return allowed

    def handle_pay(self, d_tag: str, bolt11: str, done: Callable[[dict], None]) -> None:
        napplet = self._start(d_tag, done)
        if not napplet:
            return
        self._busy = True

        def finished(result: dict):
            self._busy = False
            done(result)

        self.wallet.pay(self._label(napplet), bolt11,
                        f'{d_tag} · local, unsigned · sha256 {napplet["sha256"][:16]}…', finished)

    def handle_make_invoice(self, d_tag: str, amount_sat: str, memo: str, done: Callable[[dict], None]) -> None:
        napplet = self._start(d_tag, done)
        if not napplet:
            return
        amount = int(amount_sat) if amount_sat.isdigit() else 0
        if not 1 <= amount <= MAX_INVOICE_SAT:
            done({'error': f'Invoice amount must be 1 to {MAX_INVOICE_SAT:,} sats'})
        elif not self._allowed(napplet, 'receive', 'create Lightning invoices that pay into this wallet'):
            done({'error': 'Not allowed by the wallet user'})
        else:
            done(self.wallet.make_invoice(amount, memo[:200]))

    def handle_balance(self, d_tag: str, done: Callable[[dict], None]) -> None:
        napplet = self._start(d_tag, done)
        if not napplet:
            return
        if not self._allowed(napplet, 'balance', 'see how much this wallet can send and receive over Lightning'):
            done({'error': 'Not allowed by the wallet user'})
        else:
            done(self.wallet.balance())

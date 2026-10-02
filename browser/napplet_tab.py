"""The Napplets tab: a NIP-5D shell running sandboxed napplets inside Electrum.

The tab has its own web view, which only ever shows napplet_shell.html at
NAPPLET_SHELL_URL. The shell reaches the wallet through napplet_bridge.js and
`_NappletBridge` (QWebChannel); napplets reach the shell only through
postMessage from their sandboxed iframes.

Qt only, no Electrum imports. The wallet side is the same WalletApi the
Browser tab uses (see webview.py and qt.py).
"""
import hashlib
import json
from typing import Callable, Optional

from PyQt6.QtCore import QFile, QIODevice, QObject, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineScript
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QVBoxLayout, QWidget

# .invalid never resolves; https makes the shell, and so its napplets, a secure context.
NAPPLET_SHELL_URL = 'https://napplets.electrum.invalid/'


class NappletCatalog:
    """Local napplets the shell can run, from the plugin's napplets/ folder.

    Loaded from local files, so they are not checked against a signed
    NIP-5A manifest; the shell labels them "local · unsigned".
    """

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
                'title': entry['title'],
                'domains': list(entry['domains']),
                'connect': list(entry['connect']),
                'sha256': hashlib.sha256(html).hexdigest(),
                'html': html.decode('utf-8'),
            })
        return cls(read_file('napplet_shell.html').decode('utf-8'),
                   read_file('napplet_shim.js').decode('utf-8'), napplets)

    def get(self, d_tag: str) -> Optional[dict]:
        return self._napplets.get(d_tag)

    def listing(self) -> dict:
        fields = ('dTag', 'title', 'domains', 'connect', 'sha256')
        return {
            'napplets': [{k: n[k] for k in fields} for n in self._napplets.values()],
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

    # Payments open a modal dialog, so they start after the slot has returned.
    @pyqtSlot(str, str, str)
    def nappletPay(self, request_id: str, d_tag: str, bolt11: str):
        QTimer.singleShot(0, lambda: self._tab.handle_pay(d_tag, bolt11, self._reply(request_id)))


def _qwebchannel_js() -> str:
    f = QFile(':/qtwebchannel/qwebchannel.js')
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError('qwebchannel.js not found in Qt resources')
    try:
        return bytes(f.readAll()).decode('utf-8')
    finally:
        f.close()


class NappletsWidget(QWidget):

    def __init__(self, *, wallet, catalog: NappletCatalog, bridge_js: str, parent: QWidget = None):
        super().__init__(parent)
        self.wallet = wallet
        self.catalog = catalog
        self._payment_pending = False
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
        napplet = self.catalog.get(d_tag)
        if napplet is None:
            done({'error': f'Unknown napplet {d_tag!r}'})
        return napplet

    def handle_list(self, done: Callable[[dict], None]) -> None:
        if self._from_shell(done):
            done(self.catalog.listing())

    def handle_source(self, d_tag: str, done: Callable[[dict], None]) -> None:
        napplet = self._napplet(d_tag, done)
        if napplet:
            done({'html': napplet['html']})

    def handle_pay(self, d_tag: str, bolt11: str, done: Callable[[dict], None]) -> None:
        napplet = self._napplet(d_tag, done)
        if not napplet:
            return
        if self._payment_pending:
            done({'error': 'Another payment is waiting for approval'})
            return
        self._payment_pending = True

        def finished(result: dict):
            self._payment_pending = False
            done(result)

        self.wallet.pay(f'Napplet "{napplet["title"]}"', bolt11,
                        f'{d_tag} · local, unsigned · sha256 {napplet["sha256"][:16]}…', finished)

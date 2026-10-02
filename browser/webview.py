"""The browser widget that lives in Electrum's Browser tab.

This module only uses Qt (no Electrum imports), so it can be exercised on its
own. The wallet side is passed in as a `WalletApi` (implemented in qt.py).

Pages reach the wallet through inject.js, which talks to `_Bridge` over
QWebChannel. Every payment goes through `BrowserWidget.request_payment`,
which hands it to the wallet; the wallet asks the user to confirm.
"""
import json
import re
from typing import Callable, Optional, Protocol
from urllib.parse import urlsplit

from PyQt6.QtCore import QObject, QUrl, QFile, QIODevice, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineScript, QWebEngineUrlRequestInterceptor
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QToolButton, QLabel, QStyle

PaymentResult = dict  # {'preimage': hex} or {'error': message}


class WalletApi(Protocol):
    def node_info(self) -> dict: ...

    def pay(self, origin: str, bolt11: str, purpose: Optional[str],
            done: Callable[[PaymentResult], None]) -> None:
        """Ask the user, pay, then call `done` on the GUI thread."""


_L402_PARAM = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def parse_l402_challenge(header: str) -> Optional[tuple[str, str]]:
    """'L402 macaroon="...", invoice="..."' -> (macaroon, invoice)"""
    scheme, _, params = header.strip().partition(' ')
    if scheme.upper() not in ('L402', 'LSAT'):
        return None
    fields = dict(_L402_PARAM.findall(params))
    macaroon = fields.get('macaroon') or fields.get('token')
    invoice = fields.get('invoice')
    if not macaroon or not invoice:
        return None
    return macaroon, invoice


def _token_key(url: str) -> str:
    # L402 tokens are scoped by path, so the query string is not part of the key
    parts = urlsplit(url)
    port = f':{parts.port}' if parts.port else ''
    return f'{parts.scheme.lower()}://{(parts.hostname or "").lower()}{port}{parts.path or "/"}'


class L402Tokens:
    """L402 credentials bought during this session, by URL."""

    def __init__(self):
        self._tokens = {}  # type: dict[str, str]

    def add(self, url: str, macaroon: str, preimage: str) -> None:
        self._tokens[_token_key(url)] = f'L402 {macaroon}:{preimage}'

    def get(self, url: str) -> Optional[str]:
        return self._tokens.get(_token_key(url))


class _Interceptor(QWebEngineUrlRequestInterceptor):
    """Adds the Authorization header to requests for URLs we hold an L402 token for."""

    def __init__(self, tokens: L402Tokens, parent=None):
        super().__init__(parent)
        self._tokens = tokens

    def interceptRequest(self, info):
        token = self._tokens.get(info.requestUrl().toString())
        if token:
            info.setHttpHeader(b'Authorization', token.encode())


class _Bridge(QObject):
    """The `electrum` object that inject.js calls over QWebChannel."""
    resolved = pyqtSignal(str, str)  # request id, JSON result

    def __init__(self, browser: 'BrowserWidget'):
        super().__init__(browser)
        self._browser = browser

    def _reply(self, request_id: str) -> Callable[[PaymentResult], None]:
        return lambda result: self.resolved.emit(request_id, json.dumps(result))

    @pyqtSlot(str)
    def getInfo(self, request_id: str):
        self._reply(request_id)(self._browser.wallet.node_info())

    # Payments open a modal dialog, so they start after the slot has returned.

    @pyqtSlot(str, str)
    def sendPayment(self, request_id: str, bolt11: str):
        QTimer.singleShot(0, lambda: self._browser.request_payment(bolt11, done=self._reply(request_id)))

    @pyqtSlot(str, str, str)
    def payL402(self, request_id: str, url: str, challenge: str):
        QTimer.singleShot(0, lambda: self._browser.pay_l402(url, challenge, done=self._reply(request_id)))


class _Page(QWebEnginePage):
    lightning_url = pyqtSignal(str)

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        if url.scheme().lower() == 'lightning':
            self.lightning_url.emit(url.toString())
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


def _qwebchannel_js() -> str:
    f = QFile(':/qtwebchannel/qwebchannel.js')
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError('qwebchannel.js not found in Qt resources')
    try:
        return bytes(f.readAll()).decode('utf-8')
    finally:
        f.close()


def _user_input_to_url(text: str) -> QUrl:
    text = text.strip()
    if '://' not in text and not text.startswith(('about:', 'data:')):
        host = text.split('/')[0].split(':')[0]
        is_local = host == 'localhost' or host.endswith('.localhost') or re.fullmatch(r'[\d.]+', host)
        text = ('http://' if is_local else 'https://') + text
    return QUrl(text)


class BrowserWidget(QWidget):

    def __init__(self, *, wallet: WalletApi, tokens: L402Tokens, inject_js: str, start_html: str,
                 home_url: str = '', parent: QWidget = None):
        super().__init__(parent)
        self.wallet = wallet
        self.home_url = home_url
        self._tokens = tokens
        self._start_html = start_html
        self._payment_pending = False

        self.view = QWebEngineView(self)
        self.page = _Page(self.view)
        self.view.setPage(self.page)
        self._interceptor = _Interceptor(tokens, self)
        self.page.setUrlRequestInterceptor(self._interceptor)

        main_world = QWebEngineScript.ScriptWorldId.MainWorld.value
        self._bridge = _Bridge(self)
        channel = QWebChannel(self.page)
        channel.registerObject('electrum', self._bridge)
        self.page.setWebChannel(channel, main_world)
        script = QWebEngineScript()
        script.setName('electrum-wallet')
        script.setSourceCode(_qwebchannel_js() + '\n' + inject_js)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(main_world)
        script.setRunsOnSubFrames(False)
        self.page.scripts().insert(script)

        self.page.lightning_url.connect(self._on_lightning_url)
        # links with target=_blank and window.open() stay in this tab
        self.page.newWindowRequested.connect(lambda request: self.page.load(request.requestedUrl()))

        self.url_bar = QLineEdit()
        self.url_bar.setPlaceholderText('Type a web address')
        self.url_bar.returnPressed.connect(lambda: self.navigate(self.url_bar.text()))
        self.view.urlChanged.connect(self._on_url_changed)
        self.status = QLabel()

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(4, 4, 8, 4)
        for icon, tooltip, action in (
                (QStyle.StandardPixmap.SP_ArrowBack, 'Back', self.view.back),
                (QStyle.StandardPixmap.SP_ArrowForward, 'Forward', self.view.forward),
                (QStyle.StandardPixmap.SP_BrowserReload, 'Reload', self.view.reload),
                (QStyle.StandardPixmap.SP_DirHomeIcon, 'Home', self.go_home)):
            button = QToolButton()
            button.setIcon(self.style().standardIcon(icon))
            button.setToolTip(tooltip)
            button.clicked.connect(action)
            toolbar.addWidget(button)
        toolbar.addWidget(self.url_bar, 1)
        toolbar.addWidget(self.status)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(toolbar)
        layout.addWidget(self.view, 1)

    def navigate(self, text: str) -> None:
        url = _user_input_to_url(text)
        if url.isValid():
            self.view.load(url)

    def go_home(self) -> None:
        if self.home_url:
            self.navigate(self.home_url)
        else:
            self.view.setHtml(self._start_html)

    def _on_url_changed(self, url: QUrl) -> None:
        self.url_bar.setText('' if url.scheme() in ('data', 'about') else url.toString())

    @staticmethod
    def origin_of(url) -> str:
        url = QUrl(url)
        if url.scheme() not in ('http', 'https'):
            return 'Electrum start page'
        port = f':{url.port()}' if url.port() != -1 else ''
        return f'{url.scheme()}://{url.host()}{port}'

    def request_payment(self, bolt11: str, *, done: Callable[[PaymentResult], None],
                        origin: str = None, purpose: str = None) -> None:
        if self._payment_pending:
            done({'error': 'Another payment is waiting for approval'})
            return
        self._payment_pending = True
        origin = origin or self.origin_of(self.view.url())
        self.status.setText(f'⚡ Payment requested by {origin}')

        def finished(result: PaymentResult):
            self._payment_pending = False
            if 'preimage' in result:
                self.status.setText('⚡ Paid')
            else:
                self.status.setText(f"⚡ {result['error']}")
            done(result)

        self.wallet.pay(origin, bolt11, purpose, finished)

    def pay_l402(self, url: str, challenge: str, *, done: Callable[[PaymentResult], None]) -> None:
        parsed = parse_l402_challenge(challenge)
        if not parsed:
            done({'error': 'Could not read the L402 challenge'})
            return
        macaroon, invoice = parsed

        def paid(result: PaymentResult):
            if 'preimage' in result:
                self._tokens.add(url, macaroon, result['preimage'])
            done(result)

        self.request_payment(invoice, done=paid, origin=self.origin_of(url), purpose=f'Access to {url}')

    def _on_lightning_url(self, url: str) -> None:
        bolt11 = re.sub(r'^lightning:(//)?', '', url, flags=re.IGNORECASE)
        self.request_payment(bolt11, done=lambda result: None)

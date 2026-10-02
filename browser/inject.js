// Injected into every page the Electrum Browser loads, after qwebchannel.js.
// Gives the page three ways to ask the wallet for a Lightning payment:
//   1. window.webln.sendPayment(invoice)      (WebLN, what "Pay now" buttons use)
//   2. clicking a <a href="lightning:..."> link
//   3. an HTTP 402 with "WWW-Authenticate: L402 ..." (fetch() calls and page loads)
// Every payment is confirmed by the user in an Electrum dialog.
(function () {
    'use strict';
    if (window.webln && window.webln.__electrum) return;

    const idPrefix = Math.random().toString(36).slice(2) + ':';
    let nextId = 0;
    const pending = new Map();
    let bridge = null;

    const ready = new Promise((resolve) => {
        function connect() {
            new QWebChannel(qt.webChannelTransport, (channel) => {
                bridge = channel.objects.electrum;
                bridge.resolved.connect((id, json) => {
                    const p = pending.get(id);
                    if (!p) return;
                    pending.delete(id);
                    const result = JSON.parse(json);
                    if (result.error) p.reject(new Error(result.error));
                    else p.resolve(result);
                });
                resolve();
            });
        }
        if (window.qt && qt.webChannelTransport) connect();
        else document.addEventListener('DOMContentLoaded', connect, { once: true });
    });

    function call(method, ...args) {
        return ready.then(() => new Promise((resolve, reject) => {
            const id = idPrefix + (++nextId);
            pending.set(id, { resolve, reject });
            bridge[method](id, ...args);
        }));
    }

    function unsupported(name) {
        return async () => { throw new Error(name + ' is not supported by Electrum'); };
    }

    // --- 1. WebLN ---------------------------------------------------------
    const webln = {
        __electrum: true,
        enabled: false,
        async enable() { this.enabled = true; },
        async isEnabled() { return this.enabled; },
        getInfo() { return call('getInfo'); },
        sendPayment(paymentRequest) { return call('sendPayment', String(paymentRequest)); },
        makeInvoice: unsupported('makeInvoice'),
        keysend: unsupported('keysend'),
        signMessage: unsupported('signMessage'),
        verifyMessage: unsupported('verifyMessage'),
    };
    Object.defineProperty(window, 'webln', { value: webln, writable: false, configurable: false });
    window.addEventListener('DOMContentLoaded', () => window.dispatchEvent(new Event('webln:ready')));

    // --- 2. lightning: links ----------------------------------------------
    document.addEventListener('click', (ev) => {
        const link = ev.target && ev.target.closest && ev.target.closest('a[href]');
        if (!link) return;
        const href = link.getAttribute('href') || '';
        if (!/^lightning:/i.test(href)) return;
        ev.preventDefault();
        ev.stopPropagation();
        webln.sendPayment(href.replace(/^lightning:(\/\/)?/i, ''))
            .then((res) => window.dispatchEvent(new CustomEvent('electrum:paid', { detail: res })))
            .catch((err) => console.warn('Electrum payment failed:', err.message));
    }, true);

    // --- 3. L402 ----------------------------------------------------------
    // The wallet pays the invoice and remembers "L402 <macaroon>:<preimage>"
    // for that URL; Electrum then adds it as the Authorization header to
    // every later request for the URL, so a plain retry is enough.
    function l402Challenge(response) {
        if (response.status !== 402) return null;
        const header = response.headers.get('WWW-Authenticate');
        return header && /^\s*(L402|LSAT)\s/i.test(header) ? header : null;
    }

    const originalFetch = window.fetch.bind(window);
    window.fetch = async function (input, init) {
        const request = new Request(input, init);
        const response = await originalFetch(request.clone());
        const challenge = l402Challenge(response);
        if (!challenge) return response;
        await call('payL402', request.url, challenge);
        return originalFetch(request);
    };

    // A page that was itself served with 402: fetch the challenge, pay, reload.
    window.addEventListener('load', async () => {
        const nav = performance.getEntriesByType('navigation')[0];
        if (!nav || nav.responseStatus !== 402) return;
        const response = await originalFetch(location.href, { credentials: 'include', cache: 'no-store' });
        const challenge = l402Challenge(response);
        if (!challenge) return;
        try {
            await call('payL402', location.href, challenge);
            location.reload();
        } catch (err) {
            console.warn('Electrum L402 payment failed:', err.message);
        }
    });
})();

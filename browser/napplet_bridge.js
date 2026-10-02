// Injected (after qwebchannel.js) into the Napplets tab only, where the one page
// ever shown is napplet_shell.html. Gives the shell window.__electrumNapplets.call(),
// which reaches NappletsWidget over QWebChannel. Napplets themselves never see it:
// they run in sandboxed iframes.
(function () {
    'use strict';
    if (location.origin !== 'https://napplets.electrum.invalid') return;

    const idPrefix = Math.random().toString(36).slice(2) + ':';
    let nextId = 0;
    const pending = new Map();
    const ready = new Promise((resolve) => {
        new QWebChannel(qt.webChannelTransport, (channel) => {
            const bridge = channel.objects.electrum;
            bridge.resolved.connect((id, json) => {
                const p = pending.get(id);
                if (!p) return;
                pending.delete(id);
                const result = JSON.parse(json);
                if (result.error) p.reject(new Error(result.error));
                else p.resolve(result);
            });
            resolve(bridge);
        });
    });

    function call(method, ...args) {
        return ready.then((bridge) => new Promise((resolve, reject) => {
            const id = idPrefix + (++nextId);
            pending.set(id, { resolve, reject });
            bridge[method](id, ...args);
        }));
    }

    Object.defineProperty(window, '__electrumNapplets', { value: Object.freeze({ call }) });
})();

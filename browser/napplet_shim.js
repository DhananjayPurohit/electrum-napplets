// window.napplet, injected by the Electrum napplet shell into each napplet
// before any napplet script runs (NIP-5D). Only the granted NAP domains are
// present; the shell substitutes that list into `domains` below.
(() => {
  "use strict";
  const shell = window.parent;
  const domains = __NAPPLET_DOMAINS__;
  let environment = null;
  const queued = [];          // capability calls made before shell.init
  const pending = new Map();  // request id -> {resolve, reject}
  const readyWaiters = [];
  const topicHandlers = new Map();
  let nextId = 0;

  function send(message) {
    if (environment || message.type === "shell.ready") shell.postMessage(message, "*");
    else queued.push(message);
  }

  function request(type, fields) {
    return new Promise((resolve, reject) => {
      const id = "r" + (++nextId);
      pending.set(id, { resolve, reject });
      send({ type, id, ...fields });
    });
  }

  window.addEventListener("message", (event) => {
    if (event.source !== shell) return;
    const message = event.data;
    if (!message || typeof message.type !== "string") return;
    if (message.type === "shell.init") {
      if (environment) return;
      environment = Object.freeze({ capabilities: message.capabilities, services: message.services || [] });
      readyWaiters.splice(0).forEach((fn) => fn(environment));
      queued.splice(0).forEach((m) => shell.postMessage(m, "*"));
      return;
    }
    if (message.type === "inc.event") {
      for (const fn of topicHandlers.get(message.topic) || [])
        fn({ topic: message.topic, sender: message.sender, payload: message.payload });
      return;
    }
    if (message.type.endsWith(".result") && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) reject(new Error(message.error));
      else resolve(message.result);
    }
  });

  const napplet = {
    shell: Object.freeze({
      supports: (domain) => !!environment && environment.capabilities.domains.includes(domain),
      get services() { return environment ? environment.services : []; },
      ready: () => environment ? Promise.resolve(environment)
        : new Promise((resolve) => readyWaiters.push(resolve)),
      onReady(handler) {
        if (environment) handler(environment); else readyWaiters.push(handler);
        return { close() {} };
      },
    }),
  };

  if (domains.includes("inc")) {
    napplet.inc = Object.freeze({
      emit(topic, payload) { send({ type: "inc.emit", topic: String(topic), payload }); },
      on(topic, handler) {
        topic = String(topic);
        if (!topicHandlers.has(topic)) topicHandlers.set(topic, new Set());
        topicHandlers.get(topic).add(handler);
        request("inc.subscribe", { topic });
        return {
          close() {
            const handlers = topicHandlers.get(topic);
            handlers.delete(handler);
            if (!handlers.size) {
              topicHandlers.delete(topic);
              send({ type: "inc.unsubscribe", topic });
            }
          },
        };
      },
    });
  }

  if (domains.includes("wallet")) {
    napplet.wallet = Object.freeze({
      // -> { preimage }. The wallet asks its user to confirm every payment.
      pay: (invoice) => request("wallet.pay", { invoice: String(invoice) }),
    });
  }

  Object.defineProperty(window, "napplet", { value: Object.freeze(napplet), configurable: false, writable: false });
  send({ type: "shell.ready" });
})();

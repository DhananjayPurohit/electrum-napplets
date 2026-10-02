// BroadcastChannel stand-in for napplets: the same postMessage / addEventListener
// API, carried over NAP-INC topics through the shell. Uint8Array values (keys,
// signatures) are tagged so they survive the JSON wire format.
function NappletChannel(topic) {
  const toHex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  const fromHex = (hex) => Uint8Array.from(hex.match(/../g) || [], (b) => parseInt(b, 16));
  const encode = (value) => JSON.parse(JSON.stringify(value,
    (key, v) => (v instanceof Uint8Array ? { __u8: toHex(v) } : v)));
  const decode = (value) => JSON.parse(JSON.stringify(value), (key, v) =>
    (v && typeof v === "object" && typeof v.__u8 === "string" && Object.keys(v).length === 1
      ? fromHex(v.__u8) : v));

  const listeners = [];
  if (window.napplet && window.napplet.inc) {
    window.napplet.inc.on(topic, (event) => {
      const data = decode(event.payload);
      for (const fn of listeners) fn({ data, sender: event.sender });
    });
  }
  return {
    postMessage(message) {
      if (window.napplet && window.napplet.inc) window.napplet.inc.emit(topic, encode(message));
    },
    addEventListener(type, fn) {
      if (type === "message") listeners.push(fn);
    },
  };
}

# Electrum Browser

A Chromium browser inside the Electrum wallet, as an Electrum plugin. It adds a
**Browser** tab next to History / Send / Receive. Websites opened there can ask the
wallet for Lightning payments, and you approve each one in an Electrum dialog.

Usually the wallet is a plugin in your browser. Here the browser is a plugin in your wallet.

## Run it

Linux or WSL (on Windows 11, WSLg shows the window). No root needed.

```bash
./setup.sh                                                  # Electrum 4.8.2 from source + Qt WebEngine
./run.sh --offline setconfig plugins.browser.enabled true   # add --testnet to both commands for testnet
./run.sh
```

You can also enable it from **Tools → Plugins → Browser**, then restart Electrum.
Chromium can only be loaded at startup, so the restart is required.

Set the page the tab opens on in the plugin's **Settings**, or:

```bash
./run.sh --offline setconfig plugins.browser.home_url https://your-shop.example
```

Leave it empty to get the built-in start page, where you can paste any invoice and press **Pay now**.

## How a website asks for a payment

Any of these work. Each one opens the Electrum confirmation dialog (site, amount, invoice description; **No** is the default button).

**WebLN**: what a "Pay now" button should use.

```js
const { preimage } = await window.webln.sendPayment(bolt11Invoice);
```

**lightning: links**

```html
<a href="lightning:lnbc...">Pay with wallet</a>
```

**L402**: respond with `402` and `WWW-Authenticate: L402 macaroon="...", invoice="..."`.
This works for `fetch()` calls and for whole pages. After paying, the browser sends
`Authorization: L402 <macaroon>:<preimage>` on later requests to that URL, so
[l402_middleware](https://github.com/DhananjayPurohit/l402_middleware)-protected routes just work.
For cross-origin `fetch()`, the server must expose the header
(`Access-Control-Expose-Headers: WWW-Authenticate`).

The wallet needs a Lightning channel with enough outbound balance. Invoices without an amount are rejected.

## Napplets

The plugin also adds a **Napplets** tab: a [NIP-5D](https://github.com/nostr-protocol/nips/pull/2303) shell that runs
sandboxed napplets inside Electrum. It ships with the pizza demo split into two napplets, the
**buyer** and the **facilitator**, side by side.

Napplets are ordinary HTML/JS. Only the host side (the tab, the bridge to the wallet) is Python, because Electrum is.

- Each napplet runs in an `<iframe sandbox="allow-scripts">` loaded via `srcdoc`, so it has an opaque origin and no storage.
  It gets a CSP whose `connect-src` allows only the mint, and no `window.nostr` or `window.webln`.
- The shell injects `window.napplet` (`napplet_shim.js`) with only the granted domains:
  - `shell`: the NAP-SHELL handshake (`shell.ready` / `shell.init`)
  - `inc`: NAP-INC topics, which carry the buyer↔facilitator messages that used to go over a BroadcastChannel.
    Payloads are forced to JSON, so no ports or other transferables cross between napplets.
  - `wallet`: Electrum's own domain. The buyer gets it; the facilitator doesn't.
- **`wallet` domain (draft, not a published NAP).** The napplet sends
  `{type: "wallet.pay", id, invoice}`. The shell replies `{type: "wallet.pay.result", id, result: {preimage}}`
  or `{..., error}`. Electrum shows its confirmation dialog naming the napplet and its sha256 before anything is paid.
- Napplets load from local files (`browser/napplets/`) and are labelled **local · unsigned**.
  Loading from a signed manifest on Nostr with Blossom hash checks (NIP-5A) is not implemented yet.

### Rebuilding the pizza napplets

`napplets/upstream/` holds the original pages and bundles from the team's flow demo, pinned by sha256.
`napplets/build.py` inlines the bundles and the roster, swaps BroadcastChannel for napplet INC, fixes the mint at
build time, and adds **Pay with Electrum** to the buyer:

```bash
python3 napplets/build.py                                            # testnut (default)
python3 napplets/build.py --mint https://cdk-a056e0f.cashu.exchange  # signet
```

- **testnut** marks its own invoices paid, and they aren't real. The whole flow runs, but Electrum has nothing real to pay.
- **The signet mint** issues real signet invoices that Electrum can pay with `./run.sh --signet` and a signet Lightning channel.
  It was down when this was built (Cloudflare error 1033).

## Files

| | |
|---|---|
| `browser/qt.py` | Electrum plugin: adds the tab, confirms and pays through the wallet's Lightning node |
| `browser/webview.py` | The browser widget, the page↔wallet bridge, L402 token store (Qt only, no Electrum imports) |
| `browser/inject.js` | Injected into every page: `window.webln`, `lightning:` link handling, L402 retry |
| `browser/start.html` | Start page with a paste-an-invoice "Pay now" |
| `browser/napplet_tab.py` | The Napplets tab: its own web view, the napplet catalog, payments for napplets |
| `browser/napplet_shell.html` | The shell: sandboxed iframes, handshake, INC relay, `wallet.pay` (JS) |
| `browser/napplet_shim.js` | `window.napplet`, injected into each napplet (JS) |
| `browser/napplet_bridge.js` | Gives the shell page (only) its channel to the wallet (JS) |
| `browser/napplets/` | The built pizza napplets and `catalog.json` (domains and allowed mint per napplet) |
| `napplets/` | Upstream sources, `build.py`, and the BroadcastChannel→INC adapter |
| `run.sh` | Links the plugin into the Electrum source tree as a built-in plugin and starts Electrum |
| `make-zip.sh` | Builds `dist/browser-<version>.zip` for Electrum's external-plugin installer |

## Tests

```bash
./tests/run.sh            # browser widget vs. a local fake shop: WebLN, lightning: link, L402 fetch + page
./tests/run.sh electrum   # real Electrum GUI, throwaway testnet wallet: tabs, dialog, Yes/No, napplet payment
./tests/run.sh napplets   # pizza napplets vs. the real mint: sandbox, CSP, vetting, Pay with Electrum, ecash hand-off
```

Prefix a command with `QT_QPA_PLATFORM=offscreen` to run it without opening windows. Screenshots go to `tests/screenshots/`.
The Electrum test stubs only the final Lightning send, because a fresh wallet has no channels.

## Limits

- **It needs Electrum run from source.** The official Electrum downloads don't bundle QtWebEngine, so the plugin can't run there.
- **External zip install** (`make-zip.sh`, then Tools → Plugins → Add) also requires Electrum's one-time plugin key, which writes `/etc/electrum/plugins_key` with sudo. `run.sh` avoids that by installing the plugin as a built-in.
- **Untrusted web pages run beside a hot wallet.** Pages run in Chromium's sandboxed renderer processes. They can only reach the wallet through `getInfo`, `sendPayment` and the L402 payment call, and every payment needs a click on **Yes**. Even so, this is a hackathon project.

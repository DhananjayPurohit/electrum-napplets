# Notes for the trust_vendor team

From the Electrum Browser plugin (Electrum 4.8.2). Two items from your handover's §5 "not proven" list that we hit too and fixed on our side.

## 1. "On-screen menu didn't fire: wallet wizard opened, not the wallet window"

On a fresh data dir, Electrum 4.8 opens the **terms-of-use wizard** before any wallet window when
`terms_of_use_accepted` is below `TERMS_OF_USE_LATEST_VERSION` (`electrum/gui/qt/__init__.py`, around line 538).
Two more first-run prompts are modal and block a scripted run: the update-check question
(`electrum/gui/qt/main_window.py`, around line 302) and the testnet warning.

Set all three through the CLI rather than hand-writing the config file (which also sidesteps the nested-dict config quirk you noted):

```bash
E="./run_electrum --testnet --offline -D /tmp/test-data"
$E create --password ''
$E setconfig terms_of_use_accepted "$(python3 -c 'from electrum.gui.messages import TERMS_OF_USE_LATEST_VERSION as v; print(v)')"
$E setconfig check_updates false
$E setconfig dont_show_testnet_warning true
$E setconfig plugins.<your_plugin>.enabled true
```

## 2. "Qt-level smoke test segfaulted offscreen"

Our test drives the real Electrum Qt GUI offscreen (`QT_QPA_PLATFORM=offscreen`) and passes reliably. The pattern:

- Wrap `electrum.gui.qt.ElectrumGui.main` so it schedules a `QTimer.singleShot(...)` driver before calling the original. The `QApplication` exists by then.
- Start Electrum in-process with `runpy.run_path('run_electrum', run_name='__main__')`, with the arguments in `sys.argv`.
- In the driver, use `gui.windows[0]` to get the wallet window, then its `tabs` and menus.
- Modal dialogs run a nested event loop, so a timer fires while one is open. Fetch it with `QApplication.activeModalWidget()`, read `.text()`, and `.button(QMessageBox.StandardButton.Yes).click()`.
- Take screenshots with `widget.grab().save(path)`.
- Finish with `os._exit(code)`. This skips Qt and Electrum teardown. We didn't reproduce your segfault, but teardown is where offscreen Qt usually crashes, and the test doesn't need it.

Reference: `tests/smoke_electrum.py` and `tests/run.sh electrum` in the Electrum Browser project.

## 3. Installable external plugin

Same wall for us: Electrum 4.8 external zips need the root-owned `/etc/electrum/plugins_key`. Our `make-zip.sh` builds a zip that Electrum's manifest reader accepts, but installing it still needs that one-time sudo step.
For the demo we install as a built-in plugin instead: a symlink into an unmodified Electrum source tree at launch, so no fork is needed.

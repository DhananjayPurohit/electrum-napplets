#!/usr/bin/env bash
# Smoke tests. Each opens a window for a few seconds; set QT_QPA_PLATFORM=offscreen to hide it.
#   ./tests/run.sh           browser widget against a fake shop and a fake wallet
#   ./tests/run.sh electrum  the plugin inside the real Electrum GUI (throwaway testnet wallet)
#   ./tests/run.sh napplets  the pizza napplets in the napplet shell, against the real mint
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
export LD_LIBRARY_PATH="$here/.syslibs/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
python="$here/.venv/bin/python"

case "${1:-}" in
    electrum) ;;
    napplets) exec "$python" "$here/tests/smoke_napplets.py" ;;
    *) exec "$python" "$here/tests/smoke_webview.py" ;;
esac

data="$here/tests/.electrum-data"
rm -rf "$data"
ln -sfn "$here/browser" "$here/electrum-src/electrum/plugins/browser"
electrum() { "$python" "$here/electrum-src/run_electrum" --testnet --offline -D "$data" "$@"; }
electrum create --password '' > /dev/null
electrum setconfig plugins.browser.enabled true
electrum setconfig check_updates false
electrum setconfig dont_show_testnet_warning true
electrum setconfig terms_of_use_accepted \
    "$("$python" -c 'from electrum.gui.messages import TERMS_OF_USE_LATEST_VERSION as v; print(v)')"
exec "$python" "$here/tests/smoke_electrum.py" --testnet --offline -D "$data" -w "$data/testnet/wallets/default_wallet"

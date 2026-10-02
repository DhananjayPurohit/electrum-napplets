#!/usr/bin/env bash
# Start Electrum from source with the Browser plugin. Arguments go to Electrum,
# e.g. ./run.sh --testnet
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)

# Install the plugin as a built-in plugin of the source checkout, which skips the
# plugin-key (sudo) step that external plugin zips need.
ln -sfn "$here/browser" "$here/electrum-src/electrum/plugins/browser"
export LD_LIBRARY_PATH="$here/.syslibs/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$here/.venv/bin/python" "$here/electrum-src/run_electrum" "$@"

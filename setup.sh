#!/usr/bin/env bash
# One-time setup: Electrum from source + Qt WebEngine (Chromium), no root needed.
set -euo pipefail
cd "$(dirname "$0")"

ELECTRUM_TAG=4.8.2

if [ ! -d electrum-src ]; then
    git clone --depth 1 --branch "$ELECTRUM_TAG" https://github.com/spesmilo/electrum.git electrum-src
fi

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -U pip wheel setuptools
# ELECTRUM_ECC_DONT_COMPILE=0 builds the bundled libsecp256k1 (needs automake, libtool)
ELECTRUM_ECC_DONT_COMPILE=0 .venv/bin/pip install -q -e "electrum-src[gui,crypto]"

# QtWebEngine must match the Qt that PyQt6 was installed with.
qt_ver=$(.venv/bin/pip show PyQt6-Qt6 | awk '/^Version:/ {print $2}')
.venv/bin/pip install -q "PyQt6-WebEngine==${qt_ver%.*}.*" "PyQt6-WebEngine-Qt6==${qt_ver}"

# Chromium needs a few system libraries that a minimal Ubuntu/WSL may lack.
# Fetch them as .debs and unpack locally instead of asking for sudo.
webengine_dir=$(.venv/bin/python -c 'import PyQt6, os; print(os.path.join(os.path.dirname(PyQt6.__file__), "Qt6"))')
missing=$(ldd "$webengine_dir/lib/libQt6WebEngineCore.so.6" "$webengine_dir/plugins/platforms/libqxcb.so" 2>/dev/null \
    | awk '/not found/ {print $1}' | sort -u)
if [ -n "$missing" ]; then
    echo "Missing system libraries: $missing"
    mkdir -p .syslibs/debs
    (cd .syslibs/debs && apt-get download libnss3 libnspr4 libxcb-cursor0 libxkbcommon-x11-0 libxcb-util1 \
        libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-xkb1 libasound2t64 2>/dev/null || true)
    for deb in .syslibs/debs/*.deb; do dpkg -x "$deb" .syslibs; done
fi

echo "Setup done. Start with ./run.sh"

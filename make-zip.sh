#!/usr/bin/env bash
# Package the plugin as an Electrum external plugin: dist/browser-<version>.zip
# Install it with Tools > Plugins > "Add plugin" in Electrum (run from source,
# since the official binaries do not include QtWebEngine).
set -euo pipefail
cd "$(dirname "$0")"
python3 - <<'EOF'
import json, os, zipfile
version = json.load(open('browser/manifest.json'))['version']
os.makedirs('dist', exist_ok=True)
path = f'dist/browser-{version}.zip'
with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
    for folder, dirs, files in os.walk('browser'):
        dirs[:] = sorted(d for d in dirs if d != '__pycache__')
        for name in sorted(files):
            z.write(os.path.join(folder, name))
print(path)
EOF

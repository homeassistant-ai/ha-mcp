#!/bin/bash
set -euo pipefail
mkdir -p /tmp/desktop-inspection
curl -fL --retry 3 https://downloads.claude.ai/claude-desktop/apt/stable/pool/main/c/claude-desktop/claude-desktop_1.46388.2_amd64.deb -o /tmp/claude.deb
echo '98bf54e85e4916068c4281459b0f0431d8ff68034773f3ee98311d7206566ab1  /tmp/claude.deb' | sha256sum -c -
dpkg-deb -x /tmp/claude.deb /tmp/claude-package
asar_path=$(find /tmp/claude-package -name app.asar -print -quit)
test -n "$asar_path"
sha256sum /tmp/claude.deb "$asar_path" > /tmp/desktop-inspection/checksums.txt
npx --yes @electron/asar@3.4.1 extract "$asar_path" /tmp/claude-source
python3 investigation/issue-2367/inspect_desktop.py /tmp/claude-source
npm install --prefix /tmp/desktop-analysis --no-audit --no-fund acorn@8.15.0 acorn-walk@8.3.4 eslint-scope@8.4.0
node investigation/issue-2367/inspect_desktop_ast.cjs
node investigation/issue-2367/extract_desktop_transport.cjs
find /tmp/claude-package -maxdepth 5 -type f -executable > /tmp/desktop-inspection/executables.txt
sudo apt-get update
sudo apt-get install -y --no-install-recommends xvfb
mkdir -p /tmp/desktop-harness
cp investigation/issue-2367/desktop_main.cjs /tmp/desktop-harness/main.cjs
cp investigation/issue-2367/desktop_renderer.html /tmp/desktop-harness/renderer.html
echo '{"name":"issue-2367-transport-harness","version":"1.0.0","main":"main.cjs"}' > /tmp/desktop-harness/package.json
npx --yes @electron/asar@3.4.1 pack /tmp/desktop-harness "$asar_path"
timeout 90 python3 investigation/issue-2367/desktop_preflight.py

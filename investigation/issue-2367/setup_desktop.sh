#!/bin/bash
set -euo pipefail
mkdir -p /tmp/desktop-inspection
curl -fL --retry 3 --retry-all-errors --connect-timeout 30 --max-time 180 https://downloads.claude.ai/claude-desktop/apt/stable/pool/main/c/claude-desktop/claude-desktop_1.46388.2_amd64.deb -o /tmp/claude.deb
echo '98bf54e85e4916068c4281459b0f0431d8ff68034773f3ee98311d7206566ab1  /tmp/claude.deb' | sha256sum -c -
dpkg-deb -x /tmp/claude.deb /tmp/claude-package
asar_path=$(find /tmp/claude-package -name app.asar -print -quit)
test -n "$asar_path"
sha256sum /tmp/claude.deb "$asar_path" > /tmp/desktop-inspection/checksums.txt
npx --yes @electron/asar@3.4.1 extract "$asar_path" /tmp/claude-source
python3 investigation/issue-2367/inspect_desktop.py /tmp/claude-source
npm install --prefix /tmp/desktop-analysis --no-audit --no-fund acorn@8.15.0 acorn-walk@8.3.4 eslint-scope@8.4.0 @electron/asar@3.4.1
python3 investigation/issue-2367/inspect_session.py
if [ "${REPRO_SESSION_MODE:-false}" = true ]; then python3 investigation/issue-2367/inspect_web_client.py; fi
node investigation/issue-2367/inspect_desktop_ast.cjs
node investigation/issue-2367/extract_desktop_transport.cjs
node investigation/issue-2367/extract_session.cjs
# Official current MSIX, or the Sep 2 release preceding the issue report.
case "${WINDOWS_VERSION:-1.46388.4}" in
  1.46388.4)
    desktop_windows_url=https://downloads.claude.ai/releases/win32/x64/1.46388.4/Claude-50e62f90a2c85243eef42913398f7c8f1534abef.msix
    desktop_windows_sha=f3925248cf40b46c59043878b4c4f1835e7687082b5a52217bd72b73bfbf0b12
    ;;
  1.44121.2)
    desktop_windows_url=https://downloads.claude.ai/releases/win32/x64/1.44121.2/Claude-817a7b4563855a33d4b678faefc71f87554445d8.msix
    desktop_windows_sha=38aa4ebd4a2a91a64c696a89a3d4011955a0555007046050ef73b6fba3b007d6
    ;;
  *) echo 'Unknown Windows version' >&2; exit 1 ;;
esac
curl -fL --retry 3 --retry-all-errors --connect-timeout 30 --max-time 180 "$desktop_windows_url" -o /tmp/claude-windows.msix
echo "$desktop_windows_sha  /tmp/claude-windows.msix" | sha256sum -c -
sha256sum /tmp/claude-windows.msix >> /tmp/desktop-inspection/checksums.txt
python3 - <<'EXTRACT_WINDOWS'
import zipfile
from pathlib import Path
with zipfile.ZipFile('/tmp/claude-windows.msix') as z:
    names=[n for n in z.namelist() if n.endswith('/resources/app.asar')]
    assert len(names)==1,names
    Path('/tmp/claude-windows.asar').write_bytes(z.read(names[0]))
    prefix=names[0]+'.unpacked/'
    for name in z.namelist():
        if name.startswith(prefix) and not name.endswith('/'):
            target=Path('/tmp/claude-windows.asar.unpacked')/name[len(prefix):]
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(z.read(name))
EXTRACT_WINDOWS
# Only JavaScript and package metadata are needed for static comparison.
# The Windows archive references a native binding absent under that filename.
node - <<'WINDOWS_SOURCE'
const fs=require('node:fs'),path=require('node:path');
const asar=require('/tmp/desktop-analysis/node_modules/@electron/asar');
for(const entry of asar.listPackage('/tmp/claude-windows.asar')) {
  const name=entry.replace(/^\//,'');
  if(name!=='package.json'&&!(name.startsWith('.vite/build/')&&name.endsWith('.js')))continue;
  const target=path.join('/tmp/claude-windows-source',name);
  fs.mkdirSync(path.dirname(target),{recursive:true});
  fs.writeFileSync(target,asar.extractFile('/tmp/claude-windows.asar',name));
}
WINDOWS_SOURCE
node investigation/issue-2367/compare_desktop.cjs
find /tmp/claude-package -maxdepth 5 -type f -executable > /tmp/desktop-inspection/executables.txt
sudo apt-get update
sudo apt-get install -y --no-install-recommends xvfb
mkdir -p /tmp/desktop-harness
cp investigation/issue-2367/desktop_main.cjs /tmp/desktop-harness/transport-main.cjs
cp investigation/issue-2367/desktop_session_main.cjs /tmp/desktop-harness/session-main.cjs
cp investigation/issue-2367/desktop_session_renderer.html /tmp/desktop-harness/session-renderer.html
printf '%s\n' 'require(process.env.REPRO_SESSION_MODE==="true"?"./session-main.cjs":"./transport-main.cjs");' > /tmp/desktop-harness/main.cjs
cp investigation/issue-2367/desktop_renderer.html /tmp/desktop-harness/renderer.html
echo '{"name":"issue-2367-transport-harness","version":"1.0.0","main":"main.cjs"}' > /tmp/desktop-harness/package.json
npx --yes @electron/asar@3.4.1 pack /tmp/desktop-harness "$asar_path"
timeout 90 python3 investigation/issue-2367/desktop_preflight.py

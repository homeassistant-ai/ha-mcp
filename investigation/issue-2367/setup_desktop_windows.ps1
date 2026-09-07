# Manual investigation only. No HA credentials or authenticated Desktop session.
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$reproRepo = $env:GITHUB_WORKSPACE
New-Item -ItemType Directory -Force C:/tmp/desktop-inspection, C:/tmp/desktop-harness | Out-Null
Set-Location C:/
function Fetch-Pinned($url, $destination, $expected) {
  curl.exe -fL --retry 3 --retry-all-errors --connect-timeout 30 --max-time 180 $url -o $destination
  $actual = (Get-FileHash $destination -Algorithm SHA256).Hash.ToLower()
  if ($actual -ne $expected) { throw "Checksum mismatch for $destination" }
  "$actual  $destination" | Add-Content C:/tmp/desktop-inspection/checksums.txt
}
Fetch-Pinned 'https://downloads.claude.ai/releases/win32/x64/1.46388.4/Claude-50e62f90a2c85243eef42913398f7c8f1534abef.msix' 'C:/tmp/claude-windows.zip' 'f3925248cf40b46c59043878b4c4f1835e7687082b5a52217bd72b73bfbf0b12'
Expand-Archive C:/tmp/claude-windows.zip C:/tmp/claude-msix
$reproAsar = @(Get-ChildItem C:/tmp/claude-msix -Filter app.asar -Recurse)
if ($reproAsar.Count -ne 1) { throw 'Expected one Desktop archive' }
Copy-Item $reproAsar[0].FullName C:/tmp/claude-windows.asar
npm.cmd install --prefix C:/tmp/desktop-analysis --no-audit --no-fund acorn@8.15.0 acorn-walk@8.3.4 eslint-scope@8.4.0 @electron/asar@3.4.1
@'
const fs=require('node:fs'),path=require('node:path');
const asar=require('C:/tmp/desktop-analysis/node_modules/@electron/asar');
for(const entry of asar.listPackage('C:/tmp/claude-windows.asar')) {
  const name=entry.replace(/^[/\\]+/,'').replace(/\\/g,'/');
  if(name!=='package.json'&&!(name.startsWith('.vite/build/')&&name.endsWith('.js')))continue;
  const target=path.join('C:/tmp/claude-source',name);
  fs.mkdirSync(path.dirname(target),{recursive:true});
  fs.writeFileSync(target,asar.extractFile('C:/tmp/claude-windows.asar',path.normalize(name)));
}
'@ | Set-Content -Encoding utf8 C:/tmp/extract-windows.cjs
node C:/tmp/extract-windows.cjs
$env:REPRO_PLATFORM='win32'
# Keep script resolution on C: alongside the extracted dependency tree.
Copy-Item "$reproRepo/investigation/issue-2367/extract_desktop_transport.cjs" C:/tmp/extract-transport.cjs
node C:/tmp/extract-transport.cjs
Fetch-Pinned 'https://github.com/electron/electron/releases/download/v42.10.0/electron-v42.10.0-win32-x64.zip' 'C:/tmp/electron.zip' '6988553dc944800c127f6600133b9dd7810a83a82b9b68d2faa07dbb10ef5071'
Expand-Archive C:/tmp/electron.zip C:/tmp/electron
Copy-Item "$reproRepo/investigation/issue-2367/desktop_main.cjs" C:/tmp/desktop-harness/main.cjs
Copy-Item "$reproRepo/investigation/issue-2367/desktop_renderer.html" C:/tmp/desktop-harness/renderer.html
'{"name":"issue-2367-windows-transport","version":"1.0.0","main":"main.cjs"}' | Set-Content -Encoding utf8 C:/tmp/desktop-harness/package.json
& C:/tmp/desktop-analysis/node_modules/.bin/asar.cmd pack C:/tmp/desktop-harness C:/tmp/electron/resources/app.asar
$env:DESKTOP_BINARY='C:/tmp/electron/electron.exe'
python "$reproRepo/investigation/issue-2367/desktop_preflight.py"

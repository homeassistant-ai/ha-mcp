"""Run only on GitHub: summarize bundled Desktop transport source for analysis."""
import hashlib
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
out = Path('/tmp/desktop-inspection')
out.mkdir(parents=True, exist_ok=True)
patterns = [
    'transportToClient', 'transportToServer', 'spawnLocalProcess',
    'class StdioClientTransport', 'readMessage()', 'processReadBuffer',
    'claude.web_$_Mcp', 'tools/call', '240000', 'Request timed out',
    'mcp__', 'JSON.stringify(message)', 'writeMessage(',
]
manifest = []
with (out / 'snippets.txt').open('w') as report:
    for path in sorted(root.rglob('*.js')):
        if 'node_modules' in path.parts:
            continue
        source = path.read_text(errors='replace')
        matches = [(pattern, list(re.finditer(re.escape(pattern), source))) for pattern in patterns]
        if not any(hits for _, hits in matches):
            continue
        manifest.append({'file': str(path.relative_to(root)), 'bytes': path.stat().st_size,
                         'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                         'hits': {p: len(h) for p, h in matches if h}})
        for pattern, hits in matches:
            for match in hits[:5]:
                report.write(f'\nFILE {path.relative_to(root)} PATTERN {pattern} OFFSET {match.start()}\n')
                report.write(source[max(0, match.start()-900):match.end()+1700] + '\n')
(out / 'manifest.json').write_text(json.dumps(manifest, indent=2))
for path in root.rglob('package.json'):
    if path.parent == root:
        (out / 'desktop-package.json').write_bytes(path.read_bytes())

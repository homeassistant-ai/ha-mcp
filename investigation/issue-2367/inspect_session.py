"""Runner-only inventory of Desktop's chat-facing session and preload code."""
import hashlib
import json
import re
import shutil
from pathlib import Path

root=Path('/tmp/claude-source')
out=Path('/tmp/desktop-inspection/session-source')
out.mkdir(parents=True,exist_ok=True)
terms=['preload','contextBridge','exposeInMainWorld','rendererMessagePort','mcp:','mcp-','Mcp','mcpServers','No result received','240000','claude.web','connectToServer','tool_use','tool_result']
manifest=[]
for p in sorted(root.rglob('*')):
    if not p.is_file() or 'node_modules' in p.parts or p.suffix not in ('.js','.cjs','.mjs','.html'):continue
    s=p.read_text(errors='replace')
    hits={t:s.count(t) for t in terms if t in s}
    if not hits and 'preload' not in p.name.lower():continue
    relative=str(p.relative_to(root))
    copied=('preload' in p.name.lower() or 'contextBridge' in hits or 'rendererMessagePort' in hits or ('mcp:' in hits) or ('mcp-' in hits))
    if copied:
        dst=out/relative
        dst.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(p,dst)
    manifest.append({'file':relative,'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'hits':hits,'copied':copied})
Path('/tmp/desktop-inspection/session-manifest.json').write_text(json.dumps(manifest,indent=2))
print('Session source inventory:',len(manifest),'files;',sum(e['copied'] for e in manifest),'retained')

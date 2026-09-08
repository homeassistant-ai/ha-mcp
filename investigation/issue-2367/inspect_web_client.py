"""GitHub-only inspection of anonymously public Claude web assets; no login."""
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

out=Path('/tmp/desktop-inspection/web-client')
out.mkdir(parents=True,exist_ok=True)
manifest=[]
def fetch(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'ha-mcp-issue-2367-public-source-inspection'}),timeout=20) as response:
            body=response.read(6*1024*1024)
            return response.status,body
    except urllib.error.HTTPError as error:
        return error.code,b''
    except Exception as error:
        manifest.append({'url':url,'error':type(error).__name__})
        return 0,b''
status,body=fetch('https://claude.ai/new')
manifest.append({'url':'https://claude.ai/new','status':status,'bytes':len(body)})
if status==200:
    html=body.decode(errors='replace')
    (out/'public-page.html').write_text(html)
    urls=list(dict.fromkeys(urllib.parse.urljoin('https://claude.ai/new',u) for u in re.findall(r'<script[^>]+src=[\"\']([^\"\']+)',html)))
    for index,url in enumerate(urls[:30]):
        host=urllib.parse.urlparse(url).hostname or ''
        if host!='claude.ai' and not host.endswith('.claude.ai'):continue
        code,data=fetch(url)
        text=data.decode(errors='replace')
        terms=['claudeAppBindings','mcp-server-connected','No result received','tools/call','tool_use','tool_result','240000']
        hits={term:text.count(term) for term in terms if term in text}
        manifest.append({'url':url,'status':code,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'hits':hits})
        if hits:(out/f'chunk-{index}.js').write_bytes(data)
else:
    manifest.append({'note':'Anonymous page unavailable. No credentials supplied and no access-control workarounds attempted.'})
(out/'manifest.json').write_text(json.dumps(manifest,indent=2))
print('Public chat source inspection:',status,';',len(manifest),'records')

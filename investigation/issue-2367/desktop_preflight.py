"""GitHub-only smoke test of actual Desktop transport with a tiny echo child."""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

out=Path('/tmp/desktop-inspection')
config=out/'smoke-config.json'
config.write_text(json.dumps({'mcpServers':{'primary':{'command':sys.executable,'args':['-u','-c',
    'import sys,json\nsys.stdin.reconfigure(encoding="utf-8");sys.stdout.reconfigure(encoding="utf-8")\nfor line in sys.stdin:\n m=json.loads(line);print(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":{"echo":m["params"]}}),flush=True)']}}}))
env={**os.environ,'REPRO_DESKTOP_CONFIG':str(config),'REPRO_DESKTOP_LOG':str(out/'smoke-trace.jsonl'),'REPRO_DESKTOP_PROFILE':'/tmp/desktop-smoke-profile'}
with (out/'electron-stderr.txt').open('w') as stderr:
    command=([os.environ['DESKTOP_BINARY'],'--no-sandbox'] if os.name=='nt' else ['xvfb-run','-a','/tmp/claude-package/usr/lib/claude-desktop/claude-desktop','--no-sandbox'])
    p=subprocess.Popen(command,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=stderr,text=True,encoding='utf-8')
    watchdog=threading.Timer(120,p.kill)
    watchdog.start()
    try:
        with (out/'electron-stdout.txt').open('w') as startup:
            for line in p.stdout:
                startup.write(line);startup.flush()
                if line.startswith('{'):
                    ready=json.loads(line)
                    break
            else:
                raise RuntimeError('Electron exited before ready')
        (out/'smoke-ready.json').write_text(json.dumps(ready,indent=2))
        assert ready['type']=='ready',ready
        value={'python_transform':"{% if something %} {{ states('sensor.test') }} % ü"*2000}
        p.stdin.write(json.dumps({'jsonrpc':'2.0','id':1,'method':'echo','params':value})+'\n');p.stdin.flush()
        result=json.loads(p.stdout.readline())
        assert result['result']['echo']==value,result
        p.stdin.close()
        assert p.wait(timeout=20)==0
        (out/'smoke-pass.txt').write_text('Exact bundled transport and real Electron/Chromium ports preserved a large Unicode/template payload.\n')
    finally:
        watchdog.cancel()
        if p.poll() is None:p.kill()

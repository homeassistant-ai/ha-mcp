"""GitHub-only smoke test of Desktop transport with a tiny echo child."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

out=Path('/tmp/desktop-inspection')
config=out/'smoke-config.json'
config.write_text(json.dumps({'mcpServers':{'primary':{'command':sys.executable,'args':['-u','-c',
    'import sys,json\nsys.stdin.reconfigure(encoding="utf-8");sys.stdout.reconfigure(encoding="utf-8")\nfor line in sys.stdin:\n m=json.loads(line);print(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":{"echo":m["params"]}}),flush=True)']}}}))
env={**os.environ,'REPRO_DESKTOP_CONFIG':str(config),'REPRO_DESKTOP_LOG':str(out/'smoke-trace.jsonl'),'REPRO_DESKTOP_PROFILE':str(Path('/tmp/desktop-smoke-profile').resolve())}
windows=os.name=='nt'
control_file=out/'smoke-control.json'
if windows:
    control_file.unlink(missing_ok=True)
    env['REPRO_CONTROL_FILE']=str(control_file.resolve())
command=([os.environ['DESKTOP_BINARY'],'--no-sandbox'] if windows else ['xvfb-run','-a','/tmp/claude-package/usr/lib/claude-desktop/claude-desktop','--no-sandbox'])
with (out/'electron-stderr.txt').open('w') as stderr, (out/'electron-native-stdout.txt').open('w') as native_stdout:
    p=subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL if windows else subprocess.PIPE,
        stdout=native_stdout if windows else subprocess.PIPE,stderr=stderr,text=True,encoding='utf-8')
    watchdog=threading.Timer(120,p.kill)
    watchdog.start()
    control=None
    try:
        reader,writer=p.stdout,p.stdin
        if windows:
            # Windows GUI processes do not provide the harness's console stdin.
            # This loopback socket replaces ONLY the external test-controller link;
            # Desktop's extracted child-process stdio transport remains unchanged.
            deadline=time.monotonic()+90
            while not control_file.exists():
                if p.poll() is not None or time.monotonic()>deadline:raise RuntimeError('No Desktop control endpoint')
                time.sleep(0.05)
            control=socket.create_connection(('127.0.0.1',json.loads(control_file.read_text())['port']),timeout=90)
            reader=control.makefile('r',encoding='utf-8')
            writer=control.makefile('w',encoding='utf-8',newline='\n')
        with (out/'electron-stdout.txt').open('w') as startup:
            for line in reader:
                startup.write(line);startup.flush()
                if line.startswith('{'):
                    ready=json.loads(line)
                    break
            else:
                raise RuntimeError('Electron exited before ready')
        (out/'smoke-ready.json').write_text(json.dumps(ready,indent=2))
        assert ready['type']=='ready',ready
        value={'python_transform':"{% if something %} {{ states('sensor.test') }} % ü"*2000}
        writer.write(json.dumps({'jsonrpc':'2.0','id':1,'method':'echo','params':value})+'\n');writer.flush()
        result=json.loads(reader.readline())
        assert result['result']['echo']==value,result
        writer.close()
        if control:
            control.shutdown(socket.SHUT_WR)
            reader.close()
            control.close()
        assert p.wait(timeout=20)==0
        (out/'smoke-pass.txt').write_text('Extracted Desktop transport and real Electron/Chromium ports preserved a large Unicode/template payload.\n')
    finally:
        watchdog.cancel()
        if p.poll() is None:p.kill()
        if control:control.close()

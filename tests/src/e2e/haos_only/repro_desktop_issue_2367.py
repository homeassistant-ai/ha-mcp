"""Branch-only reproduction through actual Desktop Electron + extracted transport."""
import asyncio
import contextlib
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from ruamel.yaml import YAML

from .repro_issue_2367 import (
    DATA, EXACT_TRANSFORM, attempt, call, get_dashboard, make_client, record,
)

pytestmark=[pytest.mark.haos_embedded_only,pytest.mark.timeout(1200)]


class DesktopClient:
    def __init__(self,url,folder,dual,protocol):
        self.url=url
        self.folder=folder
        self.dual=dual
        self.protocol=protocol
        self.pending={}
        self.sequence=0
        self.secondary_reads=0

    async def __aenter__(self):
        self.folder.mkdir(parents=True)
        uvx=shutil.which('uvx')
        assert uvx
        server={'command':uvx,'args':['--from','fastmcp-remote==4.0.3','fastmcp-remote',self.url,'--auth','none'],
                'env':{'PATH':os.environ['PATH']}}
        config=self.folder/'config.json'
        config.write_text(json.dumps({'mcpServers':{'primary':server,**({'secondary':server} if self.dual else {})}}))
        self.stderr=(self.folder/'electron-stderr.txt').open('w')
        env={**os.environ,'REPRO_DESKTOP_CONFIG':str(config),
             'REPRO_DESKTOP_LOG':str(self.folder/'transport.jsonl'),
             'REPRO_DESKTOP_PROFILE':str(self.folder/'profile')}
        self.ready=asyncio.get_running_loop().create_future()
        self.proc=await asyncio.create_subprocess_exec('xvfb-run','-a',
            '/tmp/claude-package/usr/lib/claude-desktop/claude-desktop','--no-sandbox',
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=self.stderr,env=env,limit=1024*1024)
        self.reader=asyncio.create_task(self._read())
        async with asyncio.timeout(90):
            ready=await self.ready
        record('desktop_ready',folder=self.folder.name,dual=self.dual,versions=ready['versions'])
        for route in ['primary']+(['secondary'] if self.dual else []):
            result=await self._rpc('initialize',{'protocolVersion':self.protocol,
                'capabilities':{'roots':{'listChanged':True}},
                'clientInfo':{'name':'desktop-transport-reproduction','version':'1.46388.2'}},route)
            record('desktop_initialized',route=route,requested=self.protocol,negotiated=result['protocolVersion'])
            self.proc.stdin.write((json.dumps({'route':route,'jsonrpc':'2.0','method':'notifications/initialized'})+'\n').encode())
            await self.proc.stdin.drain()
            tools=await self._rpc('tools/list',{},route)
            assert any(t['name']=='ha_config_set_dashboard' for t in tools['tools'])
        self.background=asyncio.create_task(self._secondary()) if self.dual else None
        return self

    async def _read(self):
        try:
            while line:=await self.proc.stdout.readline():
                message=json.loads(line)
                if message.get('type')=='ready':
                    self.ready.set_result(message)
                elif message.get('type') in ('fatal','transport_error'):
                    raise RuntimeError(message)
                elif 'id' in message:
                    key=(message.get('route','primary'),message['id'])
                    future=self.pending.get(key)
                    if future and not future.done():
                        if 'error' in message:future.set_exception(RuntimeError(message['error']))
                        else:future.set_result(message['result'])
            raise RuntimeError('Desktop stdout closed')
        except Exception as exc:
            if not self.ready.done():self.ready.set_exception(exc)
            for future in self.pending.values():
                if not future.done():future.set_exception(exc)

    async def _rpc(self,method,params,route='primary'):
        self.sequence+=1
        ident=self.sequence
        future=asyncio.get_running_loop().create_future()
        key=(route,ident)
        self.pending[key]=future
        try:
            self.proc.stdin.write((json.dumps({'route':route,'jsonrpc':'2.0','id':ident,'method':method,'params':params},ensure_ascii=False)+'\n').encode())
            await self.proc.stdin.drain()
            async with asyncio.timeout(240):return await future
        finally:self.pending.pop(key,None)

    async def call_tool(self,name,arguments):
        result=await self._rpc('tools/call',{'name':name,'arguments':arguments})
        return SimpleNamespace(content=[SimpleNamespace(**item) for item in result.get('content',[])],
            structured_content=result.get('structuredContent'),is_error=result.get('isError',False))

    async def _secondary(self):
        while True:
            result=await self._rpc('tools/call',{'name':'ha_config_get_dashboard','arguments':{'url_path':'dashboard-media'}},'secondary')
            assert not result.get('isError'),result
            self.secondary_reads+=1
            await asyncio.sleep(0.1)

    async def __aexit__(self,*exc):
        if self.background:
            self.background.cancel()
            with contextlib.suppress(asyncio.CancelledError):await self.background
        self.proc.stdin.close()
        try:
            async with asyncio.timeout(20):await self.proc.wait()
        except TimeoutError:
            self.proc.kill()
            await self.proc.wait()
        await self.reader
        self.stderr.close()
        # Config includes the VM-only webhook URL; artifacts need only timings.
        (self.folder/'config.json').unlink(missing_ok=True)
        record('desktop_closed',folder=self.folder.name,secondary_reads=self.secondary_reads,returncode=self.proc.returncode)
        assert self.proc.returncode==0


@pytest.mark.parametrize('protocol',['2024-11-05','2025-11-25'])
@pytest.mark.parametrize('dual',[False,True],ids=['single','dual'])
@pytest.mark.parametrize('bps',['default','false'])
async def test_desktop_issue_2367(ha_container_with_fresh_config,protocol,dual,bps):
    info=ha_container_with_fresh_config
    assert info['backend']=='haos_embedded'
    url=info['embedded_webhook_url']
    baseline_config=YAML(typ='safe').load((DATA/'dashboard-media-sanitized.yaml').read_text())
    label=f'desktop/{protocol}/{dual}/{bps}'
    artifact_root=Path('/tmp/desktop-measurements')/label
    async with make_client(url,False) as observer:
        await call(observer,'ha_config_set_dashboard',{'url_path':'dashboard-media','config':baseline_config,'MandatoryBPS':False},label+'/setup')
        baseline=await get_dashboard(observer,label+'/baseline')
        assert baseline['config']==baseline_config
        for session in range(3):
            async with DesktopClient(url,artifact_root/f'fresh-{session}',dual,protocol) as writer:
                await attempt(writer,observer,baseline,EXACT_TRANSFORM,bps,label+f'/fresh/{session}',True)
        async with DesktopClient(url,artifact_root/'reuse',dual,protocol) as writer:
            for iteration in range(30):
                large=iteration%2==0
                transform=EXACT_TRANSFORM if large else "config['views'][0]['sections'][1]['cards'][0]['icon'] = 'mdi:music-box'"
                await attempt(writer,observer,baseline,transform,bps,label+f'/reuse/{iteration}',large)
        await call(observer,'ha_config_delete_dashboard',{'url_path':'dashboard-media'},label+'/cleanup')
    record('desktop_case_pass',protocol=protocol,dual=dual,bps=bps,attempts=33)

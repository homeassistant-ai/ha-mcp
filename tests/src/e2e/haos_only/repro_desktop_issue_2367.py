"""Branch-only reproduction through actual Desktop Electron + extracted transport."""
import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from ruamel.yaml import YAML
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from .repro_issue_2367 import (
    DATA, EXACT_TRANSFORM, attempt, call, get_dashboard, make_client, record,
)

pytestmark=[pytest.mark.timeout(1200)]


@pytest.fixture(scope='session',autouse=True)
def bare_haos():
    """Remove baked MCP integrations before boot; never install a server in HA."""
    if os.environ['HAOS_TEST_MODE'] != 'stdio':
        yield
        return
    from .. import conftest as suite
    def remove(image):
        with tempfile.TemporaryDirectory() as folder:
            prefix=['guestfish','--rw','-a',str(image),'run',':','mount','/dev/sda8','/',':']
            storage='/supervisor/homeassistant/.storage/'
            subprocess.run(prefix+['copy-out',storage+'core.config_entries',folder],check=True,capture_output=True,timeout=180)
            target=Path(folder)/'core.config_entries'
            doc=json.loads(target.read_text())
            removed=[e for e in doc['data']['entries'] if e['domain'] in ('ha_mcp_tools','mcp_proxy')]
            assert any(e['domain']=='ha_mcp_tools' for e in removed)
            doc['data']['entries']=[e for e in doc['data']['entries'] if e not in removed]
            target.write_text(json.dumps(doc))
            subprocess.run(prefix+['copy-in',str(target),storage,':','rm-rf','/supervisor/homeassistant/custom_components/ha_mcp_tools',':','rm-rf','/supervisor/homeassistant/custom_components/mcp_proxy'],check=True,capture_output=True,timeout=180)
            record('bare_haos_prepared',removed_domains=[e['domain'] for e in removed])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(suite,'remove_tools_entry_in_qcow2',remove)
        patch.setattr(suite,'stage_embedded_server_wheel_in_qcow2',lambda image:None)
        yield


@pytest.fixture(scope='session',name='ha_container_with_fresh_config')
def desktop_haos_backend(bare_haos,ha_container_with_fresh_config):
    """Make offline cleanup a prerequisite of the parent VM fixture."""
    return ha_container_with_fresh_config


def server_env(info,folder):
    return {'HOMEASSISTANT_URL':info['base_url'],'HOMEASSISTANT_TOKEN':info['token'],
            'HA_MCP_CONFIG_DIR':str(folder),'HAMCP_ENV_FILE':'/tmp/repro-no-env-file',
            'PATH':os.environ['PATH'],'ENABLE_STRICT_MANDATORY_BPS':'false'}


class DesktopClient:
    def __init__(self,info,folder,dual,protocol):
        self.info=info
        self.folder=folder
        self.dual=dual
        self.protocol=protocol
        self.pending={}
        self.sequence=0
        self.secondary_reads=0
        self.background=None

    async def __aenter__(self):
        try:
            return await self._start()
        except BaseException:
            if hasattr(self,'proc'):
                with contextlib.suppress(Exception):await self.__aexit__(None,None,None)
            raise

    async def _start(self):
        self.folder.mkdir(parents=True)
        uvx=shutil.which('uvx')
        assert uvx
        spec=os.environ['REPRO_HAMCP_SPEC']
        servers={route:{'command':uvx,'args':['--from',spec,'ha-mcp'],
                       'env':server_env(self.info,self.folder/('server-'+route))}
                 for route in ['primary']+(['secondary'] if self.dual else [])}
        if self.info['backend'] != 'haos_stdio':
            url=self.info['embedded_webhook_url'] or self.info['addon_mcp_url']
            servers={route:{'command':uvx,'args':['--from','fastmcp-remote==4.0.3','fastmcp-remote',url,'--auth','none'],
                            'env':{'PATH':os.environ['PATH']}}
                     for route in servers}
        config=self.folder/'config.json'
        config.write_text(json.dumps({'mcpServers':servers}))
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
                if not line.startswith(b'{'):
                    self.stderr.write(line.decode(errors='replace'))
                    self.stderr.flush()
                    continue
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
        from ha_mcp.stdio_settings_sidecar import retire_sidecar
        for route in ['primary']+(['secondary'] if self.dual else []):
            await asyncio.to_thread(retire_sidecar,self.folder/('server-'+route))
        # Config includes the disposable VM token; upload only transport metrics.
        (self.folder/'config.json').unlink(missing_ok=True)
        record('desktop_closed',folder=self.folder.name,secondary_reads=self.secondary_reads,returncode=self.proc.returncode)
        assert self.proc.returncode==0


@pytest.mark.parametrize('protocol',['2024-11-05','2025-11-25'])
@pytest.mark.parametrize('dual',[False,True],ids=['single','dual'])
@pytest.mark.parametrize('bps',['default','false'])
async def test_desktop_issue_2367(ha_container_with_fresh_config,protocol,dual,bps):
    info=ha_container_with_fresh_config
    standalone=info['backend']=='haos_stdio'
    import httpx
    async with httpx.AsyncClient() as rest:
        headers={'Authorization':'Bearer '+info['token']}
        config=await rest.get(info['base_url']+'/api/config',headers=headers)
        config.raise_for_status()
        if standalone:
            assert info['embedded_webhook_url'] is None
            assert info['addon_mcp_url'] is None
            services=await rest.get(info['base_url']+'/api/services',headers=headers)
            services.raise_for_status()
            assert not any(s['domain'] in ('ha_mcp_tools','mcp_proxy') for s in services.json())
            assert 'ha_mcp_tools' not in config.json()['components']
        record('desktop_topology',ha_version=config.json()['version'],backend=info['backend'],ha_mcp_component_loaded='ha_mcp_tools' in config.json()['components'],server_spec=os.environ['REPRO_HAMCP_SPEC'])
    baseline_config=YAML(typ='safe').load((DATA/'dashboard-media-sanitized.yaml').read_text())
    label=f'desktop/{protocol}/{dual}/{bps}'
    artifact_root=Path('/tmp/desktop-measurements')/label
    observer_client=(Client(StdioTransport(command='ha-mcp',args=[],env=server_env(info,artifact_root/'observer'),keep_alive=False),timeout=240)
                     if standalone else make_client(info['embedded_webhook_url'] or info['addon_mcp_url'],False))
    async with observer_client as observer:
        await call(observer,'ha_config_set_dashboard',{'url_path':'dashboard-media','config':baseline_config,'MandatoryBPS':False},label+'/setup')
        baseline=await get_dashboard(observer,label+'/baseline')
        assert baseline['config']==baseline_config
        for session in range(3):
            async with DesktopClient(info,artifact_root/f'fresh-{session}',dual,protocol) as writer:
                await attempt(writer,observer,baseline,EXACT_TRANSFORM,bps,label+f'/fresh/{session}',True)
        async with DesktopClient(info,artifact_root/'reuse',dual,protocol) as writer:
            for iteration in range(30):
                large=iteration%2==0
                transform=EXACT_TRANSFORM if large else "config['views'][0]['sections'][1]['cards'][0]['icon'] = 'mdi:music-box-multiple'"
                await attempt(writer,observer,baseline,transform,bps,label+f'/reuse/{iteration}',large,small_icon='mdi:music-box-multiple')
        await call(observer,'ha_config_delete_dashboard',{'url_path':'dashboard-media'},label+'/cleanup')
    if standalone:
        from ha_mcp.stdio_settings_sidecar import retire_sidecar
        await asyncio.to_thread(retire_sidecar,artifact_root/'observer')
    record('desktop_case_pass',protocol=protocol,dual=dual,bps=bps,attempts=33)

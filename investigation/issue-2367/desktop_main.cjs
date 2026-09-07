// GitHub-only harness: official Electron binary, extracted Desktop transport,
// real Chromium renderer <-> MessageChannelMain <-> uvx pipes.
const {app, BrowserWindow, MessageChannelMain, ipcMain} = require('electron');
const fs = require('node:fs');
const readline = require('node:readline');
const assert = require('node:assert/strict');
let T;
try {T=require('/tmp/claude-source/.vite/build/repro-transport.cjs')}
catch(error){process.stdout.write(JSON.stringify({type:'fatal',error:error.stack})+'\n');process.exit(1)}
const config = JSON.parse(fs.readFileSync(process.env.REPRO_DESKTOP_CONFIG, 'utf8'));
const logPath=process.env.REPRO_DESKTOP_LOG;
const emit = msg => process.stdout.write(JSON.stringify(msg)+'\n');
const trace = (event,fields={})=>fs.appendFileSync(logPath,JSON.stringify({time:Date.now(),event,...fields})+'\n');
const logger={info:()=>{},warn:(...a)=>trace('warning',{message:String(a[0])}),error:(...a)=>trace('error',{message:String(a[0])})};
console.info=(...a)=>logger.info(...a);console.warn=(...a)=>logger.warn(...a);console.error=(...a)=>logger.error(...a);
app.setPath('userData',process.env.REPRO_DESKTOP_PROFILE);
app.disableHardwareAcceleration();
let win;
const transports=[];
let nextControl=0;
const control=new Map();
ipcMain.on('reply',(_event,message)=>emit(message));
ipcMain.on('port-ready',(_event,id)=>{control.get(id)?.();control.delete(id)});
async function open(route) {
  const server=config.mcpServers[route];
  assert(server);
  const spec=T.spawnSpec({cmd:server.command,args:server.args,processGroup:true});
  const transport=new (spec.processGroupLeader?T.GroupTransport:T.StdioTransport)({
    command:spec.cmd,args:spec.args,env:server.env,stderr:'pipe',maxBufferSize:T.maxBufferSize,
  });
  const {port1,port2}=new MessageChannelMain();
  const toRenderer=new T.PortTransport(port1);
  T.bridge({transportToClient:toRenderer,transportToServer:transport,logger,serverName:route,
    onerror:e=>{trace('transport_error',{route,error:String(e)});emit({type:'transport_error',route,error:String(e)})},
    onclose:side=>trace('transport_close',{route,side}),onClientClose:'defer-to-onclose'});
  const originalSend=transport.send.bind(transport);
  transport.send=message=>{trace('pipe_send',{route,id:message.id,method:message.method,bytes:Buffer.byteLength(JSON.stringify(message))});return originalSend(message)};
  const originalResponse=transport.onmessage;
  transport.onmessage=message=>{trace('pipe_response',{route,id:message.id,bytes:Buffer.byteLength(JSON.stringify(message))});originalResponse(message)};
  transport.stderr?.on('data',chunk=>trace('bridge_stderr',{route,bytes:chunk.length}));
  await transport.start();
  transport._process.stdout.on('data',chunk=>trace('stdout_chunk',{route,bytes:chunk.length}));
  await toRenderer.start();
  transports.push(transport);
  const id=++nextControl;
  const attached=new Promise(resolve=>control.set(id,resolve));
  win.webContents.postMessage('port',{route,id},[port2]);
  await attached;
  trace('opened',{route,pid:transport.pid,process_group:spec.processGroupLeader});
}
async function main(){
  await app.whenReady();
  win=new BrowserWindow({show:false,webPreferences:{nodeIntegration:true,contextIsolation:false}});
  await win.loadFile('/tmp/desktop-harness/renderer.html');
  for(const route of Object.keys(config.mcpServers))await open(route);
  emit({type:'ready',versions:process.versions,desktop:process.env.REPRO_PLATFORM==='win32'?'1.46388.4':'1.46388.2',routes:Object.keys(config.mcpServers)});
  const input=readline.createInterface({input:process.stdin});
  input.on('line',line=>{
    const {route='primary',...message}=JSON.parse(line);
    trace('driver_request',{route,id:message.id,method:message.method});
    win.webContents.send('request',{route,message});
  });
  input.on('close',async()=>{await Promise.all(transports.map(t=>t.close()));app.exit(0)});
}
main().catch(e=>{emit({type:'fatal',error:e.stack});app.exit(1)});

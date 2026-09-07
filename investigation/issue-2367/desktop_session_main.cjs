// Isolated chat session: real Desktop preload + extracted lifecycle and stdio.
const {app,BrowserWindow,ipcMain,session}=require('electron');
const fs=require('node:fs'),path=require('node:path'),readline=require('node:readline');
const config=JSON.parse(fs.readFileSync(process.env.REPRO_DESKTOP_CONFIG,'utf8'));
const log=process.env.REPRO_DESKTOP_LOG;
const trace=(event,data={})=>fs.appendFileSync(log,JSON.stringify({time:Date.now(),event,...data})+'\n');
let output=process.stdout;
const emit=data=>output.write(JSON.stringify(data)+'\n');
process.on('uncaughtException',error=>{trace('fatal',{error:error.stack});emit({type:'fatal',error:error.stack});app.exit(1)});
app.setPath('userData',path.resolve(process.env.REPRO_DESKTOP_PROFILE));
app.disableHardwareAcceleration();
let win,lifecycle;
const profile=path.resolve(process.env.REPRO_DESKTOP_PROFILE);
fs.mkdirSync(profile,{recursive:true});
// Preserve asynchronous on-disk application logging and direct child stderr.
// These files stay in the private test profile, outside uploaded metrics.
const logger={info:(...a)=>trace('desktop_info',{message:String(a[0])}),warn:(...a)=>trace('desktop_warning',{message:String(a[0])}),error:(...a)=>trace('desktop_error',{message:String(a[0])}),debug:()=>{}};
const T=require('/tmp/claude-source/.vite/build/repro-transport.cjs');
const metadata=JSON.parse(fs.readFileSync('/tmp/desktop-inspection/session-extraction.json','utf8'));
const unsupported=name=>(...args)=>{throw Error('Unexpected Desktop service: '+name)};
const services=Object.fromEntries(metadata.application_bindings.map(name=>[name,unsupported(name)]));
Object.assign(services,{
 N:logger,XA:()=>false,
 vh:{Initializing:'initializing',Running:'running',Failed:'failed'},
 BW:name=>({...logger,createProcessStream:async()=>{const s=fs.createWriteStream(path.join(profile,'mcp-server-'+name+'.log'),{flags:'a'});await new Promise((resolve,reject)=>{s.once('open',resolve);s.once('error',reject)});return s}}),
 xG:{getMcpServersConfig:async()=>config.mcpServers},
 dHn:async()=>({kind:'legacy'}), // Plain uvx config has no installed extension.
 o:require('electron'),SHn:T.PortTransport,jHn:T.bridge,
 ub:async(name,spec,options)=>{
   if(spec.extensionId)throw Error('Session probe only supports plain stdio config');
   const spawn=T.spawnSpec({cmd:spec.command,args:spec.args||[],processGroup:true});
   const t=new (spawn.processGroupLeader?T.GroupTransport:T.StdioTransport)({command:spawn.cmd,args:spawn.args,env:spec.env,stderr:options.stderr,maxBufferSize:T.maxBufferSize});
   const send=t.send.bind(t);t.send=m=>{trace('pipe_send',{route:name,id:m.id,method:m.method,bytes:Buffer.byteLength(JSON.stringify(m))});return send(m)};
   const start=t.start.bind(t);t.start=async()=>{await start();const receive=t.onmessage;t.onmessage=m=>{trace('pipe_response',{route:name,id:m.id,bytes:Buffer.byteLength(JSON.stringify(m))});receive(m)};trace('opened',{route:name,pid:t.pid});t._process.stdout.on('data',c=>trace('stdout_chunk',{route:name,bytes:c.length}))};
   return t;
 },
 Vc:e=>String(e),n:{default:path},
 hUn:(name,error)=>trace('launch_error',{route:name,error:String(error)}),
 Y:()=>{},ZHn:()=>{},hrt:()=>{},YW:()=>{},
 ba:{ListMcpServers:'list-mcp-servers',ConnectToMcpServer:'connect-to-mcp-server',McpServerConnected:'mcp-server-connected',McpServerAutoReconnect:'mcp-server-auto-reconnect'},
 HWn:async()=>Object.keys(config.mcpServers),KZr:()=>{},Jge:()=>null,grt:()=>false,
});
console.info=logger.info;console.warn=logger.warn;console.error=logger.error;
const prefix='$eipc_message$_720e1c5c-930a-4c0d-9628-82278fbf18cc_$_claude.web_$_';
let controllerReady;
let rendererReady=new Promise(resolve=>{controllerReady=resolve});
async function main(){
 await app.whenReady();
 // Entire chat fixture is local; no request reaches claude.ai or uses real auth.
 await session.defaultSession.protocol.handle('https',async request=>{
   const url=new URL(request.url);
   if(url.origin!=='https://claude.ai')return new Response('Offline fixture',{status:403});
   if(url.pathname==='/__repro/reply'){
     const message=await request.json();
     if(message.type==='session_ready'){trace('session_ready',message);controllerReady(message)}
     else if(message.type==='fatal'){trace('renderer_error',message);emit(message)}
     else {trace('renderer_response',{route:message.route,id:message.id});emit(message)}
     return new Response('{}',{headers:{'content-type':'application/json'}});
   }
   return new Response(fs.readFileSync('/tmp/desktop-harness/session-renderer.html'),{headers:{'content-type':'text/html'}});
 });
 win=new BrowserWindow({show:false,webPreferences:{preload:path.resolve('/tmp/claude-source/.vite/build/mainView.js'),contextIsolation:true,nodeIntegration:false,sandbox:true,backgroundThrottling:false}});
 services.B=win;
 lifecycle=require('/tmp/claude-source/.vite/build/repro-session.cjs')(services);
 ipcMain.handle('list-mcp-servers',lifecycle.listHandler());
 ipcMain.handle('connect-to-mcp-server',lifecycle.connectHandler(win));
 ipcMain.handle('$eipc_message$_720e1c5c-930a-4c0d-9628-82278fbf18cc_$_claude.buddy_$_BuddyBleTransport_$_reportState',()=>{});
 ipcMain.handle('artifact-window-open-gate:state',()=>({active:false}));
 ipcMain.handle('$eipc_message$_720e1c5c-930a-4c0d-9628-82278fbf18cc_$_claude.telemetry_$_RendererMemoryReporter_$_report',()=>{});
 ipcMain.handle(prefix+'Auth_$_prepareForSignedIn',()=>trace('simulated_signin_prepared'));
 ipcMain.handle(prefix+'Account_$_setAccountDetails',(_e,details)=>trace('simulated_account',{accountUuid:details.accountUuid}));
 win.webContents.on('preload-error',(_e,file,error)=>{trace('preload_error',{file,error:error.stack});emit({type:'fatal',error:error.stack})});
 win.webContents.on('console-message',(_e,...args)=>trace('renderer_console',{message:String(args[1])}));
 win.webContents.on('did-navigate',()=>lifecycle.navigated());
 await win.loadURL('https://claude.ai/new');
 await rendererReady;
 let input=process.stdin;
 if(process.env.REPRO_CONTROL_FILE){input=output=await new Promise(resolve=>{const server=require('node:net').createServer(s=>{server.close();resolve(s)});server.listen(0,'127.0.0.1',()=>fs.writeFileSync(process.env.REPRO_CONTROL_FILE,JSON.stringify({port:server.address().port})))})}
 emit({type:'ready',versions:process.versions,desktop:'1.46388.2',session_mode:true,routes:Object.keys(config.mcpServers)});
 const rl=readline.createInterface({input});
 async function dispatch(message){
   trace('driver_request',{route:message.route||'primary',id:message.id,method:message.method});
   if(message.method==='repro/reload'){
     const before=[...lifecycle.connections.keys()];
     rendererReady=new Promise(resolve=>{controllerReady=resolve});
     win.webContents.reload();
     await rendererReady;
     trace('renderer_reloaded',{servers:before});
     emit({jsonrpc:'2.0',id:message.id,result:{reloaded:true,servers:before}});
   }else await win.webContents.executeJavaScript('window.__repro.receive('+JSON.stringify(message)+')');
 }
 rl.on('line',line=>dispatch(JSON.parse(line)).catch(e=>emit({type:'fatal',error:e.stack})));
 rl.on('close',async()=>{await lifecycle.shutdownAll(false);app.exit(0)});
}
main().catch(error=>{trace('fatal',{error:error.stack});emit({type:'fatal',error:error.stack});app.exit(1)});

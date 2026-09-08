// Runner-only: retain exact Desktop lifecycle declarations and IPC callback.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const acorn=require('/tmp/desktop-analysis/node_modules/acorn');
const walk=require('/tmp/desktop-analysis/node_modules/acorn-walk');
const scope=require('/tmp/desktop-analysis/node_modules/eslint-scope');
const root='/tmp/claude-source/.vite/build';
const file=path.join(root,'index.chunk-DrnJEXHK.js');
const source=fs.readFileSync(file,'utf8');
const hash=s=>crypto.createHash('sha256').update(s).digest('hex');
if(hash(source)!=='ec48534a81eb0a7faabeccf41d8bfcb93f7c4d678e7e8ddc1cbd83825070a26c')throw Error('Session source drift');
const ast=acorn.parse(source,{ecmaVersion:'latest',sourceType:'script',ranges:true});
const scopes=scope.analyze(ast,{ecmaVersion:2024,sourceType:'script',optimistic:true,ignoreEval:true});
const defs=new Map();
for(const node of ast.body){
 if(['FunctionDeclaration','ClassDeclaration'].includes(node.type))defs.set(node.id.name,node);
 if(node.type==='VariableDeclaration')for(const d of node.declarations)if(d.id.type==='Identifier')defs.set(d.id.name,d);
}
const names=['gUn','cUn','sUn','lUn','uUn','dUn','rUn','KW','UW','WHn','oUn','GHn','qW','XW','pUn','fUn'];
const nodes=names.map(name=>{const node=defs.get(name);if(!node)throw Error('Missing '+name);return node});
const callbacks={};
walk.simple(ast,{CallExpression(n){
 if(n.callee.type!=='Identifier'||n.callee.name!=='t2r')return;
 const [event,handler]=n.arguments;
 if(event?.type==='MemberExpression'&&event.object.name==='ba'&&['ConnectToMcpServer','ListMcpServers'].includes(event.property.name))callbacks[event.property.name]=handler;
}});
if(Object.keys(callbacks).length!==2)throw Error('Expected both chat MCP callbacks');
const retained=new Set(names);
const free=new Set();
for(const node of [...nodes,...Object.values(callbacks)])for(const s of scopes.scopes)for(const ref of s.references){
 const id=ref.identifier;
 if(id.start<node.start||id.end>node.end)continue;
 const target=ref.resolved;
 if(!target||target.defs.some(d=>d.name.start>=node.start&&d.name.end<=node.end))continue;
 if(retained.has(target.name))continue;
 // Callback's outer `t` is the main WebContentsView, supplied by its factory.
 if(Object.values(callbacks).includes(node)&&target.name==='t')continue;
 free.add(target.name);
}
let output='"use strict";\nmodule.exports=function(services){\n';
output+='const {'+[...free].join(',')+'}=services;\n';
for(const n of nodes.sort((a,b)=>a.start-b.start))output+=(n.type==='VariableDeclarator'?'var ':'')+source.slice(n.start,n.end)+';\n';
output+='return {launch:gUn,shutdown:lUn,shutdownAll:dUn,navigated:rUn,connections:KW,status:UW,connectHandler(t){return '+source.slice(callbacks.ConnectToMcpServer.start,callbacks.ConnectToMcpServer.end)+'},listHandler(){return '+source.slice(callbacks.ListMcpServers.start,callbacks.ListMcpServers.end)+'}};\n};\n';
fs.writeFileSync(path.join(root,'repro-session.cjs'),output);
fs.writeFileSync('/tmp/desktop-inspection/session-lifecycle.txt',output);
fs.writeFileSync('/tmp/desktop-inspection/session-extraction.json',JSON.stringify({source:file,source_sha256:hash(source),retained:names,application_bindings:[...free],output_sha256:hash(output)},null,2));
console.log('Session lifecycle:',names.length,'declarations; external services:',[...free].join(','));
// Export syntax hashes and reference locations, not proprietary source, so the
// Windows runner can identify its own exact declarations and service bindings.
const {canonical}=require('./source_shapes.cjs');
const references=new Map(scopes.scopes.flatMap(s=>s.references).map(r=>[r.identifier.start,r]));
const specifications=[];
for(const [name,node] of [...names.map(n=>[n,defs.get(n)]),...Object.entries(callbacks)]){
 const refs=[];
 function scan(n,keys=[]){
  if(n===null||typeof n!=='object')return;
  if(n.type==='Identifier'){
   const ref=references.get(n.start),target=ref?.resolved;
   if(target&&!target.defs.some(d=>d.name.start>=node.start&&d.name.end<=node.end)){
    if(retained.has(target.name)||free.has(target.name))refs.push({path:keys,name:target.name});
    else if(Object.hasOwn(callbacks,name)&&target.name==='t')refs.push({path:keys,name:'$mainView'});
   }
  }
  for(const [key,value] of Object.entries(n))if(!['start','end','range','loc'].includes(key)){
   if(Array.isArray(value))value.forEach((v,i)=>scan(v,[...keys,key,i]));else if(value&&typeof value==='object')scan(value,[...keys,key]);
  }
 }
 scan(node);
 specifications.push({name,kind:Object.hasOwn(callbacks,name)?'callback':'declaration',shape_sha256:hash(canonical(node)),references:refs});
}
fs.writeFileSync('/tmp/desktop-inspection/session-shapes.json',JSON.stringify({linux_sha256:hash(source),bindings:[...free],specifications},null,2));

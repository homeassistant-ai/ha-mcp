// Extract Windows' own session code only after matching Linux reference syntax.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const acorn=require('C:/tmp/desktop-analysis/node_modules/acorn');
const walk=require('C:/tmp/desktop-analysis/node_modules/acorn-walk');
const {canonical}=require('./source_shapes.cjs');
const spec=JSON.parse(fs.readFileSync(path.join(process.env.GITHUB_WORKSPACE,'investigation/issue-2367/session-shapes.json'),'utf8'));
const root='C:/tmp/claude-source/.vite/build',file=path.join(root,'index.chunk--WuAOADe.js');
const source=fs.readFileSync(file,'utf8'),hash=s=>crypto.createHash('sha256').update(s).digest('hex');
if(hash(source)!=='beb8b9ad91b0979aa0e89b0ab373cc790e95ccb06a161e29a2978bdec653beb8')throw Error('Windows session source drift');
const ast=acorn.parse(source,{ecmaVersion:'latest',sourceType:'script'}),defs=new Map(),byShape=new Map(),callbacks={};
for(const node of ast.body){
 if(['FunctionDeclaration','ClassDeclaration'].includes(node.type))defs.set(node.id.name,node);
 if(node.type==='VariableDeclaration')for(const d of node.declarations)if(d.id.type==='Identifier')defs.set(d.id.name,d);
}
for(const [name,node]of defs){const h=hash(canonical(node));if(!byShape.has(h))byShape.set(h,[]);byShape.get(h).push({name,node})}
walk.simple(ast,{CallExpression(n){
 const [event,handler]=n.arguments;
 if(event?.type==='MemberExpression'&&['ConnectToMcpServer','ListMcpServers'].includes(event.property?.name)&&['ArrowFunctionExpression','FunctionExpression'].includes(handler?.type))callbacks[event.property.name]=handler;
}});
const aliases=new Map(),picked=new Map();
const at=(node,keys)=>keys.reduce((n,k)=>n[k],node);
function bind(name,value){if(aliases.has(name)&&aliases.get(name)!==value)throw Error('Inconsistent Windows mapping: '+name);aliases.set(name,value)}
while(picked.size<spec.specifications.length){
 let progress=false;
 for(const item of spec.specifications){
  if(picked.has(item.name))continue;
  const candidates=item.kind==='callback'?[{name:item.name,node:callbacks[item.name]}]:(byShape.get(item.shape_sha256)||[]);
  const matches=candidates.filter(c=>c.node&&hash(canonical(c.node))===item.shape_sha256&&(!aliases.has(item.name)||aliases.get(item.name)===c.name)&&item.references.every(r=>!aliases.has(r.name)||at(c.node,r.path).name===aliases.get(r.name)));
  if(matches.length!==1)continue;
  const chosen=matches[0];picked.set(item.name,chosen);if(item.kind==='declaration')bind(item.name,chosen.name);
  for(const r of item.references)bind(r.name,at(chosen.node,r.path).name);
  progress=true;
 }
 if(!progress)throw Error('Unresolved Windows session shapes: '+spec.specifications.filter(s=>!picked.has(s.name)).map(s=>s.name));
}
for(const name of spec.bindings)if(!aliases.has(name))throw Error('Unmapped Windows service '+name);
let output='"use strict";\nmodule.exports=function(services){\nconst {'+spec.bindings.map(n=>n+':'+aliases.get(n)).join(',')+'}=services;\n';
for(const item of spec.specifications.filter(s=>s.kind==='declaration').map(s=>picked.get(s.name)).sort((a,b)=>a.node.start-b.node.start))output+=(item.node.type==='VariableDeclarator'?'var ':'')+source.slice(item.node.start,item.node.end)+';\n';
const text=name=>{const n=picked.get(name).node;return source.slice(n.start,n.end)};
output+='return {launch:'+aliases.get('gUn')+',shutdown:'+aliases.get('lUn')+',shutdownAll:'+aliases.get('dUn')+',navigated:'+aliases.get('rUn')+',connections:'+aliases.get('KW')+',status:'+aliases.get('UW')+',connectHandler('+aliases.get('$mainView')+'){return '+text('ConnectToMcpServer')+'},listHandler(){return '+text('ListMcpServers')+'}};\n};\n';
fs.writeFileSync(path.join(root,'repro-session.cjs'),output);
fs.writeFileSync('C:/tmp/desktop-inspection/session-lifecycle.txt',output);
fs.writeFileSync('C:/tmp/desktop-inspection/session-extraction.json',JSON.stringify({source:file,source_sha256:hash(source),application_bindings:spec.bindings,windows_aliases:Object.fromEntries(aliases),verified_shapes:picked.size,output_sha256:hash(output),preload_sha256:hash(fs.readFileSync(path.join(root,'mainView.js')))},null,2));
console.log('Matched and extracted',picked.size,'Windows session declarations/callbacks');

// Extract actual declarations and their lexical dependencies; never hand-rewrite transport code.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const acorn = require('/tmp/desktop-analysis/node_modules/acorn');
const walk = require('/tmp/desktop-analysis/node_modules/acorn-walk');
const scope = require('/tmp/desktop-analysis/node_modules/eslint-scope');
const dir = '/tmp/claude-source/.vite/build';
const filename = path.join(dir, 'index.chunk-DrnJEXHK.js');
const source = fs.readFileSync(filename, 'utf8');
if(crypto.createHash('sha256').update(source).digest('hex') !== 'ec48534a81eb0a7faabeccf41d8bfcb93f7c4d678e7e8ddc1cbd83825070a26c') throw Error('Desktop source drift');
const ast = acorn.parse(source, {ecmaVersion:'latest',sourceType:'script',ranges:true});
const scopes = scope.analyze(ast,{ecmaVersion:2024,sourceType:'script',optimistic:true,ignoreEval:true});
const defs = new Map();
for(const node of ast.body) {
  if(node.type==='FunctionDeclaration' || node.type==='ClassDeclaration') defs.set(node.id.name,node);
  if(node.type==='VariableDeclaration') for(const d of node.declarations) if(d.id.type==='Identifier') defs.set(d.id.name,d);
}
// Only application logging is substituted. Preserve framing, validation, spawn,
// process IO, forwarding, message-port handling and shutdown source unchanged.
const substitutes = new Map([['N','const N = console;']]);
const selected = new Set();
const globalMembers=new Set();
const references = scopes.scopes.flatMap(s=>s.references).sort((a,b)=>a.identifier.start-b.identifier.start);
function isTop(target) {
  return target && target.defs.some(d=>d.name && defs.has(d.name.name) && defs.get(d.name.name).start<=d.name.start && d.name.end<=defs.get(d.name.name).end);
}
const refCache=new Map();
function refsWithin(node) {
  if(refCache.has(node))return refCache.get(node);
  let lo=0,hi=references.length;
  while(lo<hi){const mid=(lo+hi)>>>1;if(references[mid].identifier.start<node.start)lo=mid+1;else hi=mid;}
  const found=[];
  for(let i=lo;i<references.length&&references[i].identifier.start<node.end;i++)if(references[i].identifier.end<=node.end)found.push(references[i]);
  refCache.set(node,found);return found;
}
function include(name) {
  if(selected.has(name)) return;
  selected.add(name);
  if(substitutes.has(name))return;
  const node=defs.get(name);
  if(!node)throw Error('No top-level declaration for '+name);
  walk.simple(node,{MemberExpression(n){if(n.object.type==='Identifier' && n.object.name==='globalThis' && !n.computed)globalMembers.add(n.property.name)}});
  for(const ref of refsWithin(node)) {
    const target=ref.resolved;
    if(target && target.defs.some(d=> d.name && defs.has(d.name.name) && defs.get(d.name.name).start <= d.name.start && d.name.end <= defs.get(d.name.name).end)) include(target.name);
  }
}
for(const name of ['Op','SHn','jHn','brt','Srt','Dqe','Mp','Tqe','vrt'])include(name);
// Declarations alone omit bundle initialization such as globalThis[zodKey]
// ??= {} and enum IIFEs. Retain writes to selected bindings/objects in their
// original order, then close over their dependencies too.
const effects=[];
for(const node of ast.body) if(node.type==='ExpressionStatement') {
  if(node.expression.type==='SequenceExpression') effects.push(...node.expression.expressions);
  else effects.push(node.expression);
} else if(!['VariableDeclaration','FunctionDeclaration','ClassDeclaration','EmptyStatement'].includes(node.type))effects.push(node);
const usedEffects=new Set();
function touchesSelected(node) {
  return refsWithin(node).some(r=>isTop(r.resolved)&&selected.has(r.resolved.name));
}
let changed=true;
while(changed) {
  changed=false;
  for(const node of effects) {
    if(usedEffects.has(node))continue;
    let needed=refsWithin(node).some(r=>r.isWrite()&&isTop(r.resolved)&&selected.has(r.resolved.name));
    walk.simple(node,{AssignmentExpression(n){
      if(n.left.type!=='MemberExpression')return;
      if(touchesSelected(n.left))needed=true;
      if(n.left.object.type==='Identifier'&&n.left.object.name==='globalThis'&&!n.left.computed&&globalMembers.has(n.left.property.name))needed=true;
    }});
    if(!needed)continue;
    usedEffects.add(node);changed=true;
    for(const ref of refsWithin(node)) {
      const target=ref.resolved;
      if(target&&target.defs.some(d=>d.name&&defs.has(d.name.name)&&defs.get(d.name.name).start<=d.name.start&&d.name.end<=defs.get(d.name.name).end))include(target.name);
    }
  }
}
const picked=[...selected].filter(n=>!substitutes.has(n)).map(n=>[n,defs.get(n)]);
const nodes=[...picked.map(([name,node])=>({name,node})),...[...usedEffects].map(node=>({node}))].sort((a,b)=>a.node.start-b.node.start);
let output='"use strict";\n'+[...substitutes.values()].join('\n')+'\n';
for(const {node}of nodes)output+=(node.type==='VariableDeclarator'?'var ':'')+source.slice(node.start,node.end)+';\n';
fs.writeFileSync('/tmp/desktop-inspection/global-init-source.txt',[...globalMembers].map(key=>{const pos=source.indexOf('globalThis.'+key);return source.slice(Math.max(0,pos-150),pos+500)}).join('\n'));
fs.writeFileSync('/tmp/desktop-inspection/initializers.txt',[...usedEffects].map(n=>source.slice(n.start,n.end)).join('\n'));
output+='module.exports={StdioTransport:Op,PortTransport:SHn,bridge:jHn,GroupTransport:brt,spawnSpec:Mp,maxBufferSize:vrt};\n';
fs.writeFileSync(path.join(dir,'repro-transport.cjs'),output);
fs.writeFileSync('/tmp/desktop-inspection/extracted-transport.txt',output);
fs.writeFileSync('/tmp/desktop-inspection/extraction.json',JSON.stringify({source:filename,selected:[...selected],bytes:output.length,source_sha256:crypto.createHash('sha256').update(source).digest('hex'),extracted_sha256:crypto.createHash('sha256').update(output).digest('hex')},null,2));
console.log('Extracted',picked.length,'declarations',output.length,'bytes');

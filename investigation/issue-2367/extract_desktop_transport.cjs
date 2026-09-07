// Extract actual declarations and their lexical dependencies; never hand-rewrite transport code.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const acorn = require('/tmp/desktop-analysis/node_modules/acorn');
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
const references = scopes.scopes.flatMap(s=>s.references);
function include(name) {
  if(selected.has(name)) return;
  selected.add(name);
  if(substitutes.has(name))return;
  const node=defs.get(name);
  if(!node)throw Error('No top-level declaration for '+name);
  for(const ref of references) {
    if(ref.identifier.start<node.start || ref.identifier.end>node.end)continue;
    const target=ref.resolved;
    if(target && target.defs.some(d=> d.name && defs.has(d.name.name) && defs.get(d.name.name).start <= d.name.start && d.name.end <= defs.get(d.name.name).end)) include(target.name);
  }
}
for(const name of ['Op','SHn','jHn','brt','Srt','Dqe','Mp','Tqe','vrt'])include(name);
const picked=[...selected].filter(n=>!substitutes.has(n)).map(n=>[n,defs.get(n)]).sort((a,b)=>a[1].start-b[1].start);
let output='"use strict";\n'+[...substitutes.values()].join('\n')+'\n';
for(const [name,node]of picked) {
  output+=(node.type==='VariableDeclarator'?'var ':'')+source.slice(node.start,node.end)+';\n';
  // Bundler normalizes Node imports with a following namespace assignment.
  if(node.type==='VariableDeclarator' && node.init?.type==='CallExpression' && node.init.callee.name==='require' && node.init.arguments[0]?.type==='Literal' && !String(node.init.arguments[0].value).startsWith('.')) output+=`if (${name} && !${name}.default) ${name} = Object.assign({default:${name}}, ${name});\n`;
}
output+='module.exports={StdioTransport:Op,PortTransport:SHn,bridge:jHn,GroupTransport:brt,spawnSpec:Mp,maxBufferSize:vrt};\n';
fs.writeFileSync(path.join(dir,'repro-transport.cjs'),output);
fs.writeFileSync('/tmp/desktop-inspection/extraction.json',JSON.stringify({source:filename,selected:[...selected],bytes:output.length,source_sha256:crypto.createHash('sha256').update(source).digest('hex'),extracted_sha256:crypto.createHash('sha256').update(output).digest('hex')},null,2));
console.log('Extracted',picked.length,'declarations',output.length,'bytes');

// GitHub-only source inspection. Does not execute Desktop.
const fs = require('node:fs');
const path = require('node:path');
const acorn = require('/tmp/desktop-analysis/node_modules/acorn');
const walk = require('/tmp/desktop-analysis/node_modules/acorn-walk');
const root = '/tmp/claude-source/.vite/build';
const source = fs.readFileSync(path.join(root, 'index.chunk-DrnJEXHK.js'), 'utf8');
const ast = acorn.parse(source, {ecmaVersion:'latest', sourceType:'script'});
const definitions = new Map();
for(const node of ast.body) {
  if(node.type==='FunctionDeclaration' || node.type==='ClassDeclaration') definitions.set(node.id.name, node);
  if(node.type==='VariableDeclaration') for(const d of node.declarations) if(d.id.type==='Identifier') definitions.set(d.id.name, d);
}
const selected = new Set(['brt','Mp','vrt','Dp','wrt','xHn','Ap','pqe','Tqe','Srt','Dqe','dqe','ub','dHn','SHn','jHn','THn','CHn','DHn','EHn','AHn','gUn']);
for(const [name,node] of definitions) {
  const text = source.slice(node.start,node.end);
  if(text.includes('StdioClientTransport already started') || text.includes('ReadBuffer exceeded maximum') || text.includes('function tUe(')) selected.add(name);
}
let report='';
for(const name of selected) {
  const node=definitions.get(name);
  if(!node) {report+=`\nMISSING ${name}\n`;continue;}
  const refs=new Set();
  walk.full(node,n=>{if(n.type==='Identifier' && definitions.has(n.name)) refs.add(n.name)});
  report+=`\nNAME ${name} OFFSET ${node.start} REFS ${[...refs].join(',')}\n${source.slice(node.start,node.end)}\n`;
}
for(const file of ['index.chunk-BEwvNqVd.js','index.chunk-BUGGQy2s.js']) {
  report+=`\nCOMPLETE MODULE ${file}\n${fs.readFileSync(path.join(root,file),'utf8')}\n`;
}
const host=fs.readFileSync(path.join(root,'mcp-runtime/directMcpHost.js'),'utf8');
report+='\nHOST START\n'+host.slice(0,1500)+'\nHOST END\n'+host.slice(-22000)+'\n';
fs.writeFileSync('/tmp/desktop-inspection/transport-definitions.txt',report);

// Runner-only structural comparison; identifiers can be renamed by bundling.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const acorn=require('/tmp/desktop-analysis/node_modules/acorn');
const hash=s=>crypto.createHash('sha256').update(s).digest('hex');
function load(root){
 const dir=path.join(root,'.vite/build');
 const files=fs.readdirSync(dir).filter(f=>f.endsWith('.js'));
 const matches=files.filter(f=>fs.readFileSync(path.join(dir,f),'utf8').includes('StdioClientTransport already started'));
 if(matches.length!==1)throw Error('Expected one main transport bundle: '+matches);
 const filename=path.join(dir,matches[0]),source=fs.readFileSync(filename,'utf8');
 const ast=acorn.parse(source,{ecmaVersion:'latest',sourceType:'script'});
 const defs=new Map();
 for(const n of ast.body){
  if(n.type==='FunctionDeclaration'||n.type==='ClassDeclaration')defs.set(n.id.name,n);
  if(n.type==='VariableDeclaration')for(const d of n.declarations)if(d.id.type==='Identifier')defs.set(d.id.name,d);
 }
 return{filename,source,defs,package:JSON.parse(fs.readFileSync(path.join(root,'package.json'),'utf8'))};
}
function canonical(node){
 const names=new Map();
 const rename=name=>{if(!names.has(name))names.set(name,'id'+names.size);return names.get(name)};
 function visit(n,parent,key){
  if(typeof n==='bigint')return {bigint:String(n)};
  if(n===null||typeof n!=='object')return n;
  if(Array.isArray(n))return n.map(x=>visit(x,parent,key));
  if(n.type==='Identifier'){
   const property=(parent?.type==='MemberExpression'&&key==='property'&&!parent.computed)||
     (['Property','MethodDefinition','PropertyDefinition'].includes(parent?.type)&&key==='key'&&!parent.computed);
   return {type:n.type,name:property?n.name:rename(n.name)};
  }
  const result={};
  for(const [k,v]of Object.entries(n))if(!['start','end','loc','range','raw'].includes(k))result[k]=visit(v,n,k);
  return result;
 }
 return JSON.stringify(visit(node));
}
const linux=load('/tmp/claude-source'),win=load('/tmp/claude-windows-source');
const winByShape=new Map();
for(const [name,node]of win.defs){const key=hash(canonical(node));if(!winByShape.has(key))winByShape.set(key,[]);winByShape.get(key).push(name)}
const requested=['Op','$He','tUe','SHn','jHn','Mp','Dp','brt','Tqe','ub','dHn','wrt','vrt'];
const rows=[];let snippets='';
for(const name of requested){
 const node=linux.defs.get(name);if(!node)throw Error('Missing Linux symbol '+name);
 const shape=canonical(node),equivalent=winByShape.get(hash(shape))||[];
 const original=linux.source.slice(node.start,node.end);
 rows.push({linux_symbol:name,linux_sha256:hash(original),identifier_normalized_sha256:hash(shape),windows_matches:equivalent,
   byte_identical_matches:equivalent.filter(n=>win.source.slice(win.defs.get(n).start,win.defs.get(n).end)===original)});
 snippets+='\nLINUX '+name+'\n'+original+'\n';
 for(const n of equivalent)snippets+='\nWINDOWS '+n+'\n'+win.source.slice(win.defs.get(n).start,win.defs.get(n).end)+'\n';
 if(!equivalent.length){
   // Report candidates sharing distinctive literal text; do not declare them equal.
   const strings=[...original.matchAll(/"([^"\\]{12,})"/g)].map(m=>m[1]);
   const candidates=[...win.defs].map(([n,d])=>({n,d,text:win.source.slice(d.start,d.end)})).map(c=>({...c,score:strings.filter(s=>c.text.includes(s)).length})).filter(c=>c.score).sort((a,b)=>b.score-a.score).slice(0,2);
   for(const c of candidates)snippets+='\nWINDOWS CANDIDATE '+c.n+'\n'+c.text+'\n';
 }
}
const metadata=x=>({file:x.filename,bundle_sha256:hash(x.source),version:x.package.version,dependencies:x.package.dependencies});
fs.writeFileSync('/tmp/desktop-inspection/platform-comparison.json',JSON.stringify({linux:metadata(linux),windows:metadata(win),note:'Identifier-normalized syntax equality is not proof of identical dependencies or operating-system behavior.',symbols:rows},null,2));
fs.writeFileSync('/tmp/desktop-inspection/platform-transport-source.txt',snippets);
console.log('Platform comparison',linux.package.version,win.package.version,rows.map(r=>[r.linux_symbol,r.windows_matches]));

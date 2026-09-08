// Structural comparison of pinned bundles; preserve property names and literals.
function canonical(node){
 const names=new Map();
 const builtins=new Set(['require','process','Buffer','console','globalThis','Object','Array','Error','TypeError','RangeError','SyntaxError','Map','Set','WeakMap','WeakSet','Promise','JSON','Math','Number','String','Boolean','Symbol','BigInt','RegExp','Date','Reflect','Proxy','undefined','NaN','Infinity','setTimeout','clearTimeout','setInterval','clearInterval','queueMicrotask','URL','URLSearchParams','AbortController','AbortSignal','TextEncoder','TextDecoder','navigator','window']);
 const rename=name=>{if(!names.has(name))names.set(name,'id'+names.size);return names.get(name)};
 function visit(n,parent,key){
  if(typeof n==='bigint')return {bigint:String(n)};
  if(n===null||typeof n!=='object')return n;
  if(Array.isArray(n))return n.map(x=>visit(x,parent,key));
  if(n.type==='Identifier'){
   const property=(parent?.type==='MemberExpression'&&key==='property'&&!parent.computed)||(['Property','MethodDefinition','PropertyDefinition'].includes(parent?.type)&&key==='key'&&!parent.computed);
   return {type:n.type,name:property||builtins.has(n.name)?n.name:rename(n.name)};
  }
  const result={};for(const [k,v]of Object.entries(n))if(!['start','end','loc','range','ranges','raw'].includes(k))result[k]=visit(v,n,k);return result;
 }
 return JSON.stringify(visit(node));
}
module.exports={canonical};

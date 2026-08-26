/** Full flow the Workspace performs: sign up -> upload -> graph -> ask -> delete. */
import { readFileSync } from 'node:fs';
const BASE='http://localhost:8000';
globalThis.localStorage={_d:{},getItem(k){return this._d[k]??null},setItem(k,v){this._d[k]=v},removeItem(k){delete this._d[k]}};
let src=readFileSync('src/services/api.js','utf8').replace("import.meta.env.VITE_API_URL || 'http://localhost:8000'",`'${BASE}'`);
const m=await import('data:text/javascript;base64,'+Buffer.from(src).toString('base64'));
let pass=0,fail=0; const ck=(n,ok,d='')=>{ok?(pass++,console.log('  PASS '+n)):(fail++,console.log('  FAIL '+n+(d?'  -> '+d:'')));};

const email=`ws_${Date.now()}@monash.edu`;
const reg=await m.register(email,'a good password','WS Tester');
localStorage.setItem('sg_token',reg.token);
ck('sign up',Boolean(reg.token));

// graph is empty before any upload
let g=await m.fetchGraph();
ck('graph starts empty',(g.nodes||[]).length===0,JSON.stringify(g).slice(0,60));

// upload exactly as Workspace does (5 args, callback 4th)
const pdf=new File([Buffer.from('%PDF-1.4\nrespiration\n%%EOF\n'.repeat(40))],'lecture.pdf',{type:'application/pdf'});
const stages=[];
const up=await m.uploadDocument(pdf,'llm',null,(s)=>{if(s.stage==='running')stages.push(s.operation||'run')},null);
ck('upload completes',up.document.status==='Ready',JSON.stringify(up.document).slice(0,90));
ck('progress callback fired',stages.length>0,stages.join(','));

// THE key check: the graph is generated from the upload
g=await m.fetchGraph();
ck('GRAPH GENERATED from the upload',(g.nodes||[]).length>0,`${(g.nodes||[]).length} nodes`);
ck('graph has edges',(g.edges||[]).length>0,`${(g.edges||[]).length} edges`);

// the mapping Workspace applies
const raw=(g.nodes||[]).map(n=>{const w=(n.weeks||[]).map(x=>parseInt(String(x).replace(/\D+/g,''),10)).filter(Number.isFinite);
  return {id:String(n.id??n.label),name:n.label??n.id,week:w.length?Math.min(...w):1,degree:n.degree??0};});
const ds=[...raw].map(n=>n.degree).sort((a,b)=>b-a); const cut=Math.max(1,ds[Math.floor(ds.length/3)]??1);
const nodes=raw.map(n=>({...n,kind:n.degree>=cut?'core':'support'}));
const ids=new Set(nodes.map(n=>n.id));
const edges=(g.edges||[]).filter(e=>ids.has(String(e.from))&&ids.has(String(e.to)));
ck('every node maps with id/name/week/kind',nodes.every(n=>n.id&&n.name&&n.week&&n.kind));
ck('edges survive the id filter',edges.length>0,`${edges.length}`);
ck('at least one core node (drawn larger)',nodes.some(n=>n.kind==='core'));

// documents tab
const docs=(await m.listDocuments()).documents;
ck('documents table has the row',docs.length===1);
ck('row shows concept count',(docs[0].nodes??0)>0,String(docs[0].nodes));

// chat
const a=await m.askQuestion('how does the proton gradient form');
ck('chat answers',Boolean(a.answer),String(a.answer).slice(0,50));

await m.deleteDocument(docs[0].id);
ck('delete works',(await m.listDocuments()).documents.length===0);
console.log(`\nRESULT: ${pass}/${pass+fail} passed`); process.exit(fail?1:0);

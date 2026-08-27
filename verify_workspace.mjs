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
const _deg={}; (g.edges||[]).forEach(e=>{_deg[e.source]=(_deg[e.source]||0)+1;_deg[e.target]=(_deg[e.target]||0)+1;});
const raw=(g.nodes||[]).map(n=>{const w=(n.weeks||[]).map(x=>parseInt(String(x).replace(/\D+/g,''),10)).filter(Number.isFinite);
  return {id:String(n.id),name:n.name??String(n.id),week:w.length?Math.min(...w):1,degree:_deg[n.id]||0};});
const ds=[...raw].map(n=>n.degree).sort((a,b)=>b-a); const cut=Math.max(1,ds[Math.floor(ds.length/3)]??1);
const nodes=raw.map(n=>({...n,kind:n.degree>=cut?'core':'support'}));
const ids=new Set(nodes.map(n=>n.id));
const edges=(g.edges||[]).filter(e=>ids.has(String(e.source))&&ids.has(String(e.target)));
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
// The contract main.py actually serves. These names were wrong once — the
// mapping was written against the dev stub instead of the real backend, so
// every edge was dropped and the week slider collapsed to "Week 1 of 1".
const n0 = (g.nodes||[])[0], e0 = (g.edges||[])[0];
ck('node carries name (not label)', 'name' in n0, Object.keys(n0).join(','));
ck('node carries weeks (not just a count)', Array.isArray(n0.weeks), JSON.stringify(n0.weeks));
ck('node carries source list', Array.isArray(n0.source), JSON.stringify(n0.source));
ck('edge uses source/target (not from/to)',
   e0 && 'source' in e0 && 'target' in e0 && !('from' in e0), Object.keys(e0||{}).join(','));
ck('edge carries a label', Boolean(e0 && (e0.label || e0.relation)), JSON.stringify(e0));

// the mapping the Workspace performs, end to end
const deg={}; (g.edges||[]).forEach(e=>{deg[e.source]=(deg[e.source]||0)+1;deg[e.target]=(deg[e.target]||0)+1;});
const mapped=(g.nodes||[]).map(n=>({id:String(n.id),name:n.name||String(n.id),
  week:(n.weeks||[]).length?Math.min(...n.weeks.map(w=>parseInt(String(w).replace(/\D+/g,''),10))):1,
  degree:deg[n.id]||0}));
const mids=new Set(mapped.map(n=>n.id));
const medges=(g.edges||[]).filter(e=>mids.has(String(e.source))&&mids.has(String(e.target)));
ck('EDGES SURVIVE THE MAPPING', medges.length>0, `${medges.length} of ${(g.edges||[]).length}`);
ck('names are readable, not snake_case', mapped.every(n=>!/^[a-z0-9]+(_[a-z0-9]+)+$/.test(n.name)),
   mapped.slice(0,3).map(n=>n.name).join(' | '));
ck('at least one node has a real degree', mapped.some(n=>n.degree>0));

// status keys
const st = await m.getPipelineStatus();
ck('status reports raw_concepts', 'raw_concepts' in st || st.stage!=='done', Object.keys(st).join(','));

console.log(`\nRESULT: ${pass}/${pass+fail} passed`); process.exit(fail?1:0);

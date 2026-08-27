/** A row left on "Running" (browser refreshed mid-upload) must self-heal. */
import { readFileSync } from 'node:fs';
const BASE='http://localhost:8000';
globalThis.localStorage={_d:{},getItem(k){return this._d[k]??null},setItem(k,v){this._d[k]=v},removeItem(k){delete this._d[k]}};
let src=readFileSync('src/services/api.js','utf8').replace("import.meta.env.VITE_API_URL || 'http://localhost:8000'",`'${BASE}'`);
const m=await import('data:text/javascript;base64,'+Buffer.from(src).toString('base64'));
let pass=0,fail=0; const ck=(n,ok,d='')=>{ok?(pass++,console.log('  PASS '+n)):(fail++,console.log('  FAIL '+n+(d?'  -> '+d:'')));};

const reg=await m.register(`rc_${Date.now()}@monash.edu`,'a good password','R');
localStorage.setItem('sg_token',reg.token);

// upload normally so the engine really holds concepts
const pdf=new File([Buffer.from('%PDF-1.4\nlecture\n%%EOF\n'.repeat(40))],'stuck.pdf',{type:'application/pdf'});
const up=await m.uploadDocument(pdf,'llm',null,null,null);
ck('upload completed', up.document.status==='Ready', up.document.status);

// simulate the browser dying mid-poll: force the row back to running/0/0
await m.patchDocument(up.document.id,{status:'running',concepts:0,relationships:0});
let d=(await m.listDocuments()).documents[0];
ck('row is stuck on Running with 0/0',
   String(d.status).toLowerCase()==='running' && (d.nodes??0)===0, `${d.status} ${d.nodes}`);

// stats must know the truth
const {stats}=await m.fetchDocumentStats();
ck('stats report the document', Boolean(stats['stuck.pdf']), JSON.stringify(stats).slice(0,90));
ck('stats give a concept count', (stats['stuck.pdf']?.concepts??0)>0, String(stats['stuck.pdf']?.concepts));

// reconcile exactly as the Workspace does
const fix=stats['stuck.pdf'];
await m.patchDocument(d.id,{status:'done',concepts:fix.concepts,relationships:fix.edges});
d=(await m.listDocuments()).documents[0];
ck('ROW RECOVERS to Ready', String(d.status).toLowerCase()==='ready', d.status);
ck('concept count restored',(d.nodes??0)>0, String(d.nodes));

console.log(`\nRESULT: ${pass}/${pass+fail} passed`); process.exit(fail?1:0);

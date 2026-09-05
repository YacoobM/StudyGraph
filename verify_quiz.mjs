/** Quizzes must be generated from the uploaded document, not canned. */
import { readFileSync } from 'node:fs';
const BASE='http://localhost:8000';
globalThis.localStorage={_d:{},getItem(k){return this._d[k]??null},setItem(k,v){this._d[k]=v},removeItem(k){delete this._d[k]}};
let src=readFileSync('src/services/api.js','utf8').replace("import.meta.env.VITE_API_URL || 'http://localhost:8000'",`'${BASE}'`);
const m=await import('data:text/javascript;base64,'+Buffer.from(src).toString('base64'));
let pass=0,fail=0; const ck=(n,ok,d='')=>{ok?(pass++,console.log('  PASS '+n)):(fail++,console.log('  FAIL '+n+(d?'  -> '+d:'')));};
const PTS={easy:10,medium:20,hard:30};

const reg=await m.register(`qz_${Date.now()}@monash.edu`,'a good password','Quiz Tester');
localStorage.setItem('sg_token',reg.token);

let qz=await m.fetchQuiz();
ck('no quizzes before any upload',qz.quizzes.length===0&&qz.generated===false,JSON.stringify(qz).slice(0,70));
ck('the empty state explains why',Boolean(qz.reason),String(qz.reason));

const pdf=new File([Buffer.from('%PDF-1.4\nlecture\n%%EOF\n'.repeat(40))],'wk1.pdf',{type:'application/pdf'});
await m.uploadDocument(pdf,'llm',null,null,null);

qz=await m.fetchQuiz();
ck('QUIZZES GENERATED after upload',qz.quizzes.length>0,`${qz.quizzes.length} quizzes`);
ck('topics generated too',qz.topics.length>0,`${qz.topics.length}`);
ck('reports how it generated them',['llm','graph'].includes(qz.source),String(qz.source));

const all=qz.quizzes.flatMap(q=>q.questions);
ck('every question has q/o/a/why/d',all.every(q=>q.q&&q.o?.length>=3&&Number.isInteger(q.a)&&q.why&&PTS[q.d]!==undefined));
ck('every answer index is in range',all.every(q=>q.a>=0&&q.a<q.o.length));
ck('no duplicate options within a question',all.every(q=>new Set(q.o).size===q.o.length));
ck('no distractor leaks from the question stem',
   all.every(q=>q.o.every((o,i)=>i===q.a||!q.q.toLowerCase().includes(o.toLowerCase()))));

// the decisive check: questions mention concepts from the graph
const g=await m.fetchGraph();
const concepts=new Set((g.nodes||[]).map(n=>n.name||String(n.id)));
const grounded=all.filter(q=>[...concepts].some(c=>q.q.includes(c)||q.o.includes(c)));
ck('questions are about the uploaded document\'s concepts',grounded.length===all.length,
   `${grounded.length}/${all.length}`);
ck('answers are real concepts, not invented',all.every(q=>concepts.has(q.o[q.a])));
ck('explanations cite the material',all.every(q=>q.why.length>10));

// results round trip
const first=qz.quizzes[0];
const r=await m.saveQuizResult(first.id,first.questions.length,first.questions.length,60);
ck('a perfect attempt passes',r.passed===true,JSON.stringify(r));
const r2=await m.saveQuizResult(first.id,0,first.questions.length,0);
ck('a worse re-attempt keeps the better score',r2.result.points===60,JSON.stringify(r2.result));

console.log(`\nRESULT: ${pass}/${pass+fail} passed`); process.exit(fail?1:0);

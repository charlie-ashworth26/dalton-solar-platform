/*
 * Upload state must not leak between enrollments.
 *
 * BUG: starting Enrollment B still showed Enrollment A's bill filename.
 * resetWizardState() cleared state.bill but NOT DocSets - the multi-file
 * upload state that actually renders the visible filename list. docSetReset()
 * existed but was never called by anything. The visible list and the real
 * state had diverged, which is why Continue still correctly blocked.
 */
const fs=require('fs'),path=require('path'),vm=require('vm');
const ROOT=path.join(__dirname,'..');
const src=fs.readFileSync(path.join(ROOT,'static','js','app.js'),'utf8');
const html=fs.readFileSync(path.join(ROOT,'templates','index.html'),'utf8');
const IDS=new Set([...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]));
const DYN=new Set(['bill-fileset','lmi-fileset','program-options','agr-card-error']);
const R=[]; const check=(l,c)=>{R.push({l,ok:!!c});console.log(`  [${c?'PASS':'FAIL'}] ${l}`);};

function mk(id){return {id,innerHTML:'',textContent:'',value:'',disabled:false,checked:false,
  style:{},dataset:{},className:'',classList:{add(){},remove(){},toggle(){},contains:()=>false},
  setAttribute(){},getAttribute:()=>null,appendChild(){},insertBefore(){},removeChild(){},
  firstChild:null,focus(){},click(){},addEventListener(){},removeEventListener(){},
  querySelector:()=>null,querySelectorAll:()=>[],offsetWidth:0};}

function env(){
  const cache=new Map();
  const doc={getElementById:id=>((IDS.has(id)||DYN.has(id))?(cache.has(id)||cache.set(id,mk(id)),cache.get(id)):null),
    createElement:mk,querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){},
    removeEventListener(){},body:Object.assign(mk('body'),{style:{}}),activeElement:null};
  const sb={console,document:doc,
    sessionStorage:{getItem:()=>'tok',setItem(){},removeItem(){}},
    localStorage:{getItem:()=>'tok',setItem(){},removeItem(){}},
    setTimeout:()=>0,clearTimeout(){},setInterval:()=>0,clearInterval(){},alert(){},
    scrollTo(){},confirm:()=>true,FormData:function(){this.append=()=>{}},
    navigator:{userAgent:'h'},location:{href:''},requestAnimationFrame:()=>0,
    pdfjsLib:{GlobalWorkerOptions:{}},Tesseract:{},
    URL:{createObjectURL:()=>'blob:x',revokeObjectURL(){}},open:()=>({location:'',close(){}}),
    fetch:()=>Promise.resolve({ok:true,status:200,json:()=>Promise.resolve({})})};
  sb.window=sb; const ctx=vm.createContext(sb); vm.runInContext(src,ctx,{filename:'app.js'});
  return {ctx,cache};
}

console.log('='.repeat(72));
console.log('UPLOAD STATE ISOLATION between enrollments');
console.log('='.repeat(72));

const e=env();
const run=code=>vm.runInContext(code,e.ctx);

console.log('\n--- A. Enrollment A uploads a bill ---');
run(`currentDraft={enrollment_id:1};
     DocSets.utility_bill = {id: 11, files: [
       {key:'k1', file:{name:'A-bill.pdf', size:2048}, documentId: 101}]};
     DocSets.lmi_document = {id: 22, files: [
       {key:'k2', file:{name:'A-lmi.pdf', size:1024}, documentId: 202}]};
     state.bill = {fileName:'A-bill.pdf', amount:'120', documentId:101};
     state.lmi.fileName = 'A-lmi.pdf'; state.lmi.documentId = 202;
     renderDocSetList('utility_bill');`);
check('A has a bill file in DocSets', run(`DocSets.utility_bill.files.length`)===1);
check('  ...with A\'s filename', run(`DocSets.utility_bill.files[0].file.name`)==='A-bill.pdf');
check('  ...and A\'s document id', run(`DocSets.utility_bill.files[0].documentId`)===101);
check('  ...rendered into the visible list',
  (e.cache.get('bill-fileset')||{}).innerHTML.includes('A-bill.pdf'));
check('state.bill holds A\'s document', run(`state.bill.documentId`)===101);

console.log('\n--- B. Starting Enrollment B clears EVERYTHING ---');
run(`resetWizardState();`);
check('DocSets utility_bill emptied', run(`DocSets.utility_bill.files.length`)===0);
check('  ...and its set id cleared', run(`DocSets.utility_bill.id`)===null);
check('DocSets lmi_document emptied', run(`DocSets.lmi_document.files.length`)===0);
check('  ...and its set id cleared', run(`DocSets.lmi_document.id`)===null);
check('state.bill cleared', run(`state.bill.documentId`)===null && run(`state.bill.fileName`)==='');
check('  ...amount cleared', run(`state.bill.amount`)==='');
check('state.lmi cleared',
  run(`state.lmi.documentId`)===null && run(`state.lmi.fileName`)==='');
check('the visible bill list is empty',
  (e.cache.get('bill-fileset')||{}).innerHTML==='');
check('the visible LMI list is empty',
  (e.cache.get('lmi-fileset')||{}).innerHTML==='');
check('the legacy file chip is empty',
  (e.cache.get('bill-file-chip')||{}).innerHTML==='');
check('the file input is cleared', (e.cache.get('bill-file')||{}).value==='');
check('the LMI file input is cleared', (e.cache.get('lmi-file')||{}).value==='');
check('the OCR result region is cleared',
  (e.cache.get('ocr-container')||{}).innerHTML==='');
check('runtime file handles dropped',
  run(`billRuntimeFile===null && lmiRuntimeFile===null`)===true);
check('  ...and in-flight uploads abandoned',
  run(`billUploadPromise===null && lmiUploadPromise===null`)===true);
check('billTouched reset', run(`billTouched`)===false);

console.log('\n--- C. No trace of A\'s document ids remains ---');
const dump = run(`JSON.stringify({d:DocSets, b:state.bill, l:state.lmi})`);
check('101 (A bill doc) absent from B state', !dump.includes('101'));
check('202 (A LMI doc) absent from B state', !dump.includes('202'));
check('11 / 22 (A set ids) absent',
  run(`DocSets.utility_bill.id===null && DocSets.lmi_document.id===null`)===true);
check('A-bill.pdf absent', !dump.includes('A-bill.pdf'));
check('A-lmi.pdf absent', !dump.includes('A-lmi.pdf'));

console.log('\n--- E. B can upload its own bill normally ---');
run(`DocSets.utility_bill = {id: 33, files: [
       {key:'k9', file:{name:'B-bill.pdf', size:4096}, documentId: 303}]};
     state.bill = {fileName:'B-bill.pdf', amount:'90', documentId:303};
     renderDocSetList('utility_bill');`);
check('B\'s own file registers', run(`DocSets.utility_bill.files[0].documentId`)===303);
check('  ...and renders', (e.cache.get('bill-fileset')||{}).innerHTML.includes('B-bill.pdf'));
check('  ...with no trace of A',
  !(e.cache.get('bill-fileset')||{}).innerHTML.includes('A-bill.pdf'));

console.log('\n--- F/G. Switching enrollments again clears B ---');
run(`resetWizardState();`);
check('B\'s state cleared on the next switch',
  run(`DocSets.utility_bill.files.length`)===0 && run(`state.bill.documentId`)===null);
check('  ...visible list empty', (e.cache.get('bill-fileset')||{}).innerHTML==='');

console.log('\n--- I. OCR state resets between enrollments ---');
check('OCR container cleared by the form reset',
  /getElementById\('ocr-container'\)\.innerHTML=''/.test(src));
check('prefill scope note is re-rendered per enrollment', /renderPrefillScope/.test(src));

console.log('\n--- the fix itself ---');
check('docSetResetAll() exists', /function docSetResetAll\(\)/.test(src));
check('  ...and is called by resetWizardState', /docSetResetAll\(\);/.test(src));
check('  ...iterating every category (future-proof)',
  /Object\.keys\(DocSets\)\.forEach/.test(src.split('function docSetResetAll')[1]));
check('reset runs on BOTH entry paths',
  /async function startWizardFresh[\s\S]{0,600}resetWizardState\(\)/.test(src)
  && /async function openEnrollment[\s\S]{0,600}resetWizardState\(\)/.test(src));
check('the DocSets containers are emptied in the DOM',
  /\['bill-fileset','lmi-fileset'\]\.forEach/.test(src));
check('the filename is not merely hidden (state itself is reset)',
  !/display:\s*none[^;]*bill-fileset/.test(src));

console.log('\n--- J. multi-file flow intact ---');
for(const fn of ['docSetAddFiles','docSetRemove','renderDocSetList','uploadDocumentSet',
                 'docSetNameHtml','openStoredDocument','openPendingDocument'])
  check(`${fn}() still present`, src.includes('function '+fn+'('));
check('multi-file upload endpoint unchanged', src.includes('/document-sets'));
check('inline document viewing preserved', src.includes('/view'));

console.log('\n--- unrelated behaviour preserved ---');
check('program persistence', src.includes('loadProgramOptions') && src.includes('selected_customer_type'));
check('branch-aware steps', src.includes('function activeSteps'));
check('one acknowledgement checkbox', (src.match(/id="agr-ack-check"/g)||[]).length===1);
check('one Agree & finish button', (src.match(/id="agr-agree-btn"/g)||[]).length===1);
check('resume reconciliation', src.includes('canRegenerate'));
check('OCR path', src.includes('extractTextFromFile') && src.includes('parseUtilityBill'));

const f=R.filter(r=>!r.ok);
console.log('\n'+'='.repeat(72));
console.log(`${R.length-f.length} passed, ${f.length} failed`);
console.log('='.repeat(72));
if(f.length){f.forEach(x=>console.log('  FAILED: '+x.l));process.exit(1);}
process.exit(0);

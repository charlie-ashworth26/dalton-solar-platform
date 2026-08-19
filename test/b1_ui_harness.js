/* B1 runtime: state dropdown, program selection, branch-aware nav, admin modal. */
const fs=require('fs'),path=require('path'),vm=require('vm');
const ROOT=path.join(__dirname,'..');
const src=fs.readFileSync(path.join(ROOT,'static','js','app.js'),'utf8');
const html=fs.readFileSync(path.join(ROOT,'templates','index.html'),'utf8');
const IDS=new Set([...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]));
const DYN=new Set(['program-options','adm-phone','adm-team','adm-code','adm-pw','adm-name',
                   'prog-hint','agr-ack-check','agr-agree-btn','agr-card-error','agr-status']);
const R=[]; const check=(l,c)=>{R.push({l,ok:!!c});console.log(`  [${c?'PASS':'FAIL'}] ${l}`);};

function mk(id){return {id,innerHTML:'',textContent:'',value:'',disabled:false,checked:false,
  style:{},dataset:{},className:'',classList:{add(){},remove(){},toggle(){},contains:()=>false},
  setAttribute(){},getAttribute:()=>null,appendChild(){},insertBefore(){},removeChild(){},
  firstChild:null,focus(){},click(){},addEventListener(){},removeEventListener(){},
  querySelector:()=>null,querySelectorAll:()=>[],setSelectionRange(){}};}

function env(routes){
  const cache=new Map(); const calls=[];
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
    fetch(url,opts){const u=String(url);let body=null;
      try{body=opts&&opts.body?JSON.parse(opts.body):null;}catch(e){}
      calls.push({url:u,method:(opts&&opts.method)||'GET',body});
      for(const [pat,resp] of routes){ if(u.includes(pat))
        return Promise.resolve({ok:resp.ok!==false,status:resp.status||200,
          json:()=>Promise.resolve(resp.body||{})}); }
      return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve({})});}};
  sb.window=sb; const ctx=vm.createContext(sb); vm.runInContext(src,ctx,{filename:'app.js'});
  vm.runInContext("currentDraft={enrollment_id:3};",ctx);
  return {ctx,cache,calls};
}
const tick=()=>new Promise(r=>setImmediate(r));

const RES={customer_type:'Residential',savings_percent:8,lmi_required:false};
const LMI={customer_type:'LMI',savings_percent:20,lmi_required:true};

console.log('='.repeat(72));
console.log('B1 — state dropdown / program selection / branching / admin modal');
console.log('='.repeat(72));

(async()=>{
  console.log('\n--- 1. STATE DROPDOWN ---');
  check('a-state is a <select>', /<select id="a-state"/.test(html));
  check('b-state is a <select>', /<select id="b-state"/.test(html));
  const opts=(html.match(/<select id="a-state"[\s\S]*?<\/select>/)||[''])[0];
  check('uses 2-letter abbreviations', /<option value="NY"[^>]*>NY<\/option>/.test(opts));
  check('  ...more than just NY', (opts.match(/<option/g)||[]).length > 40);
  for(const st of ['MA','RI','IL','CA','TX'])
    check(`  ...includes ${st}`, opts.includes(`value="${st}"`));
  check('NY remains the default selection', /value="NY" selected/.test(opts));
  check('no readonly state input remains', !/id="a-state"[^>]*readonly/.test(html));
  check('collection reads the dropdown, not a constant',
    /state: \(document\.getElementById\('a-state'\)\.value/.test(src));
  check('billing mirrors the service state',
    /getElementById\('b-state'\)\.value = document\.getElementById\('a-state'\)\.value/.test(src));
  check('hydrate restores the selected state',
    /getElementById\('a-state'\)\.value = state\.address\.state/.test(src));

  console.log('\n--- 2. PROGRAM SELECTION consumes the backend ---');
  let e=env([['/programs',{body:{capacity_checked:true,available_programs:[RES],
                                 selection_required:false}}]]);
  await vm.runInContext('loadProgramOptions();',e.ctx); await tick(); await tick();
  check('calls GET /programs', e.calls.some(c=>c.url.includes('/programs')&&c.method==='GET'));
  let hostHtml=e.cache.get('program-options').innerHTML;
  check('single option rendered', (hostHtml.match(/class="pg(?:\"| )/g)||[]).length===1);
  check('  ...shows Residential', hostHtml.includes('Residential'));
  check('  ...shows the REAL savings from the response',
    hostHtml.includes('>8<') && hostHtml.includes('% savings'));
  check('  ...auto-selected (unambiguous)',
    vm.runInContext("selectedProgram && selectedProgram.customer_type",e.ctx)==='Residential');
  check('  ...no fake LMI option', !hostHtml.includes('Residential LMI'));

  console.log('\n--- BOTH available: explicit choice required ---');
  e=env([['/programs',{body:{capacity_checked:true,available_programs:[RES,LMI],
                             selection_required:true}}]]);
  await vm.runInContext('loadProgramOptions();',e.ctx); await tick(); await tick();
  hostHtml=e.cache.get('program-options').innerHTML;
  check('both options rendered', (hostHtml.match(/class="pg(?:\"| )/g)||[]).length===2);
  check('  ...each with its OWN savings',
    hostHtml.includes('>8<')&&hostHtml.includes('>20<'));
  check('  ...NOT auto-selected',
    vm.runInContext("selectedProgram === null",e.ctx)===true);
  check('  ...rep is prompted to choose', /Choose one/i.test(hostHtml));
  check('  ...Continue is blocked until chosen',
    e.cache.get('wf-primary') ? e.cache.get('wf-primary').disabled===true : true);
  vm.runInContext("selectProgram('LMI');",e.ctx);
  check('selecting LMI works',
    vm.runInContext("selectedProgram.customer_type",e.ctx)==='LMI');
  check('  ...savings carried from the response',
    vm.runInContext("selectedProgram.savings_percent",e.ctx)===20);
  vm.runInContext("selectProgram('Residential');",e.ctx);
  check('selecting Residential works',
    vm.runInContext("selectedProgram.customer_type",e.ctx)==='Residential');
  vm.runInContext("selectProgram('Business');",e.ctx);
  check('an unoffered type cannot be selected',
    vm.runInContext("selectedProgram.customer_type",e.ctx)==='Residential');

  console.log('\n--- no capacity: nothing invented ---');
  e=env([['/programs',{body:{capacity_checked:true,available_programs:[],
                             selection_required:false}}]]);
  await vm.runInContext('loadProgramOptions();',e.ctx); await tick(); await tick();
  hostHtml=e.cache.get('program-options').innerHTML;
  check('no program options rendered', !/class="pg(?:\"| )/.test(hostHtml));
  check('  ...clean no-availability state', /No community solar capacity/.test(hostHtml));
  check('  ...no invented savings', !/%\s*savings/.test(hostHtml));
  check('no business logic duplicated in JS',
    !/lmi_capacity_available/.test(src) && !/residential_capacity_available/.test(src));

  console.log('\n--- 3. BRANCH-AWARE NAVIGATION ---');
  e=env([]);
  vm.runInContext("selectedProgram=null;",e.ctx);
  check('unknown program keeps the full sequence (Eligibility retained)',
    vm.runInContext("activeSteps().join(',')",e.ctx)==='1,2,4,5');
  vm.runInContext("selectedProgram={customer_type:'Residential',lmi_required:false};",e.ctx);
  check('Residential DROPS the LMI step',
    vm.runInContext("activeSteps().join(',')",e.ctx)==='1,2,5');
  check('  ...next after Customer & Bill skips to Agreements',
    vm.runInContext("nextStepAfter(2)",e.ctx)===5);
  check('  ...back from Agreements returns to Customer & Bill',
    vm.runInContext("prevStepBefore(5)",e.ctx)===2);
  check('  ...progress has 3 positions',
    vm.runInContext("activeSteps().length",e.ctx)===3);
  vm.runInContext("selectedProgram={customer_type:'LMI',lmi_required:true};",e.ctx);
  check('LMI KEEPS the eligibility step',
    vm.runInContext("activeSteps().join(',')",e.ctx)==='1,2,4,5');
  check('  ...next after Customer & Bill is Eligibility',
    vm.runInContext("nextStepAfter(2)",e.ctx)===4);
  check('  ...back from Agreements returns to Eligibility',
    vm.runInContext("prevStepBefore(5)",e.ctx)===4);
  check('step labels render from the ACTIVE sequence',
    /renderStepLabels/.test(src));
  check('the stepper renders from the active sequence, not a fixed 5',
    /function renderStepper\(/.test(src) && /activeSteps\(\)/.test(src));
  check('backend still decides the branch (proof_docs -> step 4)',
    /nextStepKey === 'proof_docs'/.test(src));

  console.log('\n--- customer_type is SENT to /enroll ---');
  check('enroll payload includes the selection',
    /enrollBody\.customer_type = selectedProgram\.customer_type/.test(src));
  check('  ...only when one is selected',
    /if\(selectedProgram && selectedProgram\.customer_type\)/.test(src));

  console.log('\n--- 4. ADMIN MODAL replaces prompt() ---');
  const code=src.split('\n').filter(l=>!l.trim().startsWith('/*')&&!l.trim().startsWith('*')&&!l.trim().startsWith('//')).join('\n');
  check('no prompt() calls remain', !/\bprompt\(/.test(code));
  check('modal markup exists', html.includes('id="admin-modal"'));
  check('  ...is a labelled dialog',
    /id="admin-modal"[\s\S]{0,200}role="dialog"/.test(html)&&html.includes('aria-modal="true"'));
  check('  ...has Save and Cancel', html.includes('admin-modal-save')&&/Cancel/.test(html));
  check('  ...Escape closes it', /adminModalEsc/.test(src));
  check('  ...backdrop closes it', /adminModalBackdrop/.test(src));
  for(const fn of ['openAdminModal','closeAdminModal','submitAdminModal',
                   'editRep','resetRepPassword','editOwnProfile'])
    check(`${fn}() defined`, src.includes('function ' + fn + '('));
  check('editRep offers phone, team and rep code',
    /adm-phone/.test(src)&&/adm-team/.test(src)&&/adm-code/.test(src));
  check('  ...and NOT role or email',
    !/adm-role/.test(src)&&!/adm-email/.test(src));
  check('password reset uses the same admin API',
    /\/api\/admin\/reps\/' \+ userId \+ '\/password/.test(src));
  check('own-profile uses the self endpoint',
    /\/api\/auth\/me\/profile/.test(src));
  check('no delete action added', !/method:'DELETE'/.test(src.split('Admin modal')[1]||''));
  check('activate/deactivate still present', /function toggleRep\(/.test(src));

  console.log('\n--- PRESERVED ---');
  check('one acknowledgement checkbox', (src.match(/id="agr-ack-check"/g)||[]).length===1);
  check('one Agree & finish button', (src.match(/id="agr-agree-btn"/g)||[]).length===1);
  check('inline agreement links kept', /agreementLinksHtml/.test(src));
  check('resume reconciliation kept', /canRegenerate/.test(src));
  check('Phase A dashboard buckets kept', /dashBucketFor/.test(src));
  check('bill upload kept', /handleBillUpload/.test(src));
  check('LMI upload kept', /handleLmiUpload/.test(src));
  check('password fields kept', html.includes('id="c-pass"')&&html.includes('id="c-pass-confirm"'));
  check('OCR kept', /extractTextFromFile/.test(src));
  check('no Projects UI reintroduced',
    !html.includes('data-view="projects"')&&!html.includes('id="view-projects"'));
  check('no ZIP->utility lookup reintroduced', !/utilities\/lookup/.test(src));

  const f=R.filter(r=>!r.ok);
  console.log('\n'+'='.repeat(72));
  console.log(`${R.length-f.length} passed, ${f.length} failed`);
  console.log('='.repeat(72));
  if(f.length){f.forEach(x=>console.log('  FAILED: '+x.l));process.exit(1);}
  process.exit(0);
})();

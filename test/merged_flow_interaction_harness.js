/*
 * INTERACTION harness for the merged Customer & Bill flow.
 *
 * The Pass 1/2 harnesses asserted STRUCTURE (ids/classes present) and therefore
 * missed two live failures:
 *   BUG 1 - after a successful capacity check the app still rendered the
 *           backend's capacity_result descriptor as a standalone
 *           "Capacity confirmed" screen, while RESUME went to Customer & Bill.
 *   BUG 2 - submitContact() drove #btn-contact-next, removed in the merge, so
 *           Continue threw and every failure showed one generic message.
 *
 * This harness DRIVES the functions and asserts observable outcomes.
 */
const fs=require('fs'),path=require('path'),vm=require('vm');
const ROOT=path.join(__dirname,'..');
const src=fs.readFileSync(path.join(ROOT,'static','js','app.js'),'utf8');
const html=fs.readFileSync(path.join(ROOT,'templates','index.html'),'utf8');
const IDS=new Set([...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]));
const DYN=new Set(['stepper','svc-context','program-options','c-email-display',
  'complete-summary','rv-lmi-row','wf-primary','wf-form-error','agr-card-error']);
const R=[]; const check=(l,c)=>{R.push({l,ok:!!c});console.log(`  [${c?'PASS':'FAIL'}] ${l}`);};

function mk(id){return {id,innerHTML:'',textContent:'',value:'',disabled:false,checked:false,
  style:{},dataset:{},className:'',classList:{_s:new Set(),add(c){this._s.add(c);},
    remove(c){this._s.delete(c);},toggle(){},contains(c){return this._s.has(c);}},
  setAttribute(){},getAttribute:()=>null,appendChild(){},insertBefore(){},removeChild(){},
  firstChild:null,focus(){this.focused=true;},click(){},addEventListener(){},
  removeEventListener(){},querySelector:()=>null,querySelectorAll:()=>[],offsetWidth:0};}

function env(routes){
  const cache=new Map(); const calls=[];
  // wf-* fields are rendered dynamically by renderWorkflowStep, so the stub must
  // hand them back like a real DOM would.
  const known=id=>IDS.has(id)||DYN.has(id)||id.startsWith('wf-');
  const doc={getElementById:id=>(known(id)?(cache.has(id)||cache.set(id,mk(id)),cache.get(id)):null),
    createElement:mk,querySelector:()=>null,
    querySelectorAll:sel=>{ if(sel==='.wizard-step') return [...IDS].filter(i=>i.startsWith('step-')).map(i=>cache.get(i)||(cache.set(i,mk(i)),cache.get(i))); return []; },
    addEventListener(){},removeEventListener(){},body:Object.assign(mk('body'),{style:{}}),
    activeElement:null};
  const sb={console,document:doc,
    sessionStorage:{getItem:()=>'t',setItem(){},removeItem(){}},
    localStorage:{getItem:()=>'t',setItem(){},removeItem(){}},
    setTimeout:(f)=>{return 0;},clearTimeout(){},setInterval:()=>0,clearInterval(){},
    alert(){},scrollTo(){},confirm:()=>true,FormData:function(){this.append=()=>{}},
    navigator:{userAgent:'h'},location:{href:''},requestAnimationFrame:()=>0,
    pdfjsLib:{GlobalWorkerOptions:{}},Tesseract:{},
    URL:{createObjectURL:()=>'b',revokeObjectURL(){}},open:()=>({location:'',close(){}}),
    fetch(url,opts){const u=String(url);let b=null;
      try{b=opts&&opts.body?JSON.parse(opts.body):null;}catch(e){}
      calls.push({url:u,method:(opts&&opts.method)||'GET',body:b});
      for(const [pat,resp] of (routes||[])){ if(u.includes(pat))
        return Promise.resolve({ok:resp.ok!==false,status:resp.status||200,
          json:()=>Promise.resolve(resp.body||{})}); }
      return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve({})});}};
  sb.window=sb; const ctx=vm.createContext(sb); vm.runInContext(src,ctx,{filename:'app.js'});
  return {ctx,cache,calls};
}
const tick=()=>new Promise(r=>setImmediate(r));

console.log('='.repeat(72));
console.log('MERGED FLOW — interaction tests');
console.log('='.repeat(72));

(async()=>{
  // ── BUG 1 ──
  console.log('\n--- BUG 1: capacity success lands on Customer & Bill ---');
  let e=env([
    ['/enrollments/capacity',{body:{enrollment_id:7,enrollment_code:'ENR-7',
      result:{zip_code:'10901',utility_slug:'orange-and-rockland',
              utility_display_name:'Orange & Rockland',project_details:{}},
      workflow:{step:{key:'capacity_result'}}}}],
    ['/programs',{body:{capacity_checked:true,selection_required:false,
      available_programs:[{customer_type:'Residential',savings_percent:5,lmi_required:false}]}}],
  ]);
  let ctx=e.ctx;
  vm.runInContext(`currentWorkflow={step:{key:'service_area',fields:[
      {name:'email',label:'Email',type:'email',required:true},
      {name:'zip_code',label:'ZIP',type:'text',required:true},
      {name:'utility_name',label:'Utility',type:'select',required:true,options:[{value:'orange-and-rockland',label:'O&R'}]}],
      primary_action:{operation:'check_capacity'}}};
    currentDraft=null;`,ctx);
  const setv=(id,v)=>{const el=e.cache.get(id)||(e.cache.set(id,mk(id)),e.cache.get(id)); el.value=v;};
  setv('wf-email','a@b.com'); setv('wf-zip_code','10901'); setv('wf-utility_name','orange-and-rockland');
  await vm.runInContext('submitCapacity();',ctx);
  for(let i=0;i<10;i++) await tick();
  check('POST /enrollments/capacity was made',
    e.calls.some(c=>c.url.includes('/enrollments/capacity')&&c.method==='POST'));
  check('currentStep is 2 (Customer & Bill) immediately',
    vm.runInContext('currentStep',ctx)===2);
  check('  ...NOT left on step 1 rendering "Capacity confirmed"',
    vm.runInContext('currentStep',ctx)!==1);
  check('the enrollment was adopted', vm.runInContext('currentDraft && currentDraft.enrollment_id',ctx)===7);
  check('program options were loaded for the merged screen',
    e.calls.some(c=>c.url.includes('/programs')));
  check('capacity_result is never rendered as a screen after success',
    !/renderWorkflowStep\(currentWorkflow\);\s*\n\s*\/\/ Capacity succeeded/.test(src));
  check('source shows the transition, not a descriptor render',
    /await loadProgramOptions\(\);\s*\n\s*\/\/[^\n]*\n?\s*goStep\(2\);|goStep\(2\);\s*\/\/ straight into/.test(src));

  console.log('\n--- no_capacity still renders its dead-end screen ---');
  let e2=env([
    ['/enrollments/capacity',{body:{enrollment_id:8,result:{zip_code:'99999',
      utility_slug:'x',project_details:{}},workflow:{step:{key:'no_capacity'}}}}],
  ]);
  vm.runInContext(`currentWorkflow={step:{key:'service_area',fields:[
      {name:'email',type:'email',required:true},{name:'zip_code',type:'text',required:true},
      {name:'utility_name',type:'select',required:true,options:[{value:'x',label:'X'}]}],
      primary_action:{operation:'check_capacity'}}}; currentDraft=null;`,e2.ctx);
  ['wf-email','wf-zip_code','wf-utility_name'].forEach((id,i)=>{
    const el=e2.cache.get(id)||(e2.cache.set(id,mk(id)),e2.cache.get(id));
    el.value=['a@b.com','99999','x'][i];});
  await vm.runInContext('submitCapacity();',e2.ctx);
  for(let i=0;i<8;i++) await tick();
  check('no_capacity does NOT advance to Customer & Bill',
    vm.runInContext('currentStep',e2.ctx)!==2);

  // ── BUG 2 ──
  console.log('\n--- BUG 2: merged Continue validates field-by-field ---');
  function contactEnv(vals, routes){
    const en=env(routes||[]);
    vm.runInContext(`currentDraft={enrollment_id:9};
      perchContext.email='a@b.com'; perchContext.enrollmentSubmitted=false;
      state.customer.first='Jane'; state.customer.last='Doe';
      state.bill.documentId=55;
      selectedProgram={customer_type:'Residential',lmi_required:false};`,en.ctx);
    for(const [k,v] of Object.entries(vals)){
      const el=en.cache.get(k)||(en.cache.set(k,mk(k)),en.cache.get(k)); el.value=v;
    }
    return en;
  }
  const GOOD={'c-phone':'5185550100','c-pass':'Password1','c-pass-confirm':'Password1'};

  let ok=contactEnv(GOOD,[['/enroll',{body:{next_step_key:'contracts'}}]]);
  let res=await vm.runInContext('submitContactDetails();',ok.ctx);
  for(let i=0;i<6;i++) await tick();
  check('valid input SUCCEEDS', res===true);
  check('  ...PATCHes the customer', ok.calls.some(c=>c.method==='PATCH'));
  // NOTE: '/api/enrollments/9' also contains '/enroll' - match the endpoint exactly.
  const enrollCalls=ok.calls.filter(c=>c.url.endsWith('/enroll'));
  check('  ...POSTs /enroll exactly once', enrollCalls.length===1);
  check('  ...sending the selected customer_type',
    (enrollCalls[0]||{body:{}}).body.customer_type==='Residential');
  check('  ...and the saved bill document id',
    (enrollCalls[0]||{body:{}}).body.document_id===55);
  check('  ...no generic "fill in every field" on success',
    !(ok.cache.get('contact-submit-error')||{}).textContent.includes('every field'));

  const cases=[
    ['missing phone',       {...GOOD,'c-phone':''},          /phone number/i,  'c-phone'],
    ['missing password',    {...GOOD,'c-pass':''},           /Create a password/i,'c-pass'],
    ['short password',      {...GOOD,'c-pass':'abc','c-pass-confirm':'abc'}, /at least 6/i,'c-pass'],
    ['missing confirm',     {...GOOD,'c-pass-confirm':''},   /Confirm the password/i,'c-pass-confirm'],
    ['password mismatch',   {...GOOD,'c-pass-confirm':'Different1'}, /don't match/i,'c-pass-confirm'],
  ];
  for(const [label,vals,re,focusId] of cases){
    const en=contactEnv(vals);
    const r=await vm.runInContext('submitContactDetails();',en.ctx);
    await tick();
    const err=en.cache.get('contact-submit-error')||{};
    check(`${label} BLOCKS`, r===false);
    check(`  ...with a specific message ("${err.textContent}")`, re.test(err.textContent||''));
    check(`  ...error is visible`, err.style && err.style.display==='block');
    check(`  ...focuses #${focusId}`, (en.cache.get(focusId)||{}).focused===true);
    check(`  ...and makes NO API call`, en.calls.length===0);
  }

  console.log('\n--- no dead controls ---');
  const code=src.split('\n').filter(l=>!l.trim().startsWith('//')&&!l.trim().startsWith('*')).join('\n');
  check('no code drives btn-contact-next', !/getElementById\('btn-contact-next'\)/.test(code));
  check('  ...and it is not in the read-only lock list',
    !/READ_ONLY_LOCK_IDS[\s\S]{0,200}'btn-contact-next'/.test(code));
  check('ONE Continue on Customer & Bill',
    (html.match(/id="btn-bill-next"/g)||[]).length===1 && !html.includes('btn-contact-next'));
  check('submitBill drives the merged submit', /await submitContactDetails\(\)/.test(src));
  check('  ...and stops cleanly when validation fails', /if\(!ok\) return;/.test(src));
  check('enrollment email comes from the session, not a hidden field',
    /perchContext\.email \|\| state\.customer\.email/.test(src));

  console.log('\n--- branch after the merged submit ---');
  check('Residential -> Agreements', /nextStepKey === 'contracts'[\s\S]{0,200}goStep\(5\)/.test(src)
    || /goStep\(5\); mountRepAgreements/.test(src));
  check('LMI -> Eligibility', /nextStepKey === 'proof_docs'[\s\S]{0,120}goStep\(4\)/.test(src));
  check('branch decided by the BACKEND next_step_key', /continueFromPerchNextStep/.test(src));

  console.log('\n--- resume matches immediate behaviour ---');
  check('capacity_result resumes to step 2', /capacity_result: 2/.test(src));
  check('enroll resumes to step 2', /enroll: 2,/.test(src));
  check('  ...same destination the live path now uses', /goStep\(2\);/.test(src));

  const f=R.filter(r=>!r.ok);
  console.log('\n'+'='.repeat(72));
  console.log(`${R.length-f.length} passed, ${f.length} failed`);
  console.log('='.repeat(72));
  if(f.length){f.forEach(x=>console.log('  FAILED: '+x.l));process.exit(1);}
  process.exit(0);
})();

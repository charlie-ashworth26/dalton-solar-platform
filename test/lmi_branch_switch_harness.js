/*
 * BUG 3 — Eligibility Back trap, and switching LMI -> Residential.
 * Drives the real functions; asserts which screen is visible and what persists.
 */
const fs=require('fs'),path=require('path'),vm=require('vm');
const ROOT=path.join(__dirname,'..');
const src=fs.readFileSync(path.join(ROOT,'static','js','app.js'),'utf8');
const html=fs.readFileSync(path.join(ROOT,'templates','index.html'),'utf8');
const IDS=new Set([...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]));
const R=[]; const check=(l,c)=>{R.push({l,ok:!!c});console.log(`  [${c?'PASS':'FAIL'}] ${l}`);};
const STEP_IDS=['step-project','step-bill','step-lmi','step-agreement'];

function mk(id){return {id,innerHTML:'',textContent:'',value:'',disabled:false,checked:false,
  style:{},dataset:{},className:'',
  classList:{_s:new Set(),add(c){this._s.add(c);},remove(c){this._s.delete(c);},
    contains(c){return this._s.has(c);},toggle(){}},
  setAttribute(){},getAttribute:()=>null,appendChild(){},insertBefore(){},removeChild(){},
  firstChild:null,focus(){},click(){},addEventListener(){},removeEventListener(){},
  querySelector:()=>null,querySelectorAll:()=>[],scrollIntoView(){},offsetWidth:0};}

function env(programs, persisted){
  const cache=new Map(); const calls=[];
  const known=id=>IDS.has(id)||id.startsWith('wf-');
  const get=id=>(cache.has(id)||cache.set(id,mk(id)),cache.get(id));
  STEP_IDS.forEach(get);
  const doc={getElementById:id=>(known(id)?get(id):null),createElement:mk,
    querySelector:()=>null,
    querySelectorAll:sel=>sel==='.wizard-step'?STEP_IDS.map(get):[],
    addEventListener(){},removeEventListener(){},body:Object.assign(mk('b'),{style:{}}),
    activeElement:null};
  const sb={console,document:doc,
    sessionStorage:{getItem:()=>'t',setItem(){},removeItem(){}},
    localStorage:{getItem:()=>'t'},setTimeout:()=>0,clearTimeout(){},setInterval:()=>0,
    clearInterval(){},alert(){},scrollTo(){},confirm:()=>true,
    FormData:function(){this.append=()=>{}},navigator:{userAgent:'h'},location:{href:''},
    requestAnimationFrame:()=>0,pdfjsLib:{GlobalWorkerOptions:{}},Tesseract:{},
    URL:{createObjectURL:()=>'b'},open:()=>({}),
    fetch(u,o){const s=String(u);let b=null;
      try{b=o&&o.body?JSON.parse(o.body):null;}catch(e){}
      calls.push({url:s,method:(o&&o.method)||'GET',body:b});
      if(s.includes('/programs')) return Promise.resolve({ok:true,status:200,
        json:()=>Promise.resolve({capacity_checked:true,available_programs:programs,
          selection_required:programs.length>1,selected_customer_type:persisted||null})});
      if(s.endsWith('/program')) return Promise.resolve({ok:true,status:200,
        json:()=>Promise.resolve({selected_customer_type:(b||{}).customer_type})});
      return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve({})});}};
  sb.window=sb; const ctx=vm.createContext(sb); vm.runInContext(src,ctx);
  vm.runInContext("currentDraft={enrollment_id:9,enrollment_code:'ENR-9'};",ctx);
  return {ctx,cache,calls,get,
    visible:()=>STEP_IDS.filter(id=>get(id).classList.contains('active'))};
}
const tick=()=>new Promise(r=>setImmediate(r));
const RES={customer_type:'Residential',savings_percent:5,lmi_required:false};
const LMI={customer_type:'LMI',savings_percent:20,lmi_required:true};

console.log('='.repeat(72));
console.log('LMI BACK + BRANCH SWITCH');
console.log('='.repeat(72));

(async()=>{
  console.log('\n--- the exact defect: Back from Eligibility ---');
  check('Eligibility Back no longer targets the REMOVED step 3',
    !/onclick="goStep\(3\)"/.test(html));
  check('  ...it calls backFromLmi()', /onclick="backFromLmi\(\)"/.test(html));
  check('backFromLmi navigates via the ACTIVE sequence',
    /function backFromLmi\(\)\{[\s\S]{0,160}prevStepBefore\(4\)/.test(src));
  check('goStep refuses to blank the wizard on an unknown step',
    /const targetId = stepIds\[n\];[\s\S]{0,200}if\(!targetId/.test(src));

  // A) LMI -> Eligibility -> Back -> Customer & Bill
  console.log('\n--- A. LMI enrollment: Eligibility -> Back -> Customer & Bill ---');
  const e=env([RES,LMI],'LMI');
  await vm.runInContext('loadProgramOptions();',e.ctx); for(let i=0;i<6;i++) await tick();
  check('branch is LMI', vm.runInContext("activeSteps().join(',')",e.ctx)==='1,2,4,5');
  vm.runInContext("state.customer.first='Jane'; state.customer.last='Doe';"
    +"state.customer.acct='1234567890'; state.address.street='56 Lime Kiln Rd';"
    +"state.address.city='Suffern'; state.address.zip='10901';"
    +"state.bill.fileName='bill.pdf'; state.bill.documentId=55;"
    +"DocSets.utility_bill={id:11,files:[{key:'k',file:{name:'bill.pdf',size:2048},documentId:55}]};",e.ctx);
  vm.runInContext('goStep(4);',e.ctx);
  check('Eligibility is showing', e.visible().join()==='step-lmi');
  vm.runInContext('backFromLmi();',e.ctx); await tick();
  check('Back lands on Customer & Bill', e.visible().join()==='step-bill');
  check('  ...exactly ONE screen visible (page did not disappear)', e.visible().length===1);
  check('  ...currentStep is 2', vm.runInContext('currentStep',e.ctx)===2);

  // D) state intact
  console.log('\n--- D. state survives the Back ---');
  check('program still LMI', vm.runInContext("selectedProgram.customer_type",e.ctx)==='LMI');
  check('customer intact', vm.runInContext("state.customer.first+' '+state.customer.last",e.ctx)==='Jane Doe');
  check('account intact', vm.runInContext("state.customer.acct",e.ctx)==='1234567890');
  check('address intact', vm.runInContext("state.address.street",e.ctx)==='56 Lime Kiln Rd');
  check('uploaded bill intact', vm.runInContext("state.bill.documentId",e.ctx)===55);
  check('  ...document set intact', vm.runInContext("DocSets.utility_bill.files.length",e.ctx)===1);
  check('  ...and its Perch document id', vm.runInContext("DocSets.utility_bill.files[0].documentId",e.ctx)===55);

  // B) switch to Residential
  console.log('\n--- B. switch LMI -> Residential from Customer & Bill ---');
  const before=e.calls.length;
  await vm.runInContext("selectProgram('Residential');",e.ctx); for(let i=0;i<6;i++) await tick();
  check('persisted through the existing program endpoint',
    e.calls.slice(before).some(c=>c.url.endsWith('/program')&&c.method==='POST'
      &&c.body.customer_type==='Residential'));
  check('  ...selection is Residential', vm.runInContext("selectedProgram.customer_type",e.ctx)==='Residential');
  check('Eligibility REMOVED from the stepper',
    vm.runInContext("activeSteps().indexOf(4)",e.ctx)===-1);
  check('  ...sequence is 1,2,5', vm.runInContext("activeSteps().join(',')",e.ctx)==='1,2,5');
  check('Continue now goes straight to Agreements', vm.runInContext('nextStepAfter(2)',e.ctx)===5);
  check('NO new enrollment created',
    !e.calls.slice(before).some(c=>c.url.includes('/drafts')||c.url.endsWith('/capacity')));
  check('  ...and no duplicate Perch enrollment',
    !e.calls.slice(before).some(c=>c.url.endsWith('/enroll')));
  check('bill/OCR/customer data still intact after the switch',
    vm.runInContext("state.bill.documentId===55 && state.customer.first==='Jane'"
      +" && DocSets.utility_bill.files.length===1",e.ctx)===true);
  check('Availability not restarted', vm.runInContext('currentStep',e.ctx)===2);

  // C) resume after switching
  console.log('\n--- C. reopen after switching: no forced Eligibility ---');
  const e2=env([RES,LMI],'Residential');
  vm.runInContext(`currentEnrollmentDetail=null;`,e2.ctx);
  // Backend workflow still sits on the LMI step from before the switch.
  vm.runInContext(`selectedProgram={customer_type:'Residential',lmi_required:false};
    let key='proof_docs';
    let target = WORKFLOW_STEP_TO_WIZARD[key] || 1;
    if(activeSteps().indexOf(target) === -1){
      target = (selectedProgram && !programRequiresLmi()) ? 2 : (activeSteps()[0] || 1);
    }
    goStep(target);`,e2.ctx);
  check('branch hydrates Residential', vm.runInContext("activeSteps().join(',')",e2.ctx)==='1,2,5');
  check('  ...resume does NOT force Eligibility', vm.runInContext('currentStep',e2.ctx)!==4);
  check('  ...it lands on Customer & Bill', vm.runInContext('currentStep',e2.ctx)===2);
  check('  ...Eligibility screen is not visible', !e2.visible().includes('step-lmi'));
  check('resume guard exists in source',
    /if\(activeSteps\(\)\.indexOf\(target\) === -1\)/.test(src));

  console.log('\n--- LMI resume still works when LMI is still selected ---');
  const e3=env([RES,LMI],'LMI');
  vm.runInContext(`selectedProgram={customer_type:'LMI',lmi_required:true};
    let t = WORKFLOW_STEP_TO_WIZARD['proof_docs'];
    if(activeSteps().indexOf(t) === -1){ t = 2; }
    goStep(t);`,e3.ctx);
  check('LMI enrollment still resumes to Eligibility', vm.runInContext('currentStep',e3.ctx)===4);
  check('  ...and Eligibility is visible', e3.visible().join()==='step-lmi');

  const f=R.filter(r=>!r.ok);
  console.log('\n'+'='.repeat(72));
  console.log(`${R.length-f.length} passed, ${f.length} failed`);
  console.log('='.repeat(72));
  if(f.length){f.forEach(x=>console.log('  FAILED: '+x.l));process.exit(1);}
  process.exit(0);
})();

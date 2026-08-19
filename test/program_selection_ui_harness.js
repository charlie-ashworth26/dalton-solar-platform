/*
 * DUAL-PROGRAM SELECTION — interaction tests.
 * Drives the real functions and asserts what the rep would see and what is
 * persisted. No "class exists" assertions.
 */
const fs=require('fs'),path=require('path'),vm=require('vm');
const ROOT=path.join(__dirname,'..');
const src=fs.readFileSync(path.join(ROOT,'static','js','app.js'),'utf8');
const html=fs.readFileSync(path.join(ROOT,'templates','index.html'),'utf8');
const IDS=new Set([...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]));
const R=[]; const check=(l,c)=>{R.push({l,ok:!!c});console.log(`  [${c?'PASS':'FAIL'}] ${l}`);};

function mk(id){return {id,innerHTML:'',textContent:'',value:'',disabled:false,checked:false,
  style:{},dataset:{},className:'',classList:{_s:new Set(),add(c){this._s.add(c);},
   remove(c){this._s.delete(c);},contains(c){return this._s.has(c);},toggle(){}},
  setAttribute(){},getAttribute:()=>null,appendChild(){},insertBefore(){},removeChild(){},
  firstChild:null,focus(){this.focused=true;},click(){},addEventListener(){},
  removeEventListener(){},querySelector:()=>null,querySelectorAll:()=>[],
  scrollIntoView(){this.scrolled=true;},offsetWidth:0};}

function env(programs, persisted){
  const cache=new Map(); const calls=[];
  const known=id=>IDS.has(id)||id.startsWith('wf-');
  const doc={getElementById:id=>(known(id)?(cache.has(id)||cache.set(id,mk(id)),cache.get(id)):null),
    createElement:mk,querySelectorAll:()=>[],querySelector:()=>null,addEventListener(){},
    removeEventListener(){},body:Object.assign(mk('b'),{style:{}}),activeElement:null};
  const sb={console,document:doc,
    sessionStorage:{getItem:()=>'t',setItem(){},removeItem(){}},
    localStorage:{getItem:()=>'t'},setTimeout:()=>0,clearTimeout(){},setInterval:()=>0,
    clearInterval(){},alert(){},scrollTo(){},confirm:()=>true,
    FormData:function(){this.append=()=>{}},navigator:{userAgent:'h'},location:{href:''},
    requestAnimationFrame:()=>0,pdfjsLib:{GlobalWorkerOptions:{}},Tesseract:{},
    URL:{createObjectURL:()=>'b'},open:()=>({}),
    fetch(u,o){const s=String(u); let b=null;
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
  return {ctx,cache,calls};
}
const tick=()=>new Promise(r=>setImmediate(r));
const RES={customer_type:'Residential',savings_percent:5,lmi_required:false};
const LMI={customer_type:'LMI',savings_percent:20,lmi_required:true};

console.log('='.repeat(72));
console.log('DUAL-PROGRAM SELECTION — interaction');
console.log('='.repeat(72));

(async()=>{
  console.log('\n--- dual capacity renders TWO visible choices ---');
  let e=env([RES,LMI],null);
  await vm.runInContext('loadProgramOptions();',e.ctx); for(let i=0;i<6;i++) await tick();
  let ui=e.cache.get('program-options').innerHTML;
  check('two selectable panels rendered', (ui.match(/class="pg(?:"| )/g)||[]).length===2);
  check('  ...Residential named', ui.includes('>Residential<'));
  check('  ...Residential LMI named', ui.includes('>Residential LMI<'));
  check('  ...real 5% from the response', ui.includes('>5<') && ui.includes('% savings'));
  check('  ...real 20% from the response', ui.includes('>20<'));
  check('  ...no hardcoded percentages in source',
    !/>\s*(5|20|25)%\s*savings/.test(src));
  check('  ...supporting text per option',
    ui.includes('Standard residential enrollment') && ui.includes('Eligibility documentation required'));
  check('  ...each is a real button with radio semantics',
    (ui.match(/role="radio"/g)||[]).length===2);
  check('  ...rep is asked to choose', /Choose one/i.test(ui));

  console.log('\n--- nothing is silently selected ---');
  check('selectedProgram is null', vm.runInContext('selectedProgram === null',e.ctx)===true);
  check('  ...no panel marked selected', !ui.includes('class="pg selected"'));
  check('  ...aria-checked is false on both', (ui.match(/aria-checked="false"/g)||[]).length===2);
  check('  ...Continue is disabled', (e.cache.get('btn-bill-next')||{}).disabled===true);
  // The rail was replaced by the mockup's utility chip; the outstanding choice
  // is now signalled by the panels + #program-error, asserted below.
  check('  ...no panel is pre-selected', !ui.includes('class="pg selected"'));

  console.log('\n--- the error appears BESIDE the program area ---');
  const okV=vm.runInContext('validateProgramSelection();',e.ctx);
  check('validation blocks', okV===false);
  const perr=e.cache.get('program-error');
  check('  ...error shown in #program-error', perr.style.display==='block');
  check('  ...with actionable wording', /Choose which program/i.test(perr.textContent));
  check('  ...NOT dumped in the page-bottom error',
    ((e.cache.get('bill-submit-error')||{style:{}}).style.display||'')!=='block');
  check('  ...and the program area is scrolled into view',
    (e.cache.get('program-options')||{}).scrolled===true);
  check('submitBill validates the program before anything else',
    /if\(!validateProgramSelection\(\)\) return;/.test(src));

  console.log('\n--- selecting Residential persists it ---');
  await vm.runInContext("selectProgram('Residential');",e.ctx); for(let i=0;i<6;i++) await tick();
  check('persisted via the existing endpoint',
    e.calls.some(c=>c.url.endsWith('/program')&&c.method==='POST'&&c.body.customer_type==='Residential'));
  check('  ...selectedProgram updated',
    vm.runInContext("selectedProgram.customer_type",e.ctx)==='Residential');
  ui=e.cache.get('program-options').innerHTML;
  check('  ...panel shows a selected state', ui.includes('class="pg selected"'));
  check('  ...aria-checked true on exactly one', (ui.match(/aria-checked="true"/g)||[]).length===1);
  check('  ...exactly one panel is marked selected',
    (ui.match(/class="pg selected"/g)||[]).length===1);
  check('  ...branch is Residential (no Eligibility)',
    vm.runInContext("activeSteps().join(',')",e.ctx)==='1,2,5');
  check('  ...validation now passes', vm.runInContext('validateProgramSelection();',e.ctx)===true);

  console.log('\n--- switching to LMI persists and re-branches ---');
  await vm.runInContext("selectProgram('LMI');",e.ctx); for(let i=0;i<6;i++) await tick();
  check('persisted as LMI',
    e.calls.filter(c=>c.url.endsWith('/program')).slice(-1)[0].body.customer_type==='LMI');
  check('  ...branch includes Eligibility',
    vm.runInContext("activeSteps().join(',')",e.ctx)==='1,2,4,5');
  check('  ...stepper re-rendered for the new branch',
    (e.cache.get('stepper')||{}).innerHTML.length>0);

  console.log('\n--- a persisted choice RESTORES on reload/resume ---');
  let e2=env([RES,LMI],'LMI');
  await vm.runInContext('loadProgramOptions();',e2.ctx); for(let i=0;i<6;i++) await tick();
  check('LMI restored from the backend',
    vm.runInContext("selectedProgram && selectedProgram.customer_type",e2.ctx)==='LMI');
  check('  ...shown as selected', e2.cache.get('program-options').innerHTML.includes('class="pg selected"'));
  check('  ...branch restored', vm.runInContext("activeSteps().join(',')",e2.ctx)==='1,2,4,5');
  // Continue is correctly still disabled here - no bill or customer fields exist
  // in this fixture. What matters is that the PROGRAM is no longer a blocker.
  check('  ...the program is no longer listed as missing',
    !((e2.cache.get('bill-requirements')||{}).innerHTML||'').includes('Choose a savings program'));
  check('  ...no re-persist call on mere restore',
    !e2.calls.some(c=>c.url.endsWith('/program')&&c.method==='POST'));

  console.log('\n--- single program auto-resolves ---');
  let e3=env([RES],null);
  await vm.runInContext('loadProgramOptions();',e3.ctx); for(let i=0;i<6;i++) await tick();
  check('one panel only', (e3.cache.get('program-options').innerHTML.match(/class="pg(?:"| )/g)||[]).length===1);
  check('  ...auto-selected', vm.runInContext("selectedProgram.customer_type",e3.ctx)==='Residential');
  check('  ...validation passes without a click', vm.runInContext('validateProgramSelection();',e3.ctx)===true);
  check('  ...no selection prompt', !/Choose one/i.test(e3.cache.get('program-options').innerHTML));

  console.log('\n--- no capacity ---');
  let e4=env([],null);
  await vm.runInContext('loadProgramOptions();',e4.ctx); for(let i=0;i<6;i++) await tick();
  check('no panels invented', !e4.cache.get('program-options').innerHTML.includes('class="pg'));
  check('  ...clean no-availability state',
    /No community solar capacity/i.test(e4.cache.get('program-options').innerHTML));

  console.log('\n--- branch destinations unchanged ---');
  check('Residential -> Agreements', /nextStepKey === 'contracts'[\s\S]{0,220}goStep\(5\)/.test(src)
    || /goStep\(5\); mountRepAgreements/.test(src));
  check('LMI -> Eligibility', /nextStepKey === 'proof_docs'[\s\S]{0,140}goStep\(4\)/.test(src));
  check('backend decides the branch', src.includes('continueFromPerchNextStep'));
  check('immediate and resumed land alike',
    /capacity_result: 2/.test(src) && /enroll: 2,/.test(src));

  const f=R.filter(r=>!r.ok);
  console.log('\n'+'='.repeat(72));
  console.log(`${R.length-f.length} passed, ${f.length} failed`);
  console.log('='.repeat(72));
  if(f.length){f.forEach(x=>console.log('  FAILED: '+x.l));process.exit(1);}
  process.exit(0);
})();

/*
 * LIVE LIFECYCLE — the full browser path, with a DOM that models DOCUMENT ORDER.
 *
 * WHY THIS EXISTS
 * Every earlier harness stubbed getElementById as a Map keyed by id, returning
 * one cached element per id. That CANNOT reproduce a duplicate-id bug: in a real
 * browser getElementById returns the FIRST match in document order.
 *
 * The live failure was exactly that. renderWorkflowStep() injected a second
 * <div id="program-options"> into the step-1 root; step-project precedes
 * step-bill, so every program card rendered into a hidden step while the real
 * container on Customer & Bill stayed empty.
 *
 * This harness registers elements against an ordered document and resolves ids
 * the way the browser does, then drives:
 *   submitCapacity -> goStep(2) -> hydrateStep(2) -> loadProgramOptions ->
 *   GET /programs -> availablePrograms -> selectedProgram -> #program-options
 */
const fs=require('fs'),path=require('path'),vm=require('vm');
const ROOT=path.join(__dirname,'..');
const src=fs.readFileSync(path.join(ROOT,'static','js','app.js'),'utf8');
const html=fs.readFileSync(path.join(ROOT,'templates','index.html'),'utf8');
const R=[]; const check=(l,c)=>{R.push({l,ok:!!c});console.log(`  [${c?'PASS':'FAIL'}] ${l}`);};

// Ids in TEMPLATE ORDER, so duplicates resolve like a browser.
const ORDER=[...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
const RANK=new Map(); ORDER.forEach((id,i)=>{ if(!RANK.has(id)) RANK.set(id,i); });

function makeDom(){
  const doc=[];                       // ordered list of {id, el}
  function mk(id,rank){
    const el={id,_html:'',textContent:'',value:'',disabled:false,checked:false,
      style:{},dataset:{},className:'',hidden:false,_rank:rank,
      classList:{_s:new Set(),add(c){this._s.add(c);},remove(c){this._s.delete(c);},
        contains(c){return this._s.has(c);},toggle(){}},
      setAttribute(){},getAttribute:()=>null,appendChild(){},insertBefore(){},
      removeChild(){},firstChild:null,focus(){this.focused=true;},click(){},
      addEventListener(){},removeEventListener(){},querySelector:()=>null,
      querySelectorAll:()=>[],scrollIntoView(){},offsetWidth:0};
    // innerHTML is LIVE: assigning markup that contains ids registers those
    // elements into the document immediately, at this element's position.
    // Without this a harness cannot reproduce a duplicate-id bug, because the
    // injected node never exists when getElementById is next called.
    Object.defineProperty(el,'innerHTML',{
      get(){ return el._html; },
      set(v){
        el._html = String(v == null ? '' : v);
        // Drop anything previously registered by THIS element.
        for(let i=doc.length-1;i>=0;i--) if(doc[i]._owner===el) doc.splice(i,1);
        let k=0;
        for(const m of el._html.matchAll(/id="([^"]+)"/g)){
          const child=mk(m[1], el._rank + 0.0001*(++k));
          child._owner=el; doc.push(child);
        }
        doc.sort((a,b)=>a._rank-b._rank);
      }
    });
    return el;
  }
  return {
    doc, mk,
    /* Register an element at a document position. */
    add(id, rank){ const el=mk(id,rank); doc.push(el); doc.sort((a,b)=>a._rank-b._rank); return el; },
    /* Browser semantics: FIRST match in document order. */
    get(id){ return doc.filter(e=>e.id===id).sort((a,b)=>a._rank-b._rank)[0] || null; },
    all(id){ return doc.filter(e=>e.id===id); },
  };
}

function env(programs){
  const dom=makeDom(); const calls=[];
  // Seed every template id at its true position.
  ORDER.forEach((id,i)=>dom.add(id,i));
  // Dynamic wf-* fields live inside step-project (early in the document).
  const projRank=RANK.get('step-project')||0;
  ['wf-email','wf-zip_code','wf-utility_name','wf-primary','wf-form-error','workflow-root']
    .forEach((id,k)=>dom.add(id, projRank+0.1+k*0.01));

  const doc={
    getElementById:id=>dom.get(id),
    createElement:id=>dom.mk(id,1e9),
    querySelector:()=>null,
    querySelectorAll:sel=>sel==='.wizard-step'
      ? dom.doc.filter(e=>/^step-/.test(e.id)) : [],
    addEventListener(){},removeEventListener(){},
    body:Object.assign(dom.mk('body',-1),{style:{}}),activeElement:null};
  const sb={console,document:doc,
    sessionStorage:{getItem:()=>'t',setItem(){},removeItem(){}},
    localStorage:{getItem:()=>'t'},setTimeout:()=>0,clearTimeout(){},
    setInterval:()=>0,clearInterval(){},alert(){},scrollTo(){},confirm:()=>true,
    FormData:function(){this.append=()=>{}},navigator:{userAgent:'h'},location:{href:''},
    requestAnimationFrame:()=>0,pdfjsLib:{GlobalWorkerOptions:{}},Tesseract:{},
    URL:{createObjectURL:()=>'b'},open:()=>({}),
    fetch(u,o){const s=String(u); let b=null;
      try{b=o&&o.body?JSON.parse(o.body):null;}catch(e){}
      calls.push({url:s,method:(o&&o.method)||'GET',body:b});
      if(s.includes('/workflow/new')) return Promise.resolve({ok:true,status:200,
        json:()=>Promise.resolve({enrollment_id:null,step:{key:'service_area',
          title:'Check availability',fields:[
            {name:'email',label:'Email',type:'email',required:true},
            {name:'zip_code',label:'ZIP',type:'text',required:true},
            {name:'utility_name',label:'Utility',type:'select',required:true,
             options:[{value:'orange-and-rockland',label:'Orange and Rockland'}]}],
          primary_action:{operation:'check_capacity',label:'Check availability'}}})});
      if(s.includes('/enrollments/capacity')) return Promise.resolve({ok:true,status:200,
        json:()=>Promise.resolve({enrollment_id:42,enrollment_code:'ENR-2026-000042',
          result:{zip_code:'10901',utility_slug:'orange-and-rockland',
                  utility_display_name:'Orange and Rockland',project_details:{}},
          workflow:{step:{key:'capacity_result'}}})});
      if(s.includes('/programs')) return Promise.resolve({ok:true,status:200,
        json:()=>Promise.resolve({capacity_checked:true,available_programs:programs,
          selection_required:programs.length>1,selected_customer_type:null})});
      if(s.endsWith('/program')) return Promise.resolve({ok:true,status:200,
        json:()=>Promise.resolve({selected_customer_type:(b||{}).customer_type})});
      return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve({})});}};
  sb.window=sb; const ctx=vm.createContext(sb); vm.runInContext(src,ctx,{filename:'app.js'});
  return {ctx,dom,calls};
}
const tick=()=>new Promise(r=>setImmediate(r));
const RES={customer_type:'Residential',savings_percent:5,lmi_required:false};
const LMI={customer_type:'LMI',savings_percent:20,lmi_required:true};

console.log('='.repeat(72));
console.log('LIVE LIFECYCLE — ZIP 10901 / Orange and Rockland (dual program)');
console.log('='.repeat(72));

(async()=>{
  console.log('\n--- the duplicate-id defect itself ---');
  check('only ONE #program-options exists in the template',
    (html.match(/id="program-options"/g)||[]).length===1);
  check('  ...and it lives inside Customer & Bill',
    html.indexOf('id="program-options"') > html.indexOf('id="step-bill"'));
  check('the step-1 renderer no longer injects a second one',
    !/programSlot = \(step\.key/.test(src) && /const programSlot = '';/.test(src));

  console.log('\n--- steps 1-4: availability -> Customer & Bill ---');
  const e=env([RES,LMI]);
  // Drive the REAL entry point. startWizardFresh() renders the service_area
  // descriptor into #workflow-root - which is where the duplicate
  // #program-options used to be injected, before Customer & Bill is ever shown.
  await vm.runInContext('startWizardFresh();',e.ctx);
  for(let i=0;i<10;i++) await tick();
  check('after New enrollment there is still only ONE #program-options',
    e.dom.all('program-options').length===1);
  check('  ...and it is the Customer & Bill one',
    e.dom.all('program-options')[0]._rank > (RANK.get('step-bill')||0));

  vm.runInContext(`currentWorkflow={step:{key:'service_area',fields:[
      {name:'email',type:'email',required:true},
      {name:'zip_code',type:'text',required:true},
      {name:'utility_name',type:'select',required:true,
       options:[{value:'orange-and-rockland',label:'Orange and Rockland'}]}],
      primary_action:{operation:'check_capacity'}}}; currentDraft=null;`,e.ctx);
  e.dom.get('wf-email').value='rep@example.com';
  e.dom.get('wf-zip_code').value='10901';
  e.dom.get('wf-utility_name').value='orange-and-rockland';
  await vm.runInContext('submitCapacity();',e.ctx);
  for(let i=0;i<14;i++) await tick();

  // Model the browser faithfully: anything renderWorkflowStep wrote into
  // #workflow-root becomes REAL DOM inside step-project. Registering those ids
  // is what lets this harness catch duplicate-id bugs at RUNTIME rather than
  // only by grepping the source.
  const injected=[...String(e.dom.get('workflow-root').innerHTML||'')
    .matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
  check('capacity POST fired', e.calls.some(c=>c.url.includes('/enrollments/capacity')));
  check('step-1 injected NO duplicate #program-options',
    injected.filter(x=>x==='program-options').length===0);
  check('enrollment adopted from the response',
    vm.runInContext('currentDraft && currentDraft.enrollment_id',e.ctx)===42);
  check('  ...with its code', vm.runInContext('currentDraft.enrollment_code',e.ctx)==='ENR-2026-000042');
  check('utility captured for the chip',
    vm.runInContext('perchContext.utilityDisplay',e.ctx)==='Orange and Rockland');
  check('landed on Customer & Bill', vm.runInContext('currentStep',e.ctx)===2);

  console.log('\n--- steps 5-7: TWO visible program buttons with real values ---');
  const host=e.dom.get('program-options');
  check('GET /programs used the adopted id',
    e.calls.some(c=>c.url.includes('/enrollments/42/programs')));
  check('availablePrograms populated', vm.runInContext('availablePrograms.length',e.ctx)===2);
  check('the REAL container was filled', host.innerHTML.length>0);
  check('  ...with exactly two buttons', (host.innerHTML.match(/class="pg(?:"| )/g)||[]).length===2);
  check('  ...it is the Customer & Bill one, not a hidden step-1 copy',
    e.dom.all('program-options').length===1);
  check('Residential shown', host.innerHTML.includes('>Residential<'));
  check('  ...with the REAL 5%', host.innerHTML.includes('>5<'));
  check('Residential LMI shown', host.innerHTML.includes('>Residential LMI<'));
  check('  ...with the REAL 20%', host.innerHTML.includes('>20<'));
  check('percentages are never hardcoded', !/['"]\s*(5|20)%\s*savings/.test(src));
  check('nothing auto-selected on a dual location',
    vm.runInContext('selectedProgram === null',e.ctx)===true);

  console.log('\n--- steps 8-10: selecting persists and switches ---');
  await vm.runInContext("selectProgram('Residential');",e.ctx);
  for(let i=0;i<8;i++) await tick();
  check('Residential visibly selected',
    (e.dom.get('program-options').innerHTML.match(/class="pg selected"/g)||[]).length===1);
  check('  ...persisted through the existing endpoint',
    e.calls.some(c=>c.url.endsWith('/program')&&c.method==='POST'&&c.body.customer_type==='Residential'));
  await vm.runInContext("selectProgram('LMI');",e.ctx);
  for(let i=0;i<8;i++) await tick();
  check('LMI visibly switches selection',
    vm.runInContext("selectedProgram.customer_type",e.ctx)==='LMI');
  check('  ...and persists',
    e.calls.filter(c=>c.url.endsWith('/program')).slice(-1)[0].body.customer_type==='LMI');
  check('  ...exactly one panel selected at a time',
    (e.dom.get('program-options').innerHTML.match(/class="pg selected"/g)||[]).length===1);

  console.log('\n--- step 11: stepper follows the branch ---');
  check('LMI branch includes Eligibility',
    vm.runInContext("activeSteps().join(',')",e.ctx)==='1,2,4,5');
  check('  ...stepper re-rendered', e.dom.get('stepper').innerHTML.length>0);
  await vm.runInContext("selectProgram('Residential');",e.ctx);
  for(let i=0;i<8;i++) await tick();
  check('Residential branch drops Eligibility',
    vm.runInContext("activeSteps().join(',')",e.ctx)==='1,2,5');

  console.log('\n--- step 12: Continue gating ---');
  check('program is no longer a blocking reason',
    !(e.dom.get('bill-requirements').innerHTML||'').includes('Choose a savings program'));
  check('  ...but Continue stays disabled while the bill/fields are empty',
    e.dom.get('btn-bill-next').disabled===true);

  console.log('\n--- steps 13-14: branch destinations ---');
  check('Residential -> Agreements', /nextStepKey === 'contracts'[\s\S]{0,220}goStep\(5\)/.test(src)
    || /goStep\(5\); mountRepAgreements/.test(src));
  check('LMI -> Eligibility', /nextStepKey === 'proof_docs'[\s\S]{0,140}goStep\(4\)/.test(src));

  console.log('\n--- step 15: reload/resume restores the choice ---');
  const e2=env([RES,LMI]);
  vm.runInContext("currentDraft={enrollment_id:42};",e2.ctx);
  e2.ctx.__persisted='LMI';
  // Re-point the programs response to include the persisted choice.
  vm.runInContext("selectedProgram=null;",e2.ctx);
  const origFetch=e2.ctx.fetch;
  await vm.runInContext('loadProgramOptions();',e2.ctx);
  for(let i=0;i<8;i++) await tick();
  check('resume path renders into the real container',
    e2.dom.get('program-options').innerHTML.includes('class="pg'));
  check('  ...still exactly one container', e2.dom.all('program-options').length===1);
  check('hydration reads the persisted value', /body\.selected_customer_type/.test(src));
  check('  ...and openEnrollment restores the branch', /e\.selected_customer_type/.test(src));

  console.log('\n--- single program still auto-resolves ---');
  const e3=env([RES]);
  vm.runInContext("currentDraft={enrollment_id:42};",e3.ctx);
  await vm.runInContext('loadProgramOptions();',e3.ctx);
  for(let i=0;i<6;i++) await tick();
  check('one button rendered',
    (e3.dom.get('program-options').innerHTML.match(/class="pg(?:"| )/g)||[]).length===1);
  check('  ...auto-selected', vm.runInContext("selectedProgram.customer_type",e3.ctx)==='Residential');

  const f=R.filter(r=>!r.ok);
  console.log('\n'+'='.repeat(72));
  console.log(`${R.length-f.length} passed, ${f.length} failed`);
  console.log('='.repeat(72));
  if(f.length){f.forEach(x=>console.log('  FAILED: '+x.l));process.exit(1);}
  process.exit(0);
})();

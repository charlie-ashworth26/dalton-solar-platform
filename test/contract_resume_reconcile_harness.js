/*
 * Reopen reconciliation: GET /status BEFORE POST /contracts.
 *
 * THE HOSTED FAILURE THIS GUARDS (2026-08-17):
 *   acceptance 422'd on the timestamp -> Dalton stayed at contracts_review while
 *   Perch advanced to contracts_accept -> reopening called POST /contracts ->
 *   422 "Cannot modify this stage because later stages have already started."
 *   The error told the rep to reopen, which reproduced it forever.
 */
const fs=require('fs'), path=require('path'), vm=require('vm');
const ROOT=path.join(__dirname,'..');
const src=fs.readFileSync(path.join(ROOT,'static','js','app.js'),'utf8');
const html=fs.readFileSync(path.join(ROOT,'templates','index.html'),'utf8');
const IDS=new Set([...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]));
const DYN=new Set(['agr-ack-check','agr-agree-btn','agr-card-error','agr-status']);
const R=[]; const check=(l,c)=>{R.push({l,ok:!!c});console.log(`  [${c?'PASS':'FAIL'}] ${l}`);};

function mk(id){return {id,innerHTML:'',textContent:'',value:'',disabled:false,checked:false,
  style:{},dataset:{},classList:{add(){},remove(){},toggle(){},contains:()=>false},
  setAttribute(){},getAttribute:()=>null,appendChild(){},insertBefore(){},
  removeChild(){},firstChild:null,focus(){},click(){},
  addEventListener(){},removeEventListener(){},querySelector:()=>null,querySelectorAll:()=>[]};}

function env(routes){
  const cache=new Map(); const calls=[];
  const doc={getElementById:id=>((IDS.has(id)||DYN.has(id))?(cache.has(id)||cache.set(id,mk(id)),cache.get(id)):null),
    createElement:mk,querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){},
    body:Object.assign(mk('body'),{style:{}}),activeElement:null};
  const sb={console,document:doc,
    sessionStorage:{getItem:()=>'tok',setItem(){},removeItem(){}},
    localStorage:{getItem:()=>'tok',setItem(){},removeItem(){}},
    setTimeout:()=>0,clearTimeout(){},setInterval:()=>0,clearInterval(){},alert(){},scrollTo(){},
    FormData:function(){this.append=()=>{}},navigator:{userAgent:'h'},location:{href:''},
    requestAnimationFrame:()=>0,pdfjsLib:{GlobalWorkerOptions:{}},Tesseract:{},
    URL:{createObjectURL:()=>'blob:x',revokeObjectURL(){}},open:()=>({location:'',close(){}}),
    fetch(url,opts){
      const u=String(url); calls.push({url:u,method:(opts&&opts.method)||'GET'});
      for(const [pat,resp] of routes){
        if(u.includes(pat)) return Promise.resolve({ok:resp.ok!==false,status:resp.status||200,
          json:()=>Promise.resolve(resp.body||{})});
      }
      return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve({})});
    }};
  sb.window=sb; const ctx=vm.createContext(sb); vm.runInContext(src,ctx,{filename:'app.js'});
  vm.runInContext("currentDraft={enrollment_id:9};",ctx);
  return {ctx,cache,calls};
}
const tick=()=>new Promise(r=>setImmediate(r));

const PERSISTED=[{contract_name:'Community Distributed Generation Disclosure Form'},
                 {contract_name:'Community Solar Agency Agreement'}];
function detail(stepKey,terminal,blocked){
  return {id:9,enrollment_code:'ENR-2026-000009',
    customer:{first_name:'Jane',last_name:'Doe',email:'j@e.com'},
    service_address:{street:'1 Main',city:'Albany',zip:'12207'},
    utility_account:{utility_name:'national-grid-ny',account_number:'123'},
    project:{id:1,name:'P'},workflow_step_key:stepKey,
    workflow_is_terminal:terminal,workflow_is_blocked:blocked,
    workflow_last_response:{contracts:PERSISTED}};
}

console.log('='.repeat(72));
console.log('REOPEN RECONCILIATION — status before contracts');
console.log('='.repeat(72));

(async()=>{
  // ── THE EXACT HOSTED CASE ──
  console.log('\n--- Perch says contracts_accept (the live failure) ---');
  let e=env([
    ['/api/enrollments/9',{body:detail('contracts_review',false,false)}],
    ['/perch-status',{body:{completed:false,next_step_key:'contracts_accept',
                            completed_steps:['a','b','c','d','e'],remaining_steps:['x']}}],
    ['/contracts',{ok:false,status:422,body:{error:'Perch rejected the contract request: unprocessable_entity: Cannot modify this stage because later stages have already started.'}}],
  ]);
  await vm.runInContext('openEnrollment(9);',e.ctx);
  for(let i=0;i<8;i++) await tick();

  const statusCalls=e.calls.filter(c=>c.url.includes('/perch-status'));
  const contractPosts=e.calls.filter(c=>c.url.includes('/contracts')&&c.method==='POST'&&!c.url.includes('review'));
  check('GET /status was called', statusCalls.length>=1);
  check('ZERO POST /contracts when Perch is at contracts_accept', contractPosts.length===0);
  check('  ...status was called BEFORE any contracts call',
    e.calls.findIndex(c=>c.url.includes('/perch-status')) <
    (contractPosts.length? e.calls.findIndex(c=>c.url.includes('/contracts')&&c.method==='POST') : Infinity));

  const host=e.cache.get('agr-host-rep'); const rendered=host?host.innerHTML:'';
  check('persisted agreement names render',
    rendered.includes('Community Distributed Generation Disclosure Form')
    && rendered.includes('Community Solar Agency Agreement'));
  check('acceptance is offered (one attempt allowed)',
    vm.runInContext('Agreements.acceptanceEnabled',e.ctx)===true);
  check('  ...and the screen is NOT read-only',
    vm.runInContext('Agreements.readOnly',e.ctx)===false);
  const note=e.cache.get('agr-card-error');
  check('rep is told documents cannot be reopened',
    note && /cannot be reopened/i.test(note.textContent));
  check('the misleading "reopen" advice is gone',
    !/Return to the dashboard and reopen/i.test(rendered+(note?note.textContent:'')));
  check('  ...and no stage-conflict error is shown',
    !note || !/later stages have already started/i.test(note.textContent));

  // ── still legitimately at contracts ──
  console.log('\n--- Perch still at contracts_review: regeneration IS valid ---');
  e=env([
    ['/api/enrollments/9',{body:detail('contracts_review',false,false)}],
    ['/perch-status',{body:{completed:false,next_step_key:'contracts_review',
                            completed_steps:['a'],remaining_steps:['x','y']}}],
    ['/contracts',{body:{contracts:[{index:0,contract_name:'ESIGN',url_present:true}],
                         acceptance_enabled:true,next_step_key:'contracts_accept'}}],
  ]);
  await vm.runInContext('openEnrollment(9);',e.ctx);
  for(let i=0;i<8;i++) await tick();
  check('POST /contracts DOES run when still at contracts',
    e.calls.filter(c=>c.url.includes('/contracts')&&c.method==='POST'&&!c.url.includes('review')).length===1);
  check('  ...after status', e.calls.findIndex(c=>c.url.includes('/perch-status')) <
                             e.calls.findIndex(c=>c.url.includes('/contracts')&&c.method==='POST'));
  check('  ...fresh packet used', vm.runInContext('Agreements.contracts.length',e.ctx)===1);
  check('  ...acceptance enabled from the backend',
    vm.runInContext('Agreements.acceptanceEnabled',e.ctx)===true);

  // ── completed ──
  console.log('\n--- completed enrollment: zero Perch calls, read-only ---');
  e=env([
    ['/api/enrollments/9',{body:detail('contracts_accepted',true,false)}],
    ['/perch-status',{body:{completed:true,next_step_key:null}}],
    ['/contracts',{body:{}}],
  ]);
  await vm.runInContext('openEnrollment(9);',e.ctx);
  for(let i=0;i<8;i++) await tick();
  check('ZERO POST /contracts',
    e.calls.filter(c=>c.url.includes('/contracts')&&c.method==='POST').length===0);
  check('  ...and no status call needed either',
    e.calls.filter(c=>c.url.includes('/perch-status')).length===0);
  check('read-only', vm.runInContext('Agreements.readOnly',e.ctx)===true);
  check('  ...no acceptance controls',
    !(e.cache.get('agr-host-rep')||{}).innerHTML.includes('agr-agree-btn'));

  // ── uncertain, unresolved ──
  console.log('\n--- uncertain acceptance NOT cleared by status: stays read-only ---');
  e=env([
    ['/api/enrollments/9',{body:detail('contracts_accept_uncertain',false,true)}],
    ['/perch-status',{body:{completed:false,next_step_key:'status'}}],
    ['/contracts',{body:{}}],
  ]);
  await vm.runInContext('openEnrollment(9);',e.ctx);
  for(let i=0;i<8;i++) await tick();
  check('ZERO POST /contracts',
    e.calls.filter(c=>c.url.includes('/contracts')&&c.method==='POST').length===0);
  check('stays read-only', vm.runInContext('Agreements.readOnly',e.ctx)===true);
  check('  ...acceptance NOT blindly re-enabled',
    vm.runInContext('Agreements.acceptanceEnabled',e.ctx)===false);

  // ── uncertain, but status proves acceptance still outstanding ──
  console.log('\n--- uncertain, status proves acceptance still needed ---');
  e=env([
    ['/api/enrollments/9',{body:detail('contracts_accept_uncertain',false,true)}],
    ['/perch-status',{body:{completed:false,next_step_key:'contracts_accept'}}],
    ['/contracts',{body:{}}],
  ]);
  await vm.runInContext('openEnrollment(9);',e.ctx);
  for(let i=0;i<8;i++) await tick();
  check('still ZERO POST /contracts',
    e.calls.filter(c=>c.url.includes('/contracts')&&c.method==='POST').length===0);
  check('persisted names still shown',
    (e.cache.get('agr-host-rep')||{}).innerHTML.includes('Community Solar Agency Agreement'));

  // ── status unavailable ──
  console.log('\n--- /status unavailable: fall back, never guess ---');
  e=env([
    ['/api/enrollments/9',{body:detail('contracts_review',false,false)}],
    ['/perch-status',{ok:false,status:503,body:{error:'unavailable'}}],
    ['/contracts',{body:{contracts:[{index:0,contract_name:'ESIGN',url_present:true}],
                         acceptance_enabled:true}}],
  ]);
  await vm.runInContext('openEnrollment(9);',e.ctx);
  for(let i=0;i<8;i++) await tick();
  check('a failed status does not crash the reopen',
    (e.cache.get('agr-host-rep')||{}).innerHTML.length>0);

  console.log('\n--- acceptance itself is unchanged ---');
  check('accept still posts customer_confirmed', src.includes('customer_confirmed: true'));
  check('accept still guards duplicates', src.includes('Agreements.inFlight'));
  check('ambiguous outcome still not retryable', /uncertain/i.test(src));
  check('no client-supplied acceptance metadata',
    !/ip_address\s*:/.test(src) && !/user_agent\s*:/.test(src));

  const f=R.filter(r=>!r.ok);
  console.log('\n'+'='.repeat(72));
  console.log(`${R.length-f.length} passed, ${f.length} failed`);
  console.log('='.repeat(72));
  if(f.length){f.forEach(x=>console.log('  FAILED: '+x.l));process.exit(1);}
  process.exit(0);
})();

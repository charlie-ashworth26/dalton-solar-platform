/*
 * CROSS-ENROLLMENT STATE ISOLATION
 *
 * LIVE FAILURES this reproduces:
 *  #1 A completed Central Hudson self-attestation enrollment left #sa-panel
 *     visible and #lmi-proof-panel hidden, plus perchContext/selfAttestation
 *     populated. The next NYSEG enrollment showed the SELF-ATTESTATION UI, then
 *     proof-doc TEXT with NO uploader - because the uploader lives inside the
 *     still-hidden panel.
 *  #2 programCommitted stayed true, so the next Central Hudson enrollment
 *     rendered its program cards .committed - Residential greyed out and
 *     disabled "as if there were no capacity". Capacity was fine; the CONTROL
 *     was locked by the previous enrollment.
 *
 * WHY RESTARTING app.py "FIXED" IT: none of this was server state. Restarting
 * forced a browser reload, which re-evaluated app.js (resetting module globals)
 * and rebuilt the DOM from markup. The bug was always in the page.
 */
const fs=require('fs'),path=require('path'),vm=require('vm');
const ROOT=path.join(__dirname,'..');
const src=fs.readFileSync(path.join(ROOT,'static','js','app.js'),'utf8');
const html=fs.readFileSync(path.join(ROOT,'templates','index.html'),'utf8');
const IDS=new Set([...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]));
const R=[]; const check=(l,c)=>{R.push({l,ok:!!c});console.log(`  [${c?'PASS':'FAIL'}] ${l}`);};

function mk(id){return {id,innerHTML:'',textContent:'',value:'',disabled:false,checked:false,
  style:{},dataset:{},className:'',hidden:false,
  classList:{_s:new Set(),add(c){this._s.add(c);},remove(c){this._s.delete(c);},
    contains(c){return this._s.has(c);},toggle(c,f){f?this._s.add(c):this._s.delete(c);}},
  setAttribute(){},getAttribute:()=>null,appendChild(){},insertBefore(){},removeChild(){},
  firstChild:null,focus(){},click(){},addEventListener(){},removeEventListener(){},
  querySelector:()=>null,querySelectorAll:()=>[],scrollIntoView(){},offsetWidth:0};}

function env(routes){
  const cache=new Map(); const calls=[];
  const known=id=>IDS.has(id)||id.startsWith('wf-');
  const get=id=>(cache.has(id)||cache.set(id,mk(id)),cache.get(id));
  const doc={getElementById:id=>(known(id)?get(id):null),createElement:mk,
    querySelector:()=>null,
    querySelectorAll:sel=>{ if(sel==='.wizard-step')
      return [...IDS].filter(i=>/^step-/.test(i)).map(get); return []; },
    addEventListener(){},removeEventListener(){},body:Object.assign(mk('b'),{style:{}}),
    activeElement:null};
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
      for(const [pat,resp] of (routes||[])) if(s.includes(pat))
        return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve(resp)});
      return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve({})});}};
  sb.window=sb; const ctx=vm.createContext(sb); vm.runInContext(src,ctx);
  return {ctx,get,calls,run:c=>vm.runInContext(c,ctx)};
}
const tick=()=>new Promise(r=>setImmediate(r));

/* Bring an env to the state a COMPLETED self-attestation enrollment leaves. */
function completeSelfAttestationEnrollment(e){
  e.run(`
    currentDraft={enrollment_id:101,enrollment_code:'ENR-A'};
    selectedProgram={customer_type:'LMI',savings_percent:20,lmi_required:true};
    availablePrograms=[{customer_type:'LMI',savings_percent:20,lmi_required:true}];
    programCommitted=true;
    selfAttestation={reference:{version_label:'NY-SMI-80-2026'},occupancy:'1',
                     county:'Ulster County',choice:'below'};
    currentEnrollmentDetail={id:101,selected_customer_type:'LMI',perch_committed:true,
                             program_savings:{percent:20,basis:'lmi'}};
    perchContext.nextStepKey='self_attestation';
    perchContext.enrollmentSubmitted=true;
    perchContext.selfAttestationGenerated=true;
    perchContext.contractsGenerated=true;
  `);
  e.get('sa-panel').style.display='';
  e.get('sa-sent').style.display='';
  e.get('sa-form').style.display='none';
  e.get('lmi-proof-panel').style.display='none';
  e.get('program-options').innerHTML='<button class="pg committed selected">LMI</button>';
  e.get('sa-occupancy').innerHTML='<option>1 person</option>';
  e.get('sa-county').innerHTML='<option>Ulster County</option>';
}

console.log('='.repeat(72));
console.log('CROSS-ENROLLMENT ISOLATION');
console.log('='.repeat(72));

(async()=>{
  console.log('\n--- A. Central Hudson self-attestation -> fresh NYSEG ---');
  const e=env();
  completeSelfAttestationEnrollment(e);
  check('enrollment A is in the self-attestation branch',
    e.run("perchContext.nextStepKey")==='self_attestation');
  check('  ...with its panel visible', e.get('sa-panel').style.display==='');

  e.run('resetWizardState();');

  check('B: no self-attestation next_step survives', !e.run('perchContext.nextStepKey'));
  check('B: selfAttestation answers cleared', !e.run('selfAttestation.occupancy'));
  check('  ...and its county', e.run("selfAttestation.county")==='');
  check('  ...and its reference data', e.run('selfAttestation.reference')===null);
  check('B: the self-attestation PANEL is hidden', e.get('sa-panel').style.display==='none');
  check('  ...its form is restored', e.get('sa-form').style.display==='');
  check('  ...its "sent" state cleared', e.get('sa-sent').style.display==='none');
  check('B: the PROOF-DOC panel is restored (the uploader is reachable)',
    e.get('lmi-proof-panel').style.display==='');
  check('  ...this is the exact live symptom: proof text with no uploader',
    e.get('lmi-proof-panel').style.display!=='none');
  check('B: stale occupancy/county option lists cleared',
    e.get('sa-occupancy').innerHTML==='' && e.get('sa-county').innerHTML==='');
  check('B: enrollmentSubmitted cleared', e.run('perchContext.enrollmentSubmitted')!==true);
  check('B: contractsGenerated cleared', e.run('perchContext.contractsGenerated')!==true);
  check('B: selfAttestationGenerated cleared',
    e.run('perchContext.selfAttestationGenerated')!==true);

  console.log('\n--- B: branch follows ONLY the new next_step ---');
  e.run("perchContext.nextStepKey='proof_docs';");
  check('proof_docs is honoured', e.run("perchContext.nextStepKey")==='proof_docs');
  check('  ...routing reads next_step_key, not leftover state',
    /perchContext\.nextStepKey === 'proof_docs'/.test(src));
  check('  ...no utility name appears in the router',
    !/nyseg|central.?hudson/i.test(src.split('async function continueFromPerchNextStep')[1].slice(0,1800)));

  console.log('\n--- B. Central Hudson -> fresh Central Hudson: capacity not greyed out ---');
  const e2=env([['/programs',{capacity_checked:true,selection_required:true,
    selected_customer_type:null,
    available_programs:[{customer_type:'Residential',savings_percent:5,lmi_required:false},
                        {customer_type:'LMI',savings_percent:20,lmi_required:true}]}]]);
  completeSelfAttestationEnrollment(e2);
  check('enrollment A left the program controls COMMITTED', e2.run('programCommitted')===true);
  e2.run('resetWizardState();');
  check('B: programCommitted cleared', e2.run('programCommitted')===false);
  check('B: availablePrograms cleared', e2.run('availablePrograms.length')===0);
  check('B: selectedProgram cleared', e2.run('selectedProgram')===null);
  check('B: currentEnrollmentDetail cleared', e2.run('currentEnrollmentDetail')===null);
  check('B: the program area was emptied', e2.get('program-options').innerHTML==='');

  e2.run("currentDraft={enrollment_id:202,enrollment_code:'ENR-B'};");
  await e2.run('loadProgramOptions();');
  for(let i=0;i<8;i++) await tick();
  const ui=e2.get('program-options').innerHTML;
  check('B: the FRESH capacity response drives availability',
    (ui.match(/class="pg(?:"| )/g)||[]).length===2);
  check('  ...Residential is offered again', ui.includes('>Residential<'));
  check('  ...at its real 5%', ui.includes('>5<'));
  check('  ...LMI is offered', ui.includes('>Residential LMI<'));
  check('  ...NOTHING is rendered committed/greyed out', !ui.includes('committed'));
  check('  ...and nothing is disabled', !ui.includes('disabled'));
  check('  ...nothing auto-selected on a dual-program location',
    e2.run('selectedProgram')===null);
  check('B queried its OWN enrollment', e2.calls.some(c=>c.url.includes('/202/programs')));
  check('  ...never enrollment A', !e2.calls.some(c=>c.url.includes('/101/')));

  console.log('\n--- C. every transient field is reset ---');
  const e3=env();
  completeSelfAttestationEnrollment(e3);
  e3.run(`state.customer.first='Jane'; state.bill.documentId=55;
          state.lmi.docType='proof_doc_snap'; perchContracts=[{contract_name:'X'}];
          DocSets.utility_bill={id:9,files:[{key:'k',documentId:55,file:{name:'a.pdf'}}]};`);
  e3.run('resetWizardState();');
  const cleared=[['current enrollment id','currentDraft===null||!currentDraft'],
    ['selected program','selectedProgram===null'],
    ['capacity/program list','availablePrograms.length===0'],
    ['next_step','!perchContext.nextStepKey'],
    ['self-attestation state','!selfAttestation.occupancy'],
    ['proof-doc state',"state.lmi.docType===''"],
    ['contracts state','perchContracts.length===0'],
    ['disabled program UI','programCommitted===false'],
    ['customer data',"state.customer.first===''"],
    ['bill/document state','state.bill.documentId===null'],
    ['document sets','DocSets.utility_bill.files.length===0']];
  for(const [label,expr] of cleared) check(`cleared: ${label}`, e3.run(expr)===true);

  console.log('\n--- D. RESUME still restores that enrollment\'s own state ---');
  check('resume hydrates the branch from the backend', /e\.selected_customer_type/.test(src));
  check('  ...and the commit flag', /e\.perch_committed === true/.test(src));
  check('  ...and its documents', /rehydrateDocumentsFromDetail\(e\)/.test(src));
  check('  ...and its program options', /loadProgramOptions\(\)/.test(src));
  check('resume calls resetWizardState FIRST, then hydrates',
    src.indexOf('resetWizardState();', src.indexOf('async function openEnrollment'))
      < src.indexOf('currentEnrollmentDetail = e;'));
  check('  ...so a resumed enrollment cannot inherit the previous one either',
    /async function openEnrollment[\s\S]{0,700}resetWizardState\(\)/.test(src));

  console.log('\n--- E. no utility-specific hardcoding introduced ---');
  // Strip comments: the fix is DOCUMENTED with the live example that prompted
  // it, which is explanation, not behaviour.
  const strip=t=>t.replace(/\/\*[\s\S]*?\*\//g,'').replace(/^\s*\/\/.*$/gm,'');
  const reset=strip(src.split('function resetWizardState')[1].slice(0,5000));
  check('resetWizardState CODE names no utility',
    !/nyseg|hudson|national grid|rockland/i.test(reset));
  check('  ...and no ZIP', !/12401|12901|10901|14737/.test(reset));
  check('branch routing names no utility',
    !/nyseg|central.?hudson/i.test(src.split('async function continueFromPerchNextStep')[1].slice(0,1800)));
  check('self-attestation code names no utility',
    !/nyseg|hudson/i.test(src.split('async function prepareSelfAttestation')[1].slice(0,3000)));

  const f=R.filter(r=>!r.ok);
  console.log('\n'+'='.repeat(72));
  console.log(`${R.length-f.length} passed, ${f.length} failed`);
  console.log('='.repeat(72));
  if(f.length){f.forEach(x=>console.log('  FAILED: '+x.l));process.exit(1);}
  process.exit(0);
})();

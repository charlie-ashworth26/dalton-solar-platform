/*
 * PASS 1 — structural wizard redesign, proving nothing functional was lost.
 *
 * Old flow: Availability -> Capacity confirmed -> Bill -> Contact & Login
 *           -> [Eligibility] -> Agreements
 * New flow: Availability -> Customer & Bill -> [Eligibility] -> Agreements
 */
const fs=require('fs'),path=require('path'),vm=require('vm'),crypto=require('crypto');
const ROOT=path.join(__dirname,'..');
const src=fs.readFileSync(path.join(ROOT,'static','js','app.js'),'utf8');
const html=fs.readFileSync(path.join(ROOT,'templates','index.html'),'utf8');
const css=fs.readFileSync(path.join(ROOT,'static','css','app.css'),'utf8');
const IDS=new Set([...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]));
const DYN=new Set(['stepper','svc-context','program-options','c-email-display','agr-card-error']);
const stripComments=t=>t.replace(/\/\*[\s\S]*?\*\//g,'').replace(/^\s*\/\/.*$/gm,'');
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
    sessionStorage:{getItem:()=>'t',setItem(){},removeItem(){}},
    localStorage:{getItem:()=>'t',setItem(){},removeItem(){}},
    setTimeout:()=>0,clearTimeout(){},setInterval:()=>0,clearInterval(){},alert(){},
    scrollTo(){},confirm:()=>true,FormData:function(){this.append=()=>{}},
    navigator:{userAgent:'h'},location:{href:''},requestAnimationFrame:()=>0,
    pdfjsLib:{GlobalWorkerOptions:{}},Tesseract:{},
    URL:{createObjectURL:()=>'b',revokeObjectURL(){}},open:()=>({location:'',close(){}}),
    fetch:()=>Promise.resolve({ok:true,status:200,json:()=>Promise.resolve({})})};
  sb.window=sb; const ctx=vm.createContext(sb); vm.runInContext(src,ctx,{filename:'app.js'});
  return {ctx,cache};
}
const e=env(); const run=c=>vm.runInContext(c,e.ctx);

console.log('='.repeat(72));
console.log('PASS 1 — structural redesign');
console.log('='.repeat(72));

console.log('\n--- PROTECTED agreement block is byte-identical ---');
const EXPECT={agreementLinksHtml:'5822142aa810981cb29486195b35eb46',
  ack_label:'a8da353295a59308773674a1c5427d96',
  updateAgreeButton:'9a3b5a86b1081eb6514ad939c2f4afa3',
  submitAgreements:'97d38043a5ee1495c9d6a2dd8843ac86',
  openAgreementDoc:'dd6bf2d59bd665a30b15a17eeb658c4f'};
const md5=t=>crypto.createHash('md5').update(t).digest('hex');
function grab(name){
  if(name==='ack_label'){const i=src.indexOf('html += \'<label class="agr-ack"');
    return src.slice(i, src.indexOf('</label>',i)+'</label>\';'.length);}
  const re=new RegExp((name==='submitAgreements'?'async ':'')+'function '+name+'\\([\\s\\S]*?\\n\\}');
  const m=src.match(re); return m?m[0]:'';
}
for(const [k,h] of Object.entries(EXPECT))
  check(`${k} unchanged (md5)`, md5(grab(k))===h);
check('exact acknowledgement wording intact',
  src.includes('By checking this box, I acknowledge that I have reviewed and agree to the ')
  && src.includes('. By continuing, I am submitting my electronic signature.'));
check('exactly ONE acknowledgement checkbox', (src.match(/id="agr-ack-check"/g)||[]).length===1);
check('exactly ONE Agree & finish button', (src.match(/id="agr-agree-btn"/g)||[]).length===1);
check('links still built by agreementLinksHtml', src.includes('agreementLinksHtml()'));
check('link targets still openAgreementDoc(index)', /onclick="openAgreementDoc\(/.test(src));
check('agreements are NOT cards', !/agr-card-item|agreement-card/.test(src));

console.log('\n--- STEPPER: structural, branch-aware ---');
check('SVG arc removed from markup', !html.includes('arc-svg') && !html.includes('arc-sun'));
check('  ...and from JS', !src.includes('arc-progress'));
check('semantic stepper mounted', html.includes('id="stepper"'));
check('  ...as a labelled nav', /<nav class="stepper"[^>]*aria-label/.test(html));
check('renderStepper() drives it', src.includes('function renderStepper('));
check('  ...with done/now/todo states', /i < pos \? 'done'/.test(src));
check('  ...checkmarks on completed nodes',
  src.includes('viewBox="0 0 16 16"') && /state === 'done'\s*\n?\s*\?/.test(src));
check('  ...connectors that fill', css.includes('.stp-line.filled::after'));
check('  ...current node emphasised', css.includes('.stp-item.now .stp-node'));
check('  ...aria-current on the active step', src.includes('aria-current="step"'));

console.log('\n--- activeSteps: OLD vs NEW ---');
run("selectedProgram=null;");
check('unknown program -> [1,2,4,5] (Eligibility retained, nothing skipped on a guess)',
  run("activeSteps().join(',')")==='1,2,4,5');
run("selectedProgram={customer_type:'Residential',lmi_required:false};");
check('RESIDENTIAL -> [1,2,5]  (was 1,2,3,5)', run("activeSteps().join(',')")==='1,2,5');
check('  ...Eligibility ABSENT from the sequence', run("activeSteps().indexOf(4)")===-1);
check('  ...3 steps, not 4', run("activeSteps().length")===3);
check('  ...next after Customer & Bill is Agreements', run("nextStepAfter(2)")===5);
check('  ...back from Agreements is Customer & Bill', run("prevStepBefore(5)")===2);
run("selectedProgram={customer_type:'LMI',lmi_required:true};");
check('LMI -> [1,2,4,5]  (was 1,2,3,4,5)', run("activeSteps().join(',')")==='1,2,4,5');
check('  ...Eligibility PRESENT', run("activeSteps().indexOf(4)")!==-1);
check('  ...4 steps', run("activeSteps().length")===4);
check('  ...next after Customer & Bill is Eligibility', run("nextStepAfter(2)")===4);
check('  ...back from Agreements is Eligibility', run("prevStepBefore(5)")===4);
check('step 3 removed from the step map', !/3:'step-contact'/.test(src));
// One reference remains in a comment explaining the Eligibility Back bug.
check('  ...and no CODE navigates to it', !stripComments(src).includes('goStep(3)'));


check('labels reflect the merge',
  src.includes("2:'Customer & Bill'") && !/3:'Customer'/.test(src));

console.log('\n--- CAPACITY: page removed, lookup preserved ---');
check('capacity_result resumes to Customer & Bill, not its own page',
  /capacity_result: 2/.test(src));
check('capacity LOOKUP call unchanged', src.includes("/api/perch/enrollments/capacity'"));
check('  ...still validated server-side (frontend has no capacity rules)',
  !/residential_capacity_available|small_commercial_capacity_available/.test(src));
check('result now renders as quiet context', src.includes('function renderServiceContext('));
// The context strip was replaced by the mockup's utility chip in the program
// section head; both are bound to the same live perchContext values.
check('  ...surfaced as the live utility chip',
  html.includes('id="util-chip"') && /perchContext\.utilityDisplay/.test(src));
check('  ...without raw availability diagnostics',
  !/Not available<\/span>/.test(src));
check('program cards render on Customer & Bill', html.includes('id="program-options"'));

console.log('\n--- CONTACT & LOGIN merged into Customer & Bill ---');
check('step-contact screen removed', !html.includes('id="step-contact"'));
check('  ...and its Continue button', !html.includes('btn-contact-next'));
check('phone survived', html.includes('id="c-phone"'));
check('password survived', html.includes('id="c-pass"'));
check('confirm password survived', html.includes('id="c-pass-confirm"'));
check('both eye toggles survived',
  html.includes('c-pass-eye') && html.includes('c-pass-confirm-eye'));
check('  ...still call togglePasswordVisibility', /togglePasswordVisibility\('c-pass'/.test(html));
check('email is not re-asked as a field',
  /<input type="email" id="c-email" readonly hidden>/.test(html)
  && !/id="c-email"[^>]*placeholder/.test(html));
check('  ...and #c-email still exists for existing reads/writes', html.includes('id="c-email"'));
check('contact fields hydrate on step 2 (Back/resume restores them)',
  /getElementById\('c-phone'\)\.value = state\.customer\.phone/.test(src));
check('  ...password too', /getElementById\('c-pass'\)\.value = state\.customer\.password/.test(src));
check('phone/password collected on submit',
  /state\.customer\.phone = \(document\.getElementById\('c-phone'\)/.test(src));
check('submitContact() still owns /enroll', /submitContact[\s\S]{0,4000}\/enroll'/.test(src));
// BUG 2 refactor: submitBill now calls submitContactDetails() and only advances
// when it returns true, instead of blindly chaining a page-shaped function.
check('  ...and is chained from submitBill',
  /const ok = await submitContactDetails\(\);/.test(src) && /if\(!ok\) return;/.test(src));
check('password validation preserved', src.includes('c-pass-confirm'));

console.log('\n--- BILL AMOUNT removed cleanly ---');
check('field gone from markup', !html.includes('bill-amount'));
check('  ...and from JS', !src.includes('bill-amount'));
check('  ...no longer gates Continue', !/const amt = document\.getElementById/.test(src));
check('OCR parser still extracts amount (unused, not deleted)',
  src.includes('parsed.amount'));

console.log('\n--- BILL OCR chain intact ---');
for(const fn of ['handleBillUpload','extractTextFromFile','parseUtilityBill'])
  check(`${fn}() intact`, src.includes('function '+fn+'('));
check('upload -> extract -> parse order preserved',
  src.indexOf('extractTextFromFile(f)') < src.indexOf('parseUtilityBill(text)'));
for(const f of ['c-first','c-last','c-acct','a-street','a-city','a-zip'])
  check(`  OCR still populates #${f}`, new RegExp("getElementById\\('"+f+"'\\)\\.value = parsed").test(src));
check('  ...and marks them as auto-filled', src.includes("markOcrFilled('c-first')"));
check('  ...fields remain editable (no readonly added)',
  !/getElementById\('c-first'\)\.readOnly/.test(src));

console.log('\n--- LMI OCR + Program/Format taxonomy ---');
check('classifyLmiDoc unchanged in architecture', src.includes('function classifyLmiDoc('));
check('handleLmiUpload intact', src.includes('function handleLmiUpload('));
check('lmiTypes table retained', src.includes('const lmiTypes'));
check('program select carries the PERCH source_type as its value',
  /<option value="proof_doc_snap">SNAP<\/option>/.test(html));
check('  ...with clean rep-facing labels',
  html.includes('>HEAP / EAP / LIHEAP<') && html.includes('value="proof_doc_liheap"'));
check('  ...Section 8 labelled for reps', html.includes('>Section 8 / Housing<'));
// NOTE: proof_doc_section_8 contains a digit - the character class must allow it.
const progVals=[...new Set((html.match(/value="proof_doc_[a-z0-9_]+"/g)||[]))];
check(`  ...no invented programs (${progVals.length} options, all real source types)`,
  progVals.length===7);
check('format remains a SEPARATE dimension', html.includes('id="lmi-format"'));
check('  ...with the supported formats', html.includes('value="card"') && html.includes('value="letter"'));
check('OCR fills program from matched.sourceType',
  /getElementById\('lmi-doctype'\)\.value = analysis\.matched\.sourceType/.test(src));
check('OCR fills format from matched.defaultDocType',
  /getElementById\('lmi-format'\)\.value = analysis\.matched\.defaultDocType/.test(src));
check('  ...only when known - the missing dimension is NEVER fabricated',
  /if\(analysis\.matched\.sourceType\)/.test(src) && /if\(analysis\.matched\.defaultDocType\)/.test(src));
check('lmiTypeForLabel resolves by source_type', /x\.sourceType === value/.test(src));
check('  ...and still accepts legacy labels', /x\.label === value/.test(src));
check('Relationship retained (Perch requires it)', html.includes('id="lmi-relationship"'));
check('Name on document retained', html.includes('id="lmi-name-on-doc"'));
check('LMI upload retained', html.includes('id="lmi-file"'));

console.log('\n--- program persistence + resume unaffected ---');
check('loadProgramOptions retained', src.includes('function loadProgramOptions('));
check('selection persists to the backend', src.includes("/program'"));
check('  ...hydrates from the backend', src.includes('body.selected_customer_type'));
check('resume hydrates the branch', src.includes('e.selected_customer_type'));
check('resume uses backend workflow state', src.includes('WORKFLOW_STEP_TO_WIZARD'));
check('upload isolation retained', src.includes('docSetResetAll'));
check('reduced motion respected', css.includes('prefers-reduced-motion'));

const f=R.filter(r=>!r.ok);
console.log('\n'+'='.repeat(72));
console.log(`${R.length-f.length} passed, ${f.length} failed`);
console.log('='.repeat(72));
if(f.length){f.forEach(x=>console.log('  FAILED: '+x.l));process.exit(1);}
process.exit(0);

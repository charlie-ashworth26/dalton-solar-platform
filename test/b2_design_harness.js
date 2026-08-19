/*
 * B2 — visual/responsive overhaul, WITHOUT functional loss.
 *
 * The risk in a restyle is silently dropping a control or a handler. This
 * harness pins the full pre-B2 inventory (50 controls, 31 handlers) and fails
 * if any of it disappears, then checks the design system itself.
 */
const fs=require('fs'),path=require('path'),vm=require('vm');
const ROOT=path.join(__dirname,'..');
const html=fs.readFileSync(path.join(ROOT,'templates','index.html'),'utf8');
const css=fs.readFileSync(path.join(ROOT,'static','css','app.css'),'utf8');
const js=fs.readFileSync(path.join(ROOT,'static','js','app.js'),'utf8');
const R=[]; const check=(l,c)=>{R.push({l,ok:!!c});console.log(`  [${c?'PASS':'FAIL'}] ${l}`);};

console.log('='.repeat(72));
console.log('B2 — design system + responsive, no functional loss');
console.log('='.repeat(72));

// ── Inventory captured BEFORE the restyle ──
const CONTROLS=['a-city','a-state','a-street','a-unit','a-zip','admin-modal-save','b-city',
 'b-state','b-street','b-unit','b-zip','bill-amount','bill-file','billing-same','btn-bill-next',
 'btn-contact-next','btn-lmi-next','c-acct','c-email','c-first','c-last','c-pass','c-pass-confirm',
 'c-pass-confirm-eye','c-pass-eye','c-phone','c-pod','complete-cta','cust-login-btn',
 'cust-login-email','cust-login-pass','cust-login-pass-eye','lmi-doctype','lmi-file','lmi-format',
 'lmi-household-size','lmi-name-on-doc','lmi-relationship','login-email','login-pass',
 'login-submit-btn','portal-open-btn','rep-create-btn','rep-new-code','rep-new-email',
 'rep-new-name','rep-new-pass','rep-new-pass-eye','rep-new-phone','rep-new-team'];
const HANDLERS=['adminModalBackdrop','agrBackdrop','backFromCustomer','closeAdminModal',
 'closeAgreements','completeReturnToDashboard','createRep','doCustomerLogin','doLogin',
 'exitWizard','goStep','openCustomerContracts','resetAll','setIncomeAnswer','setLmiMode',
 'showScreen','showView','startWizardFresh','submitAdminModal','submitBill','submitContact',
 'submitLmi','togglePasswordVisibility','checkBillReady','checkLmiReady','handleBillUpload',
 'handleLmiUpload','renderCustomers','syncBillingFromService','toggleBillingAddress',
 'updateAmiThreshold'];

console.log('\n--- every control survived the restyle ---');
let missing=CONTROLS.filter(id=>!html.includes(`id="${id}"`));
check(`all ${CONTROLS.length} controls still present (missing: ${missing.join(',')||'none'})`, !missing.length);
let missingH=HANDLERS.filter(h=>!html.includes(h+'(') && !js.includes('function '+h+'('));
check(`all ${HANDLERS.length} handlers still wired (missing: ${missingH.join(',')||'none'})`, !missingH.length);

console.log('\n--- design tokens ---');
for(const t of ['--brand','--navy','--gold','--ink','--bg','--surface','--border',
                '--radius','--shadow','--ease','--t-base'])
  check(`token ${t} defined`, css.includes(t+':'));
check('green reserved (not used as a page background)',
  !/--bg:\s*#[0-9a-f]*[^;]*green/i.test(css));
check('spacing scale exists', css.includes('--s1:')&&css.includes('--s6:'));
check('single easing curve', (css.match(/--ease:/g)||[]).length===1);

console.log('\n--- typography ---');
check('h1 uses a fluid clamp', /h1\{font-size:clamp\(/.test(css));
check('negative letter-spacing on headings', /letter-spacing:-0\.0\d+em/.test(css));
check('lead copy has a measure cap', /\.lead\{[^}]*max-width:\s*\d+ch/.test(css));
check('body line-height set for readability', /body\{[^}]*line-height:1\.5/.test(css));

console.log('\n--- one obvious primary action ---');
check('primary button is the brand colour', /\.btn-primary\{background:var\(--brand\)/.test(css));
check('secondary is quiet', /\.btn-ghost\{background:var\(--surface\)/.test(css));
check('disabled state defined', /\.btn:disabled\{/.test(css));
check('focus-visible ring on buttons', /\.btn:focus-visible\{/.test(css));

console.log('\n--- form controls ---');
check('inputs share one height', /input\[type=text\][\s\S]{0,400}height:44px/.test(css));
check('focus ring on inputs', /input:focus[\s\S]{0,200}box-shadow:0 0 0 3px var\(--brand-ring\)/.test(css));
check('error state defined', /input\.err[\s\S]{0,160}--danger/.test(css));
check('selects get a custom chevron', /select\{[\s\S]{0,300}background-image:url\("data:image\/svg\+xml/.test(css));
check('disabled inputs are visibly disabled', /input:disabled[\s\S]{0,120}surface-sunk/.test(css));

console.log('\n--- motion is meaningful, not decorative ---');
check('prefers-reduced-motion honoured', /@media \(prefers-reduced-motion: reduce\)/.test(css));
check('  ...durations collapse to 1ms', /--t-base:1ms/.test(css));
check('  ...animations disabled', /animation-duration:1ms !important/.test(css));
check('entrance animation is subtle (<=8px travel)',
  /@keyframes ceeIn\{from\{opacity:0;transform:translateY\([0-8]px\)/.test(css));
check('success check animates once', /\.complete-check\{animation:ceeCheck[^}]*1 both/.test(css));
check('OCR fill highlight exists', /@keyframes ocrFill/.test(css));
check('  ...and is wired to prefilled fields', js.includes('markOcrFilled('));
check('  ...without locking the field', !/readonly/.test(js.split('function markOcrFilled')[1]||''));

console.log('\n--- loading affordances ---');
check('spinner defined', /\.cee-spinner\{/.test(css));
check('skeleton shimmer defined', /@keyframes ceeShimmer/.test(css));
check('loading helper used, not bare text', (js.match(/ceeLoading\(/g)||[]).length>=3);

console.log('\n--- responsive: 4 breakpoints, no sideways scroll ---');
for(const bp of ['1200px','820px','430px'])
  check(`breakpoint ${bp} present`, css.includes(`max-width:${bp}`));
check('overflow-x hidden globally', /html,body\{max-width:100%;overflow-x:hidden/.test(css));
check('tables become cards on mobile', /\.table thead[\s\S]{0,120}clip:rect\(0 0 0 0\)/.test(css));
check('  ...cells carry their label', /\.table td::before[\s\S]{0,80}attr\(data-label\)/.test(css));
check('  ...and the JS emits data-label', (js.match(/data-label=/g)||[]).length>=15);
check('touch targets >=44px on mobile', /@media \(max-width:820px\)[\s\S]{0,900}\.btn\{height:46px/.test(css));
check('inputs 16px on mobile (no iOS zoom)',
  /@media \(max-width:820px\)[\s\S]{0,1200}input,select,textarea\{height:46px;font-size:16px/.test(css));
check('wizard actions stack on mobile',
  /@media \(max-width:820px\)[\s\S]{0,900}\.wizard-actions\{flex-direction:column-reverse/.test(css));
check('address grid stays usable on mobile', /\.row3\.address-grid\{grid-template-columns/.test(css));
check('  ...and the markup uses it', (html.match(/row3 address-grid/g)||[]).length===2);
check('modals become bottom sheets on mobile',
  /@media \(max-width:820px\)[\s\S]{0,600}\.cee-modal-overlay\{padding:0;align-items:flex-end/.test(css));
check('long values wrap rather than overflow', /overflow-wrap:anywhere/.test(css));

console.log('\n--- dashboard polish ---');
check('stat cards defined', /\.stat-card\{/.test(css));
check('  ...complete bucket is highlighted', /\.stat-card\.stat-verified \.sc-num\{color:var\(--brand\)/.test(css));
check('status pills have a dot indicator', /\.status-pill::before\{content:''/.test(css));
check('dead Project column replaced by the enrollment code',
  !html.includes('<th>Project</th>') && html.includes('<th>Enrollment</th>'));
check('  ...and the row renders it', js.includes("data-label=\"Enrollment\""));

console.log('\n--- program cards ---');
check('selected state is obvious', /\.prog-option\.selected\{box-shadow:0 0 0 3px var\(--brand-ring\)/.test(css));
check('  ...with a check affordance', /\.prog-option\.selected \.prog-name::after\{content:'✓'/.test(css));
check('savings uses the display face', /\.prog-savings\{font-family:'Manrope'/.test(css));

console.log('\n--- B1 + Phase A behaviour untouched ---');
check('one acknowledgement checkbox', (js.match(/id="agr-ack-check"/g)||[]).length===1);
check('one Agree & finish button', (js.match(/id="agr-agree-btn"/g)||[]).length===1);
check('inline agreement links', js.includes('agreementLinksHtml'));
check('branch-aware steps', js.includes('function activeSteps'));
check('program selection', js.includes('loadProgramOptions'));
check('admin modal, no prompts', js.includes('openAdminModal') && !/\bprompt\(/.test(
  js.split('\n').filter(l=>!l.trim().startsWith('/*')&&!l.trim().startsWith('*')&&!l.trim().startsWith('//')).join('\n')));
check('state dropdowns', /<select id="a-state"/.test(html)&&/<select id="b-state"/.test(html));
check('dashboard buckets', js.includes('dashBucketFor'));
check('resume reconciliation', js.includes('canRegenerate'));
check('OCR path intact', js.includes('extractTextFromFile')&&js.includes('parseUtilityBill'));
check('no Projects UI', !html.includes('id="view-projects"'));

const f=R.filter(r=>!r.ok);
console.log('\n'+'='.repeat(72));
console.log(`${R.length-f.length} passed, ${f.length} failed`);
console.log('='.repeat(72));
if(f.length){f.forEach(x=>console.log('  FAILED: '+x.l));process.exit(1);}
process.exit(0);

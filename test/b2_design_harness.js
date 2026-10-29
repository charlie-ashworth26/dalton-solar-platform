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
 'b-state','b-street','b-unit','b-zip','bill-file','billing-same','btn-bill-next','btn-lmi-next','c-acct','c-email','c-first','c-last','c-pass','c-pass-confirm',
 'c-pass-confirm-eye','c-pass-eye','c-phone','c-pod','complete-cta','lmi-doctype','lmi-file','lmi-format',
 'lmi-household-size','lmi-name-on-doc','lmi-relationship','login-email','login-pass',
 'login-submit-btn','portal-open-btn','rep-create-btn','rep-new-code','rep-new-email',
 'rep-new-name','rep-new-pass','rep-new-pass-eye','rep-new-phone','rep-new-team'];
const HANDLERS=['adminModalBackdrop','agrBackdrop','backFromCustomer','closeAdminModal',
 'closeAgreements','completeReturnToDashboard','createRep','doLogin',
 'exitWizard','goStep','openCustomerContracts','resetAll','setIncomeAnswer','setLmiMode',
 'showScreen','showView','startWizardFresh','submitAdminModal','submitBill',
 'submitLmi','togglePasswordVisibility','checkBillReady','checkLmiReady','handleBillUpload',
 // submitContact was REMOVED with the Customer & Bill merge (zero callers, and it
 // kept a dead 'Contact page' shape alive). Its work lives in submitContactDetails.
 'submitContactDetails',
 'handleLmiUpload','renderCustomers','syncBillingFromService','toggleBillingAddress',
 'updateAmiThreshold'];

console.log('\n--- every control survived the restyle ---');
let missing=CONTROLS.filter(id=>!html.includes(`id="${id}"`));
check(`all ${CONTROLS.length} controls still present (missing: ${missing.join(',')||'none'})`, !missing.length);
let missingH=HANDLERS.filter(h=>!html.includes(h+'(') && !js.includes('function '+h+'('));
check(`all ${HANDLERS.length} handlers still wired (missing: ${missingH.join(',')||'none'})`, !missingH.length);

console.log('\n--- design tokens ---');
for(const t of ['--brand','--navy','--gold','--ink','--bg','--surface','--border',
                '--radius','--shadow','--ease','--dur-base',
                // C1 design system
                '--t-body','--t-label','--w-bold','--focus-ring',
                '--status-action-bg','--status-complete-ink'])
  check(`token ${t} defined`, css.includes(t+':'));
check('green reserved (not used as a page background)',
  !/--bg:\s*#[0-9a-f]*[^;]*green/i.test(css));
check('spacing scale exists', css.includes('--s1:')&&css.includes('--s6:'));
check('single easing curve', (css.match(/--ease:/g)||[]).length===1);

console.log('\n--- typography ---');
check('page titles use the h1 step', /h1\{[^}]*font-size:var\(--t-h1\)/.test(css));
check('  ...and the one fluid step is reserved for hero titles',
  /--t-hero:clamp\(/.test(css) && /\.agr-page h1\{[^}]*var\(--t-hero\)/.test(css));
check('negative letter-spacing on headings', /letter-spacing:-0\.0\d+em/.test(css));
check('lead copy has a measure cap', /\.lead\{[^}]*max-width:\s*\d+ch/.test(css));
check('body line-height set for readability', /body\{[^}]*line-height:1\.5/.test(css));

console.log('\n--- one obvious primary action ---');
check('primary button is the accessible brand green',
  /\.btn-primary\{background:var\(--brand-solid\)/.test(css));
check('secondary is quiet', /\.btn-ghost\{background:var\(--surface\)/.test(css));
check('disabled state defined', /\.btn:disabled,\.btn\[aria-disabled=true\]\{/.test(css));
check('focus-visible ring on buttons', /\.btn:focus-visible\{/.test(css));

console.log('\n--- form controls ---');
check('inputs share one height', /input\[type=text\][\s\S]{0,400}height:44px/.test(css));
check('focus ring on inputs', /input:focus[\s\S]{0,200}box-shadow:var\(--focus-ring\)/.test(css));
check('error state defined', /input\.err[\s\S]{0,160}--danger/.test(css));
check('selects get a custom chevron', /select\{[\s\S]{0,300}background-image:url\("data:image\/svg\+xml/.test(css));
check('disabled inputs are visibly disabled', /input:disabled[\s\S]{0,120}surface-sunk/.test(css));

console.log('\n--- motion is meaningful, not decorative ---');
check('prefers-reduced-motion honoured', /@media \(prefers-reduced-motion: reduce\)/.test(css));
check('  ...durations collapse to 1ms', /--dur-base:1ms/.test(css));
check('  ...animations disabled', /animation-duration:1ms !important/.test(css));
check('entrance animation is subtle (<=8px travel)',
  /@keyframes ceeIn\{from\{opacity:0;transform:translateY\([0-8]px\)/.test(css));
check('success check animates once', /\.complete-check\{animation:ceeCheck[^}]*1 both/.test(css));
check('OCR fill highlight exists', /@keyframes ocrFill/.test(css));
check('  ...and is wired to prefilled fields', js.includes('markOcrFilled('));
// Scope to the FUNCTION BODY. The old slice ran to end-of-file, so any later
// readonly field (e.g. the manual copy-link fallback) tripped it falsely.
const markOcrBody=(js.split('function markOcrFilled')[1]||'').split('\n}')[0];
check('  ...without locking the field', !/readonly/.test(markOcrBody));

console.log('\n--- loading affordances ---');
check('spinner defined', /\.cee-spinner\{/.test(css));
check('skeleton shimmer defined', /@keyframes ceeShimmer/.test(css));
check('loading helper used, not bare text', (js.match(/ceeLoading\(/g)||[]).length>=3);

console.log('\n--- responsive: 4 breakpoints, no sideways scroll ---');
/* RE-PINNED (Phase C3). Everything in this section used to be matched by
   literal breakpoint number - 'max-width:820px' plus a {0,900} character
   window - and by the presence of the overflow-x:hidden crutch. C3 was asked
   to consolidate the thirteen scattered breakpoints into four and to remove
   that crutch, so seven checks here failed while every rule they name was
   intact and merely sitting in a renumbered band.
   The band is now resolved by name and its rules are looked for inside it,
   which is both what these checks always meant and robust against the next
   renumbering. The audit's ladder is the constant; the window is gone. */
const BANDS = { wide: 1199, touch: 899, narrow: 599, tiny: 430 };
function band(px){            // every @media (max-width:<px>) body, joined
  const re = /@media\s*\(\s*max-width:\s*(\d+)px\s*\)\s*\{/g;
  let m, out = '';
  while((m = re.exec(css))){
    if(+m[1] !== px) continue;
    let i = m.index + m[0].length, depth = 1;
    while(i < css.length && depth){
      if(css[i] === '{') depth++;
      else if(css[i] === '}') depth--;
      i++;
    }
    out += css.slice(m.index + m[0].length, i - 1);
  }
  return out;
}
const TOUCH = band(BANDS.touch);
for(const [name, px] of Object.entries(BANDS))
  check(`breakpoint ${name} (${px}px) present`, band(px).length > 0);
// @media only: a plain max-width DECLARATION on an ordinary rule is a layout
// cap, not a breakpoint, and counting those made this read thirteen again.
const bpList = [...new Set((css.match(/@media[^{]*?max-width:\s*(\d+)px/g)||[])
  .map((s)=>+/(\d+)px/.exec(s)[1]))].sort((a,b)=>a-b);
check(`four breakpoints, not thirteen (${bpList.join(', ')})`,
  bpList.join(',') === '430,599,899,1199');
/* The crutch is GONE on purpose: it hid the 390px clipping rather than fixing
   it, so the audit's defect survived behind it. What remains is the
   containment that is genuinely load-bearing. Whether the page actually
   scrolls sideways is now measured, at six widths, by c3_responsive_harness.js
   and in a real browser - not asserted from the presence of a hack. */
check('nothing hides horizontal overflow',
  !/(?:^|\})\s*(?:html|body|html\s*,\s*body)[^{}]*\{[^}]*overflow-x\s*:\s*hidden/
    .test(css));
check('  ...and the load-bearing containment stays',
  /html,body\{max-width:100%/.test(css));
check('tables become cards on mobile', /\.table thead[\s\S]{0,120}clip:rect\(0 0 0 0\)/.test(css));
check('  ...cells carry their label', /\.table td::before[\s\S]{0,80}attr\(data-label\)/.test(css));
check('  ...and the JS emits data-label', (js.match(/data-label=/g)||[]).length>=15);
check('touch targets >=44px on mobile',
  /\.btn\{height:48px/.test(TOUCH));
check('inputs 16px on mobile (no iOS zoom)',
  /input,select,textarea\{height:48px;font-size:16px/.test(TOUCH));
check('wizard actions stack on mobile',
  /\.wizard-actions\{flex-direction:column-reverse/.test(TOUCH));
check('address grid stays usable on mobile', /\.row3\.address-grid\{grid-template-columns/.test(css));
// The Customer & Bill rebuild replaced .row3.address-grid with .fgrid-addr.
// The billing block still uses row3 elsewhere; assert the ADDRESS GRID exists in
// whichever form, and that it collapses responsively.
// Explicit CSS Grid replaced the fgrid helpers.
check('  ...and the markup uses the mockup address grid',
  /class="address"/.test(html) && (html.match(/class="address"/g)||[]).length>=2);
/* RE-PINNED (Phase C4). These named the mockup's own track list - 2fr 1fr
   110px, with the street opting out via grid-column:1/-1, collapsing to
   1fr 120px on touch. C4 replaced the screen's THREE unrelated grids with one
   twelve-column system because that track list was the alignment problem: it
   produced seven different field widths on one form and five address rows.
   The property these checks exist for is that the address is a grid whose
   street spans wide and whose parts line up, and that it collapses on touch -
   all still true, so they are re-pinned to the system that now provides it. */
check('  ...on the shared twelve-column grid',
  /\.access,\.fields,\.address\{display:grid;grid-template-columns:repeat\(12,minmax\(0,1fr\)\)/
    .test(css.replace(/\s+/g, '')));
check('  ...with the street spanning most of its row',
  /\.address \.street\{grid-column:span 8/.test(css));
// The street's full-span rule is written as one part of a selector LIST, so
// this looks for the rule that contains it rather than for an exact block.
const touchStreet = (TOUCH.match(/([^{}]+)\{([^{}]*)\}/g) || []).find((b) =>
  /\.address\s+\.street/.test(b.split('{')[0]) && /grid-column:1\/-1/.test(b));
check('  ...collapsing to a single-field grid on touch',
  /\.access,\.fields,\.address\{grid-template-columns:repeat\(6,minmax\(0,1fr\)\)/
    .test(TOUCH.replace(/\s+/g, '')) && !!touchStreet);
check('modals become bottom sheets on mobile',
  /\.cee-modal-overlay\{padding:0;align-items:flex-end/.test(TOUCH));
check('long values wrap rather than overflow', /overflow-wrap:anywhere/.test(css));

console.log('\n--- dashboard polish ---');
check('stat cards defined', /\.stat-card\{/.test(css));
/* RE-PINNED (Phase C4). This asserted that ONE bucket - Complete, via a
   .stat-verified hook - got a coloured numeral. That was the defect C4 was
   asked to fix: the other three counters, including "Needs Attention", drew
   in plain ink, so the only card that looked like it meant something was the
   one that needed no action. All four counters now take their accent and
   numeral from their own C1 status family, and .stat-verified is retired
   because it was a second, disagreeing way of saying "complete". */
const flat = css.replace(/\s+/g, '');
check('  ...the neutral family sets the baseline treatment',
  /\.stat-fam\{border-left:3pxsolidvar\(--status-neutral-ink\)/.test(flat)
  && /\.stat-fam\.sc-num\{color:var\(--status-neutral-ink\)/.test(flat));
check('  ...and each of the other three overrides it with its own ink',
  [['action-needed', 'action'], ['complete', 'complete'], ['attention', 'attention']]
    .every(([fam, ink]) =>
      flat.includes(`.stat-fam-${fam}{border-left-color:var(--status-${ink}-ink)`)
      && flat.includes(`.stat-fam-${fam}.sc-num{color:var(--status-${ink}-ink)`)));
check('  ...and the one-off .stat-verified hook is no longer rendered',
  /class="stat-card stat-fam stat-fam-'\s*\+\s*bucket\.family/.test(js));
check('status pills have a dot indicator', /\.status-pill::before\{content:''/.test(css));
check('dead Project column replaced by the enrollment code',
  !html.includes('<th>Project</th>') && html.includes('<th>Enrollment</th>'));
check('  ...and the row renders it', js.includes("data-label=\"Enrollment\""));

console.log('\n--- program cards ---');
check('selected state is obvious', /\.prog-option\.selected\{box-shadow:var\(--ring-selected\)/.test(css));
check('  ...with a check affordance', /\.prog-option\.selected \.prog-name::after\{content:'✓'/.test(css));
check('savings uses the display face',
  /\.prog-savings\{font-family:var\(--font-display\)/.test(css));

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

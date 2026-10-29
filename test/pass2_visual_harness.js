/* PASS 2 — visual overhaul, proving structure changed and nothing broke. */
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const ROOT=path.join(__dirname,'..');
const src=fs.readFileSync(path.join(ROOT,'static','js','app.js'),'utf8');
const html=fs.readFileSync(path.join(ROOT,'templates','index.html'),'utf8');
const css=fs.readFileSync(path.join(ROOT,'static','css','app.css'),'utf8');
const wf=fs.readFileSync(path.join(ROOT,'services','perch','workflow.py'),'utf8');
const R=[]; const check=(l,c)=>{R.push({l,ok:!!c});console.log(`  [${c?'PASS':'FAIL'}] ${l}`);};

console.log('='.repeat(72));
console.log('PASS 2 — visual overhaul');
console.log('='.repeat(72));

console.log('\n--- PROTECTED agreement block still byte-identical ---');
const EXPECT={agreementLinksHtml:'5822142aa810981cb29486195b35eb46',
  ack_label:'a8da353295a59308773674a1c5427d96',
  updateAgreeButton:'9a3b5a86b1081eb6514ad939c2f4afa3',
  // C2 moved these two: see the note on the same freeze in
  // pass1_structure_harness.js. Only the error-display lines changed.
  submitAgreements:'69d2d118424c3bd2ddccb9302dafbe0e',
  openAgreementDoc:'9851430b4cb5ba2225108270fde73642'};
const md5=t=>crypto.createHash('md5').update(t).digest('hex');
function grab(n){
  if(n==='ack_label'){const i=src.indexOf('html += \'<label class="agr-ack"');
    return src.slice(i, src.indexOf('</label>',i)+'</label>\';'.length);}
  const m=src.match(new RegExp((n==='submitAgreements'?'async ':'')+'function '+n+'\\([\\s\\S]*?\\n\\}'));
  return m?m[0]:'';
}
for(const [k,h] of Object.entries(EXPECT)) check(`${k} unchanged`, md5(grab(k))===h);

console.log('\n--- DASHBOARD: compact CRM ---');
check('six CRM columns', (html.match(/<th>Utility<\/th>/g)||[]).length===2);
check('  ...Customer | Enrollment | Status | Utility | Added | Last modified',
  /<th>Customer<\/th><th>Enrollment<\/th><th>Status<\/th><th>Utility<\/th><th>Added<\/th><th>Last modified<\/th>/.test(html));
check('no separate Open/Action column', !/<th><\/th>/.test(html));
check('STATUS is the resume control', /class="status-pill status-action/.test(src));
check('  ...rendered as a real <button>', /<button type="button" class="status-pill status-action/.test(src));
check('  ...calling backend-authoritative openEnrollment(id)',
  /status-action[\s\S]{0,300}onclick="openEnrollment\(/.test(src));
// REWRITTEN (Phase B), and STRICTER than before. The old form was a 120-char
// PROXIMITY window: "workflow_step_label ... indexOf" anywhere within 120
// characters. Phase B passes the backend label into the read-only agreement
// mount, and the very next statement happens to be
// REHYDRATE_CONTRACT_STEPS.indexOf(key) - an indexOf over a list of workflow
// KEYS, which is exactly the authoritative routing this check wants. Proximity
// could never tell those apart.
//
// This tests the real property instead: the visible label is never string
// matched, in either direction. label.indexOf(...) / .includes / .match and
// anything.indexOf(label) all fail, wherever they sit in the file.
check('  ...never parses the visible status string to navigate',
  !/workflow_step_label[^,;)]*\.(indexOf|includes|match|search|startsWith|endsWith)\(/.test(src)
  && !/\.(indexOf|includes|search)\([^)]*workflow_step_label/.test(src)
  && !/\bstatus(Label)?\.(includes|indexOf|match)\(/.test(src));
check('  ...keyboard reachable + labelled', /status-action[\s\S]{0,400}aria-label=/.test(src));
check('  ...has focus ring', css.includes('.status-action:focus-visible'));
check('customer cell is name + secondary identity',
  /cell-cust[\s\S]{0,200}cust-name[\s\S]{0,200}cust-sub/.test(src));
check('tighter row density', /\.crm-row td\{padding:var\(--s3\) var\(--s3\)/.test(css));
check('compact uppercase headers',
  /\.data-table th,\.table th\{font-size:var\(--t-label\)/.test(css));
check('subtle row hover', css.includes('.crm-row:hover'));
check('utility column populated from real data', /e\.utility_name/.test(src));
check('dates from persisted timestamps',
  /formatTimestamp\(e\.created_at\)/.test(src) && /formatTimestamp\(e\.updated_at\)/.test(src));
/* RE-PINNED (Phase C3). The responsive checks below matched a literal
   breakpoint number - 'max-width:820px', 'max-width:900px' - and C3 was asked
   to consolidate thirteen scattered breakpoints into four (430/599/899/1199).
   Every rule they name is intact; only the band around it was renumbered. The
   band is now resolved by name and its rules looked for inside it, which is
   what these checks always meant and is robust against the next change. */
const BANDS = { wide: 1199, touch: 899, narrow: 599, tiny: 430 };
function band(px){            // every @media (max-width:<px>) body, joined
  const bre = /@media\s*\(\s*max-width:\s*(\d+)px\s*\)\s*\{/g;
  let m, out = '';
  while((m = bre.exec(css))){
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
check('mobile converts rows to cards intentionally',
  /\.crm-row td\{padding:var\(--s1\) 0/.test(TOUCH));
check('  ...status stays a large tap target on mobile',
  /\.status-action\{height:34px/.test(css));

console.log('\n--- AGREEMENTS: one summary, no data dumps ---');
check('duplicate Customer/Project review blocks removed',
  (html.match(/class="review-block"/g)||[]).length<=1);
check('one premium summary', html.includes('class="summary"'));
// Rebuilt to the approved agreements mockup: .group replaces .sum-group.
check('  ...with Customer / Service / Program groups',
  (html.match(/class="group"/g)||[]).length===3
  && (html.match(/class="group-label"/g)||[]).length===3);
// RE-PINNED (Phase C4): the exact fractions 1.05fr 1.2fr 1fr were the reason
// the Customer column - which holds the longest value on the card, an email -
// was the narrowest, and a normal address broke mid-word at 1440px. The
// property is three columns on desktop and one on touch; the widths now follow
// what each column carries, so they are no longer pinned to a literal.
const summaryTracks = (() => {
  const m = /\.summary-grid\{display:grid;grid-template-columns:([^;}]+)/.exec(css);
  return m ? m[1].trim().split(/\s+/).map(parseFloat) : [];
})();
check(`  ...three-column desktop, stacked mobile (${summaryTracks.join(' ')})`,
  summaryTracks.length === 3 && summaryTracks.every((n) => !isNaN(n))
  && /\.summary-grid\{grid-template-columns:1fr/.test(TOUCH));
check('  ...the customer column is the widest, since it holds the email',
  summaryTracks.length === 3
  && summaryTracks[0] >= summaryTracks[1] && summaryTracks[0] > summaryTracks[2]);
for(const id of ['rv-name','rv-email','rv-phone','rv-acct','rv-utility','rv-address','rv-lmi'])
  check(`  existing ${id} preserved (fillReview untouched)`, html.includes('id="'+id+'"'));
check('program + savings shown', html.includes('id="rv-program"'));
check('  ...from the SELECTED program\'s persisted savings only',
  /detail\.program_savings/.test(src) && !/savings_percent_res_commercial/.test(src));
check('enrollment code shown quietly', html.includes('id="rv-code"'));
check('eligibility row only when proof was submitted AND branch is LMI',
  /if\(perchContext\.proofSubmitted && isLmi\)/.test(src)
  && /lmiRow\.style\.display = 'none'/.test(src));
check('heading rewritten to Review & finish', html.includes('Review &amp; finish'));

console.log('\n--- CUSTOMER & BILL coherence ---');
// The context strip became the mockup's utility chip in the program head.
check('service context is a quiet utility chip', html.includes('id="util-chip"'));
check('program cards at the top', html.includes('id="program-options"'));
// Customer & Bill was REBUILT: .form-section / .fs-title / .ctx-line were part
// of the old card composition and are gone. Sections are now numbered blocks
// separated by spacing, and the email is metadata in the context rail.
check('sectioned, not one endless form',
  css.includes('.sec-head') && (html.match(/class="sec"/g)||[]).length>=4);
// The numbered mini-steps were REMOVED - they read as a second wizard inside
// the wizard. Sections are now plain headings; the global stepper owns progress.
check('  ...no numbered mini-steps', !html.includes('class="blk-num"'));
check('  ...each section has a real heading',
  /\.sec-head h2\{font-size:var\(--t-h2\)/.test(css));
check('  ...separated by a rule, not nested cards',
  /\.sec\{padding:var\(--s5\) 0;border-top:1px solid/.test(css));
check('customer access is a section in the same flow',
  /<h2>Customer access<\/h2>/.test(html));
check('email is not re-asked as a visible field',
  /<input type="email" id="c-email" readonly hidden>/.test(html));

console.log('\n--- STEPPER ---');
check('no SVG arc anywhere', !html.includes('arc-svg') && !src.includes('arc-progress'));
check('nodes + connectors', css.includes('.stp-node') && css.includes('.stp-line'));
check('three visual states', css.includes('.stp-item.done') &&
  css.includes('.stp-item.now') && css.includes('.stp-node'));
check('connector fills with a transition', /\.stp-line::after\{[\s\S]{0,200}transition:transform/.test(css));
check('branch-aware render', /renderStepper[\s\S]{0,300}activeSteps\(\)/.test(src));
check('re-renders when the branch becomes known',
  (src.match(/renderStepper\(currentStep \|\| 1\)/g)||[]).length>=2);
check('mobile keeps it readable', /\.stp-label\{display:none/.test(TOUCH));

console.log('\n--- COMPLETION ---');
check('calm success mark', html.includes('class="done-mark'));
check('  ...animates once, no gimmicks', /\.complete-check\{animation:ceeCheck[^}]*1 both/.test(css));
check('concise summary', html.includes('id="complete-summary"'));
check('  ...real values only, omitted when absent', /\.filter\(function\(r\)\{ return r\[1\]; \}\)/.test(src));
check('Back to dashboard is the primary action', html.includes('>Back to dashboard</button>'));
check('no confetti / gimmicks', !/confetti|sparkle|bounce/i.test(css));

console.log('\n--- COPY audit ---');
check('no Perch-session implementation detail in field help',
  !wf.includes('Perch requires this to open an enrollment session'));
/* RE-PINNED (Phase C3). Pass 2 replaced an implementation-detail help line
   ("Perch requires this to open an enrollment session") with plain copy, and
   asserted the plain copy was THERE. C3 item 9 removed that line outright: it
   restated the label and put a row of grey text above every other field on
   the step. So the property is no longer "the better sentence is present" but
   "the email field carries no help line at all, and neither old wording came
   back". The field's own dict is read rather than the file, because the
   comment recording the removal necessarily quotes the sentence it removed. */
const _emailField = (() => {
  const i = wf.indexOf('"name": "email"');
  if (i === -1) return '';
  const open = wf.lastIndexOf('{', i);
  let j = open, depth = 0;
  do {
    if (wf[j] === '{') depth++;
    else if (wf[j] === '}') depth--;
    j++;
  } while (j < wf.length && depth);
  return wf.slice(open, j);
})();
check('  ...and the email field now carries no help line at all',
  _emailField.length > 0 && !/"help"\s*:/.test(_emailField));
check('  ...neither wording came back',
  !/"help"\s*:\s*"[^"]*(?:securely resume|enrollment session)/.test(wf));
check('no caching wording', !wf.includes('rather than cached'));
check('availability headline rewritten', wf.includes('"title": "Check availability"'));
check('  ...with concise supporting copy',
  wf.includes('to see available savings programs.'));

console.log('\n--- MOTION / RESPONSIVE ---');
check('reduced motion honoured', css.includes('prefers-reduced-motion'));
check('durations stay restrained',
  /--dur-quick:140ms/.test(css) && /--dur-slow:260ms/.test(css));
// The no-gradient rule governs the ENROLLMENT UI. The UBS sign-in screen is a
// deliberate exception: its patterned background is the approved design and is
// fully scoped under #screen-login, so it cannot affect anything else.
const appCss = css.slice(0, css.indexOf('UBS SIGN IN') === -1
                            ? css.length : css.indexOf('UBS SIGN IN'));
check('no gradients used decoratively in the enrollment UI',
  (appCss.match(/linear-gradient/g)||[]).length<=1);
check('  ...the UBS login pattern is scoped to #screen-login',
  css.includes('#screen-login{') && css.includes('#screen-login::before'));
const bpList = [...new Set((css.match(/@media[^{]*?max-width:\s*(\d+)px/g)||[])
  .map((s)=>+/(\d+)px/.exec(s)[1]))].sort((a,b)=>a-b);
check(`breakpoints ${bpList.join(' / ')}`, bpList.join(',') === '430,599,899,1199');
/* The overflow-x:hidden guard is GONE on purpose. It removed the scrollbar
   that would have shown the 390px clipping, so the audit's defect survived
   behind it for as long as it was there - a check for its PRESENCE was a check
   that the symptom stayed hidden. What remains is the containment that is
   genuinely load-bearing; whether the page actually scrolls sideways is now
   measured at six widths by c3_responsive_harness.js and in a real browser. */
check('no horizontal overflow guard hiding broken layout',
  !/(?:^|\})\s*(?:html|body|html\s*,\s*body)[^{}]*\{[^}]*overflow-x\s*:\s*hidden/
    .test(css));
check('  ...but the load-bearing containment stays',
  /html,body\{max-width:100%/.test(css));

console.log('\n--- PASS 1 + prior behaviour intact ---');
check('Residential [1,2,5]', src.includes('if(needsLmi === false) return [1,2,5];'));
check('LMI [1,2,4,5]', src.includes('return [1,2,4,5];'));
check('no step-contact', !html.includes('id="step-contact"'));
check('no bill-amount', !html.includes('bill-amount') && !src.includes('bill-amount'));
check('bill OCR chain', src.includes('parseUtilityBill') && src.includes('extractTextFromFile'));
check('LMI OCR chain', src.includes('classifyLmiDoc') && src.includes('lmiTypes'));
check('LMI program values are Perch source types',
  (html.match(/value="proof_doc_[a-z0-9_]+"/g)||[]).length===7);
check('relationship retained', html.includes('id="lmi-relationship"'));
check('program persistence', src.includes('loadProgramOptions'));
check('upload isolation', src.includes('docSetResetAll'));
check('one checkbox / one button',
  (src.match(/id="agr-ack-check"/g)||[]).length===1 &&
  (src.match(/id="agr-agree-btn"/g)||[]).length===1);

const f=R.filter(r=>!r.ok);
console.log('\n'+'='.repeat(72));
console.log(`${R.length-f.length} passed, ${f.length} failed`);
console.log('='.repeat(72));
if(f.length){f.forEach(x=>console.log('  FAILED: '+x.l));process.exit(1);}
process.exit(0);

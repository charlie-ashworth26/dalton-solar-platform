/*
 * RUNTIME PROOF: UBS 2.0 Phase B - the completed / read-only enrollment
 * experience, browser routing, and the unified utility display name.
 *
 * WHY A RUNTIME HARNESS AND NOT A SOURCE CHECK
 * --------------------------------------------
 * Every claim here is about what the rep SEES after opening an enrollment:
 * "a completed enrollment does not say Final step", "a blocked one is not
 * described as complete", "the next enrollment does not inherit the previous
 * one's status wording", "the capacity card shows Dominion Energy and not
 * dominion-energy-va". A grep can prove the strings exist somewhere; only
 * running openEnrollment() can prove which of them lands on the page.
 *
 * METHOD
 *   1. Evaluate app.js in a VM against a DOM stub built from the REAL ids in
 *      templates/index.html, so a check can only pass on an element the
 *      markup actually has.
 *   2. Drive the real entry point - openEnrollment() - with the real shape of
 *      the detail payload for each workflow key.
 *   3. Assert the rendered DOM and the module state, and inspect the fetch log
 *      so "makes no Perch call" is proved by the absence of a request rather
 *      than by intent.
 *
 * node test/phase_b_readonly_harness.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(ROOT, 'templates', 'index.html'), 'utf8');
const REAL_IDS = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));
['wf-email', 'wf-zip_code', 'wf-utility_name', 'wf-err-utility_name',
 'wf-err-zip_code', 'wf-primary', 'wf-form-error', 'workflow-root',
 // Created on the first render and then FOUND on every later one, which is
 // the state a browser is in for all but the very first open. Registering
 // them means renderResumeBanner() and the customer-link slot write into
 // elements this harness can read back.
 'resume-banner', 'customer-link-slot']
  .forEach((id) => REAL_IDS.add(id));

// THE MARKUP'S OWN TEXT for the step-5 framing nodes, so the stub starts where
// a real browser starts. Without this, "the live wording is restored for the
// next enrollment" could only ever be compared against an empty string - and
// would pass whether the restore worked or not.
const MARKUP_TEXT = {};
for (const [, id, inner] of html.matchAll(/id="(agr-eyebrow|agr-title|agr-subtitle|agr-section-title|agr-section-sub|agr-ready-pill|agr-final-label)"[^>]*>([^<]*)</g)) {
  MARKUP_TEXT[id] = inner
    .replace(/&amp;/g, '&').replace(/&#10003;/g, '✓')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').trim();
}
['agr-eyebrow', 'agr-title', 'agr-subtitle', 'agr-section-title',
 'agr-section-sub', 'agr-ready-pill'].forEach((id) => {
  if (typeof MARKUP_TEXT[id] !== 'string' || !MARKUP_TEXT[id]) {
    throw new Error('markup text not found for #' + id + ' - the harness would '
                    + 'be comparing against nothing');
  }
});

const results = [];
function check(label, cond) {
  results.push({ label, ok: !!cond });
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${label}`);
}
function section(t) {
  console.log('\n' + '-'.repeat(72) + '\n' + t + '\n' + '-'.repeat(72));
}

function makeEl(id) {
  const el = {
    id, value: '', checked: false, disabled: false,
    textContent: MARKUP_TEXT[id] || '', innerHTML: MARKUP_TEXT[id] || '',
    title: '', type: 'text', style: {}, dataset: {}, options: [],
    // The markup's own default for the status pill. Set here so a check on
    // hidden can never pass merely because the property was undefined.
    hidden: id === 'agr-ready-pill' || id === 'agr-final-label' ? true : false,
    _listeners: {},
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      toggle(c, on) {
        if (on === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); }
        else if (on) { this._s.add(c); } else { this._s.delete(c); }
      },
      contains(c) { return this._s.has(c); },
    },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    appendChild() {}, insertBefore() {}, removeChild() {},
    addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
    removeEventListener() {},
    async fire(ev) {
      for (const fn of (this._listeners[ev] || [])) await fn({ target: this });
    },
    focus() {}, click() {}, closest() { return null; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    scrollIntoView() {}, getContext() { return null; }, reset() {},
  };
  el.parentNode = null; el.firstChild = null;
  return el;
}

let routes = {};
const fetchLog = [];
const elCache = new Map();
function byClassSelector(sel) {
  const want = String(sel).replace(/^\./, '').split('[')[0];
  const out = [];
  for (const el of elCache.values()) {
    if (el.classList.contains(want)) out.push(el);
  }
  return out;
}
const documentStub = {
  getElementById(id) {
    if (!REAL_IDS.has(id)) return null;
    if (!elCache.has(id)) elCache.set(id, makeEl(id));
    return elCache.get(id);
  },
  querySelector() { return null; },
  querySelectorAll(sel) { return byClassSelector(sel); },
  createElement(tag) { return makeEl('created-' + tag); },
  addEventListener() {}, removeEventListener() {},
  body: makeEl('body'), documentElement: makeEl('html'), activeElement: null,
};
documentStub.body.style = {};
const storageStub = {
  _d: {},
  getItem(k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; },
  setItem(k, v) { this._d[k] = String(v); },
  removeItem(k) { delete this._d[k]; },
  clear() { this._d = {}; },
};

// A minimal history, so the Phase B router's pushes are observable rather
// than silently skipped by its non-browser guard.
const historyLog = [];
const locationStub = { href: 'http://harness/', pathname: '/', reload() {} };
const historyStub = {
  pushState(st, _t, url) { historyLog.push({ kind: 'push', url: String(url) }); locationStub.pathname = String(url); },
  replaceState(st, _t, url) { historyLog.push({ kind: 'replace', url: String(url) }); locationStub.pathname = String(url); },
};

const sandbox = {
  console, document: documentStub, sessionStorage: storageStub, localStorage: storageStub,
  history: historyStub, location: locationStub,
  setTimeout(fn) { if (typeof fn === 'function') fn(); return 0; },
  clearTimeout() {}, setInterval() { return 0; }, clearInterval() {},
  alert() {}, confirm() { return true; },
  fetch(url, opts) {
    fetchLog.push({
      url: String(url),
      method: (opts && opts.method) || 'GET',
      body: (opts && opts.body) || null,
    });
    for (const key of Object.keys(routes)) {
      if (String(url).includes(key)) {
        const r = routes[key];
        return Promise.resolve({
          ok: (r.status || 200) < 400, status: r.status || 200,
          json: () => Promise.resolve(r.body || {}),
        });
      }
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  },
  FormData: function () { this.append = function () {}; },
  FileReader: function () { this.readAsDataURL = function () {}; },
  Blob: function () {},
  URL: { createObjectURL() { return 'blob:stub'; }, revokeObjectURL() {} },
  navigator: { userAgent: 'harness' },
  requestAnimationFrame() { return 0; },
  pdfjsLib: { getDocument() { return { promise: Promise.resolve({ numPages: 0 }) }; }, GlobalWorkerOptions: {} },
  Tesseract: { recognize() { return Promise.resolve({ data: { text: '' } }); } },
};
sandbox.scrollTo = function () {};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

console.log('='.repeat(72));
console.log('RUNTIME PROOF - UBS 2.0 PHASE B: READ-ONLY STATUS, ROUTING, LABELS');
console.log('='.repeat(72));

let ctx;
try {
  ctx = vm.createContext(sandbox);
  vm.runInContext(src, ctx, { filename: 'app.js' });
  check('app.js evaluates without throwing', true);
} catch (e) {
  check('app.js evaluates without throwing - ' + e.message, false);
  report();
}

function el(id) { return documentStub.getElementById(id); }
function run(code) { return vm.runInContext(code, ctx); }
async function settle() {
  for (let i = 0; i < 8; i += 1) await new Promise((r) => setImmediate(r));
}

// ── The real detail payload, as routes/enrollment_routes.py serializes it ──
function detail(overrides) {
  return Object.assign({
    id: 9,
    enrollment_code: 'ENR-0009',
    status: 'submitted',
    created_at: '2026-10-01 12:00:00',
    updated_at: '2026-10-02 09:30:00',
    customer: { id: 4, first_name: 'Dana', last_name: 'Reyes',
                email: 'dana@example.com', phone: '5405551234' },
    service_address: { street: '14 Caroline St', unit: '', city: 'Fredericksburg',
                       state: 'VA', zip: '22401' },
    billing_address: { same_as_service: true },
    utility_account: { utility_name: 'dominion-energy-va', account_number: '••••7781',
                       meter_number: null, secondary_account_identifier: null },
    project: null,
    sales_rep: null,
    lmi_qualification: null,
    // The RESOLVED display name, exactly as the shared resolver returns it.
    utility_name: 'Dominion Energy',
    utility_slug: 'dominion-energy-va',
    service_zip: '22401',
    perch_token_email: 'dana@example.com',
    customer_has_password: true,
    document_sets: [],
    perch_committed: true,
    workflow_step_key: 'contracts_accepted',
    workflow_step_label: 'Complete',
    workflow_is_terminal: true,
    workflow_is_blocked: false,
    workflow_updated_at: '2026-10-02 09:31:00',
    perch_next_step_url: null,
    workflow_last_response: { contracts: [{ contract_name: 'Community Solar Agreement' }] },
    program_savings: { percent: 10, basis: 'perch' },
    selected_customer_type: 'Residential',
  }, overrides || {});
}

function withBase(specific) {
  return Object.assign({}, specific || {}, {
    '/api/enrollments': { body: [] },
    '/api/projects': { body: [] },
    '/api/perch': { body: {} },
  });
}

async function open(d) {
  elCache.clear(); fetchLog.length = 0; historyLog.length = 0;
  routes = withBase({ '/api/enrollments/9': { body: d } });
  storageStub.setItem('dalton_auth_token', 'staff-token');
  const ok = await run('openEnrollment(9)');
  await settle();
  return ok;
}

function framing() {
  return {
    eyebrow: el('agr-eyebrow').textContent,
    title: el('agr-title').textContent,
    subtitle: el('agr-subtitle').textContent,
    sectionSub: el('agr-section-sub').textContent,
    finalLabelHidden: el('agr-final-label').hidden,
    pillText: el('agr-ready-pill').textContent,
    pillHidden: el('agr-ready-pill').hidden,
    footDisplay: el('agr-foot').style.display,
    card: el('agr-host-rep').innerHTML,
    step: run('currentStep'),
  };
}

function perchCalls() {
  return fetchLog.filter((f) => f.url.includes('/api/perch/'));
}

(async function main() {
  // ═══════════════════════════════════════════════════════════════════
  section('1. A COMPLETED ENROLLMENT STOPS ASKING TO BE COMPLETED');
  // The defect: contracts_accepted opened straight onto the live step-5 page,
  // headed "Final step / Review & finish", with a "Final step" chip over the
  // agreements block. A finished enrollment presented as unfinished work.
  await open(detail());
  let f = framing();
  check(`a terminal enrollment opens on the status step (step ${f.step})`,
        f.step === 5);
  check(`  ...the eyebrow is its status, not "Final step" (${JSON.stringify(f.eyebrow)})`,
        f.eyebrow === 'Complete');
  check(`  ...the heading says it is done (${JSON.stringify(f.title)})`,
        f.title === 'Enrollment complete');
  check('  ...the word "Final step" is nowhere in the page framing',
        !/Final step/.test(f.eyebrow + ' ' + f.title + ' ' + f.subtitle + ' ' + f.sectionSub));
  check('  ...the "Final step" chip is hidden', f.finalLabelHidden === true);
  check('  ...the subtitle no longer asks the rep to accept anything',
        !/accept the agreements/.test(f.subtitle) && f.subtitle.length > 0);
  check(`  ...success looks like success: the pill reads ${JSON.stringify(f.pillText)}`,
        f.pillText === '✓ Complete' && f.pillHidden === false);
  check('  ...the agreements block is framed as a record, not a task',
        /cannot be changed/.test(f.sectionSub));
  check('  ...and Back is gone, because it leads into the locked steps',
        f.footDisplay === 'none');

  section('1b. ...AND ITS CARD IS THE COMPLETE CARD');
  check('the card states the enrollment is complete',
        /Enrollment complete/.test(f.card));
  check('  ...and that nothing further is needed',
        /Nothing further is needed/.test(f.card));
  check('  ...the real agreement name is listed from persisted metadata',
        /Community Solar Agreement/.test(f.card));
  check('  ...with no acceptance control on it',
        !/agr-agree-btn/.test(f.card) && !/agr-ack-check/.test(f.card));
  check('  ...one way out: back to the dashboard',
        /backToDashboard\(\)/.test(f.card));
  check('  ...and WHEN the status was recorded, honestly labelled',
        /Status recorded/.test(f.card));
  check('the component carries the authoritative key',
        run('Agreements.statusKey') === 'contracts_accepted'
        && run('Agreements.readOnly') === true
        && run('Agreements.submitted') === true);
  check('the editable steps are locked',
        run('readOnlyLocked') === true
        && el('btn-bill-next').disabled === true
        && run("READ_ONLY_LOCK_IDS.filter(function(id){var e=document.getElementById(id);"
               + "return e && !e.disabled;}).length") === 0);
  check('opening it made NO Perch call at all',
        perchCalls().length === 0);
  check('  ...and the URL carries the enrollment, so the link is shareable',
        historyLog.some((h) => h.url === '/enrollments/9'));

  // ═══════════════════════════════════════════════════════════════════
  section('2. A BLOCKED ENROLLMENT IS NOT DESCRIBED AS COMPLETE');
  // THE honesty defect this item exists for: renderAgreementCard()'s read-only
  // branch was reached by terminal AND blocked enrollments and hardcoded
  // "Your enrollment is complete ... Nothing further is needed." So an
  // acceptance Perch could not confirm was reported to the rep as finished.
  await open(detail({
    workflow_step_key: 'contracts_accept_uncertain',
    workflow_step_label: 'Needs review',
    workflow_is_terminal: false,
    workflow_is_blocked: true,
  }));
  f = framing();
  check(`a blocked enrollment also opens on the status step (step ${f.step})`,
        f.step === 5);
  check(`  ...headed with the real outcome (${JSON.stringify(f.title)})`,
        f.title === 'Acceptance could not be confirmed');
  check(`  ...flagged for review, not completion (${JSON.stringify(f.eyebrow)})`,
        f.eyebrow === 'Needs review');
  check('  ...the card does NOT claim the enrollment is complete',
        !/is complete/.test(f.card) && !/Nothing further is needed/.test(f.card));
  check('  ...it says the acceptance was not confirmed',
        /did not confirm/.test(f.card));
  check('  ...and tells the rep to check with Perch before acting',
        /Check this enrollment with Perch/.test(f.card));
  check('  ...explicitly NOT to re-submit',
        /do not re-submit/.test(f.card));
  check('  ...no completion pill on an enrollment that did not complete',
        f.pillHidden === true && !/Complete/.test(f.pillText));
  check('  ...and no acceptance control either',
        !/agr-agree-btn/.test(f.card));
  check('the component carries the blocked key, and submitted is false',
        run('Agreements.statusKey') === 'contracts_accept_uncertain'
        && run('Agreements.submitted') === false);
  check('opening it made no Perch call', perchCalls().length === 0);

  // ═══════════════════════════════════════════════════════════════════
  section('3. THE TWO BLOCKED KEYS THAT USED TO LAND ON EDITABLE FORMS');
  // enroll_outcome_uncertain was mapped to the Customer & Bill FORM and
  // lmi_not_qualified was not in WORKFLOW_STEP_TO_WIZARD at all, so it fell
  // back to Availability. Both are editable-looking screens whose Continue is
  // merely disabled - settled enrollments presented as unfinished work.
  await open(detail({
    workflow_step_key: 'enroll_outcome_uncertain',
    workflow_step_label: 'Enrollment outcome uncertain - check status',
    workflow_is_terminal: false,
    workflow_is_blocked: true,
  }));
  f = framing();
  check(`enroll_outcome_uncertain opens on the status step, not the bill form (step ${f.step})`,
        f.step === 5);
  check(`  ...with its own heading (${JSON.stringify(f.title)})`,
        f.title === 'Enrollment outcome uncertain');
  check('  ...and the honest instruction to check status with Perch',
        /could not be confirmed/.test(f.card) && /Perch/.test(f.card));
  check('  ...never described as complete', !/is complete/.test(f.card));

  await open(detail({
    workflow_step_key: 'lmi_not_qualified',
    workflow_step_label: 'Does not qualify for the available program',
    workflow_is_terminal: false,
    workflow_is_blocked: true,
    selected_customer_type: 'LMI',
    workflow_last_response: { contracts: [] },
  }));
  f = framing();
  check(`lmi_not_qualified opens on the status step, not Availability (step ${f.step})`,
        f.step === 5);
  check(`  ...stating the outcome (${JSON.stringify(f.title)})`,
        f.title === 'Does not qualify for the available program');
  check('  ...and that the decision was recorded in UBS, NOT sent to Perch',
        /was not sent to Perch/.test(f.card));
  check('  ...so it does not tell the rep to go and check Perch about it',
        !/Check this enrollment with Perch/.test(f.card));
  check('  ...no agreements are invented for an enrollment that generated none',
        !/Community Solar Agreement/.test(f.card));
  check('  ...and no completion pill', f.pillHidden === true);

  // ═══════════════════════════════════════════════════════════════════
  section('4. THE NEXT ENROLLMENT DOES NOT INHERIT THE STATUS WORDING');
  // Same class of leak as the read-only control lock (RC-4): whatever a
  // completed enrollment changes on the shared step-5 DOM, the next
  // enrollment must not wear.
  await open(detail());                       // terminal first
  check('precondition: the page is re-framed as complete',
        framing().title === 'Enrollment complete');
  await open(detail({                         // ...now a LIVE one
    id: 9,
    workflow_step_key: 'enroll',
    workflow_step_label: 'Ready to enroll',
    workflow_is_terminal: false,
    workflow_is_blocked: false,
    perch_committed: false,
    workflow_last_response: null,
  }));
  f = framing();
  check(`the live enrollment is back on its own step (step ${f.step})`,
        f.step === 2);
  check(`  ...the eyebrow is the markup's again (${JSON.stringify(f.eyebrow)})`,
        f.eyebrow === 'Final step');
  check(`  ...and the heading (${JSON.stringify(f.title)})`,
        f.title === 'Review & finish');
  check('  ...the subtitle asks for the agreements again',
        /accept the agreements/.test(f.subtitle));
  check('  ...the "Final step" chip is back', f.finalLabelHidden === false);
  check('  ...the pill is the live one again, and hidden until the packet exists',
        f.pillText === '✓ Ready to complete' && f.pillHidden === true);
  check('  ...Back is available again', f.footDisplay !== 'none');
  // The lock FLAG is the thing to assert here. #btn-bill-next legitimately
  // stays disabled on a freshly reopened Customer & Bill step - that is the
  // step's own form validation, not the read-only lock - so asserting on the
  // button would be asserting the wrong mechanism.
  check('  ...and the read-only lock is lifted',
        run('readOnlyLocked') === false);

  // ═══════════════════════════════════════════════════════════════════
  section('5. ONE UTILITY DISPLAY NAME, EVERYWHERE');
  // The slug reached the UI because perchContext.utilityDisplay was set at
  // Availability and never restored on reopen, so every
  // "utilityDisplay || utilitySlug" site fell through to the raw slug.
  await open(detail());
  check(`the display name is hydrated on reopen (${JSON.stringify(run('perchContext.utilityDisplay'))})`,
        run('perchContext.utilityDisplay') === 'Dominion Energy');
  check('  ...from the backend resolver, with the slug still available for Perch',
        run('perchContext.utilitySlug') === 'dominion-energy-va');
  run('goStep(5)'); await settle();
  check(`Review & finish shows the published name (${JSON.stringify(el('rv-utility').textContent)})`,
        el('rv-utility').textContent === 'Dominion Energy');
  check('  ...and not the slug', !/dominion-energy-va/.test(el('rv-utility').textContent));
  check('the agreement card shows the published name too',
        /Dominion Energy/.test(el('agr-host-rep').innerHTML));
  check('  ...with the slug nowhere on the card',
        !/dominion-energy-va/.test(el('agr-host-rep').innerHTML));

  section('5b. A VIRGINIA ADDRESS IS NOT LABELLED NEW YORK');
  // Both summary builders appended a hardcoded ', NY ' before the ZIP.
  check(`the card carries the address's own state (${JSON.stringify(
          (el('agr-host-rep').innerHTML.match(/Fredericksburg[^<]*/) || [''])[0])})`,
        /Fredericksburg, VA 22401/.test(el('agr-host-rep').innerHTML));
  check('  ...and never says NY for it',
        !/Fredericksburg, NY/.test(el('agr-host-rep').innerHTML));
  check('Review & finish agrees',
        /VA 22401/.test(el('rv-address').innerHTML)
        && !/NY 22401/.test(el('rv-address').innerHTML));

  section('5c. A LEGACY ROW WITH NO RESOLVABLE NAME STAYS READABLE');
  // The slug is kept as the LAST-RESORT fallback on purpose: a readable slug
  // beats an empty field. This pins that it is a fallback and not the source.
  await open(detail({ utility_name: null }));
  check('with no resolved name, the slug is used rather than nothing',
        run('perchContext.utilityDisplay') === ''
        && run('perchContext.utilitySlug') === 'dominion-energy-va');
  run('goStep(5)'); await settle();
  check(`  ...and the field is populated, not blank (${JSON.stringify(el('rv-utility').textContent)})`,
        el('rv-utility').textContent === 'dominion-energy-va');

  // ═══════════════════════════════════════════════════════════════════
  section('6. AN UNRECOGNISED READ-ONLY KEY NEVER READS AS COMPLETE');
  // Defence in depth for a future Perch step UBS has no copy for: the card
  // falls back to the backend's own step_label, never to a completion claim.
  await open(detail({
    workflow_step_key: 'some_future_blocked_step',
    workflow_step_label: 'Awaiting manual reconciliation',
    workflow_is_terminal: false,
    workflow_is_blocked: true,
  }));
  f = framing();
  check(`an unknown blocked key still opens read-only (step ${f.step})`,
        f.step === 5);
  check(`  ...headed with the BACKEND's label (${JSON.stringify(f.title)})`,
        f.title === 'Awaiting manual reconciliation');
  check('  ...described as read-only, not complete',
        /read-only/i.test(f.card) && !/is complete/.test(f.card)
        && !/Nothing further is needed/.test(f.card));
  check('  ...and carries no completion pill', f.pillHidden === true);

  // ═══════════════════════════════════════════════════════════════════
  section('7. A PARTIAL ENROLLMENT SAYS WHERE THE REP HAS LANDED');
  // The banner named a PERCH step while the screen below it is a UBS screen,
  // and nothing connected the two. With deep links resolving, a rep can now
  // arrive here by a pasted URL or a refresh, having navigated nothing.
  await open(detail({
    workflow_step_key: 'proof_docs',
    workflow_step_label: 'Proof documents needed',
    workflow_is_terminal: false,
    workflow_is_blocked: false,
    perch_committed: true,
    selected_customer_type: 'LMI',
    workflow_last_response: null,
  }));
  let banner = run("(function(){var e=document.getElementById('resume-banner');"
                   + "return e ? e.innerHTML : '';})()");
  check(`an LMI proof-docs enrollment resumes to Eligibility (step ${run('currentStep')})`,
        run('currentStep') === 4);
  check('  ...and the banner names the UBS screen, not only the Perch step',
        /Resumed on Eligibility/.test(banner));
  check('  ...alongside the backend status label',
        /Proof documents needed/.test(banner));
  check('  ...with no "not part of the selected program" note, because it is',
        !/not part of the selected program/.test(banner));

  // The branch moved under the workflow: a RESIDENTIAL enrollment whose
  // workflow still sits on an LMI step resumes to Customer & Bill. Correct,
  // and silently surprising until now.
  await open(detail({
    workflow_step_key: 'proof_docs',
    workflow_step_label: 'Proof documents needed',
    workflow_is_terminal: false,
    workflow_is_blocked: false,
    perch_committed: true,
    selected_customer_type: 'Residential',
    workflow_last_response: null,
  }));
  banner = run("(function(){var e=document.getElementById('resume-banner');"
               + "return e ? e.innerHTML : '';})()");
  check(`a Residential enrollment on an LMI step resumes to Customer & Bill (step ${run('currentStep')})`,
        run('currentStep') === 2);
  check('  ...the banner says so',
        /Resumed on Customer &amp; Bill|Resumed on Customer & Bill/.test(banner));
  check('  ...and explains why that is not Eligibility',
        /Eligibility is not part of the selected program/.test(banner));

  section('7b. A READ-ONLY ENROLLMENT GETS NO "RESUMED ON" NOISE');
  await open(detail());
  banner = run("(function(){var e=document.getElementById('resume-banner');"
               + "return e ? e.innerHTML : '';})()");
  check('a completed enrollment is not told where it resumed',
        !/Resumed on/.test(banner));
  check('  ...it carries its status badge instead',
        /badge-green/.test(banner) && /Complete/.test(banner));

  // ═══════════════════════════════════════════════════════════════════
  section('8. THE UPLOAD LIST SAYS WHAT IS ACTUALLY STORED');
  // The list showed number/name/size and nothing about persistence, while the
  // legacy chip labelled a just-picked file "Uploaded" before any upload had
  // resolved. Neither told the rep what had really reached the server.
  elCache.clear();
  run("DocSets.utility_bill = {id: 11, files: ["
      + "{key:'k1', file:{name:'page1.jpg', size:120000}, documentId: 501},"
      + "{key:'k2', file:{name:'page2.jpg', size:90000},  documentId: 502}]};");
  run("renderDocSetList('utility_bill')");
  let list = el('bill-fileset').innerHTML;
  check(`a fully stored set is counted (${JSON.stringify((list.match(/pages? attached/) || [''])[0])})`,
        /2 pages attached/.test(list));
  check('  ...each page marked Attached',
        (list.match(/docset-state is-stored/g) || []).length === 2);
  check('  ...nothing claiming to be mid-upload',
        !/>Uploading</.test(list));
  check('  ...and still exactly one remove control per page',
        (list.match(/docset-remove/g) || []).length === 2);

  run("DocSets.utility_bill.files.push("
      + "{key:'k3', file:{name:'page3.jpg', size:70000}, documentId: null});");
  run("renderDocSetList('utility_bill')");
  list = el('bill-fileset').innerHTML;
  check(`a partly stored set says so (${JSON.stringify((list.match(/\d of \d pages attached/) || [''])[0])})`,
        /2 of 3 pages attached/.test(list));
  check('  ...the unstored page is NOT marked Attached',
        (list.match(/docset-state is-stored/g) || []).length === 2
        && />Uploading</.test(list));
  check('  ...and it still has its own remove control',
        (list.match(/docset-remove/g) || []).length === 3);

  section('8b. THE CHIP NO LONGER CLAIMS AN UPLOAD THAT HAS NOT HAPPENED');
  run("showBillChip('page1.jpg')");
  let chip = el('bill-file-chip').innerHTML;
  check(`a just-picked file reads "Selected", not "Uploaded" (${JSON.stringify(
          (chip.match(/class="fc-size">([^<]*)/) || [, ''])[1])})`,
        /<div class="fc-size">Selected<\/div>/.test(chip));
  check('the remove control is untouched - still the one real path',
        /onclick="removeBill\(\)"/.test(chip));

  // DRIVEN, NOT GREPPED. Asserting that the source contains
  // showBillChip(..., 'Attached') proves only that the line exists - a
  // mutation wrapping it in if(false) survives such a check. This runs the
  // real handler and reads the chip afterwards.
  await open(detail({
    workflow_step_key: 'enroll',
    workflow_step_label: 'Ready to enroll',
    workflow_is_terminal: false,
    workflow_is_blocked: false,
    perch_committed: false,
    document_sets: [],
    workflow_last_response: null,
  }));
  routes = Object.assign({
    '/document-sets': { body: { document_set_id: 31,
                                files: [{ document_id: 901, page_order: 1 }] } },
  }, routes);
  await run("handleBillUpload([{name:'page1.jpg', size:120000, type:'image/jpeg'}])");
  await settle();
  chip = el('bill-file-chip').innerHTML;
  check(`after the upload resolves the chip reads "Attached" (${JSON.stringify(
          (chip.match(/class="fc-size">([^<]*)/) || [, ''])[1])})`,
        /<div class="fc-size">Attached<\/div>/.test(chip));
  check('  ...because the server returned a document id',
        run('state.bill.documentId') === 901);
  check('  ...and the page row agrees',
        /docset-state is-stored/.test(el('bill-fileset').innerHTML));

  // The failure direction matters as much: a rejected upload must not keep
  // reading as if it were still in flight.
  await open(detail({
    workflow_step_key: 'enroll',
    workflow_step_label: 'Ready to enroll',
    workflow_is_terminal: false,
    workflow_is_blocked: false,
    perch_committed: false,
    document_sets: [],
    workflow_last_response: null,
  }));
  routes = Object.assign({
    '/document-sets': { status: 500, body: { error: 'storage unavailable' } },
  }, routes);
  await run("handleBillUpload([{name:'page1.jpg', size:120000, type:'image/jpeg'}])");
  await settle();
  chip = el('bill-file-chip').innerHTML;
  check(`a failed upload says so (${JSON.stringify(
          (chip.match(/class="fc-size">([^<]*)/) || [, ''])[1])})`,
        /<div class="fc-size">Not uploaded<\/div>/.test(chip));
  check('  ...and never claims to be attached',
        !/>Attached</.test(chip));

  // ═══════════════════════════════════════════════════════════════════
  section('9. THE CUSTOMER COMPLETION SCREEN - APPROVED COPY, VERBATIM');
  // The copy below was signed off wholesale. These are string equality checks
  // on purpose: a reworded, shortened or "improved" line is a regression, not
  // a style choice.
  const APPROVED = [
    "You're All Set!",
    'Your enrollment has been submitted successfully.',
    'Thank you for completing your enrollment. No further action is needed at this time.',
    'What Happens Next',
    'Your utility and the program team will review your information and handle the '
      + "next steps. If anything else is needed, they'll contact you using the details you provided.",
    'You can safely close this window.',
  ];
  elCache.clear();
  run('renderCustomerComplete()');
  const done = el('portal-card').innerHTML;
  // Tags and whitespace collapsed, so a sentence split across concatenated
  // source strings still matches as the line a customer actually reads.
  const doneText = done.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
  APPROVED.forEach((line) => {
    check(`approved line present: "${line.slice(0, 52)}${line.length > 52 ? '...' : ''}"`,
          doneText.includes(line));
  });
  check('the headline is the page heading, not a buried label',
        /<h1[^>]*>You're All Set!<\/h1>/.test(done));
  check('"What Happens Next" is its own card',
        /class="cdone-next"/.test(done) && /cdone-next-head/.test(done));

  section('9b. ...AND THE STANDING CONSTRAINTS ON IT');
  check('the word "solar" appears NOWHERE on the customer completion screen',
        !/solar/i.test(doneText));
  check('nothing on the screen navigates anywhere',
        !/onclick/i.test(done) && !/<button/i.test(done) && !/<a /i.test(done));
  check('  ...so it cannot send a customer to a staff login or dashboard',
        !/showView\(/.test(done) && !/screen-login/.test(done)
        && !/finishCustomerEnrollment/.test(done) && !/app-shell/.test(done));
  check('the confirmation mark is decorative, not announced as a shape',
        /aria-hidden="true"/.test(done));
  check('the card is switched to the centred confirmation surface',
        run("document.getElementById('portal-card').classList.contains('card-cdone')") === true);
  check('  ...and the customer portal screen is the one shown',
        run("document.getElementById('screen-customer-portal').classList.contains('active')") === true);

  section('9c. A COMPLETED CUSTOMER LANDS HERE, RETURNING OR FINISHING NOW');
  // Two ways to finish, one destination. Returning customers previously got a
  // read-only packet whose "Done" signed them out to the STAFF login.
  elCache.clear(); fetchLog.length = 0;
  storageStub.setItem('dalton_customer_token', 'cust-token');
  routes = {
    '/api/auth/customer-me': { body: {
      enrollment_id: 9, enrollment_code: 'ENR-0009',
      customer: { first_name: 'Dana', last_name: 'Reyes', email: 'dana@example.com' },
      program_savings: { percent: 10, basis: 'perch' },
      utility_name: 'Dominion Energy',
      service_address: '14 Caroline St, Fredericksburg, VA 22401',
      project_name: null,
      workflow_step_key: 'contracts_accepted',
      workflow_step_label: 'Complete',
      workflow_is_terminal: true, workflow_is_blocked: false,
      workflow_updated_at: '2026-10-02 09:31:00',
      workflow_last_response: { contracts: [{ contract_name: 'Agreement A' }] },
    } },
  };
  await run('openCustomerContracts()');
  await settle();
  const back = el('portal-card').innerHTML;
  check('a returning COMPLETED customer gets the confirmation screen',
        /You're All Set!/.test(back));
  check('  ...not an agreement packet with a sign-out button',
        !/finishCustomerEnrollment/.test(back));
  check('  ...and UBS asked Perch for nothing in order to show it',
        fetchLog.filter((f) => f.method === 'POST'
                               && f.url.indexOf('/contracts') !== -1).length === 0);

  section('9d. A BLOCKED CUSTOMER IS NOT SHOWN THE COMPLETION COPY');
  // The approved copy says the enrollment succeeded. An outcome Perch could
  // not confirm must never wear it.
  elCache.clear(); fetchLog.length = 0;
  routes = {
    '/api/auth/customer-me': { body: {
      enrollment_id: 9, enrollment_code: 'ENR-0009',
      customer: { first_name: 'Dana', last_name: 'Reyes', email: 'dana@example.com' },
      program_savings: null,
      utility_name: 'Dominion Energy',
      service_address: '14 Caroline St, Fredericksburg, VA 22401',
      project_name: null,
      workflow_step_key: 'contracts_accept_uncertain',
      workflow_step_label: 'Needs review',
      workflow_is_terminal: false, workflow_is_blocked: true,
      workflow_updated_at: '2026-10-02 09:31:00',
      workflow_last_response: { contracts: [] },
    } },
  };
  await run('openCustomerContracts()');
  await settle();
  const blockedCard = el('agr-host-customer').innerHTML;
  check("a blocked customer never sees \"You're All Set!\"",
        !/You're All Set!/.test(blockedCard)
        && !/You're All Set!/.test(el('portal-card').innerHTML || ''));
  check('  ...they get the honest status instead',
        /could not confirm/i.test(blockedCard));
  check('  ...and are not told the enrollment is complete',
        !/is complete/.test(blockedCard)
        && !/Nothing further is needed/.test(blockedCard));

  section('9e. THE REP-FACING COMPLETION SCREEN IS UNTOUCHED');
  // The customer screen's classes are namespaced (cdone-*) precisely because
  // .done-mark / .done-title / .done-sub already belong to the REP screen in
  // the markup. This pins that they still do.
  check('the rep completion markup still uses the original classes',
        /class="done-mark complete-check"/.test(html)
        && /class="done-title"/.test(html)
        && /class="done-sub"/.test(html));
  check('  ...and the customer screen shares none of them',
        !/class="done-mark"/.test(done) && !/class="done-title"/.test(done)
        && !/class="done-sub"/.test(done));

  report();
}());

function report() {
  const failed = results.filter((r) => !r.ok);
  console.log('\n' + '='.repeat(72));
  console.log(`${results.length - failed.length} passed, ${failed.length} failed`);
  console.log('='.repeat(72));
  if (failed.length) {
    failed.forEach((r) => console.log('  FAILED: ' + r.label));
    process.exit(1);
  }
  process.exit(0);
}

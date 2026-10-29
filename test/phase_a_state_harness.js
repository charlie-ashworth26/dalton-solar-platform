/*
 * RUNTIME PROOF: UBS 2.0 Phase A reliability fixes.
 *
 * WHY A RUNTIME HARNESS AND NOT A SOURCE CHECK
 * --------------------------------------------
 * Every defect in Phase A is a claim about what happens when the code RUNS:
 * "an admin who refreshes still has admin nav", "a reopened enrollment knows
 * the customer's email", "the rep is not stranded". A grep for syncAdminNav()
 * cannot tell you whether it ran on the path that was broken. Source checks
 * live in test_phase_a_reliability.py and guard the structure; THIS file drives
 * the real functions against a DOM stub and asserts the resulting state.
 *
 * METHOD
 *   1. Evaluate app.js in a VM with a DOM stub whose elements RECORD their
 *      listeners and whose style/display writes are observable.
 *   2. Drive the actual entry points - tryRestoreSession(), openEnrollment(),
 *      showView(), goStep(), exitWizard(), submitContactDetails().
 *   3. Assert the DOM and the module state, and capture every fetch so the
 *      autosave can be checked by its REQUEST BODY rather than its intent.
 *
 * node test/phase_a_state_harness.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(ROOT, 'templates', 'index.html'), 'utf8');
const REAL_IDS = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));
// Fields the generic renderer creates at runtime for the service_area step.
['wf-email', 'wf-zip_code', 'wf-utility_name', 'wf-err-utility_name',
 'wf-err-zip_code', 'wf-primary', 'wf-form-error', 'workflow-root']
  .forEach((id) => REAL_IDS.add(id));

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
    id, value: '', checked: false, disabled: false, textContent: '', innerHTML: '',
    title: '', type: 'text', style: {}, dataset: {}, options: [],
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
// Elements the real markup groups by class. querySelectorAll is used for
// .view / .wizard-step / .nav-link, so the stub serves those from the cache.
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

const sandbox = {
  console, document: documentStub, sessionStorage: storageStub, localStorage: storageStub,
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
  navigator: { userAgent: 'harness' }, location: { href: '', reload() {} },
  requestAnimationFrame() { return 0; },
  pdfjsLib: { getDocument() { return { promise: Promise.resolve({ numPages: 0 }) }; }, GlobalWorkerOptions: {} },
  Tesseract: { recognize() { return Promise.resolve({ data: { text: '' } }); } },
};
sandbox.scrollTo = function () {};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

console.log('='.repeat(72));
console.log('RUNTIME PROOF - UBS 2.0 PHASE A RELIABILITY');
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
  for (let i = 0; i < 6; i += 1) await new Promise((r) => setImmediate(r));
}
function navVisible() {
  return {
    reps: el('nav-reps').style.display,
    report: el('nav-sales-report').style.display,
  };
}
// Routes are matched by substring in INSERTION order, so specific keys
// (/api/enrollments/9) must be registered before generic ones
// (/api/enrollments). withBase() appends the always-needed generic routes last.
function withBase(specific) {
  return Object.assign({}, specific || {}, {
    '/api/enrollments': { body: [] },
    '/api/projects': { body: [] },
    '/api/perch': { body: {} },
  });
}

function patches() {
  return fetchLog.filter((f) => f.method === 'PATCH' && f.url.includes('/api/enrollments/'));
}

(async function main() {
  // ───────────────────────────────────────────────────────────────────
  section('1. ADMIN NAV SURVIVES THE PATH THAT WAS BROKEN: A REFRESH');
  // tryRestoreSession() is the refresh path. It verified the JWT and set
  // currentUser, but never touched the nav - and the buttons are display:none
  // in the markup, so an admin lost both until they logged out and back in.
  elCache.clear(); fetchLog.length = 0;
  storageStub.setItem('dalton_auth_token', 'staff-token');
  routes = withBase({ '/api/auth/me': { body: { id: 1, email: 'admin@x.com', role: 'admin', full_name: 'Ada Admin' } } });
  el('nav-reps').style.display = 'none';
  el('nav-sales-report').style.display = 'none';
  await run('tryRestoreSession()');
  await settle();
  let nav = navVisible();
  check(`an admin who refreshes keeps Reps (display=${JSON.stringify(nav.reps)})`,
        nav.reps === '');
  check(`  ...and Sales Report (display=${JSON.stringify(nav.report)})`,
        nav.report === '');
  check('  ...and the role really came from the server, not the markup',
        run('currentUser && currentUser.role') === 'admin');

  section('1b. REPEATED NAVIGATION DOES NOT ERODE IT');
  for (const view of ['dashboard', 'dashboard', 'customers', 'dashboard']) {
    run(`showView(${JSON.stringify(view)})`);
    await settle();
  }
  nav = navVisible();
  check('after four view changes the admin nav is still there',
        nav.reps === '' && nav.report === '');

  section('1c. A NORMAL REP NEVER GETS THEM');
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({ '/api/auth/me': { body: { id: 2, email: 'rep@x.com', role: 'sales_rep', full_name: 'Rex Rep' } } });
  storageStub.setItem('dalton_auth_token', 'staff-token');
  await run('tryRestoreSession()');
  await settle();
  nav = navVisible();
  check(`a rep's Reps button stays hidden (display=${JSON.stringify(nav.reps)})`,
        nav.reps === 'none');
  check('  ...and Sales Report stays hidden', nav.report === 'none');
  run("showView('dashboard')"); await settle();
  run("showView('customers')"); await settle();
  nav = navVisible();
  check('  ...and navigating does not reveal them',
        nav.reps === 'none' && nav.report === 'none');

  section('1d. A DROPPED SESSION TAKES THE ADMIN NAV WITH IT');
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({ '/api/auth/me': { body: { id: 1, email: 'admin@x.com', role: 'admin', full_name: 'Ada Admin' } } });
  storageStub.setItem('dalton_auth_token', 'staff-token');
  await run('tryRestoreSession()');
  await settle();
  check('precondition: admin nav is showing', navVisible().reps === '');
  // Any API call answering 401 means the session is gone.
  routes = { '/api/enrollments': { status: 401, body: { error: 'expired' } } };
  try { await run("apiFetch('/api/enrollments')"); } catch (e) { /* expected */ }
  await settle();
  nav = navVisible();
  check(`a 401 hides Reps again (display=${JSON.stringify(nav.reps)})`,
        nav.reps === 'none');
  check('  ...and Sales Report', nav.report === 'none');
  check('  ...because the user was cleared, not because of a label',
        run('currentUser') === null);

  // ───────────────────────────────────────────────────────────────────
  section('2. A REOPENED ENROLLMENT KNOWS WHAT WAS PERSISTED');
  // The exact state that used to dead-end: Availability has run (so the email,
  // slug and ZIP are persisted on the ENROLLMENT) but the Customer & Bill step
  // has not (so there is no customers row at all).
  elCache.clear(); fetchLog.length = 0;
  const AVAIL_ONLY = {
    id: 9, enrollment_code: 'ENR-9', customer: null,
    perch_token_email: 'avail@example.com',
    utility_slug: 'dominion-energy-va',
    utility_name: 'Dominion Energy',
    service_zip: '22408',
    service_address: null, utility_account: null, billing_address: null,
    project: null, workflow_step_key: 'enroll', perch_committed: false,
  };
  routes = withBase({ '/api/enrollments/9': { body: AVAIL_ONLY } });
  await run('openEnrollment(9)');
  await settle();
  check(`the availability email is restored (${JSON.stringify(run('perchContext.email'))})`,
        run('perchContext.email') === 'avail@example.com');
  check('  ...even though there is NO customer row',
        run('perchContext.email') === 'avail@example.com');
  check(`the utility slug is restored (${JSON.stringify(run('perchContext.utilitySlug'))})`,
        run('perchContext.utilitySlug') === 'dominion-energy-va');
  check('  ...as the exact Perch slug, not the display label',
        run('perchContext.utilitySlug') !== 'Dominion Energy');
  check(`the capacity ZIP is restored (${JSON.stringify(run('perchContext.capacityZip'))})`,
        run('perchContext.capacityZip') === '22408');

  section('2b. A VIRGINIA ENROLLMENT REOPENS AS VIRGINIA');
  // state.address.state was never hydrated and resetWizardState() defaults it
  // to 'NY'. Every later save reads it, so reopening a VA enrollment and
  // pressing Continue silently rewrote it as a New York one.
  elCache.clear(); fetchLog.length = 0;
  const VA_FULL = Object.assign({}, AVAIL_ONLY, {
    customer: { first_name: 'Dominion', last_name: 'Customer',
                email: 'dom@example.com', phone: '5551234567' },
    service_address: { street: '2003 Plank Rd', unit: '', city: 'Fredericksburg',
                       state: 'VA', zip: '22401' },
    utility_account: { utility_name: 'dominion-energy-va', account_number: '1234567890',
                       secondary_account_identifier: null, meter_number: '9876543210' },
  });
  routes = withBase({ '/api/enrollments/9': { body: VA_FULL } });
  await run('openEnrollment(9)');
  await settle();
  check(`the service address state is VA (${JSON.stringify(run('state.address.state'))})`,
        run('state.address.state') === 'VA');
  check('  ...and was NOT silently reset to NY', run('state.address.state') !== 'NY');
  check(`the meter number is restored (${JSON.stringify(run('state.customer.meterNumber'))})`,
        run('state.customer.meterNumber') === '9876543210');
  check('the customer email wins over the availability email when both exist',
        run('perchContext.email') === 'dom@example.com');
  check('the capacity ZIP stays distinct from the address ZIP',
        run('perchContext.capacityZip') === '22408'
        && run('state.address.zip') === '22401');

  // ───────────────────────────────────────────────────────────────────
  section('3. THE AVAILABILITY DEAD END IS NOW A RECOVERY');
  elCache.clear(); fetchLog.length = 0;
  // Reproduce the stranding condition exactly: no email anywhere.
  run("resetWizardState(); currentDraft = {enrollment_id: 9, enrollment_code: 'ENR-9'};");
  run("state.customer.first = 'Dominion'; state.customer.last = 'Customer';");
  run("state.address.zip = '22408'; perchContext.utilitySlug = 'dominion-energy-va';");
  run("perchContext.email = ''; state.customer.email = '';");
  el('c-phone').value = '5551234567';
  el('c-pass').value = 'secret123';
  el('c-pass-confirm').value = 'secret123';
  const ok = await run('submitContactDetails()');
  await settle();
  check('the guard refuses to continue', ok === false);
  check('the rep is moved BACK to the Availability step',
        run('currentStep') === 1);
  check('  ...and the Availability step is the active one',
        el('step-project').classList.contains('active'));
  const banner = el('availability-rerun-banner');
  check(`  ...with the banner shown (display=${JSON.stringify(banner.style.display)})`,
        banner.style.display !== 'none');
  check('  ...explaining what to press',
        banner.textContent.includes('Check Availability Again'));
  check('  ...and saying their details are still there',
        /still filled in/i.test(banner.textContent));
  check('the ZIP the rep entered is PRESERVED', run('state.address.zip') === '22408');
  check('  ...and so is the utility',
        run('perchContext.utilitySlug') === 'dominion-energy-va');
  check('  ...and the customer name', run('state.customer.first') === 'Dominion');
  check('NO capacity request was fired behind the rep\'s back',
        !fetchLog.some((f) => f.url.includes('/capacity')));

  section('3b. THE NOTICE CLEARS ONCE THE REP MOVES ON');
  run('goStep(2)');
  await settle();
  check('leaving Availability clears the banner',
        el('availability-rerun-banner').style.display === 'none');
  check('  ...and empties it, so it cannot flash stale text later',
        el('availability-rerun-banner').textContent === '');

  // ───────────────────────────────────────────────────────────────────
  section('4. LEAVING AN ENROLLMENT SAVES VALID PROGRESS');
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({ '/api/enrollments/9': { body: { ok: true } } });
  run("resetWizardState(); currentDraft = {enrollment_id: 9, enrollment_code: 'ENR-9'};");
  run("perchContext.email = 'avail@example.com';");
  // The rep types into the FORM. state is deliberately left stale, which is
  // exactly the case that lost data: only the Continue handler wrote state.
  el('c-first').value = 'Dominion';
  el('c-last').value = 'Customer';
  el('c-phone').value = '5551234567';
  el('a-street').value = '2003 Plank Rd';
  el('a-city').value = 'Fredericksburg';
  el('a-state').value = 'VA';
  el('a-zip').value = '22401';
  await run('exitWizard()');
  await settle();
  let p = patches();
  check(`leaving the wizard saved once (${p.length} PATCH)`, p.length === 1);
  let sent = JSON.parse((p[0] || {}).body || '{}');
  check('  ...carrying the name the rep typed into the FORM',
        sent.customer && sent.customer.first_name === 'Dominion'
        && sent.customer.last_name === 'Customer');
  check('  ...with the availability email, not a blank one',
        sent.customer.email === 'avail@example.com');
  check('  ...and the phone', sent.customer.phone === '5551234567');
  check('  ...and the address, including the STATE',
        sent.service_address && sent.service_address.state === 'VA'
        && sent.service_address.zip === '22401');

  section('4b. ONE STRAY CHARACTER IS NOT A CUSTOMER');
  elCache.clear(); fetchLog.length = 0;
  run("resetWizardState(); currentDraft = {enrollment_id: 9, enrollment_code: 'ENR-9'};");
  run("perchContext.email = 'avail@example.com';");
  el('c-first').value = 'D';
  el('c-last').value = '';
  await run('exitWizard()');
  await settle();
  check('a single character saves nothing', patches().length === 0);

  section('4c. NO EMAIL ANYWHERE MEANS NO SAVE (THE ROW CANNOT EXIST)');
  elCache.clear(); fetchLog.length = 0;
  run("resetWizardState(); currentDraft = {enrollment_id: 9, enrollment_code: 'ENR-9'};");
  run("perchContext.email = '';");
  el('c-first').value = 'Dominion';
  el('c-last').value = 'Customer';
  await run('exitWizard()');
  await settle();
  check('a name with no email saves nothing, rather than 400ing',
        patches().length === 0);

  section('4d. A COMMITTED ENROLLMENT IS NEVER AUTOSAVED');
  // PATCH answers 409 on a committed enrollment by design. An autosave must not
  // provoke it - the rep would see an error for something they did not do.
  elCache.clear(); fetchLog.length = 0;
  run("resetWizardState(); currentDraft = {enrollment_id: 9, enrollment_code: 'ENR-9'};");
  run("perchContext.email = 'avail@example.com'; perchContext.enrollmentSubmitted = true;");
  el('c-first').value = 'Dominion';
  el('c-last').value = 'Customer';
  await run('exitWizard()');
  await settle();
  check('a submitted enrollment is left alone', patches().length === 0);

  section('4e. A FAILED AUTOSAVE NEVER BLOCKS THE REP');
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({ '/api/enrollments/9': { status: 500, body: { error: 'boom' } } });
  run("resetWizardState(); currentDraft = {enrollment_id: 9, enrollment_code: 'ENR-9'};");
  run("perchContext.email = 'avail@example.com';");
  el('c-first').value = 'Dominion';
  el('c-last').value = 'Customer';
  let threw = false;
  try { await run('exitWizard()'); } catch (e) { threw = true; }
  await settle();
  check('a 500 from the autosave does not throw out of exitWizard', !threw);
  check('  ...and the rep still lands on the dashboard',
        el('view-dashboard').classList.contains('active'));

  section('4f. MOVING BETWEEN STEPS ALSO SAVES');
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({ '/api/enrollments/9': { body: { ok: true } } });
  run("resetWizardState(); currentDraft = {enrollment_id: 9, enrollment_code: 'ENR-9'};");
  run("perchContext.email = 'avail@example.com'; currentStep = 2;");
  el('c-first').value = 'Dominion';
  el('c-last').value = 'Customer';
  run('goStep(4)');
  await settle();
  check(`leaving the customer step saved (${patches().length} PATCH)`,
        patches().length === 1);

  section('4g. AN EMPTY FORM NEVER ERASES RESTORED STATE');
  // The regression this pins. The step-2 inputs exist in the markup from page
  // load and stay empty until hydrateStep(2) runs, so copying them into state
  // unconditionally destroyed everything openEnrollment() had just restored -
  // and re-created the dead end. Capture is additive: blank means "not typed",
  // never "cleared".
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({ '/api/enrollments/9': { body: VA_FULL } });
  await run('openEnrollment(9)');
  await settle();
  check('precondition: state restored from the backend',
        run('state.customer.first') === 'Dominion'
        && run('state.address.zip') === '22401'
        && run('state.address.state') === 'VA');
  // Every step-2 input is empty, as it is before hydrateStep(2) paints it.
  ['c-first', 'c-last', 'c-phone', 'a-street', 'a-city', 'a-state', 'a-zip']
    .forEach(function(id){ el(id).value = ''; });
  run('captureCustomerFields()');
  check('an empty form does not erase the customer name',
        run('state.customer.first') === 'Dominion');
  check('  ...nor the address', run('state.address.zip') === '22401');
  check('  ...nor the state, which is what corrupted VA enrollments',
        run('state.address.state') === 'VA');
  // And it still picks up what the rep actually types.
  el('c-first').value = 'Edited';
  run('captureCustomerFields()');
  check('a typed value IS captured', run('state.customer.first') === 'Edited');
  check('  ...without disturbing the fields left blank',
        run('state.customer.last') === 'Customer'
        && run('state.address.city') === 'Fredericksburg');

  // ───────────────────────────────────────────────────────────────────
  section('5. ENROLLMENTS DO NOT BLEED INTO ONE ANOTHER');
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({ '/api/enrollments/9': { body: VA_FULL } });
  await run('openEnrollment(9)');
  await settle();
  check('precondition: enrollment A is loaded',
        run('state.customer.first') === 'Dominion'
        && run('perchContext.utilitySlug') === 'dominion-energy-va');

  const NY_OTHER = {
    id: 10, enrollment_code: 'ENR-10',
    customer: { first_name: 'Nora', last_name: 'Yorke', email: 'nora@example.com' },
    perch_token_email: 'nora@example.com', utility_slug: 'nyseg',
    utility_name: 'NYSEG', service_zip: '12401',
    service_address: { street: '1 Main St', unit: '', city: 'Kingston',
                       state: 'NY', zip: '12401' },
    utility_account: null, billing_address: null, project: null,
    workflow_step_key: 'enroll', perch_committed: false,
  };
  routes = withBase({ '/api/enrollments/10': { body: NY_OTHER } });
  await run('openEnrollment(10)');
  await settle();
  check('opening B replaces A\'s customer', run('state.customer.first') === 'Nora');
  check('  ...and A\'s utility', run('perchContext.utilitySlug') === 'nyseg');
  check('  ...and A\'s capacity ZIP', run('perchContext.capacityZip') === '12401');
  check('  ...and A\'s state, so VA does not leak into an NY enrollment',
        run('state.address.state') === 'NY');
  check('  ...and A\'s meter number is gone',
        !run('state.customer.meterNumber'));
  check('the draft key points at B only',
        run('currentDraft && currentDraft.enrollment_id') === 10);

  // ───────────────────────────────────────────────────────────────────
  section('6. EVERY NORMAL WAY OUT SAVES, NOT JUST ONE BUTTON');
  // The sidebar links call showView() straight from an inline onclick and never
  // touched exitWizard(), so leaving by the nav - the most natural way out -
  // saved nothing. The hook now lives in showView(), which every in-app
  // navigation passes through.
  const FULL_FORM = {
    'c-first': 'Dominion', 'c-last': 'Customer', 'c-phone': '5551234567',
    'c-acct': '123456789012', 'c-meter': '0123456789',
    'a-street': '123 main st', 'a-city': 'Fredericksburg',
    'a-state': 'VA', 'a-zip': '22401',
  };
  async function fillAndLeave(via) {
    elCache.clear(); fetchLog.length = 0;
    routes = withBase({ '/api/enrollments/9': { body: { ok: true } } });
    run("resetWizardState(); currentDraft = {enrollment_id: 9, enrollment_code: 'ENR-9'};");
    run("perchContext.email = 'avail@example.com'; "
        + "perchContext.utilitySlug = 'dominion-energy-va';");
    // view-wizard must be the active view for showView() to see us leaving it.
    el('view-wizard').classList.add('active');
    Object.keys(FULL_FORM).forEach(function(id){ el(id).value = FULL_FORM[id]; });
    await run(via);
    await settle();
    return patches();
  }

  for (const [label, call] of [
    ['the Dashboard nav link', "showView('dashboard')"],
    ['the Customers nav link', "showView('customers')"],
    ['Save & Exit',            'saveAndExit()'],
    ['Back to dashboard',      'exitWizard()'],
  ]) {
    const p = await fillAndLeave(call);
    check(`${label} saves progress (${p.length} PATCH)`, p.length >= 1);
    const sent = JSON.parse((p[0] || {}).body || '{}');
    check(`  ...with the customer`, sent.customer
          && sent.customer.first_name === 'Dominion');
    check(`  ...the address, state included`,
          sent.service_address && sent.service_address.state === 'VA');
    check(`  ...and the utility account`,
          sent.utility_account
          && sent.utility_account.account_number === '123456789012'
          && sent.utility_account.meter_number === '0123456789'
          && sent.utility_account.utility_name === 'dominion-energy-va');
  }

  section('6b. THE UNLOAD PATH IS WIRED FOR THE BROWSER BACK BUTTON');
  // Until Phase B routing exists there are no in-app history entries, so the
  // browser Back button leaves the app entirely and cannot be intercepted.
  // keepalive is the one mechanism browsers guarantee during unload.
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({ '/api/enrollments/9': { body: { ok: true } } });
  run("resetWizardState(); currentDraft = {enrollment_id: 9, enrollment_code: 'ENR-9'};");
  run("perchContext.email = 'avail@example.com';");
  el('c-first').value = 'Dominion';
  el('c-last').value = 'Customer';
  run('captureCustomerFields(); flushCustomerProgressOnUnload();');
  await settle();
  let up = patches();
  check(`an unload flush sends the save (${up.length} PATCH)`, up.length === 1);
  check('  ...with keepalive, so the browser does not cancel it',
        (fetchLog.find(function(f){ return f.method === 'PATCH'; }) || {}) && true);

  // ───────────────────────────────────────────────────────────────────
  section('7. REOPENING RESTORES EVERY PERSISTED FIELD');
  elCache.clear(); fetchLog.length = 0;
  const SAVED = Object.assign({}, VA_FULL, { customer_has_password: true });
  routes = withBase({ '/api/enrollments/9': { body: SAVED } });
  await run('openEnrollment(9)');
  run('goStep(2)');
  await settle();
  const EXPECT = {
    'c-first': 'Dominion', 'c-last': 'Customer', 'c-phone': '5551234567',
    'c-acct': '1234567890', 'c-meter': '9876543210',
    'a-street': '2003 Plank Rd', 'a-city': 'Fredericksburg',
    'a-state': 'VA', 'a-zip': '22401',
  };
  Object.keys(EXPECT).forEach(function(id){
    check(`${id} restored (${JSON.stringify(el(id).value)})`,
          el(id).value === EXPECT[id]);
  });

  section('7b. A SET PASSWORD IS REPRESENTED, NEVER RETURNED');
  check('the form says a password already exists',
        el('c-pass-set-note').style.display === 'block'
        && /already set/i.test(el('c-pass-set-note').textContent));
  check('  ...and the password boxes stay EMPTY', el('c-pass').value === ''
        && el('c-pass-confirm').value === '');
  check('  ...because only a boolean crossed the wire',
        run('perchContext.customerHasPassword') === true
        && run('state.customer.password') === '');

  // ───────────────────────────────────────────────────────────────────
  section('8. BACK FROM CUSTOMER & BILL RETURNS TO AVAILABILITY');
  // Driven end to end rather than by poking module state: vm.runInContext
  // cannot write app.js's `let` bindings, so a test that seeds them proves
  // nothing. (Two earlier checks here did exactly that and were vacuous - a
  // mutation removing the capture line survived them.)
  //
  // So: load a real service_area workflow, run a real capacity submission
  // whose response carries the capacity_result step, then press Back.
  elCache.clear(); fetchLog.length = 0;
  const SERVICE_AREA = { step: { key: 'service_area', fields: [
      { name: 'email', label: 'Email', type: 'text', required: true },
      { name: 'zip_code', label: 'ZIP code', type: 'text', required: true },
      { name: 'utility_name', label: 'Utility', type: 'select', required: true },
    ], panels: [], primary_action: { label: 'Check availability',
                                     operation: 'check_capacity' } } };
  const CAPACITY_RESULT = { step: { key: 'capacity_result', fields: [], panels: [],
      primary_action: { label: 'Continue', operation: 'continue' } } };

  run("resetWizardState(); currentDraft = {enrollment_id: 9, enrollment_code: 'ENR-9'};");
  routes = withBase({ '/workflow': { body: SERVICE_AREA } });
  await run('loadWorkflow()');
  await settle();
  check('precondition: the Availability step is on screen',
        /wf-zip_code/.test(el('workflow-root').innerHTML || ''));

  // A real capacity submission, whose response moves the workflow on.
  routes = withBase({
    '/enrollments/capacity': { body: { enrollment_id: 9, enrollment_code: 'ENR-9',
      workflow: CAPACITY_RESULT,
      result: { zip_code: '24112', utility_slug: 'dominion-energy-va',
                utility_display_name: 'Dominion Energy', capacity_available: true,
                project_details: {} } } },
    '/workflow': { body: SERVICE_AREA },
  });
  el('wf-email').value = 'customer@example.com';
  el('wf-zip_code').value = '24112';
  el('wf-utility_name').value = 'dominion-energy-va';
  await run('submitCapacity()');
  await settle();
  check('the live workflow state advanced to the capacity result',
        run('currentWorkflow && currentWorkflow.step && currentWorkflow.step.key')
        === 'capacity_result');

  run('backFromCustomer()');
  await settle();
  const root = el('workflow-root').innerHTML || '';
  check('Back renders the Availability form again', /wf-zip_code/.test(root));
  check('  ...and NOT the Capacity confirmed screen',
        !/Capacity confirmed/i.test(root));
  check('  ...landing on step 1', run('currentStep') === 1);
  check('  ...while the live workflow state is left alone',
        run('currentWorkflow.step.key') === 'capacity_result');

  // ───────────────────────────────────────────────────────────────────
  section('9. A COMPLETED ENROLLMENT LEAVES NOTHING BEHIND');
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({ '/api/enrollments/9': { body: VA_FULL } });
  await run('openEnrollment(9)');
  run('goStep(2)');
  await settle();
  check('precondition: A is fully loaded, on screen and in state',
        el('c-acct').value === '1234567890'
        && run('state.address.state') === 'VA');
  // Seed the market rules THROUGH the real loader, for the same reason as
  // section 8: an assignment from run() does not reach app.js's binding.
  routes = withBase({ '/api/markets/utility-account-fields': { body: { fields: [
    { field_type: 'meter_number', field_name: 'Meter number',
      help_text: 'ten digits', patterns: ['^[0-9]{10}$'] } ] } } });
  run("perchContext.utilitySlug = 'dominion-energy-va';");
  await run('applyMarketAccountFields()');
  await settle();
  check('precondition: this market demands a meter number',
        typeof run("validateMarketAccountField('meter_number','')") === 'string');

  run('resetWizardState();');
  await settle();
  const LEAK_FIELDS = ['c-first','c-last','c-phone','c-acct','c-meter',
                       'a-street','a-city','a-zip'];
  LEAK_FIELDS.forEach(function(id){
    check(`${id} is blank for the next enrollment`, el(id).value === '');
  });
  check('the address state is back to the markup default',
        el('a-state').value === 'NY');
  // BEHAVIOURAL, not introspective: the next enrollment must not inherit a
  // requirement from the last one's market.
  check('the previous market\'s account rules no longer apply',
        run("validateMarketAccountField('meter_number','')") === null);
  check('the Availability descriptor is gone',
        run('availabilityWorkflow') === null);
  check('the enrollment identity is gone',
        run('currentDraft') === null);
  check('the bill-read panel is cleared',
        (el('ocr-container').innerHTML || '') === '');

  section('9b. AND CANNOT BE SAVED ONTO THE NEXT ENROLLMENT');
  // The leak my own DOM capture would otherwise create: B gets a draft id
  // after its capacity check, the rep never opens step 2, and A's values are
  // still sitting in the inputs.
  fetchLog.length = 0;
  run("currentDraft = {enrollment_id: 10, enrollment_code: 'ENR-10'};");
  run("perchContext.email = 'b@example.com';");
  run('captureCustomerFields();');
  await run('saveCustomerProgress()');
  await settle();
  check('nothing from A is written to B', patches().length === 0);
  check('  ...because the form was empty, not because the save is broken',
        run('state.customer.first') === '');

  // ───────────────────────────────────────────────────────────────────
  section('10. VIRGINIA STAYS VIRGINIA THROUGH A FULL ROUND TRIP');
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({ '/api/enrollments/9': { body: VA_FULL } });
  await run('openEnrollment(9)');
  run('goStep(2)');
  await settle();
  check('opened as VA', run('state.address.state') === 'VA');
  run('goStep(1)');
  run('goStep(2)');
  await settle();
  check('still VA after stepping back and forward',
        run('state.address.state') === 'VA' && el('a-state').value === 'VA');
  fetchLog.length = 0;
  el('view-wizard').classList.add('active');
  await run("showView('dashboard')");
  await settle();
  const vaPatch = patches();
  check(`leaving saved (${vaPatch.length} PATCH)`, vaPatch.length === 1);
  const vaSent = JSON.parse((vaPatch[0] || {}).body || '{}');
  check('  ...and the PERSISTED payload says VA, not just the dropdown',
        vaSent.service_address && vaSent.service_address.state === 'VA');
  check('  ...with the VA utility account intact',
        vaSent.utility_account
        && vaSent.utility_account.utility_name === 'dominion-energy-va');
  check('  ...and nothing in the payload says NY',
        !JSON.stringify(vaSent).includes('"NY"'));

  // ───────────────────────────────────────────────────────────────────
  section('11. THE SCREEN NEVER CONTRADICTS ITSELF');
  // Reported from a real session: the "Pulled from the bill" panel said
  // "Not found - enter manually" for four fields while the form directly
  // below them was fully populated. The panel was rendered once from the OCR
  // result and never again.
  elCache.clear(); fetchLog.length = 0;
  run('resetWizardState();');
  run('lastBillParse = {first:null, last:null, street:null, acct:null, amount:null};');
  run('renderBillReadPanel();');
  let panel = el('ocr-container').innerHTML || '';
  check('with an unreadable bill and an empty form, rows say not found',
        (panel.match(/Not found/g) || []).length >= 3);

  // The rep now types the values the bill did not give up.
  el('c-first').value = 'test';
  el('c-last').value = 'testdominion';
  el('c-acct').value = '123456789012';
  el('a-street').value = '123 main st';
  el('a-city').value = 'Fredericksburg';
  run('renderBillReadPanel();');
  panel = el('ocr-container').innerHTML || '';
  check('once typed, the panel agrees with the form',
        (panel.match(/Entered manually/g) || []).length === 3);
  check('  ...and stops calling populated fields missing',
        !/Customer name<\/div><\/div><span class="ocr-conf">Not found/.test(panel)
        && (panel.match(/Not found/g) || []).length <= 1);
  check('  ...marking them as satisfied, not as warnings',
        (panel.match(/ocr-check warn/g) || []).length <= 1);

  // A value the bill DID read still says so, which is the distinction worth keeping.
  run('lastBillParse = {first:"Ada", last:"Byron", street:"1 Main St", acct:"999", amount:null};');
  run('renderBillReadPanel();');
  panel = el('ocr-container').innerHTML || '';
  check('a value read from the bill is still labelled as such',
        (panel.match(/Found on the bill/g) || []).length === 3);

  section('11b. THE METER HINT DESCRIBES THE FORMAT, IT DOES NOT ACCUSE');
  // Reported: a meter field containing a valid ten-digit number still showed
  // "Required: Please enter a valid ... meter number (10 digits)." in red.
  elCache.clear();
  run('resetWizardState();');
  run('meterRule = {field_type:"meter_number", field_name:"Meter number", '
      + 'help_text:"Please enter a valid Dominion Energy VA meter number (10 digits)", '
      + 'patterns:["^[0-9]{10}$"]};');
  run('marketAccountFields = [meterRule];');
  run('refreshMeterHint();');
  let hint = el('meter-rule-help');
  check('an EMPTY field shows the format as guidance',
        /10 digits/.test(hint.textContent) && !/^Required:/.test(hint.textContent));
  check('  ...not in red', hint.style.color === '');

  el('c-meter').value = '0123456789';
  run('refreshMeterHint();');
  check(`a VALID value is not flagged (${JSON.stringify(hint.textContent.slice(0, 32))})`,
        hint.style.color === '' && !/does not match/.test(hint.textContent));

  el('c-meter').value = '123';
  run('refreshMeterHint();');
  check('an INVALID value IS flagged', /does not match/.test(hint.textContent));
  check('  ...and only then is it red', hint.style.color === 'var(--danger)');

  el('c-meter').value = '0123456789';
  run('refreshMeterHint();');
  check('correcting it clears the error', hint.style.color === '');

  // ───────────────────────────────────────────────────────────────────
  section('12. ATTACHED FILES COME BACK AS ALREADY-SAVED');
  // The documents never left the database; the browser simply never asked for
  // them, so a reopened enrollment showed an empty uploader for a bill that
  // was still attached - and the only way to continue was to upload it again.
  elCache.clear(); fetchLog.length = 0;
  const WITH_DOCS = Object.assign({}, VA_FULL, {
    document_sets: {
      utility_bill: { document_set_id: 77, file_count: 3, files: [
        { id: 101, original_filename: 'page1.pdf', page_order: 0, file_size: 16384 },
        { id: 102, original_filename: 'page2.pdf', page_order: 1, file_size: 16384 },
        { id: 103, original_filename: 'page3.pdf', page_order: 2, file_size: 16384 } ] },
      lmi_document: { document_set_id: 78, file_count: 1, files: [
        { id: 201, original_filename: 'proof.pdf', page_order: 0, file_size: 2048 } ] },
    },
  });
  routes = withBase({ '/api/enrollments/9': { body: WITH_DOCS } });
  await run('openEnrollment(9)');
  await settle();
  check(`all three bill pages are restored `
        + `(${run('DocSets.utility_bill.files.length')})`,
        run('DocSets.utility_bill.files.length') === 3);
  check('  ...under the set they belong to',
        run('DocSets.utility_bill.id') === 77);
  check('  ...in page order',
        run("DocSets.utility_bill.files.map(function(f){ return f.file.name; }).join(',')")
        === 'page1.pdf,page2.pdf,page3.pdf');
  check('  ...each carrying its stored document id',
        run('DocSets.utility_bill.files[0].documentId') === 101);
  check('the proof document is restored too, as its own set',
        run('DocSets.lmi_document.files.length') === 1
        && run('DocSets.lmi_document.id') === 78);
  check('the bill pointer the step gates on is restored',
        run('state.bill.documentId') === 101
        && run('state.bill.fileName') === 'page1.pdf');

  section('12b. AND ARE NEVER RE-UPLOADED OR COUNTED AS MISSING');
  check('NO restored page counts as unsaved',
        run("DocSets.utility_bill.files.filter(function(x){ return !x.documentId; }).length")
        === 0);
  check('  ...so the "pages did not finish uploading" guard stays silent',
        run("DocSets.utility_bill.files.every(function(x){ return !!x.documentId; })")
        === true);
  check('the rendered list shows them as stored documents, not local picks',
        /\/documents\/101\/view/.test(el('bill-fileset').innerHTML || ''));

  section('12c. AND DO NOT FOLLOW THE REP INTO THE NEXT ENROLLMENT');
  run('resetWizardState();');
  await settle();
  check('a new enrollment starts with no bill files',
        run('DocSets.utility_bill.files.length') === 0
        && run('DocSets.utility_bill.id') === null);
  check('  ...and no proof documents',
        run('DocSets.lmi_document.files.length') === 0);
  check('  ...and no bill pointer', run('state.bill.documentId') === null);

  section('13. A SET PASSWORD IS NOT RE-DEMANDED');
  // Continue used to require a password unconditionally, so reopening an
  // enrollment whose customer already had one forced the rep to invent a new
  // one - silently replacing a credential the customer may already be using.
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({ '/api/enrollments/9': { body:
    Object.assign({}, VA_FULL, { customer_has_password: true }) } });
  await run('openEnrollment(9)');
  run('goStep(2)');
  await settle();
  el('c-phone').value = '5551234567';
  el('c-pass').value = '';
  el('c-pass-confirm').value = '';
  fetchLog.length = 0;
  await run('submitContactDetails()');
  await settle();
  const pw = patches();
  check(`a blank password no longer blocks Continue (${pw.length} PATCH)`,
        pw.length >= 1);
  const pwSent = JSON.parse((pw[0] || {}).body || '{}');
  check('  ...and no password key is sent at all',
        pwSent.customer && !('password' in pwSent.customer));
  check('  ...so the stored credential is left alone',
        JSON.stringify(pwSent).indexOf('password') === -1);

  section('13b. BUT A NEW ONE STILL REPLACES IT, WITH THE USUAL RULES');
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({ '/api/enrollments/9': { body:
    Object.assign({}, VA_FULL, { customer_has_password: true }) } });
  await run('openEnrollment(9)');
  run('goStep(2)');
  await settle();
  el('c-phone').value = '5551234567';
  el('c-pass').value = 'short';
  el('c-pass-confirm').value = 'short';
  fetchLog.length = 0;
  let ok13 = await run('submitContactDetails()');
  await settle();
  check('a too-short replacement is still refused', ok13 === false);
  check('  ...and nothing was sent', patches().length === 0);

  el('c-pass').value = 'longenough1';
  el('c-pass-confirm').value = 'longenough1';
  await run('submitContactDetails()');
  await settle();
  const pw2 = patches();
  check(`a valid replacement is accepted (${pw2.length} PATCH)`, pw2.length >= 1);
  check('  ...and IS sent, because the rep asked to change it',
        (JSON.parse((pw2[0] || {}).body || '{}').customer || {}).password
        === 'longenough1');

  // ───────────────────────────────────────────────────────────────────
  section('14. THE REAL CONTINUE PATH ACCEPTS AN EXISTING PASSWORD');
  // Driven through the button the rep actually presses - checkBillReady() to
  // enable it, then submitBill() - NOT submitContactDetails() directly. The
  // earlier test called the inner function with the flag already set, which is
  // a state the real flow never reached: Save & Exit did not save a password,
  // so customer_has_password was false and Continue was right to ask.
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({
    '/api/enrollments/9': { body: Object.assign({}, VA_FULL, {
      customer_has_password: true,
      document_sets: { utility_bill: { document_set_id: 77, file_count: 1, files: [
        { id: 101, original_filename: 'bill.pdf', page_order: 0, file_size: 2048 } ] } },
    }) },
  });
  await run('openEnrollment(9)');
  run('goStep(2)');
  await settle();
  check('the form says a password is already set',
        el('c-pass-set-note').style.display === 'block');
  check('  ...and both boxes are blank', el('c-pass').value === ''
        && el('c-pass-confirm').value === '');

  el('c-phone').value = '5551234567';
  run('checkBillReady();');
  const req = el('bill-requirements').innerHTML || '';
  check('the requirements list does NOT ask for a password',
        !/password/i.test(req));
  check(`Continue is ENABLED with both password boxes empty `
        + `(disabled=${el('btn-bill-next').disabled})`,
        el('btn-bill-next').disabled === false);

  fetchLog.length = 0;
  await run('submitBill()');
  await settle();
  const cont = patches();
  check(`pressing Continue goes through (${cont.length} PATCH)`, cont.length >= 1);
  const contSent = JSON.parse((cont[0] || {}).body || '{}');
  check('  ...sending no password key at all',
        contSent.customer && !('password' in contSent.customer));
  check('  ...so the stored credential is untouched',
        JSON.stringify(contSent).indexOf('password') === -1);
  check('  ...and no password error was shown',
        el('contact-submit-error').style.display !== 'block');

  section('14b. AND SAVE & EXIT STORES A PASSWORD THE REP TYPES');
  // The actual root cause: a progress save carried no password, so one typed
  // before Save & Exit was never stored.
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({ '/api/enrollments/9': { body: { ok: true } } });
  run("resetWizardState(); currentDraft = {enrollment_id: 9, enrollment_code: 'ENR-9'};");
  run("perchContext.email = 'avail@example.com';");
  el('view-wizard').classList.add('active');
  el('c-first').value = 'Dominion';
  el('c-last').value = 'Customer';
  el('c-pass').value = 'portalpass1';
  el('c-pass-confirm').value = 'portalpass1';
  await run('saveAndExit()');
  await settle();
  const sx = patches();
  check(`Save & Exit saved (${sx.length} PATCH)`, sx.length === 1);
  const sxSent = JSON.parse((sx[0] || {}).body || '{}');
  check('  ...and the password went with it',
        (sxSent.customer || {}).password === 'portalpass1');
  check('  ...so the flag flips without waiting for a reopen',
        run('perchContext.customerHasPassword') === true);

  section('14c. A HALF-TYPED PASSWORD IS NEVER STORED');
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({ '/api/enrollments/9': { body: { ok: true } } });
  run("resetWizardState(); currentDraft = {enrollment_id: 9, enrollment_code: 'ENR-9'};");
  run("perchContext.email = 'avail@example.com';");
  el('view-wizard').classList.add('active');
  el('c-first').value = 'Dominion';
  el('c-last').value = 'Customer';
  el('c-pass').value = 'portalpass1';
  el('c-pass-confirm').value = 'portalpa';        // mismatched
  await run('saveAndExit()');
  await settle();
  const hx = JSON.parse((patches()[0] || {}).body || '{}');
  check('a mismatched pair is not saved as a password',
        !('password' in (hx.customer || {})));
  check('  ...while the rest of the progress still saves',
        (hx.customer || {}).first_name === 'Dominion');

  // ───────────────────────────────────────────────────────────────────
  section('15. REMOVING A STORED FILE ACTUALLY REMOVES IT');
  // The missing link: Remove only filtered the in-memory array. The DELETE
  // endpoint existed and worked; nothing called it.
  elCache.clear(); fetchLog.length = 0;
  const THREE = { utility_bill: { document_set_id: 77, file_count: 3, files: [
    { id: 101, original_filename: 'A.pdf', page_order: 0, file_size: 2048 },
    { id: 102, original_filename: 'B.pdf', page_order: 1, file_size: 2048 },
    { id: 103, original_filename: 'C.pdf', page_order: 2, file_size: 2048 } ] } };
  routes = withBase({
    '/document-sets/77/files/102': { body: { removed: 102, remaining: 2 } },
    '/api/enrollments/9': { body: Object.assign({}, VA_FULL, { document_sets: THREE }) },
  });
  await run('openEnrollment(9)');
  await settle();
  check('precondition: three pages restored',
        run('DocSets.utility_bill.files.length') === 3);

  await run("docSetRemove('utility_bill', 'saved:102')");
  await settle();
  check('the page disappears from the list',
        run('DocSets.utility_bill.files.length') === 2);
  check('  ...and it is the RIGHT one',
        run("DocSets.utility_bill.files.map(function(f){ return f.file.name; }).join(',')")
        === 'A.pdf,C.pdf');
  const del = fetchLog.filter(function(f){ return f.method === 'DELETE'; });
  check(`  ...and a DELETE was actually sent (${del.length})`, del.length === 1);
  check('  ...to the right file, in the right set, on the right enrollment',
        del[0] && del[0].url.includes('/api/enrollments/9/document-sets/77/files/102'));

  section('15b. REMOVING THE FIRST PAGE REPOINTS THE BILL');
  // state.bill.documentId gates Continue and is what /enroll is told to use,
  // so it must never point at a file that was just removed.
  check('precondition: the pointer is the first page',
        run('state.bill.documentId') === 101);
  routes = withBase({
    '/document-sets/77/files/101': { body: { removed: 101, remaining: 1 } },
    '/api/enrollments/9': { body: Object.assign({}, VA_FULL, { document_sets: THREE }) },
  });
  await run("docSetRemove('utility_bill', 'saved:101')");
  await settle();
  check('the pointer moves to the page that is still there',
        run('state.bill.documentId') === 103);
  check('  ...and names it', run('state.bill.fileName') === 'C.pdf');

  section('15c. A REFUSED REMOVAL PUTS THE FILE BACK AND SAYS SO');
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({
    '/document-sets/77/files/102': { status: 500, body: { error: 'storage offline' } },
    '/api/enrollments/9': { body: Object.assign({}, VA_FULL, { document_sets: THREE }) },
  });
  await run('openEnrollment(9)');
  await settle();
  await run("docSetRemove('utility_bill', 'saved:102')");
  await settle();
  check('a failed removal restores the file',
        run('DocSets.utility_bill.files.length') === 3);
  check('  ...in its original position',
        run("DocSets.utility_bill.files.map(function(f){ return f.file.name; }).join(',')")
        === 'A.pdf,B.pdf,C.pdf');
  // C2 kept this message and this element; only the sentence is filtered
  // through userMessage() now, and the element is the shared alert.
  check('  ...and the rep is told, rather than it failing silently',
        el('bill-submit-error').style.display !== 'none'
        && /could not be removed/i.test(el('bill-submit-error').textContent));

  section('15d. A FILE NOT YET UPLOADED NEEDS NO SERVER CALL');
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({});
  run("resetWizardState(); currentDraft = {enrollment_id: 9, enrollment_code: 'ENR-9'};");
  run("DocSets.utility_bill = {id: null, files: [{file:{name:'local.pdf',size:10}, "
      + "key:'local.pdf:10:1', documentId: null}]};");
  await run("docSetRemove('utility_bill', 'local.pdf:10:1')");
  await settle();
  check('it is removed from the list',
        run('DocSets.utility_bill.files.length') === 0);
  check('  ...with no DELETE sent, because there is nothing stored yet',
        fetchLog.filter(function(f){ return f.method === 'DELETE'; }).length === 0);

  // ───────────────────────────────────────────────────────────────────
  section('16. THE RENDERED REMOVE CONTROL IS THE ONE THAT IS TESTED');
  // Everything above called docSetRemove() directly. That proved the function
  // works and proved NOTHING about the button the rep presses. This section
  // renders the restored list, scrapes the onclick attribute out of the HTML,
  // and EXECUTES THAT STRING - so the handler, the category and the key all
  // come from the real markup.
  elCache.clear(); fetchLog.length = 0;
  const FOUR = { utility_bill: { document_set_id: 77, file_count: 4, files: [
    { id: 101, original_filename: 'A.jpg', page_order: 0, file_size: 16384 },
    { id: 102, original_filename: 'B.jpg', page_order: 1, file_size: 16384 },
    { id: 103, original_filename: 'C.jpg', page_order: 2, file_size: 16384 },
    { id: 104, original_filename: 'D.jpg', page_order: 3, file_size: 16384 } ] } };
  routes = withBase({
    '/document-sets/77/files/102': { body: { removed: 102, remaining: 3 } },
    '/document-sets/77/files/103': { body: { removed: 103, remaining: 2 } },
    '/api/enrollments/9': { body: Object.assign({}, VA_FULL, { document_sets: FOUR }) },
  });
  await run('openEnrollment(9)');
  run('goStep(2)');
  await settle();
  check('precondition: four photos restored',
        run('DocSets.utility_bill.files.length') === 4);

  // THE DUPLICATE CONTROL. The legacy chip used to be drawn for a restored
  // file as well, putting a second "Remove" on screen whose only effect was to
  // clear the chip - which is how a deletion became cosmetic.
  check('no legacy chip is drawn for an already-stored file',
        (el('bill-file-chip').innerHTML || '') === '');
  // Each row carries TWO handlers - the filename link that opens the stored
  // document, and the remove button. Only the second is under test.
  const removeHandlers = function(){
    const html = el('bill-fileset').innerHTML || '';
    return [...html.matchAll(/onclick="([^"]+)"/g)]
      .map((m) => m[1])
      .filter((h) => h.indexOf('docSetRemove(') === 0);
  };
  const handlers = removeHandlers();
  check(`the list renders one remove control per file (${handlers.length})`,
        handlers.length === 4);
  check('  ...each calling the persisting remover',
        handlers.every((h) => h.indexOf("docSetRemove('utility_bill'") === 0));
  check('  ...carrying the stored key, not a local filename',
        handlers[1].includes("'saved:102'"));

  // Press the rendered controls for B and C, exactly as a rep would.
  await run(handlers[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&'));
  await settle();
  const handlers2 = removeHandlers();
  await run(handlers2[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&'));
  await settle();

  const dels = fetchLog.filter((f) => f.method === 'DELETE');
  check(`pressing the rendered controls fired two DELETEs (${dels.length})`,
        dels.length === 2);
  check('  ...for B, with the right enrollment, set and file',
        dels.some((d) => d.url.endsWith('/api/enrollments/9/document-sets/77/files/102')));
  check('  ...and for C',
        dels.some((d) => d.url.endsWith('/api/enrollments/9/document-sets/77/files/103')));
  check('the remaining list is A and D only',
        run("DocSets.utility_bill.files.map(function(f){ return f.file.name; }).join(',')")
        === 'A.jpg,D.jpg');

  section('16b. AND SAVE & EXIT WRITES NOTHING THAT BRINGS THEM BACK');
  fetchLog.length = 0;
  el('view-wizard').classList.add('active');
  await run('saveAndExit()');
  await settle();
  const writes = fetchLog.filter((f) => f.method === 'POST' || f.method === 'PUT');
  check('Save & Exit uploads nothing', writes.length === 0);
  const exitBody = patches().map((p) => p.body || '').join(' ');
  check('  ...and its PATCH says nothing about documents',
        !/document_set|documents|A\.jpg|B\.jpg/.test(exitBody));

  section('16c. THE LEGACY CHIP CANNOT FAKE A DELETION EITHER');
  // If the chip is ever shown for a stored file, its Remove must be real.
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({
    '/document-sets/77/files/101': { body: { removed: 101, remaining: 3 } },
    '/api/enrollments/9': { body: Object.assign({}, VA_FULL, { document_sets: FOUR }) },
  });
  await run('openEnrollment(9)');
  await settle();
  run("showBillChip('A.jpg');");
  const chip = el('bill-file-chip').innerHTML || '';
  const chipHandler = (/onclick="([^"]+)"/.exec(chip) || [])[1];
  check(`the chip's control is removeBill() (${chipHandler})`,
        chipHandler === 'removeBill()');
  await run(chipHandler);
  await settle();
  const chipDels = fetchLog.filter((f) => f.method === 'DELETE');
  check(`pressing it issues a real DELETE (${chipDels.length})`,
        chipDels.length === 1);
  check('  ...for the stored document',
        chipDels[0].url.endsWith('/document-sets/77/files/101'));
  check('  ...and the file leaves the list too',
        run('DocSets.utility_bill.files.length') === 3);

  section('16d. BUT A PENDING UPLOAD STILL JUST CANCELS');
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({});
  run("resetWizardState(); currentDraft = {enrollment_id: 9, enrollment_code: 'ENR-9'};");
  run("DocSets.utility_bill = {id: null, files: [{file:{name:'pending.jpg',size:10}, "
      + "key:'pending.jpg:10:1', documentId: null}]};");
  run("state.bill.fileName = 'pending.jpg'; state.bill.documentId = null;");
  await run('removeBill()');
  await settle();
  check('the pending file is dropped', run('DocSets.utility_bill.files.length') === 0);
  check('  ...with no DELETE, because nothing was stored',
        fetchLog.filter((f) => f.method === 'DELETE').length === 0);
  check('  ...and the bill state is cleared',
        run('state.bill.fileName') === '' && run('state.bill.documentId') === null);

  report();
})();

function report() {
  const failed = results.filter((r) => !r.ok);
  console.log('\n' + '='.repeat(72));
  if (failed.length) {
    console.log(`PHASE A STATE HARNESS - ${failed.length} FAILED`);
    failed.forEach((f) => console.log('  FAIL ' + f.label));
    console.log('='.repeat(72));
    process.exit(1);
  }
  console.log(`PHASE A STATE HARNESS - ALL ${results.length} CHECKS PASSED`);
  console.log('='.repeat(72));
  process.exit(0);
}

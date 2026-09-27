/*
 * Capacity failure -> wizard reset, runtime harness.
 *
 * WHY THIS EXISTS
 * ---------------
 * The first version of this fix cleared currentDraft on EVERY thrown capacity
 * error. That is wrong: the backend only discards the row when
 *   (a) this very request created it (created_here), AND
 *   (b) enrollment_cleanup approved the discard.
 * On a RETRY against an enrollment_id the browser already holds, or when
 * cleanup refuses, the row is STILL LIVE - and clearing the id would strand it
 * on the dashboard, which is the exact bug this change set exists to remove.
 *
 * A substring check against app.js cannot tell those two cases apart, because
 * both run the same catch block. So this harness EXECUTES submitCapacity()
 * against a stubbed fetch and asserts what currentDraft actually holds
 * afterwards.
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(ROOT, 'templates', 'index.html'), 'utf8');
const REAL_IDS = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));
// The wizard step is rendered into innerHTML at runtime, so these ids are not
// in the static template - but they DO exist in the browser by the time
// submitCapacity() runs. Adding them keeps the stub faithful to that moment.
['wf-form-error', 'wf-primary', 'wf-email', 'wf-zip_code', 'wf-utility_name',
 'wf-err-email', 'wf-err-zip_code', 'wf-err-utility_name'].forEach((id) => REAL_IDS.add(id));

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
    title: '', type: 'text', style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    appendChild() {}, insertBefore() {}, removeChild() {},
    addEventListener() {}, removeEventListener() {}, focus() {}, click() {},
    closest() { return null; }, querySelector() { return null; },
    querySelectorAll() { return []; }, scrollIntoView() {},
    getContext() { return null; }, reset() {},
  };
  el.parentNode = null; el.firstChild = null;
  return el;
}

// One response the stubbed fetch will return for the capacity call.
let nextResponse = null;

const elCache = new Map();
const documentStub = {
  getElementById(id) {
    if (!REAL_IDS.has(id)) return null;
    if (!elCache.has(id)) elCache.set(id, makeEl(id));
    return elCache.get(id);
  },
  querySelector() { return null; }, querySelectorAll() { return []; },
  createElement(tag) { return makeEl('created-' + tag); },
  addEventListener() {}, removeEventListener() {},
  body: makeEl('body'), documentElement: makeEl('html'), activeElement: null,
};
documentStub.body.style = {};

const storageStub = {
  _d: {},
  getItem(k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; },
  setItem(k, v) { this._d[k] = String(v); },
  removeItem(k) { delete this._d[k]; }, clear() { this._d = {}; },
};
storageStub.setItem('dalton_auth_token', 'harness-token');

const sandbox = {
  console, document: documentStub,
  sessionStorage: storageStub, localStorage: storageStub,
  setTimeout() { return 0; }, clearTimeout() {},
  setInterval() { return 0; }, clearInterval() {},
  alert() {}, confirm() { return true; },
  fetch(url) {
    if (String(url).includes('/enrollments/capacity') && nextResponse) {
      const r = nextResponse;
      return Promise.resolve({
        ok: r.status === 200, status: r.status,
        json: () => Promise.resolve(r.body),
      });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  },
  FormData: function () { this.append = function () {}; },
  FileReader: function () { this.readAsDataURL = function () {}; },
  Blob: function () {},
  URL: { createObjectURL() { return 'blob:stub'; }, revokeObjectURL() {} },
  navigator: { userAgent: 'harness' },
  location: { href: '', reload() {} },
  requestAnimationFrame() { return 0; },
  pdfjsLib: { getDocument() { return { promise: Promise.resolve({ numPages: 0 }) }; }, GlobalWorkerOptions: {} },
  Tesseract: { recognize() { return Promise.resolve({ data: { text: '' } }); } },
};
sandbox.scrollTo = function () {};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

console.log('='.repeat(72));
console.log('CAPACITY FAILURE -> WIZARD RESET HARNESS');
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

// A capacity step whose fields validate with the values we plant below.
const STEP = {
  step_key: 'service_area',
  primary_action: { label: 'Check availability' },
  fields: [
    { name: 'email', required: true },
    { name: 'zip_code', required: true },
    { name: 'utility_name', required: true },
  ],
};

function plant(draft) {
  vm.runInContext('currentWorkflow = ' + JSON.stringify({ step: STEP }) + ';', ctx);
  vm.runInContext('currentDraft = ' + JSON.stringify(draft) + ';', ctx);
  vm.runInContext("selectedProgram = {id:'p1'}; availablePrograms = [{id:'p1'}];", ctx);
  vm.runInContext("perchContext.email='held@example.com'; perchContext.capacityZip='12401';", ctx);
  ['email', 'zip_code', 'utility_name'].forEach((n) => {
    const el = documentStub.getElementById('wf-' + n);
    if (el) el.value = n === 'email' ? 'held@example.com'
      : (n === 'zip_code' ? '12401' : 'central-hudson-gas-electric');
  });
}

function readState() {
  return {
    draft: vm.runInContext('currentDraft ? currentDraft.enrollment_id : null', ctx),
    program: vm.runInContext('selectedProgram ? selectedProgram.id : null', ctx),
    email: vm.runInContext("perchContext.email || ''", ctx),
    errText: (documentStub.getElementById('wf-form-error') || {}).textContent,
  };
}

async function run() {
  const submit = sandbox.submitCapacity;
  check('submitCapacity() is defined', typeof submit === 'function');
  if (typeof submit !== 'function') return report();

  // ── CASE 1 ────────────────────────────────────────────────────────────
  section('NEWLY-CREATED provisional row, failed, SERVER CONFIRMS discard');
  plant({ enrollment_id: 77, enrollment_code: 'ENR-77' });
  nextResponse = { status: 409, body: {
    error: 'That email address is already registered with Perch and has no '
         + 'in-progress enrollment to resume. Use a different email address, '
         + "or check the customer's existing enrollment with Perch.",
    perch_error: 'PerchEnrollmentInProgressError',
    enrollment_discarded: true,
  } };
  await submit.call(sandbox);
  let s = readState();
  check('the wizard DROPS its enrollment id', s.draft === null);
  check('  ...and clears the transient program state', s.program === null);
  check('  ...and clears the captured capacity context', s.email === '');
  check('  ...while still showing the specific duplicate message',
    typeof s.errText === 'string' && s.errText.toLowerCase().includes('already registered'));
  check('  ...and NOT a generic technical message',
    !String(s.errText).toLowerCase().includes('not found'));

  // ── CASE 2 ────────────────────────────────────────────────────────────
  section('RETRY against an existing enrollment, failed, NOT discarded');
  plant({ enrollment_id: 42, enrollment_code: 'ENR-42' });
  nextResponse = { status: 404, body: {
    error: 'Perch has no in-progress enrollment for that email.',
    perch_error: 'PerchNotFoundError',
    enrollment_discarded: false,
  } };
  await submit.call(sandbox);
  s = readState();
  check('the wizard KEEPS its enrollment id', s.draft === 42);
  check('  ...keeps the selected program', s.program === 'p1');
  check('  ...keeps the captured capacity context', s.email === 'held@example.com');
  check('  ...and still shows the error to the rep',
    typeof s.errText === 'string' && s.errText.length > 0);

  // ── CASE 3 ────────────────────────────────────────────────────────────
  section('CLEANUP REFUSED on a row we created - state is preserved');
  plant({ enrollment_id: 99, enrollment_code: 'ENR-99' });
  nextResponse = { status: 400, body: {
    error: 'Perch rejected the request (422): Zip code is invalid',
    perch_error: 'PerchValidationError',
    enrollment_discarded: false,
  } };
  await submit.call(sandbox);
  s = readState();
  check('a correctable error does NOT strand the rep', s.draft === 99);
  check('  ...so they can fix the input and retry the same row', s.program === 'p1');

  // ── CASE 4 ────────────────────────────────────────────────────────────
  section('FLAG ABSENT ENTIRELY - the safe default is to preserve');
  plant({ enrollment_id: 55, enrollment_code: 'ENR-55' });
  nextResponse = { status: 502, body: { error: 'Upstream unavailable' } };
  await submit.call(sandbox);
  s = readState();
  check('a response with no flag preserves the enrollment id', s.draft === 55);

  section('A NETWORK FAILURE (no body at all) also preserves');
  plant({ enrollment_id: 61, enrollment_code: 'ENR-61' });
  nextResponse = null;
  const realFetch = sandbox.fetch;
  sandbox.fetch = () => Promise.reject(new Error('offline'));
  await submit.call(sandbox);
  sandbox.fetch = realFetch;
  s = readState();
  check('an unreachable server never discards local state', s.draft === 61);

  // ── CASE 5 ────────────────────────────────────────────────────────────
  section('SUCCESS still adopts the enrollment normally');
  plant(null);
  nextResponse = { status: 200, body: {
    enrollment_id: 123, enrollment_code: 'ENR-123', step: { step_key: 'capacity_result' },
  } };
  // Rendering the NEXT step needs more of the page than this harness stubs, so
  // a downstream render error is ignored here. Adoption happens before that
  // render, which is the only thing being asserted. The full success path is
  // covered by live_lifecycle_harness.js and the Python suites.
  await submit.call(sandbox).catch(() => {});
  check('a successful capacity call adopts the new enrollment',
    vm.runInContext('currentDraft ? currentDraft.enrollment_id : null', ctx) === 123);
  check('  ...so the reset gate did not break the happy path',
    vm.runInContext('currentDraft ? currentDraft.enrollment_code : null', ctx) === 'ENR-123');

  section('THE FLAG IS READ, NOT INFERRED');
  check('the reset is gated on the server-confirmed boolean',
    /enrollment_discarded\s*===\s*true/.test(src));
  check('  ...and apiFetch attaches the response body for it to read',
    /apiErr\.body\s*=\s*data\s*\|\|\s*\{\}/.test(src));
  check('  ...without changing the message every other caller reads',
    /new Error\(\(data && data\.error\)/.test(src));

  report();
}

function report() {
  const failed = results.filter((r) => !r.ok);
  console.log('\n' + '='.repeat(72));
  console.log(`${results.length - failed.length} passed, ${failed.length} failed`);
  console.log('='.repeat(72));
  process.exit(failed.length ? 1 : 0);
}

run().catch((e) => {
  check('harness ran to completion - ' + e.message, false);
  report();
});

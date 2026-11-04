/*
 * RUNTIME PROOF: an account number, once right, stays right.
 *
 * WHAT WENT WRONG  (traced 2026-11-03)
 *
 *   Two defects that fed each other. The in-browser reader asked for exactly
 *   ten digits and was not anchored, so an eleven-digit account came back as
 *   its first ten. And the six assignments that followed were UNCONDITIONAL,
 *   so every later upload wrote over whatever was in the box.
 *
 *   Together they produced the reported symptom at its worst: the reader drops
 *   the last digit, the rep notices and types it back, the rep uploads page two
 *   of the same bill - and the short number is written over their correction.
 *   For Orange & Rockland, which issues both ten- and eleven-digit numbers, the
 *   short value also PASSED validation, so nothing downstream objected.
 *
 * WHAT IS PROVEN HERE
 *
 *   The second half of that - the overwriting - at runtime, across the whole
 *   life of an enrollment: first upload, re-upload, a replaced bill, a removed
 *   bill, Save & Exit, Resume, and stepping back and forth through the wizard.
 *   The reading itself is proven in test/test_account_number_integrity.py.
 *
 *   This runs the real static/js/app.js against a DOM stub built from the real
 *   ids in templates/index.html, so a renamed field fails here and not in
 *   front of a rep.
 *
 * Every value below is invented for this harness.
 *
 * node test/account_number_lifecycle_harness.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(ROOT, 'templates', 'index.html'), 'utf8');
const REAL_IDS = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));

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
    title: '', type: 'text', style: {}, dataset: {}, options: [], files: null,
    maxLength: 0, placeholder: '', offsetWidth: 1, _listeners: {},
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
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
    focus() {}, click() {}, closest() { return null; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    scrollIntoView() {}, getContext() { return null; }, reset() {},
  };
  el.parentNode = null; el.firstChild = null;
  return el;
}

const elCache = new Map();
const documentStub = {
  getElementById(id) {
    if (!REAL_IDS.has(id)) return null;
    if (!elCache.has(id)) elCache.set(id, makeEl(id));
    return elCache.get(id);
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
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

const sandbox = {
  console, document: documentStub, sessionStorage: storageStub, localStorage: storageStub,
  setTimeout(fn) { if (typeof fn === 'function') fn(); return 0; },
  clearTimeout() {}, setInterval() { return 0; }, clearInterval() {},
  alert() {}, confirm() { return true; },
  fetch() { return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) }); },
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
console.log('RUNTIME PROOF - AN ACCOUNT NUMBER, ONCE RIGHT, STAYS RIGHT');
console.log('='.repeat(72));

const ctx = vm.createContext(sandbox);
vm.runInContext(src, ctx, { filename: 'app.js' });

function el(id) { return documentStub.getElementById(id); }
function run(code) { return vm.runInContext(code, ctx); }

// ── Synthetic values. Nothing here belongs to anyone. ──────────────────────
const WHOLE = '90000000011';   // 11 digits, as the bill prints it
const SHORT = '9000000001';    // the first ten - what the old reader produced
const TYPED = '90000000099';   // what a rep types by hand

/* O&R: the utility at the centre of this, because it issues BOTH lengths and
   so the truncated value passed validation. Shaped as the server publishes
   it, with the array the centralized rule now sends. */
const OR_RULE = {
  slug: 'orange-and-rockland', display_name: 'Orange & Rockland',
  requires_pod_id: false, account_number_length: 10,
  account_number_lengths: [10, 11],
  account_number_description: '10 or 11 digits', pod_id: null,
};
/* National Grid: one length only. Used to prove the fix did not loosen the
   utilities that were never wrong. */
const NG_RULE = {
  slug: 'national-grid-ny', display_name: 'National Grid',
  requires_pod_id: false, account_number_length: 10,
  account_number_lengths: [10], pod_id: null,
};

function bill(number) {
  return 'ACCOUNT NUMBER ' + number + ' SERVICE FOR PAT SAMPLE '
       + '742 EXAMPLE WAY ANYTOWN NY 10901 AMOUNT DUE $50.00';
}

/* The state a rep is in on Customer & Bill for a given utility. */
function forUtility(rule) {
  elCache.clear();
  run(`utilityRules = ${JSON.stringify({ [rule.slug]: rule })};`);
  run(`perchContext.utilitySlug = ${JSON.stringify(rule.slug)};`);
  run('marketAccountFields = null;');
  run('lastBillParse = null;');
  run('DocSets.utility_bill = {files: [], nextKey: 1};');
  run('state.bill = {fileName: "", documentId: null, uploadError: null};');
  run('state.customer = {first:"", last:"", phone:"", email:"", acct:"", podId:"", meterNumber:""};');
  run('state.address = {street:"", unit:"", city:"", zip:"", state:"NY"};');
  run('currentStep = 2;');
  el('a-state').options = [{ value: 'NY' }, { value: 'VA' }];
  el('a-state').value = 'NY';
  run('configureBillUtilityRules();');
}

/* Upload a bill carrying `number`, through the REAL handleBillUpload - the
   in-browser read, the length gate and the fill. The two things the harness
   cannot provide are stubbed: reading pixels, and the network. */
async function upload(number, serverFields) {
  run(`extractTextFromFile = function(){ return Promise.resolve(${JSON.stringify(bill(number))}); };`);
  run(`uploadDocumentSet = function(){ return Promise.resolve({
    files: [{document_id: 'doc-' + Math.random().toString(36).slice(2)}],
    extraction: ${JSON.stringify(serverFields ? { fields: serverFields, status: 'success', issues: [] } : null)}
  }); };`);
  run('docSetAddFiles = function(){};');
  run('renderDocSetList = function(){};');
  run('showBillChip = function(){};');
  run('renderPrefillScope = function(){};');
  run('syncBillingFromService = function(){};');
  await run(`handleBillUpload([{name: 'bill.pdf', size: 1024}]);`);
}

(async function main() {
  // ══════════════════════════════════════════════════════════════════════════
  section('1. THE FIRST UPLOAD INTO AN EMPTY FORM');
  // ══════════════════════════════════════════════════════════════════════════
  forUtility(OR_RULE);
  check('the field is not capped below eleven digits (' + el('c-acct').maxLength + ')',
    el('c-acct').maxLength >= 11);
  await upload(WHOLE);
  check('all eleven digits land in the box (' + el('c-acct').value + ')',
    el('c-acct').value === WHOLE);
  check('  ...and not the first ten of them', el('c-acct').value !== SHORT);
  check('the rep can see the bill supplied it',
    el('c-acct').classList.contains('ocr-filled'));

  // ══════════════════════════════════════════════════════════════════════════
  section('2. A SECOND UPLOAD CANNOT SHORTEN IT');
  // ══════════════════════════════════════════════════════════════════════════
  // The exact reported sequence: the right number is in the box, and a later
  // page is read as the short one.
  await upload(SHORT);
  check('the eleven-digit value stands (' + el('c-acct').value + ')',
    el('c-acct').value === WHOLE);
  await upload(SHORT);
  check('  ...and after a third upload too', el('c-acct').value === WHOLE);

  // ══════════════════════════════════════════════════════════════════════════
  section('3. A REP CORRECTION OUTLIVES EVERY LATER READ');
  // ══════════════════════════════════════════════════════════════════════════
  forUtility(OR_RULE);
  await upload(SHORT);                       // a 10-digit read, valid for O&R
  check('the short read fills an empty box, as it should (' + el('c-acct').value + ')',
    el('c-acct').value === SHORT);
  el('c-acct').value = TYPED;                // the rep puts it right
  await upload(SHORT);
  check('the typed value is not replaced (' + el('c-acct').value + ')',
    el('c-acct').value === TYPED);
  await upload(WHOLE);
  check('  ...not even by a LONGER, valid read', el('c-acct').value === TYPED);
  check('  ...and the rep is the only one who changes it', el('c-acct').value === TYPED);

  // The server's reading of the stored set follows the same rule.
  await upload(SHORT, { account_number: SHORT, service_zip: '10901' });
  check('the server reading does not overwrite it either (' + el('c-acct').value + ')',
    el('c-acct').value === TYPED);

  // ══════════════════════════════════════════════════════════════════════════
  section('4. REPLACING AND REMOVING THE BILL');
  // ══════════════════════════════════════════════════════════════════════════
  forUtility(OR_RULE);
  await upload(WHOLE);
  el('c-acct').value = TYPED;
  run('docSetRemove = function(){ return Promise.resolve(); };');
  run('checkBillReady = function(){};');
  await run('removeBill();');
  check('removing the bill leaves the account number alone (' + el('c-acct').value + ')',
    el('c-acct').value === TYPED);
  await upload(SHORT);                       // a different bill, read short
  check('replacing it with another bill does not overwrite it',
    el('c-acct').value === TYPED);

  // ══════════════════════════════════════════════════════════════════════════
  section('5. SAVE & EXIT, THEN RESUME');
  // ══════════════════════════════════════════════════════════════════════════
  // The two halves of leaving and coming back: captureCustomerFields() reads
  // the form into state on the way out, hydrateStep(2) writes it back on the
  // way in. Both are what goStep() calls.
  forUtility(OR_RULE);
  await upload(WHOLE);
  run('saveCustomerProgress = function(){ return Promise.resolve(); };');
  run('captureCustomerFields();');
  check('leaving carries all eleven digits into state (' + run('state.customer.acct') + ')',
    run('state.customer.acct') === WHOLE);
  el('c-acct').value = '';                   // the form is blank on a fresh load
  run('hydrateStep(2);');
  check('resuming puts all eleven back (' + el('c-acct').value + ')',
    el('c-acct').value === WHOLE);
  check('  ...and the field still allows eleven after rehydrating',
    el('c-acct').maxLength >= 11);

  // ══════════════════════════════════════════════════════════════════════════
  section('6. STEPPING BACK AND FORWARD');
  // ══════════════════════════════════════════════════════════════════════════
  el('c-acct').value = TYPED;                // corrected, then navigate away
  run('captureCustomerFields();');
  run('hydrateStep(2);');
  check('a correction survives a round trip through the wizard ('
    + el('c-acct').value + ')', el('c-acct').value === TYPED);
  // An empty box must never be read as "clear what you know" - that is how a
  // value is lost by navigating before the form has hydrated.
  el('c-acct').value = '';
  run('captureCustomerFields();');
  check('leaving an unhydrated form does not wipe stored state ('
    + run('state.customer.acct') + ')', run('state.customer.acct') === TYPED);
  run('hydrateStep(2);');
  check('  ...so coming back shows it again', el('c-acct').value === TYPED);

  // ══════════════════════════════════════════════════════════════════════════
  section('7. A LENGTH THE UTILITY DOES NOT ISSUE IS LEFT FOR THE REP');
  // ══════════════════════════════════════════════════════════════════════════
  // Not trimmed to fit, which is what caused all of this.
  forUtility(NG_RULE);
  await upload(WHOLE);                       // 11 digits, National Grid
  check('nothing is filled in (' + JSON.stringify(el('c-acct').value) + ')',
    el('c-acct').value === '');
  check('  ...and certainly not a trimmed ten', el('c-acct').value !== SHORT);
  check('the panel tells the rep to enter it',
    /account number[\s\S]{0,300}enter manually/i.test(el('ocr-container').innerHTML));
  check('  ...saying it was not valid for this utility, not "not found"',
    /not a valid account number for this utility/i.test(el('ocr-container').innerHTML));
  await upload(SHORT);                       // 10 digits, valid here
  check('the length National Grid does issue still fills ('
    + el('c-acct').value + ')', el('c-acct').value === SHORT);

  // ══════════════════════════════════════════════════════════════════════════
  section('8. THE READER ITSELF NEVER SHORTENS');
  // ══════════════════════════════════════════════════════════════════════════
  const cases = [
    ['11 digits', '90000000011', '90000000011'],
    ['10 digits', '9000000001', '9000000001'],
    ['dashed 10', '90000-00001', '9000000001'],
    ['12 digits', '900000000012', '900000000012'],
    ['label with a colon', '90000000011', '90000000011'],
  ];
  cases.forEach(function (c) {
    const text = c[0] === 'label with a colon'
      ? 'ACCOUNT NUMBER: ' + c[1] + ' AMOUNT DUE $50.00'
      : bill(c[1]);
    const got = run(`parseUtilityBill(${JSON.stringify(text)}).acct`);
    check(c[0] + ' -> ' + got, got === c[2]);
  });
  const trailing = run(`parseUtilityBill(${JSON.stringify(
    'ACCOUNT NUMBER 90000000011 DATE BILL ISSUED March 3, 2026')}).acct`);
  check('a date after the number does not bleed into it (' + trailing + ')',
    trailing === WHOLE);

  // ── Summary ───────────────────────────────────────────────────────────────
  const failed = results.filter((r) => !r.ok);
  console.log('\n' + '='.repeat(72));
  console.log(`${results.length - failed.length}/${results.length} checks passed`);
  if (failed.length) {
    failed.forEach((r) => console.log('  FAILED: ' + r.label));
    console.log('='.repeat(72));
    process.exit(1);
  }
  console.log('AN ACCOUNT NUMBER, ONCE RIGHT, STAYS RIGHT');
  console.log('='.repeat(72));
})().catch((e) => { console.error(e); process.exit(1); });

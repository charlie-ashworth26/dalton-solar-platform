/*
 * RUNTIME PROOF: what the server read off the bill reaches the form - and
 * stops at the first field the rep has touched.
 *
 * WHY A HARNESS AND NOT A PYTHON TEST
 *
 *   The Python suite proves the bill is READ correctly. This proves what the
 *   browser then DOES with it, which is where the rules that matter to a rep
 *   live: fill only what is empty, never overwrite a correction, never offer
 *   an identifier that would fail the same check manual entry is held to, and
 *   never let a prefill decide that an enrollment may continue.
 *
 *   It runs the real static/js/app.js against a DOM stub built from the real
 *   ids in templates/index.html, so a renamed field fails here rather than in
 *   front of a rep.
 *
 * Every value below is invented for this harness.
 *
 * node test/bill_prefill_harness.js
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
console.log('RUNTIME PROOF - WHAT THE BILL GAVE UP REACHES THE FORM');
console.log('='.repeat(72));

const ctx = vm.createContext(sandbox);
vm.runInContext(src, ctx, { filename: 'app.js' });
check('app.js evaluates without throwing', true);

function el(id) { return documentStub.getElementById(id); }
function run(code) { return vm.runInContext(code, ctx); }

// ── Synthetic bill values. Nothing here belongs to anyone. ──────────────────
const NY = {
  name: 'PAT Q SAMPLE', street: '742 EXAMPLE WAY', unit: 'APT 9',
  city: 'ANYTOWN', state: 'NY', zip: '14850',
  acct: '90000000011', pod: 'N01000000000001',
};
const VA = {
  name: 'ALEX SAMPLE', street: '12 PLACEHOLDER RD', city: 'ELSEWHERE',
  state: 'VA', zip: '23220', acct: '900000000012', meter: '8000000001',
};

function serverBody(fields) {
  return { extraction: { fields, status: 'success', issues: [] } };
}

/* Put the browser in the state it is in when a rep is on Customer & Bill for
   a given utility: the rules loaded, the conditional fields on screen. */
function forUtility(slug, rule, marketFields) {
  elCache.clear();
  run(`utilityRules = ${JSON.stringify({ [slug]: rule })};`);
  run(`perchContext.utilitySlug = ${JSON.stringify(slug)};`);
  run(`marketAccountFields = ${JSON.stringify(marketFields || [])};`);
  run('lastBillParse = null;');
  el('pod-id-field').style.display = rule.requires_pod_id ? 'block' : 'none';
  el('meter-number-field').style.display =
    (marketFields || []).some((f) => f.field_type === 'meter_number') ? 'block' : 'none';
  el('a-state').options = [{ value: 'NY' }, { value: 'VA' }];
  el('a-state').value = 'NY';         // the form's default, as shipped
}

const NYSEG_RULE = {
  slug: 'nyseg', display_name: 'NYSEG', requires_pod_id: true,
  account_number_length: 11,
  pod_id: { length: 15, prefix: 'N01', description: 'N01 followed by 12 digits' },
};
const DOMINION_RULE = {
  slug: 'dominion-energy-va', display_name: 'Dominion Energy Virginia',
  requires_pod_id: false, account_number_length: null, pod_id: null,
};
const DOMINION_FIELDS = [
  { field_type: 'account_number', field_name: 'Account number',
    patterns: ['\\d{10}', '\\d{12}'], help_text: '10 or 12 digits' },
  { field_type: 'meter_number', field_name: 'Meter number',
    patterns: ['\\d{10}'], help_text: '10 digits' },
];

// ════════════════════════════════════════════════════════════════════════════
section('1. AN EMPTY FORM IS FILLED FROM THE BILL');
// ════════════════════════════════════════════════════════════════════════════
forUtility('nyseg', NYSEG_RULE);
run(`applyServerBillFields(${JSON.stringify(serverBody({
  account_holder: NY.name, service_street: NY.street, service_unit: NY.unit,
  service_city: NY.city, service_state: NY.state, service_zip: NY.zip,
  account_number: NY.acct, pod_id: NY.pod,
}))});`);
check('first name', el('c-first').value === 'PAT Q');
check('last name', el('c-last').value === 'SAMPLE');
check('service street', el('a-street').value === NY.street);
check('unit', el('a-unit').value === NY.unit);
check('city', el('a-city').value === NY.city);
check('ZIP', el('a-zip').value === NY.zip);
check('account number', el('c-acct').value === NY.acct);
check('POD ID', el('c-pod').value === NY.pod);
check('each filled field is flashed so the rep can see what was read',
  el('c-acct').classList.contains('ocr-filled')
  && el('c-pod').classList.contains('ocr-filled'));
check('the panel now reports the POD ID as found on the bill',
  el('ocr-container').innerHTML.includes('POD ID')
  && !/POD ID[\s\S]{0,200}Not found/.test(el('ocr-container').innerHTML));

// ════════════════════════════════════════════════════════════════════════════
section('2. A VALUE THE REP HAS ALREADY PUT RIGHT IS NEVER OVERWRITTEN');
// ════════════════════════════════════════════════════════════════════════════
forUtility('nyseg', NYSEG_RULE);
el('c-acct').value = '11111111111';          // the rep typed this
el('a-street').value = '1 REP CORRECTED ST';
el('c-first').value = 'Corrected';
run(`applyServerBillFields(${JSON.stringify(serverBody({
  account_holder: NY.name, service_street: NY.street, service_city: NY.city,
  service_zip: NY.zip, service_state: NY.state,
  account_number: NY.acct, pod_id: NY.pod,
}))});`);
check('the corrected account number stands', el('c-acct').value === '11111111111');
check('the corrected street stands', el('a-street').value === '1 REP CORRECTED ST');
check('a half-typed name is not completed from the bill',
  el('c-first').value === 'Corrected' && el('c-last').value === '');
check('  ...while fields the rep left empty are still filled',
  el('a-city').value === NY.city && el('c-pod').value === NY.pod);

// Running it twice must change nothing: this is what a re-upload does.
const before = ['c-acct', 'a-street', 'a-city', 'c-pod'].map((i) => el(i).value);
run(`applyServerBillFields(${JSON.stringify(serverBody({
  account_number: '99999999999', service_street: 'OTHER ST', pod_id: 'N01999999999999',
}))});`);
check('a second reading cannot disturb what is already on the form',
  ['c-acct', 'a-street', 'a-city', 'c-pod'].map((i) => el(i).value).join('|')
  === before.join('|'));

// ════════════════════════════════════════════════════════════════════════════
section('3. AN IDENTIFIER THAT WOULD FAIL THE FORM IS NOT OFFERED');
// ════════════════════════════════════════════════════════════════════════════
forUtility('nyseg', NYSEG_RULE);
run(`applyServerBillFields(${JSON.stringify(serverBody({
  account_number: NY.acct, pod_id: 'R01000000000002',   // another utility's prefix
}))});`);
check('a POD ID the utility rule refuses is left blank', el('c-pod').value === '');
check('  ...and the panel says it still needs entering',
  /POD ID[\s\S]{0,200}Not found/.test(el('ocr-container').innerHTML));
check('  ...while the account number it did accept is filled',
  el('c-acct').value === NY.acct);

forUtility('dominion-energy-va', DOMINION_RULE, DOMINION_FIELDS);
run(`applyServerBillFields(${JSON.stringify(serverBody({
  account_number: VA.acct, meter_number: '80000001',    // too short
}))});`);
check('a meter number that fails the published pattern is left blank',
  el('c-meter').value === '');
check('  ...and the panel says so', /Meter number[\s\S]{0,200}Not found/
  .test(el('ocr-container').innerHTML));
check('  ...while the 12-digit account is accepted', el('c-acct').value === VA.acct);

// ════════════════════════════════════════════════════════════════════════════
section('3b. A FIELD THIS UTILITY DOES NOT ASK FOR IS NEVER FILLED');
// ════════════════════════════════════════════════════════════════════════════
// National Grid's own parse reads a meter number off the bill, and National
// Grid's form does not ask for one. Before this guard that value landed in a
// hidden input - data nobody saw and nobody agreed to.
forUtility('national-grid-ny',
  { slug: 'national-grid-ny', display_name: 'National Grid',
    requires_pod_id: false, account_number_length: 10, pod_id: null }, []);
run(`applyServerBillFields(${JSON.stringify(serverBody({
  account_holder: 'JOHN J CUSTOMER', account_number: '1234567890',
  service_street: '123 MAIN ST', service_city: 'ALBANY', service_zip: '12207',
  meter_number: '06885924', pod_id: 'N01000000000001',
}))});`);
check('a hidden meter field stays empty', el('c-meter').value === '');
check('a hidden POD field stays empty', el('c-pod').value === '');
check('  ...while the fields this utility does ask for are filled',
  el('c-acct').value === '1234567890' && el('a-city').value === 'ALBANY');

// ════════════════════════════════════════════════════════════════════════════
section('4. THE STATE FOLLOWS THE ADDRESS, AND NOTHING ELSE');
// ════════════════════════════════════════════════════════════════════════════
forUtility('dominion-energy-va', DOMINION_RULE, DOMINION_FIELDS);
run(`applyServerBillFields(${JSON.stringify(serverBody({
  service_street: VA.street, service_city: VA.city, service_zip: VA.zip,
  service_state: 'VA',
}))});`);
check('a Virginia bill moves the state off the NY default',
  el('a-state').value === 'VA');

forUtility('dominion-energy-va', DOMINION_RULE, DOMINION_FIELDS);
el('a-street').value = '9 REP TYPED LN';     // the rep entered the address
el('a-zip').value = '12207';
el('a-state').value = 'NY';
run(`applyServerBillFields(${JSON.stringify(serverBody({
  service_street: VA.street, service_city: VA.city, service_zip: VA.zip,
  service_state: 'VA',
}))});`);
check('an address the rep typed keeps the rep\'s state', el('a-state').value === 'NY');

forUtility('dominion-energy-va', DOMINION_RULE, DOMINION_FIELDS);
run(`applyServerBillFields(${JSON.stringify(serverBody({
  service_state: 'XX', service_street: VA.street, service_zip: VA.zip,
}))});`);
check('a state the form does not offer is ignored rather than forced in',
  el('a-state').value === 'NY');

// ════════════════════════════════════════════════════════════════════════════
section('5. READING A BILL NEVER DECIDES THAT AN ENROLLMENT MAY CONTINUE');
// ════════════════════════════════════════════════════════════════════════════
forUtility('nyseg', NYSEG_RULE);
el('billing-same').checked = true;
run('availablePrograms = []; selectedProgram = null;');
run("state.bill.fileName = 'bill.pdf';");   // the bill IS uploaded
run(`applyServerBillFields(${JSON.stringify(serverBody({
  account_holder: NY.name, service_street: NY.street, service_city: NY.city,
  service_zip: NY.zip, account_number: NY.acct,       // NO POD ID on this bill
}))});`);
run('checkBillReady();');
check('a NYSEG enrollment with no POD ID cannot continue',
  el('btn-bill-next').disabled === true);
check('  ...and the rep is told which field is missing',
  /N01|POD/i.test(el('bill-requirements').innerHTML));
el('c-pod').value = NY.pod;                 // the rep types it
run('checkBillReady();');
check('  ...and may continue once it is entered and valid',
  el('btn-bill-next').disabled === false);

// The same gate must hold against a POD ID that was READ rather than typed:
// the prefill is not a bypass.
forUtility('nyseg', NYSEG_RULE);
el('billing-same').checked = true;
run('availablePrograms = []; selectedProgram = null;');
run("state.bill.fileName = 'bill.pdf';");
run(`applyServerBillFields(${JSON.stringify(serverBody({
  account_holder: NY.name, service_street: NY.street, service_city: NY.city,
  service_zip: NY.zip, account_number: NY.acct, pod_id: 'R01000000000002',
}))});`);
run('checkBillReady();');
check('a POD ID the rule refuses never unlocks Continue',
  el('btn-bill-next').disabled === true && el('c-pod').value === '');

// ════════════════════════════════════════════════════════════════════════════
section('6. NOTHING READ, NOTHING BROKEN');
// ════════════════════════════════════════════════════════════════════════════
forUtility('nyseg', NYSEG_RULE);
let threw = null;
try {
  run('applyServerBillFields(null);');
  run('applyServerBillFields({});');
  run('applyServerBillFields({extraction:{fields:{}}});');
  run(`applyServerBillFields(${JSON.stringify(serverBody({
    _rejected_fields: ['pod_id'], _ambiguous_fields: ['meter_number'],
  }))});`);
} catch (e) { threw = e; }
check('an empty or failed reading changes nothing and raises nothing',
  !threw && el('c-acct').value === '' && el('c-pod').value === '');

console.log('\n' + '='.repeat(72));
const failed = results.filter((r) => !r.ok);
console.log(failed.length
  ? `BILL PREFILL - ${failed.length} of ${results.length} CHECKS FAILED`
  : `BILL PREFILL - ALL ${results.length} CHECKS PASSED`);
console.log('='.repeat(72));
failed.forEach((r) => console.log('  FAILED: ' + r.label));
process.exit(failed.length ? 1 : 0);

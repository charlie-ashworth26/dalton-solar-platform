/*
 * RUNTIME PROOF: the browser cannot lose a POD ID.
 *
 * THE SERVER HALF is asserted in test/test_pod_persistence.py - a save that
 * carries a blank POD no longer erases a stored one. This is the OTHER half:
 * the client must not send that blank in the first place, must not let OCR
 * overwrite a POD a rep typed, and must not drop one while re-rendering.
 *
 * Both halves exist because either alone leaves the hole open: a fixed client
 * does not protect an enrollment saved by an older tab still loaded in a
 * browser, and a fixed server does not stop the field going visibly blank on
 * screen while the rep is looking at it.
 *
 * Every value below is invented for this harness.
 *
 * node test/pod_persistence_harness.js
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

let routes = {};
const fetchLog = [];
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
  visibilityState: 'visible',
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
  fetch(url, opts) {
    fetchLog.push({
      url: String(url), method: (opts && opts.method) || 'GET',
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
console.log('RUNTIME PROOF - THE BROWSER CANNOT LOSE A POD ID');
console.log('='.repeat(72));

const ctx = vm.createContext(sandbox);
vm.runInContext(src, ctx, { filename: 'app.js' });
check('app.js evaluates without throwing', true);

function el(id) { return documentStub.getElementById(id); }
const el2 = el;
function run(code) { return vm.runInContext(code, ctx); }

// ── Synthetic values. Nothing here belongs to anyone. ──────────────────────
const NYSEG_RULE = {
  slug: 'nyseg', display_name: 'NYSEG', requires_pod_id: true,
  account_number_lengths: [11], account_number_description: '11 digits',
  pod_id: { required: true, length: 15, prefix: 'N01',
            description: '15 digits, starting with N01' },
};
const RGE_RULE = {
  slug: 'rochester-gas-electric', display_name: 'Rochester Gas & Electric',
  requires_pod_id: true, account_number_lengths: [11],
  account_number_description: '11 digits',
  pod_id: { required: true, length: 15, prefix: 'R01',
            description: '15 digits, starting with R01' },
};
const POD = { nyseg: 'N01000000000001', rge: 'R01000000000002' };
const TYPED = { nyseg: 'N01000000000099', rge: 'R01000000000099' };
const ACCOUNT = '90000000011';

function onUtility(rule) {
  elCache.clear();
  run(`utilityRules = ${JSON.stringify({ [rule.slug]: rule })};`);
  run(`perchContext.utilitySlug = ${JSON.stringify(rule.slug)};`);
  run('marketAccountFields = null;');
  run('lastBillParse = null;');
  run('availablePrograms = []; selectedProgram = null;');
  run('state.customer = Object.assign(state.customer || {}, {first:"",last:"",email:"",phone:"",acct:"",podId:"",meterNumber:""});');
  run('state.address = Object.assign(state.address || {}, {street:"",unit:"",city:"",state:"NY",zip:""});');
  run("state.bill = Object.assign(state.bill || {}, {fileName:'', documentId:null, uploadError:null});");
  // A progress save only exists for a real draft, and never after submission.
  run('currentDraft = {enrollment_id: 7};');
  run('perchContext.enrollmentSubmitted = false;');
  el('pod-id-field').style.display = rule.pod_id ? 'block' : 'none';
  el('meter-number-field').style.display = 'none';
  el('a-state').options = [{ value: 'NY' }, { value: 'VA' }];
  el('a-state').value = 'NY';
  el('billing-same').checked = true;
}

function fillForm(pod) {
  el('c-first').value = 'Dana';
  el('c-last').value = 'Reyes';
  el('c-email').value = 'dana@example.com';
  el('c-acct').value = ACCOUNT;
  el('a-street').value = '742 Example Way';
  el('a-city').value = 'Ithaca';
  el('a-zip').value = '14850';
  if (pod !== undefined) el('c-pod').value = pod;
}

function serverBody(fields) {
  return { extraction: { fields, status: 'success', issues: [] } };
}

// ════════════════════════════════════════════════════════════════════════════
section('1. OCR NEVER OVERWRITES A POD THE REP TYPED');
// ════════════════════════════════════════════════════════════════════════════
[['NYSEG', NYSEG_RULE, POD.nyseg, TYPED.nyseg],
 ['RG&E', RGE_RULE, POD.rge, TYPED.rge]].forEach(function (row) {
  const [name, rule, fromBill, typed] = row;
  onUtility(rule);
  fillForm(typed);                       // the rep typed this one
  run(`applyServerBillFields(${JSON.stringify(serverBody({
    pod_id: fromBill, account_number: ACCOUNT,
  }))});`);
  check(`${name}: a typed POD survives a bill that says otherwise `
    + `(${el('c-pod').value})`, el('c-pod').value === typed);

  // And the ordinary case still works: an EMPTY field is filled.
  onUtility(rule);
  fillForm('');
  run(`applyServerBillFields(${JSON.stringify(serverBody({ pod_id: fromBill }))});`);
  check(`${name}: an empty POD is filled from the bill (${el('c-pod').value})`,
    el('c-pod').value === fromBill);

  // The rep corrects it, then uploads a SECOND bill carrying the original.
  el('c-pod').value = typed;
  run(`applyServerBillFields(${JSON.stringify(serverBody({ pod_id: fromBill }))});`);
  check(`${name}: a correction survives a second upload (${el('c-pod').value})`,
    el('c-pod').value === typed);

  // A bill that reads no POD at all must not blank one.
  run(`applyServerBillFields(${JSON.stringify(serverBody({
    account_number: ACCOUNT,
  }))});`);
  check(`${name}: a bill with no POD does not blank the field`,
    el('c-pod').value === typed);
  run('applyServerBillFields({extraction:{fields:{}}});');
  check(`${name}: nor does an extraction that read nothing`,
    el('c-pod').value === typed);
});

// ════════════════════════════════════════════════════════════════════════════
section('2. A SAVE CARRIES THE POD, OR CARRIES NOTHING ABOUT IT');
// ════════════════════════════════════════════════════════════════════════════
onUtility(NYSEG_RULE);
fillForm(POD.nyseg);
run('captureCustomerFields();');
let payload = run('buildCustomerProgress()');
check('a progress save carries the POD when there is one',
  payload.utility_account.secondary_account_identifier === POD.nyseg);

onUtility(NYSEG_RULE);
fillForm('');                            // the field is empty, as on first load
run('captureCustomerFields();');
payload = run('buildCustomerProgress()');
check('THE KEY IS ABSENT when the field is empty - not sent as blank',
  payload.utility_account
  && !('secondary_account_identifier' in payload.utility_account));
check('  ...while the rest of the account still saves',
  payload.utility_account.utility_name === 'nyseg');

// The specific trap: the POD field is HIDDEN because the utility rules have
// not loaded yet, so it is empty for a reason that has nothing to do with the
// rep. A save made now must not claim the POD is blank.
onUtility(NYSEG_RULE);
fillForm('');
el('pod-id-field').style.display = 'none';
run('captureCustomerFields();');
payload = run('buildCustomerProgress()');
check('a save from a screen with the POD field hidden says nothing about it',
  payload.utility_account
  && !('secondary_account_identifier' in payload.utility_account));

// ════════════════════════════════════════════════════════════════════════════
section('3. MOVING AROUND THE WIZARD KEEPS IT');
// ════════════════════════════════════════════════════════════════════════════
onUtility(NYSEG_RULE);
fillForm(POD.nyseg);
run('captureCustomerFields();');
check('leaving the step captures the POD into state',
  run('state.customer.podId') === POD.nyseg);
el('c-pod').value = '';                  // the step is re-rendered blank
run('hydrateStep(2);');
check(`coming back repaints it from state (${el('c-pod').value})`,
  el('c-pod').value === POD.nyseg);
run('captureCustomerFields();');
check('  ...and capturing again does not lose it',
  run('state.customer.podId') === POD.nyseg);

// A re-render while the field happens to be empty must not wipe state: an
// empty input means "nothing typed here", never "clear what you know".
el('c-pod').value = '';
run('captureCustomerFields();');
check('an empty input does not erase the captured POD',
  run('state.customer.podId') === POD.nyseg);

// ════════════════════════════════════════════════════════════════════════════
section('4. REOPENING AN ENROLLMENT - THE ROOT CAUSE ITSELF');
// ════════════════════════════════════════════════════════════════════════════
// THE DEFECT: loadUtilityRules() ran when an enrollment was STARTED and not
// when one was REOPENED. A rep who pressed Resume had an empty rules map, so
// the POD field was hidden on a utility that requires one, podLooksValid()
// answered true for anything, and the next save wrote that invisible blank
// over the stored value. This drives the real reopen path with the rules
// deliberately NOT pre-loaded, which is the state a Resume starts from.
(async function reopen() {
  elCache.clear();
  run('utilityRules = {};');            // exactly as a fresh page load has it
  run('perchContext.utilitySlug = null;');
  run('currentDraft = null;');
  run('state.customer = Object.assign(state.customer || {}, {first:"",last:"",email:"",acct:"",podId:"",meterNumber:""});');
  routes = {
    '/api/perch/utilities': { body: { utilities: [NYSEG_RULE, RGE_RULE] } },
    '/api/enrollments/7': { body: {
      id: 7, enrollment_code: 'ENR-7', status: 'Draft',
      customer: { first_name: 'Dana', last_name: 'Reyes', email: 'dana@example.com' },
      service_address: { street: '742 Example Way', city: 'Ithaca', state: 'NY', zip: '14850' },
      utility_account: {
        utility_name: 'nyseg', account_number: '******0011',
        secondary_account_identifier: POD.nyseg, meter_number: null,
      },
      workflow_step_key: 'enroll', workflow_is_terminal: false,
      workflow_is_blocked: false, perch_committed: false,
      documents: [], document_sets: {},
    } },
    '/api/enrollments': { body: [] },
    '/api/perch': { body: {} },
  };
  await run('openEnrollment(7)');
  for (let i = 0; i < 8; i += 1) await new Promise((r) => setImmediate(r));

  check('reopening loads the utility rules it used to skip',
    !!run('utilityRules && utilityRules.nyseg'));
  check('  ...so the POD field is on screen for a utility that needs one',
    el('pod-id-field').style.display === 'block');
  check(`  ...the stored POD is in state (${run('state.customer.podId')})`,
    run('state.customer.podId') === POD.nyseg);
  check(`  ...and painted on the form (${el('c-pod').value})`,
    el('c-pod').value === POD.nyseg);
  check('  ...while the account number is still the mask it was served',
    el('c-acct').value === '******0011');

  // The save made straight after a reopen is the one that used to do the damage.
  run('captureCustomerFields();');
  const after = run('buildCustomerProgress()');
  check('a save straight after reopen carries the POD, not a blank',
    after && after.utility_account
    && after.utility_account.secondary_account_identifier === POD.nyseg);
  check('  ...and leaves the masked account number out entirely',
    after && after.utility_account
    && !('account_number' in after.utility_account));

  // And with the rules loaded, an empty POD is correctly refused rather than
  // silently allowed through - the other half of what the missing load broke.
  el('c-pod').value = '';
  run("state.bill.fileName = 'bill.pdf';");
  run('checkBillReady();');
  check('an empty POD now blocks Continue, as it could not before',
    el('btn-bill-next').disabled === true);
  run('captureCustomerFields();');
  const blanked = run('buildCustomerProgress()');
  // THE INVARIANT IS "NEVER A BLANK", not "never the key". After a reopen the
  // captured state legitimately still holds the stored POD - captureCustomerFields
  // is additive precisely so that a field cleared by a re-render cannot erase
  // what the rep never touched - so the save carries the stored value, which
  // writes the same thing that is already there. Either shape is safe; a blank
  // is the only one that is not.
  const sentPod = blanked && blanked.utility_account
    ? blanked.utility_account.secondary_account_identifier : undefined;
  check(`  ...and no blank is transmitted for it (${JSON.stringify(sentPod)})`,
    sentPod === undefined || sentPod === POD.nyseg);
  runRemaining();
})();

function runRemaining() {
// ════════════════════════════════════════════════════════════════════════════
// ════════════════════════════════════════════════════════════════════════════
section('5. A BAD POD STILL BLOCKS CONTINUE');
// ════════════════════════════════════════════════════════════════════════════
[['NYSEG', NYSEG_RULE, POD.nyseg, POD.rge],
 ['RG&E', RGE_RULE, POD.rge, POD.nyseg]].forEach(function (row) {
  const [name, rule, good, wrongPrefix] = row;
  onUtility(rule);
  fillForm('');
  run("state.bill.fileName = 'bill.pdf';");
  run('checkBillReady();');
  check(`${name}: no POD, no Continue`, el('btn-bill-next').disabled === true);
  check('  ...and the reason names the rule',
    /POD ID/.test(el('bill-requirements').innerHTML));
  el('c-pod').value = wrongPrefix;
  run('checkBillReady();');
  check(`${name}: the other utility's prefix does not unlock it`,
    el('btn-bill-next').disabled === true);
  el('c-pod').value = good.slice(0, 10);
  run('checkBillReady();');
  check(`${name}: nor does the right prefix at the wrong length`,
    el('btn-bill-next').disabled === true);
  el('c-pod').value = good;
  run('checkBillReady();');
  check(`${name}: the correct POD unlocks Continue`,
    el('btn-bill-next').disabled === false);
});

// ════════════════════════════════════════════════════════════════════════════
section('6. SWITCHING UTILITY DOES NOT SILENTLY DROP IT');
// ════════════════════════════════════════════════════════════════════════════
onUtility(NYSEG_RULE);
fillForm(POD.nyseg);
run('captureCustomerFields();');
// The rules for the other Avangrid utility arrive and the step reconfigures.
run(`utilityRules = ${JSON.stringify({ nyseg: NYSEG_RULE, 'rochester-gas-electric': RGE_RULE })};`);
run("perchContext.utilitySlug = 'rochester-gas-electric';");
run('configureBillUtilityRules();');
check('reconfiguring for another utility leaves the value on screen',
  el('c-pod').value === POD.nyseg);
check('  ...and the POD field stays visible for a utility that needs one',
  el('pod-id-field').style.display === 'block');
run('checkBillReady();');
check('  ...but the now-wrong prefix blocks Continue rather than being erased',
  el('btn-bill-next').disabled === true && el('c-pod').value === POD.nyseg);

// ════════════════════════════════════════════════════════════════════════════
section('6b. THE FIELD THE BACKEND NAMES IS THE FIELD THAT LIGHTS UP');
// ════════════════════════════════════════════════════════════════════════════
// The backend returns a UBS input id for a rejected enrollment, apiFetch
// attaches the parsed body to the error - and nothing read it, so the rep got
// a sentence about the POD ID with no indication of which box it meant.
onUtility(NYSEG_RULE);
fillForm(POD.nyseg);
[['c-pod', 'the POD ID'], ['c-acct', 'the account number'],
 ['c-meter', 'the meter number'], ['c-email', 'the email address']]
  .forEach(function (row) {
    const [id, label] = row;
    run('clearFieldErrors();');
    const handled = run(`highlightServerField({body: {field: ${JSON.stringify(id)}}})`);
    const el = el2(id);
    check(`${label} is marked when the backend names it`,
      handled === true && !!el && el.classList.contains('err'));
  });

// A file input cannot carry a focus ring, so its dropzone is marked instead.
['bill-file', 'lmi-file'].forEach(function (id) {
  run('clearFieldErrors();');
  const handled = run(`highlightServerField({body: {field: ${JSON.stringify(id)}}})`);
  const zone = el2(id === 'bill-file' ? 'bill-dropzone' : 'lmi-dropzone');
  check(`${id} marks its dropzone rather than the hidden input`,
    handled === true && !!zone && zone.classList.contains('err'));
});

run('clearFieldErrors();');
check('an error with no field changes nothing',
  run('highlightServerField({body: {}})') === false
  && !el2('c-pod').classList.contains('err'));
check('  ...and nor does one naming a field this screen does not have',
  run("highlightServerField({body: {field: 'not-a-real-input'}})") === false);
check('  ...nor an error with no body at all',
  run('highlightServerField(new Error("x"))') === false);

// Starting another submit clears the previous highlight, so a rep never sees
// two fields marked from two different attempts.
run("highlightServerField({body: {field: 'c-acct'}});");
check('precondition: the account field is marked',
  el2('c-acct').classList.contains('err'));
run('clearFieldErrors();');
check('a fresh submit clears the previous marking',
  !el2('c-acct').classList.contains('err'));
check('  ...and submitBill clears it before trying again',
  /clearFieldErrors\(\);/.test(
    src.split('async function submitBill')[1].slice(0, 400)));
check('  ...and applies the new one when the attempt fails',
  /highlightServerField\(err\);/.test(src));
check('the dropzone marking has a visible style behind it',
  /\.upload\.err\{[^}]*border-color:var\(--danger\)/.test(
    require('fs').readFileSync(
      require('path').join(ROOT, 'static', 'css', 'app.css'), 'utf8')));

// ════════════════════════════════════════════════════════════════════════════
section('7. THE BILL PANEL REPORTS THE POD WITHOUT TOUCHING IT');
// ════════════════════════════════════════════════════════════════════════════
onUtility(NYSEG_RULE);
fillForm(TYPED.nyseg);
run('lastBillParse = {first:"Dana", street:"742 Example Way", acct:"' + ACCOUNT + '"};');
run('renderBillReadPanel();');
check('re-rendering the panel does not change the field',
  el('c-pod').value === TYPED.nyseg);
check('  ...and a POD the rep typed is reported as entered manually',
  /POD ID/.test(el('ocr-container').innerHTML)
  && /Entered manually/.test(el('ocr-container').innerHTML));

// Removing the bill is about the document, not the identifiers.
run('removeBill();');
check('removing the bill leaves the POD alone', el('c-pod').value === TYPED.nyseg);
check('  ...and the account number too', el('c-acct').value === ACCOUNT);

  finish();
}

function finish() {
  console.log('\n' + '='.repeat(72));
  const failed = results.filter((r) => !r.ok);
  console.log(failed.length
    ? `POD PERSISTENCE - ${failed.length} of ${results.length} CHECKS FAILED`
    : `POD PERSISTENCE - ALL ${results.length} CHECKS PASSED`);
  console.log('='.repeat(72));
  failed.forEach((r) => console.log('  FAILED: ' + r.label));
  process.exit(failed.length ? 1 : 0);
}

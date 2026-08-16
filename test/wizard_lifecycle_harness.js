/*
 * Wizard lifecycle runtime harness.
 *
 * WHY THIS EXISTS
 * ---------------
 * The legacy contract-engine cleanup deleted DOM lookups like
 *     const accStatus = document.getElementById('contract-accept-status');
 * but left the following usage line intact, producing
 *     ReferenceError: accStatus is not defined
 * the instant resetWizardState() ran - which is the first thing both
 * "New enrollment" and "Start from project" do. The app's primary action was
 * completely dead.
 *
 * Every existing frontend assertion was a SUBSTRING check against app.js
 * source. Substring checks confirm a symbol is mentioned; they cannot detect an
 * identifier that is used but never bound. `new Function(src)` only validates
 * syntax. GET / only proves the HTML serves.
 *
 * So this harness ACTUALLY EXECUTES the wizard lifecycle functions against a
 * minimal DOM stub. An unbound identifier throws a real ReferenceError and
 * fails the test, exactly as it would in the browser.
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const APP_JS = path.join(ROOT, 'static', 'js', 'app.js');
const INDEX_HTML = path.join(ROOT, 'templates', 'index.html');

const src = fs.readFileSync(APP_JS, 'utf8');
const html = fs.readFileSync(INDEX_HTML, 'utf8');

// Every id present in the real template, so getElementById returns a stub for
// the ones that exist and null for the ones that don't - matching the browser.
const REAL_IDS = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));

let results = [];
function check(label, cond) {
  results.push({ label, ok: !!cond });
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${label}`);
}

function makeEl(id) {
  const el = {
    id,
    value: '',
    checked: false,
    disabled: false,
    textContent: '',
    innerHTML: '',
    title: '',
    type: 'text',
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    setAttribute() {},
    getAttribute() { return null; },
    removeAttribute() {},
    appendChild() {},
    insertBefore() {},
    removeChild() {},
    addEventListener() {},
    removeEventListener() {},
    focus() {},
    click() {},
    closest() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    scrollIntoView() {},
    getContext() { return null; },
    reset() {},
  };
  el.parentNode = null;
  el.firstChild = null;
  return el;
}

const elCache = new Map();
const documentStub = {
  getElementById(id) {
    if (!REAL_IDS.has(id)) return null;   // exactly like the browser
    if (!elCache.has(id)) elCache.set(id, makeEl(id));
    return elCache.get(id);
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement(tag) { return makeEl('created-' + tag); },
  addEventListener() {},
  removeEventListener() {},
  body: makeEl('body'),
  documentElement: makeEl('html'),
  activeElement: null,
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
  console,
  document: documentStub,
  sessionStorage: storageStub,
  localStorage: storageStub,
  setTimeout() { return 0; },
  clearTimeout() {},
  setInterval() { return 0; },
  clearInterval() {},
  alert() {},
  confirm() { return true; },
  fetch() { return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) }); },
  FormData: function () { this.append = function () {}; },
  FileReader: function () { this.readAsDataURL = function () {}; },
  Blob: function () {},
  URL: { createObjectURL() { return 'blob:stub'; }, revokeObjectURL() {} },
  navigator: { userAgent: 'harness' },
  location: { href: '', reload() {} },
  requestAnimationFrame(cb) { return 0; },
  pdfjsLib: { getDocument() { return { promise: Promise.resolve({ numPages: 0 }) }; }, GlobalWorkerOptions: {} },
  Tesseract: { recognize() { return Promise.resolve({ data: { text: '' } }); } },
};
sandbox.scrollTo = function () {};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

console.log('='.repeat(72));
console.log('WIZARD LIFECYCLE RUNTIME HARNESS');
console.log('='.repeat(72));

let ctx;
try {
  ctx = vm.createContext(sandbox);
  vm.runInContext(src, ctx, { filename: 'app.js' });
  check('app.js evaluates without throwing', true);
} catch (e) {
  check('app.js evaluates without throwing — ' + e.message, false);
  report();
}

function callFn(name, args) {
  const fn = sandbox[name];
  if (typeof fn !== 'function') return { missing: true };
  try {
    const out = fn.apply(sandbox, args || []);
    if (out && typeof out.then === 'function') {
      // Surface async ReferenceErrors rather than letting them become
      // "Uncaught (in promise)" like the reported bug.
      return { promise: out };
    }
    return { ok: true };
  } catch (e) {
    return { error: e };
  }
}

console.log('\n' + '-'.repeat(72));
console.log('THE REPORTED REGRESSION');
console.log('-'.repeat(72));

const reset = callFn('resetWizardState');
check('resetWizardState() exists', !reset.missing);
check('resetWizardState() runs without a ReferenceError',
  !reset.error || reset.error.name !== 'ReferenceError');
if (reset.error) console.log('        -> ' + reset.error.name + ': ' + reset.error.message);
check('resetWizardState() runs without ANY error', !reset.error);

const clear = callFn('clearWizardForms');
check('clearWizardForms() runs without a ReferenceError',
  !clear.error || clear.error.name !== 'ReferenceError');
if (clear.error) console.log('        -> ' + clear.error.name + ': ' + clear.error.message);

console.log('\n' + '-'.repeat(72));
console.log('LIFECYCLE ENTRY POINTS');
console.log('-'.repeat(72));

const lifecycle = [
  ['unlockEnrollmentControls', []],
  ['freshPerchContext', []],
  ['exitWizard', []],
  ['goStep', [1]],
  ['goStep', [5]],
  ['updateAgreeButton', []],
  ['renderAgreementCard', []],
  ['mountAgreements', [{ actor: 'rep', enrollmentId: 1, contracts: [], summary: {} }]],
  ['mountAgreements', [{ actor: 'customer', enrollmentId: 1, contracts: [{ contract_name: 'X' }], summary: { customerName: 'A' } }]],
  ['lockEnrollmentReadOnly', [false]],
  ['lockEnrollmentReadOnly', [true]],
  ['closeAgreements', []],
];

for (const [name, args] of lifecycle) {
  const r = callFn(name, args);
  if (r.missing) { check(`${name}() is defined`, false); continue; }
  const bad = r.error && r.error.name === 'ReferenceError';
  check(`${name}() runs without a ReferenceError`, !bad);
  if (r.error) console.log(`        -> ${r.error.name}: ${r.error.message}`);
}

console.log('\n' + '-'.repeat(72));
console.log('ASYNC ENTRY POINTS (the reported stack: startWizardFresh / ForProject)');
console.log('-'.repeat(72));

const asyncFns = [
  ['startWizardFresh', []],
  ['startWizardForProject', [{ id: 1, name: 'P' }]],
  ['openEnrollment', [1]],
  ['openCustomerContracts', []],
  ['submitAgreements', []],
];

(async () => {
  for (const [name, args] of asyncFns) {
    const r = callFn(name, args);
    if (r.missing) { check(`${name}() is defined`, false); continue; }
    let err = r.error || null;
    if (r.promise) {
      try { await r.promise; } catch (e) { err = e; }
    }
    const bad = err && err.name === 'ReferenceError';
    check(`${name}() runs without a ReferenceError`, !bad);
    if (err && bad) console.log(`        -> ${err.name}: ${err.message}`);
  }

  console.log('\n' + '-'.repeat(72));
  console.log('NO STALE LEGACY IDENTIFIERS REMAIN BOUND OR REFERENCED');
  console.log('-'.repeat(72));
  for (const gone of ['docReviewed', 'sigCtx', 'isDrawing', 'hasSigned',
                      'renderDocPacket', 'allDocsReviewed', 'initSigCanvas',
                      'checkCustomerReady', 'enterCustomerSign',
                      'renderPerchContracts', 'acceptPerchContracts',
                      'updateAcceptButtonState', 'customerAcceptInFlight']) {
    check(`legacy '${gone}' is not defined in the runtime`,
      typeof sandbox[gone] === 'undefined');
  }

  console.log('\n' + '-'.repeat(72));
  console.log('SHARED AGREEMENT COMPONENT IS PRESENT AT RUNTIME');
  console.log('-'.repeat(72));
  for (const fn of ['mountAgreements', 'closeAgreements', 'openAgreementDoc',
                    'updateAgreeButton', 'submitAgreements',
                    'renderAgreementCard', 'agreementLinksHtml']) {
    check(`${fn}() is defined at runtime`, typeof sandbox[fn] === 'function');
  }
  // `const Agreements` is block-scoped, so it never lands on the sandbox object
  // even though it exists in the script scope. Probe it through evaluated code.
  let agrKind = 'undefined';
  try {
    agrKind = vm.runInContext('typeof Agreements', ctx);
  } catch (e) { agrKind = 'error:' + e.message; }
  check('single Agreements state object exists at runtime', agrKind === 'object');
  let agrShape = null;
  try {
    agrShape = vm.runInContext('Object.keys(Agreements).sort().join(",")', ctx);
  } catch (e) {}
  check('Agreements carries the shared component state',
    !!agrShape && agrShape.includes('acceptanceEnabled') && agrShape.includes('actor'));

  report();
})();

function report() {
  const failed = results.filter((r) => !r.ok);
  console.log('\n' + '='.repeat(72));
  console.log(`${results.length - failed.length} passed, ${failed.length} failed`);
  console.log('='.repeat(72));
  if (failed.length) {
    failed.forEach((f) => console.log('  FAILED: ' + f.label));
    process.exit(1);
  }
  process.exit(0);
}

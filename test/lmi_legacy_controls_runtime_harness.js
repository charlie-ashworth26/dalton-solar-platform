/*
 * RUNTIME PROOF: the legacy LMI mode-selector cannot become visible or
 * interactive on any current workflow path.
 *
 * WHY THIS EXISTS AND WHY IT IS NOT A SOURCE CHECK
 * ------------------------------------------------
 * "The tabs are hidden" is a claim about what happens when code RUNS, in an
 * order that depends on which path the rep took. Grepping app.js cannot prove
 * it. This harness found a real hazard that a source check would have missed:
 * resetWizardState() used to set all three toggles back to display:'flex',
 * so after any wizard reset they were visible in the DOM until something
 * happened to call renderLmiSections() again.
 *
 * METHOD
 *   1. Enumerate EVERY entry path that can reach the Eligibility screen and
 *      every function that writes to these controls.
 *   2. Execute each one, in isolation and in sequence.
 *   3. After each, assert all three toggles and the legacy attest panel are
 *      display:none.
 *   4. Then INVOKE the controls' own onclick handlers, exactly as a click
 *      would - including from states a rep could not normally reach - and
 *      assert they still cannot reveal themselves or switch anything
 *      exclusive.
 *
 * "Not interactive" is proved by consequence, not by a disabled attribute:
 * firing the handler must not reveal the row, must not hide the proof-document
 * section, and must not hide the self-attestation section.
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
function section(t) { console.log('\n' + '-'.repeat(72) + '\n' + t + '\n' + '-'.repeat(72)); }

function makeEl(id) {
  const el = {
    id, value: '', checked: false, disabled: false, textContent: '', innerHTML: '',
    title: '', type: 'text', style: {}, dataset: {}, options: [],
    classList: {
      _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, on) { if (on === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); } else if (on) { this._s.add(c); } else { this._s.delete(c); } },
      contains(c) { return this._s.has(c); },
    },
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

let routes = {};
const fetchLog = [];
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
  _d: {}, getItem(k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; },
  setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; }, clear() { this._d = {}; },
};
storageStub.setItem('dalton_auth_token', 'harness-token');

const sandbox = {
  console, document: documentStub, sessionStorage: storageStub, localStorage: storageStub,
  setTimeout() { return 0; }, clearTimeout() {}, setInterval() { return 0; }, clearInterval() {},
  alert() {}, confirm() { return true; },
  fetch(url, opts) {
    fetchLog.push({ url: String(url), method: (opts && opts.method) || 'GET' });
    for (const key of Object.keys(routes)) {
      if (String(url).includes(key)) {
        const r = routes[key];
        return Promise.resolve({ ok: (r.status || 200) === 200, status: r.status || 200,
          json: () => Promise.resolve(r.body || {}) });
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
console.log('RUNTIME PROOF - THE LEGACY LMI MODE SELECTOR IS UNREACHABLE');
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

const LEGACY = ['lmi-mode-doc', 'lmi-mode-attest', 'lmi-mode-na', 'lmi-attest-panel'];
const REFERENCE = {
  table_caption: 'NY 80% SMI',
  thresholds: [{ occupancy: 3, income_level_cents: 7935000 }],
  counties: ['Albany County'], already_generated: false, already_accepted: false, submitted: null,
};

function el(id) { return documentStub.getElementById(id); }

/* Visible means "not display:none". An element whose style was never touched
   has style.display === undefined, which in a browser means it INHERITS its
   CSS - so undefined counts as visible here, not as hidden. That is the strict
   reading, and it is what caught the resetWizardState hazard. */
function isVisible(id) {
  const e = el(id);
  if (!e) return false;
  return e.style.display !== 'none';
}
function hiddenReport() {
  return LEGACY.filter(isVisible);
}
function assertAllHidden(afterWhat) {
  const visible = hiddenReport();
  check(`after ${afterWhat}: all legacy controls hidden (visible: ${visible.join(', ') || 'none'})`,
    visible.length === 0);
}

/* Deliberately dirty every legacy control first, so a path that simply never
   touches them cannot pass by accident. */
function dirtyLegacyControls() {
  LEGACY.forEach(function (id) { el(id).style.display = 'flex'; });
  el('lmi-mode-doc').classList.add('selected');
}

function prime(stepKey) {
  fetchLog.length = 0;
  routes = { '/lmi/self-attestation/reference': { status: 200, body: REFERENCE } };
  vm.runInContext('currentDraft = {enrollment_id: 909, enrollment_code: "ENR-909"};', ctx);
  vm.runInContext('perchContext.nextStepKey = ' + JSON.stringify(stepKey) + ';', ctx);
  vm.runInContext('perchContext.proofSubmitted = false; perchContext.contractsGenerated = false;', ctx);
  vm.runInContext('selfAttestation = {reference:null, occupancy:null, county:"", choice:null};', ctx);
  vm.runInContext('state.lmi = {mode:"doc", docType:"", fileName:"", documentId:null, '
    + 'nameOnDocument:"", relationship:"self", documentFormat:""};', ctx);
  ['sa-county', 'sa-occupancy'].forEach((id) => { const e = el(id); e.options = []; e.innerHTML = ''; e.value = ''; });
  dirtyLegacyControls();
}

async function run() {
  // ══════════════════════════════════════════════════════════════════
  section('A. EVERY ENTRY PATH INTO THE ELIGIBILITY SCREEN');

  const STEP_KEYS = ['proof_docs', 'self_attestation', 'self_attestation_accept',
    'contracts', 'contracts_accepted', 'enroll', 'service_area', 'capacity_result',
    'unknown_next_step', 'some_future_perch_step', null, undefined];

  for (const key of STEP_KEYS) {
    prime(key);
    vm.runInContext('goStep(4);', ctx);
    await new Promise((r) => setImmediate(r));
    assertAllHidden(`goStep(4) with next_step=${JSON.stringify(key)}`);
  }

  section('  ...via continueFromPerchNextStep(), the real router');
  for (const key of ['proof_docs', 'self_attestation', 'self_attestation_accept']) {
    prime(key);
    try { await sandbox.continueFromPerchNextStep('lmi'); } catch (e) { /* downstream render */ }
    await new Promise((r) => setImmediate(r));
    assertAllHidden(`continueFromPerchNextStep with next_step=${key}`);
  }

  section('  ...via prepareLmiForPerch(), the proof-doc entry');
  prime('proof_docs');
  sandbox.prepareLmiForPerch();
  assertAllHidden('prepareLmiForPerch()');

  section('  ...via prepareSelfAttestation(), the attestation entry');
  prime('self_attestation');
  await sandbox.prepareSelfAttestation();
  assertAllHidden('prepareSelfAttestation()');

  section('  ...via renderLmiSections() alone');
  for (const key of ['proof_docs', 'self_attestation', 'contracts', null]) {
    prime(key);
    sandbox.renderLmiSections();
    assertAllHidden(`renderLmiSections() with next_step=${JSON.stringify(key)}`);
  }

  // ══════════════════════════════════════════════════════════════════
  section('B. THE RESET PATH - the one that used to REVEAL them');
  prime('proof_docs');
  vm.runInContext('resetWizardState();', ctx);
  assertAllHidden('resetWizardState()');
  check('  ...and it no longer sets them to flex', !/lmi-mode-doc'\)\.style\.display='flex'/.test(src));

  section('  ...reset THEN entering the screen');
  prime('self_attestation');
  vm.runInContext('resetWizardState();', ctx);
  vm.runInContext('perchContext.nextStepKey = "self_attestation";', ctx);
  vm.runInContext('currentDraft = {enrollment_id: 909};', ctx);
  vm.runInContext('goStep(4);', ctx);
  await new Promise((r) => setImmediate(r));
  assertAllHidden('resetWizardState() followed by goStep(4)');

  section('  ...and a fresh wizard start');
  if (typeof sandbox.startWizardFresh === 'function') {
    prime('proof_docs');
    try { sandbox.startWizardFresh(); } catch (e) { /* needs more of the page */ }
    await new Promise((r) => setImmediate(r));
    assertAllHidden('startWizardFresh()');
  }

  // ══════════════════════════════════════════════════════════════════
  section('C. FIRING THE CONTROLS\' OWN onclick HANDLERS');
  // The markup still carries onclick="setLmiMode('doc'|'attest'|'na')".
  // Invoking those is exactly what a click does.
  const ONCLICKS = [...html.matchAll(/id="(lmi-mode-[a-z]+)"[^>]*onclick="([^"]+)"/g)]
    .map((m) => ({ id: m[1], handler: m[2] }));
  check(`found all three onclick handlers in the markup (${ONCLICKS.length})`,
    ONCLICKS.length === 3);

  for (const step of ['proof_docs', 'self_attestation', 'self_attestation_accept', 'contracts']) {
    for (const oc of ONCLICKS) {
      prime(step);
      sandbox.renderLmiSections();
      const docBefore = isVisible('lmi-doc-panel');
      const saBefore = isVisible('sa-panel');
      vm.runInContext(oc.handler + ';', ctx);      // the literal click
      await new Promise((r) => setImmediate(r));
      const visible = hiddenReport();
      check(`${oc.handler} on ${step}: reveals nothing (visible: ${visible.join(', ') || 'none'})`,
        visible.length === 0);
      check(`  ...and does not hide the proof-document section`,
        isVisible('lmi-doc-panel') === docBefore);
      check(`  ...nor the self-attestation section`,
        isVisible('sa-panel') === saBefore);
    }
  }

  section('  ...repeated clicking cannot toggle them on');
  prime('self_attestation');
  sandbox.renderLmiSections();
  for (let i = 0; i < 10; i++) {
    vm.runInContext("setLmiMode('attest');", ctx);
    vm.runInContext("setLmiMode('na');", ctx);
    vm.runInContext("setLmiMode('doc');", ctx);
  }
  assertAllHidden('30 alternating setLmiMode() calls');
  check('  ...the self-attestation section is still the one on screen',
    isVisible('sa-panel') && !isVisible('lmi-doc-panel'));

  section('  ...and an invalid mode is not a way in');
  prime('self_attestation');
  sandbox.renderLmiSections();
  ["''", 'null', 'undefined', "'DOC'", "'proof_docs'", '0', '{}'].forEach(function (arg) {
    vm.runInContext('setLmiMode(' + arg + ');', ctx);
  });
  assertAllHidden('setLmiMode() with junk arguments');

  // ══════════════════════════════════════════════════════════════════
  section('D. THE LEGACY CONTROLS CANNOT DRIVE A SUBMISSION');
  prime('self_attestation');
  await sandbox.prepareSelfAttestation();   // real hydration, as a rep would get
  vm.runInContext("setLmiMode('attest');", ctx);
  vm.runInContext('state.lmi.householdSize = "3"; state.lmi.incomeBelow = true;', ctx);
  el('lmi-household-size').value = '3';
  sandbox.updateAmiThreshold();
  sandbox.setIncomeAnswer(true);
  check('the legacy attest fields cannot arm the proof-doc Continue',
    el('btn-lmi-next').disabled === true);
  fetchLog.length = 0;
  el('btn-lmi-next').disabled = false;
  await sandbox.submitLmi();
  check('  ...and pressing it posts to no LMI endpoint',
    fetchLog.filter((f) => f.url.includes('/lmi/')).length === 0);
  assertAllHidden('the legacy attest fields being filled in and submitted');

  section('  ...the legacy panel stays hidden even when its inputs are used');
  check('#lmi-attest-panel is still hidden', !isVisible('lmi-attest-panel'));
  check('  ...while the REAL self-attestation section is the visible one',
    isVisible('sa-panel'));
  check('  ...with its county selector populated from the server',
    el('sa-county').innerHTML.includes('>Albany County<'));

  // ══════════════════════════════════════════════════════════════════
  section('E. NO WRITER LEFT THAT CAN SHOW THEM');
  // Every remaining assignment to these controls' display, executed above.
  const writers = [...src.matchAll(
    /getElementById\('(lmi-mode-[a-z]+|lmi-attest-panel)'\)\.style\.display\s*=\s*([^;]+);/g)];
  check(`every display writer found (${writers.length})`, writers.length > 0);
  const revealing = writers.filter((m) => !/['"]none['"]/.test(m[2]));
  check(`none of them assigns anything but 'none' (offenders: ${
    revealing.map((m) => m[1] + ' = ' + m[2].trim()).join('; ') || 'none'})`,
    revealing.length === 0);
  check('the one loop that sets them does so unconditionally to none',
    /\['lmi-mode-doc','lmi-mode-attest','lmi-mode-na'\][\s\S]{0,200}display = 'none'/.test(src));

  report();
}

function report() {
  const failed = results.filter((r) => !r.ok);
  console.log('\n' + '='.repeat(72));
  console.log(`${results.length - failed.length} passed, ${failed.length} failed`);
  console.log('='.repeat(72));
  process.exit(failed.length ? 1 : 0);
}

run().catch((e) => { check('harness ran to completion - ' + e.message, false); report(); });

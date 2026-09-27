/*
 * LMI Eligibility: proof documents and self-attestation COEXIST.
 *
 * WHAT WAS WRONG
 * --------------
 * `state.lmi.mode` was a mutually-exclusive radio. It conflated two different
 * questions - "which section is on screen" and "which API action may be sent" -
 * so revealing self-attestation TOOK THE UPLOADED DOCUMENTS OFF THE SCREEN
 * (prepareSelfAttestation() hid #lmi-proof-panel), and pressing Continue on the
 * attest tab ran the DOCUMENT validation against an empty #lmi-doctype.
 *
 * The two are now separate: sections are shown per Perch's step and can both be
 * open, while exactly one API action is authorised at a time.
 *
 * None of this is provable by substring checks - the old code was present and
 * syntactically fine, it just ran the wrong branch. This harness EXECUTES the
 * handlers against a stubbed DOM and asserts what the rep would actually see,
 * and which endpoints are actually called.
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
function respond(url) {
  for (const key of Object.keys(routes)) if (String(url).includes(key)) return routes[key];
  return { status: 200, body: {} };
}

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
  setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; }, clear() { this._d = {}; },
};
storageStub.setItem('dalton_auth_token', 'harness-token');

const sandbox = {
  console, document: documentStub, sessionStorage: storageStub, localStorage: storageStub,
  setTimeout() { return 0; }, clearTimeout() {}, setInterval() { return 0; }, clearInterval() {},
  alert() {}, confirm() { return true },
  fetch(url, opts) {
    fetchLog.push({ url: String(url), method: (opts && opts.method) || 'GET' });
    const r = respond(url);
    return Promise.resolve({ ok: (r.status || 200) === 200, status: r.status || 200,
      json: () => Promise.resolve(r.body || {}) });
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
console.log('LMI DUAL-STATE HARNESS - PROOF DOCS + SELF-ATTESTATION COEXIST');
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

const REFERENCE = {
  table_caption: 'NY 80% State Median Income',
  thresholds: [{ occupancy: 1, income_level_cents: 6175000 },
               { occupancy: 3, income_level_cents: 7935000 }],
  counties: ['Albany', 'Dutchess', 'Ulster'],
  already_generated: false, already_accepted: false, submitted: null,
};

function el(id) { return documentStub.getElementById(id); }
function docVisible() { return el('lmi-doc-panel').style.display !== 'none'
  && el('lmi-proof-panel').style.display !== 'none'; }
function attestVisible() { return el('sa-panel').style.display !== 'none'; }
function calls(fragment) { return fetchLog.filter((f) => f.url.includes(fragment)); }

function setup(stepKey, opts) {
  opts = opts || {};
  fetchLog.length = 0;
  routes = { '/lmi/self-attestation/reference': { status: 200, body: opts.reference || REFERENCE } };
  vm.runInContext('currentDraft = {enrollment_id: 501, enrollment_code: "ENR-501"};', ctx);
  vm.runInContext('perchContext.nextStepKey = ' + JSON.stringify(stepKey) + ';', ctx);
  vm.runInContext('perchContext.proofSubmitted = false; perchContext.contractsGenerated = false;', ctx);
  vm.runInContext('selfAttestation = {reference:null, occupancy:null, county:"", choice:null};', ctx);
  vm.runInContext('state.lmi = {mode:"doc", docType:"", fileName:"", documentId:null, '
    + 'nameOnDocument:"", relationship:"self", documentFormat:""};', ctx);
  ['sa-county', 'sa-occupancy'].forEach((id) => { const e = el(id); e.options = []; e.innerHTML = ''; e.value = ''; });
  ['lmi-submit-error', 'sa-error', 'lmi-doc-hint'].forEach((id) => {
    const e = el(id); if (e) { e.textContent = ''; e.style.display = 'none'; } });
  ['lmi-doctype', 'lmi-name-on-doc', 'lmi-format'].forEach((id) => { el(id).value = ''; });
  el('lmi-proof-panel').style.display = ''; el('sa-panel').style.display = 'none';
}

/* A proof document already uploaded and attached to this enrollment. */
function attachProofDoc() {
  vm.runInContext('state.lmi.docType = "proof_doc_liheap"; state.lmi.fileName = "liheap.pdf"; '
    + 'state.lmi.documentId = 88; state.lmi.nameOnDocument = "Tina Bell"; '
    + 'state.lmi.documentFormat = "letter"; state.lmi.relationship = "self";', ctx);
  el('lmi-doctype').value = 'proof_doc_liheap';
  el('lmi-name-on-doc').value = 'Tina Bell';
  el('lmi-format').value = 'letter';
  el('lmi-relationship').value = 'self';
}
function proofDocIntact() {
  return vm.runInContext('state.lmi.documentId', ctx) === 88
      && vm.runInContext('state.lmi.fileName', ctx) === 'liheap.pdf'
      && vm.runInContext('state.lmi.docType', ctx) === 'proof_doc_liheap';
}

async function run() {
  // ══════════════════════════════════════════════════════════════════
  section('2 + 9. PROOF DOCS STAY VISIBLE AND ATTACHED ON self_attestation');
  setup('self_attestation');
  attachProofDoc();
  vm.runInContext('goStep(4);', ctx);
  await new Promise((r) => setImmediate(r));
  check('the proof-document section is STILL on screen', docVisible());
  check('  ...and the self-attestation section is revealed too', attestVisible());
  check('  ...BOTH at once - not one replacing the other', docVisible() && attestVisible());
  check('the uploaded document is still attached', proofDocIntact());
  check('  ...and the rep is told why it cannot be sent yet',
    el('lmi-doc-hint').style.display === 'block'
    && /stays attached/i.test(el('lmi-doc-hint').textContent));
  check('the mutually-exclusive toggle row is gone from the screen',
    el('lmi-mode-doc').style.display === 'none'
    && el('lmi-mode-attest').style.display === 'none'
    && el('lmi-mode-na').style.display === 'none');
  check('  ...as is the legacy attest panel it switched to',
    el('lmi-attest-panel').style.display === 'none');

  section('11. COUNTY RENDERS, AND ONLY FROM AUTHORITATIVE DATA');
  const county = el('sa-county').innerHTML;
  ['Albany', 'Dutchess', 'Ulster'].forEach((n) =>
    check(`  ...county list includes ${n}`, county.includes('>' + n + '<')));
  check('exactly the three the server sent, plus the placeholder',
    (county.match(/<option/g) || []).length === 4);
  check('NO county is invented',
    !/Kings|Queens|Bronx|Nassau|Suffolk|Erie|Westchester/.test(county));
  check('the threshold caption came from the server',
    el('sa-caption').textContent === REFERENCE.table_caption);

  // ══════════════════════════════════════════════════════════════════
  section('6 + 12. ONLY THE SELF-ATTESTATION ACTION IS AUTHORISED HERE');
  check('the proof-doc Continue button is disabled',
    el('btn-lmi-next').disabled === true);
  fetchLog.length = 0;
  el('btn-lmi-next').disabled = false;          // even if something armed it
  await sandbox.submitLmi();
  check('pressing it does NOT post proof docs', calls('/lmi/proof_docs').length === 0);
  check('  ...and raises NO document-validation error',
    !/legacy document label/i.test(el('lmi-submit-error').textContent));
  check('  ...it explains that self-attestation is what Perch wants',
    /self-attestation/i.test(el('lmi-submit-error').textContent));
  check('  ...and reassures that the documents stay attached',
    /stay attached/i.test(el('lmi-submit-error').textContent));
  check('the uploaded document survived that press', proofDocIntact());

  section('3 + 13. COMPLETING SELF-ATTESTATION DOES NOT TOUCH PROOF DOCS');
  el('sa-occupancy').value = '3';
  sandbox.onSelfAttestationOccupancy();
  el('sa-county').value = 'Ulster';
  sandbox.onSelfAttestationCounty();
  sandbox.selectSelfAttestationChoice('below');
  check('the attestation answers are captured',
    vm.runInContext('selfAttestation.occupancy', ctx) === '3'
    && vm.runInContext('selfAttestation.county', ctx) === 'Ulster'
    && vm.runInContext('selfAttestation.choice', ctx) === 'below');
  check('the proof document is untouched by any of it', proofDocIntact());
  check('  ...and its section is still on screen', docVisible());

  fetchLog.length = 0;
  routes['/lmi/self-attestation'] = { status: 200, body: { next_step_key: 'contracts' } };
  check('self-attestation work raised no DOCUMENT-VALIDATION error',
    !/legacy document label/i.test(el('lmi-submit-error').textContent)
    && !/supported proof document/i.test(el('lmi-submit-error').textContent));
  check('  ...and the proof-doc fields were not blanked by it',
    el('lmi-doctype').value === 'proof_doc_liheap'
    && el('lmi-name-on-doc').value === 'Tina Bell');

  // ══════════════════════════════════════════════════════════════════
  section('5. ON proof_docs, ONLY /lmi/proof_docs IS POSTED');
  setup('proof_docs');
  attachProofDoc();
  vm.runInContext('goStep(4);', ctx);
  await new Promise((r) => setImmediate(r));
  check('the proof-document section is shown', docVisible());
  check('  ...and the self-attestation section is NOT', !attestVisible());
  check('  ...no hint is needed - this IS the authorised action',
    el('lmi-doc-hint').style.display === 'none');
  check('Continue is armed', el('btn-lmi-next').disabled === false);

  fetchLog.length = 0;
  routes['/lmi/proof_docs'] = { status: 200, body: { next_step_key: 'contracts' } };
  routes['/api/enrollments/501/lmi'] = { status: 200, body: {} };
  await sandbox.submitLmi();
  check('/lmi/proof_docs was posted exactly once', calls('/lmi/proof_docs').length === 1);
  check('  ...as a POST', calls('/lmi/proof_docs')[0].method === 'POST');
  check('NO self-attestation endpoint was called',
    calls('/lmi/self-attestation').filter((c) => c.method === 'POST').length === 0);
  check('  ...and no acceptance endpoint either',
    calls('/self-attestation/accept').length === 0);

  // ══════════════════════════════════════════════════════════════════
  section('9. proof_docs ADVANCING TO self_attestation KEEPS THE DOCUMENTS');
  setup('proof_docs');
  attachProofDoc();
  vm.runInContext('goStep(4);', ctx);
  await new Promise((r) => setImmediate(r));
  fetchLog.length = 0;
  routes['/lmi/proof_docs'] = { status: 200, body: { next_step_key: 'self_attestation' } };
  el('btn-lmi-next').disabled = false;
  await sandbox.submitLmi();
  await new Promise((r) => setImmediate(r));
  check('UBS follows Perch to self_attestation instead of erroring',
    vm.runInContext('perchContext.nextStepKey', ctx) === 'self_attestation');
  check('  ...no "unexpected next step" is shown',
    !/unexpected next step/i.test(el('lmi-submit-error').textContent));
  check('the proof documents are STILL attached', proofDocIntact());
  check('  ...their section is still visible', docVisible());
  check('  ...and the self-attestation section is now revealed', attestVisible());
  check('  ...the proof doc was posted ONCE, not re-posted',
    calls('/lmi/proof_docs').length === 1);

  // ══════════════════════════════════════════════════════════════════
  section('8. self_attestation ADVANCING TO proof_docs REUSES THE UPLOAD');
  setup('self_attestation');
  attachProofDoc();
  vm.runInContext('goStep(4);', ctx);
  await new Promise((r) => setImmediate(r));
  el('sa-occupancy').value = '3'; sandbox.onSelfAttestationOccupancy();
  el('sa-county').value = 'Albany'; sandbox.onSelfAttestationCounty();
  sandbox.selectSelfAttestationChoice('below');
  fetchLog.length = 0;
  routes['/lmi/self-attestation'] = { status: 200, body: { next_step_key: 'proof_docs' } };
  await sandbox.submitSelfAttestation();
  await new Promise((r) => setImmediate(r));
  check('UBS follows Perch to proof_docs',
    vm.runInContext('perchContext.nextStepKey', ctx) === 'proof_docs');
  check('the already-uploaded document is reused', proofDocIntact());
  check('  ...the rep is NOT asked to upload again',
    calls('/documents').length === 0);
  check('  ...and Continue is armed from the existing upload',
    el('btn-lmi-next').disabled === false);

  // ══════════════════════════════════════════════════════════════════
  section('4. RETURNING TO THE PROOF-DOC STEP KEEPS THE ATTESTATION ANSWERS');
  check('occupancy survived the transition',
    vm.runInContext('selfAttestation.occupancy', ctx) === '3');
  check('  ...county survived', vm.runInContext('selfAttestation.county', ctx) === 'Albany');
  check('  ...and the income answer survived',
    vm.runInContext('selfAttestation.choice', ctx) === 'below');

  // ══════════════════════════════════════════════════════════════════
  section('7. self_attestation_accept AUTHORISES ONLY THE ACCEPTANCE ACTION');
  setup('self_attestation_accept');
  attachProofDoc();
  vm.runInContext('goStep(4);', ctx);
  await new Promise((r) => setImmediate(r));
  check('both sections are on screen', docVisible() && attestVisible());
  check('the proof-doc Continue is NOT armed', el('btn-lmi-next').disabled === true);
  fetchLog.length = 0;
  el('btn-lmi-next').disabled = false;
  await sandbox.submitLmi();
  check('and posts nothing to /lmi/proof_docs', calls('/lmi/proof_docs').length === 0);
  check('the documents remain attached', proofDocIntact());

  // ══════════════════════════════════════════════════════════════════
  section('10. A STEP PERCH DID NOT NAME FORCES NOTHING');
  ['contracts', 'contracts_accepted', 'enroll', 'some_future_perch_step', null, undefined]
    .forEach(function (key) {
      setup(key);
      const s = sandbox.lmiSectionsForCurrentStep();
      check(`${JSON.stringify(key)} authorises no LMI action`, s.submits === null);
      check('  ...and shows neither section', s.doc === false && s.attest === false);
    });
  setup('contracts');
  attachProofDoc();
  fetchLog.length = 0;
  el('btn-lmi-next').disabled = false;
  await sandbox.submitLmi();
  check('pressing Continue on a downstream step calls no LMI endpoint',
    calls('/lmi/').length === 0);
  check('  ...and says the step has moved on',
    /not on an income-qualification step/i.test(el('lmi-submit-error').textContent));
  check('the resolver is the ONLY place this is decided',
    (src.match(/function lmiSectionsForCurrentStep/g) || []).length === 1);

  // ══════════════════════════════════════════════════════════════════
  section('14. RESUME RESTORES BOTH KINDS OF STATE');
  setup('self_attestation', { reference: Object.assign({}, REFERENCE, {
    submitted: { occupancy: 3, county: 'Dutchess', status: 'accepted' } }) });
  attachProofDoc();
  vm.runInContext('goStep(4);', ctx);
  await new Promise((r) => setImmediate(r));
  check('the proof document is restored', proofDocIntact());
  check('  ...household size is restored from the server record',
    vm.runInContext('selfAttestation.occupancy', ctx) === '3'
    && el('sa-occupancy').value === '3');
  check('  ...county is restored', vm.runInContext('selfAttestation.county', ctx) === 'Dutchess'
    && el('sa-county').value === 'Dutchess');
  check('  ...and the income answer is restored',
    vm.runInContext('selfAttestation.choice', ctx) === 'below');
  check('restored values came from the SERVER, not a local guess',
    /reference\.submitted/.test(src));
  check('  ...for THIS enrollment', fetchLog.some((f) => f.url.includes('/501/')));

  section('  ...and a blank record restores nothing rather than guessing');
  setup('self_attestation');
  vm.runInContext('goStep(4);', ctx);
  await new Promise((r) => setImmediate(r));
  check('no occupancy is invented', !vm.runInContext('selfAttestation.occupancy', ctx));
  check('  ...no county is invented', !vm.runInContext('selfAttestation.county', ctx));

  // ══════════════════════════════════════════════════════════════════
  section('15. A FRESH ENROLLMENT INHERITS NOTHING');
  setup('self_attestation', { reference: Object.assign({}, REFERENCE, {
    submitted: { occupancy: 3, county: 'Dutchess', status: 'accepted' } }) });
  attachProofDoc();
  vm.runInContext('goStep(4);', ctx);
  await new Promise((r) => setImmediate(r));
  check('state exists before the reset', proofDocIntact()
    && vm.runInContext('selfAttestation.county', ctx) === 'Dutchess');
  vm.runInContext('resetWizardState();', ctx);
  check('resetWizardState clears the proof document',
    !vm.runInContext('state.lmi.documentId', ctx)
    && !vm.runInContext('state.lmi.fileName', ctx));
  check('  ...and the self-attestation answers',
    !vm.runInContext('selfAttestation.occupancy', ctx)
    && !vm.runInContext('selfAttestation.county', ctx)
    && !vm.runInContext('selfAttestation.choice', ctx));
  check('  ...and the cached reference data',
    vm.runInContext('selfAttestation.reference', ctx) === null);

  // ══════════════════════════════════════════════════════════════════
  section('21. RESIDENTIAL / NON-LMI NEVER REACHES THIS SCREEN');
  const map = src.slice(src.indexOf('const WORKFLOW_STEP_TO_WIZARD'),
    src.indexOf('async function openEnrollment'));
  const toFour = (map.match(/(\w+):\s*4/g) || []).map((m) => m.split(':')[0]).sort();
  check('only the three LMI steps map to Eligibility',
    toFour.join() === 'proof_docs,self_attestation,self_attestation_accept');
  check('  ...the contracts step routes to the agreements screen, not Eligibility',
    /perchContext\.nextStepKey === 'contracts'/.test(src)
    && !/contracts:\s*4/.test(map));
  check('  ...and contracts maps to the agreements screen, not Eligibility',
    /contracts:\s*5/.test(map));

  section('THE SUMMARY REPORTS BOTH, NOT WHICHEVER TAB WAS LAST OPEN');
  setup('self_attestation');
  attachProofDoc();
  vm.runInContext('selfAttestation.occupancy = "3"; selfAttestation.choice = "below";', ctx);
  const summary = sandbox.buildLmiSummary();
  check('it mentions the document', /documentation/i.test(summary));
  check('  ...and the attestation', /self-attested/i.test(summary));
  check('  ...carrying the NYSERDA/Arcadia wording', /NYSERDA/.test(summary));

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

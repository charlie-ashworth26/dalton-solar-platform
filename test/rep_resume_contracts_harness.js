/*
 * Rep dashboard View/Resume of an INCOMPLETE enrollment at contracts_review.
 *
 * THE BUG THIS EXISTS FOR
 * -----------------------
 * rehydrateContractPacket() referenced `errEl`, whose declaration had been
 * deleted with the legacy contract UI. It threw ReferenceError on its FIRST
 * line, before the fetch, so mountRepAgreements() never ran and the rep saw a
 * final review screen with no acknowledgement checkbox and no Agree & finish.
 * The customer path worked because it uses openCustomerContracts(), a different
 * function that never touched errEl.
 *
 * Read-only must follow the ENROLLMENT STAGE, never the entry route.
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(ROOT, 'templates', 'index.html'), 'utf8');
const IDS = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));
const DYN = new Set(['agr-ack-check', 'agr-agree-btn', 'agr-card-error', 'agr-status']);

const results = [];
function check(label, cond) {
  results.push({ label, ok: !!cond });
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${label}`);
}

function mk(id) {
  return { id, style: {}, dataset: {}, innerHTML: '', textContent: '', value: '',
    disabled: false, checked: false, type: 'text', title: '',
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute() {}, getAttribute: () => null, removeAttribute() {},
    appendChild() {}, insertBefore() {}, removeChild() {}, focus() {}, click() {},
    addEventListener() {}, removeEventListener() {},
    querySelector: () => null, querySelectorAll: () => [], closest: () => null };
}

function newEnv(routes) {
  const cache = new Map();
  const calls = [];
  const doc = {
    getElementById(id) {
      if (!IDS.has(id) && !DYN.has(id)) return null;
      if (!cache.has(id)) cache.set(id, mk(id));
      return cache.get(id);
    },
    querySelector: () => null, querySelectorAll: () => [],
    createElement: mk, addEventListener() {}, removeEventListener() {},
    body: Object.assign(mk('body'), { style: {} }), activeElement: null,
  };
  const sb = {
    console, document: doc,
    sessionStorage: { getItem: () => 'tok', setItem() {}, removeItem() {} },
    localStorage: { getItem: () => 'tok', setItem() {}, removeItem() {} },
    setTimeout: (f) => { if (typeof f === 'function') {} return 0; },
    clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    alert() {}, scrollTo() {},
    FormData: function () { this.append = function () {}; },
    FileReader: function () {}, Blob: function () {},
    URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
    navigator: { userAgent: 'harness' }, location: { href: '' },
    requestAnimationFrame: () => 0,
    pdfjsLib: { GlobalWorkerOptions: {} }, Tesseract: {},
    fetch(url, opts) {
      const u = String(url);
      let body = null;
      try { body = opts && opts.body ? JSON.parse(opts.body) : null; } catch (e) {}
      calls.push({ url: u, method: (opts && opts.method) || 'GET', body });
      for (const [pattern, resp] of routes) {
        if (u.includes(pattern)) {
          return Promise.resolve({ ok: resp.ok !== false, status: resp.status || 200,
            json: () => Promise.resolve(resp.body || {}) });
        }
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    },
  };
  sb.window = sb;
  const ctx = vm.createContext(sb);
  vm.runInContext(src, ctx, { filename: 'app.js' });
  return { ctx, sb, cache, calls };
}

const CONTRACTS = {
  contracts: [
    { index: 0, contract_name: 'Community Distributed Generation Disclosure Form', url_present: true },
    { index: 1, contract_name: 'Community Solar Agency Agreement', url_present: true },
  ],
  contract_count: 2, acceptance_enabled: true, next_step_key: 'contracts_accept',
};

function enrollment(stepKey, terminal, blocked) {
  return {
    id: 7, enrollment_code: 'ENR-2026-000007',
    customer: { first_name: 'Johnnie', last_name: 'Testcustomer', email: 'j@example.com' },
    service_address: { street: '123 Main St', city: 'Albany', zip: '12207' },
    utility_account: { utility_name: 'national-grid-ny', account_number: '1234567890' },
    project: { id: 1, name: 'Albany Community Solar' },
    workflow_step_key: stepKey,
    workflow_is_terminal: terminal, workflow_is_blocked: blocked,
    workflow_last_response: terminal
      ? { contracts: [{ contract_name: 'Community Solar Agency Agreement' }] } : null,
  };
}

const tick = () => new Promise((r) => setImmediate(r));

console.log('='.repeat(72));
console.log('REP DASHBOARD VIEW/RESUME — contracts_review must stay INTERACTIVE');
console.log('='.repeat(72));

(async () => {
  // ── The reported sequence ──────────────────────────────────────────
  console.log('\n--- fresh enrollment -> contracts_review -> dashboard -> View ---');
  let env = newEnv([
    ['/api/enrollments/7', { body: enrollment('contracts_review', false, false) }],
    ['/contracts', { body: CONTRACTS }],
  ]);
  await vm.runInContext('openEnrollment(7);', env.ctx);
  for (let i = 0; i < 6; i++) await tick();

  const host = env.cache.get('agr-host-rep');
  const rendered = host ? host.innerHTML : '';

  check('rep View rendered the final review screen', rendered.length > 0);
  check('the contract packet was requested',
    env.calls.some((c) => c.url.includes('/contracts') && c.method === 'POST'));
  check('acknowledgement checkbox IS present', rendered.includes('id="agr-ack-check"'));
  check('Agree & finish button IS present', rendered.includes('id="agr-agree-btn"'));
  check('agreement links are rendered', rendered.includes('openAgreementDoc('));
  check('real agreement names appear',
    rendered.includes('Community Distributed Generation Disclosure Form'));
  check('component is NOT read-only',
    vm.runInContext('Agreements.readOnly', env.ctx) === false);
  check('acceptance is enabled from the backend response',
    vm.runInContext('Agreements.acceptanceEnabled', env.ctx) === true);
  check('not marked submitted', vm.runInContext('Agreements.submitted', env.ctx) === false);
  check('no ReferenceError text surfaced in the card',
    !/is not defined/.test(rendered));

  // Checking the box must enable the button, exactly as in the straight-through flow.
  const chk = env.cache.get('agr-ack-check');
  const btn = env.cache.get('agr-agree-btn');
  check('button starts disabled', btn && btn.disabled === true);
  chk.checked = true;
  vm.runInContext('updateAgreeButton();', env.ctx);
  check('checking the box ENABLES Agree & finish', btn.disabled === false);

  // ── Completed enrollment through the SAME dashboard View path ──────
  console.log('\n--- completed enrollment -> same dashboard View path ---');
  env = newEnv([
    ['/api/enrollments/7', { body: enrollment('contracts_accepted', true, false) }],
    ['/contracts', { body: CONTRACTS }],
  ]);
  await vm.runInContext('openEnrollment(7);', env.ctx);
  for (let i = 0; i < 6; i++) await tick();

  const roHost = env.cache.get('agr-host-rep');
  const ro = roHost ? roHost.innerHTML : '';
  const contractPosts = env.calls.filter(
    (c) => c.url.includes('/contracts') && c.method === 'POST' && !c.url.includes('review'));

  check('completed view rendered', ro.length > 0);
  check('completed view made ZERO POST /contracts calls', contractPosts.length === 0);
  check('completed view is read-only',
    vm.runInContext('Agreements.readOnly', env.ctx) === true);
  check('completed view has NO acknowledgement checkbox', !ro.includes('id="agr-ack-check"'));
  check('completed view has NO Agree & finish button', !ro.includes('id="agr-agree-btn"'));
  check('completed view exposes no document links', !ro.includes('openAgreementDoc('));
  check('completed view lists persisted agreement names',
    ro.includes('Community Solar Agency Agreement'));
  check('completed view says it is complete', /complete/i.test(ro));

  // ── Blocked (uncertain) enrollment ────────────────────────────────
  console.log('\n--- uncertain acceptance -> read-only, no controls ---');
  env = newEnv([
    ['/api/enrollments/7', { body: enrollment('contracts_accept_uncertain', false, true) }],
    ['/contracts', { body: CONTRACTS }],
  ]);
  await vm.runInContext('openEnrollment(7);', env.ctx);
  for (let i = 0; i < 6; i++) await tick();
  const bl = (env.cache.get('agr-host-rep') || {}).innerHTML || '';
  const blPosts = env.calls.filter(
    (c) => c.url.includes('/contracts') && c.method === 'POST' && !c.url.includes('review'));
  check('blocked view made ZERO POST /contracts calls', blPosts.length === 0);
  check('blocked view is read-only',
    vm.runInContext('Agreements.readOnly', env.ctx) === true);
  check('blocked view has no acceptance controls',
    !bl.includes('id="agr-agree-btn"') && !bl.includes('id="agr-ack-check"'));

  // ── Read-only must follow STAGE, not entry route ──────────────────
  console.log('\n--- read-only follows the STAGE, not the entry route ---');
  for (const [stepKey, terminal, blocked, expectReadOnly] of [
    ['contracts', false, false, false],
    ['contracts_review', false, false, false],
    ['contracts_accept', false, false, false],
    ['contracts_accepted', true, false, true],
    ['contracts_accept_uncertain', false, true, true],
  ]) {
    const e2 = newEnv([
      ['/api/enrollments/7', { body: enrollment(stepKey, terminal, blocked) }],
      ['/contracts', { body: CONTRACTS }],
    ]);
    await vm.runInContext('openEnrollment(7);', e2.ctx);
    for (let i = 0; i < 6; i++) await tick();
    const actual = vm.runInContext('Agreements.readOnly', e2.ctx);
    check(`'${stepKey}' via rep View -> readOnly=${expectReadOnly}`, actual === expectReadOnly);
  }

  // ── Rep and customer reach the SAME interactive state ─────────────
  console.log('\n--- rep and customer agree on the same enrollment ---');
  const repEnv = newEnv([
    ['/api/enrollments/7', { body: enrollment('contracts_review', false, false) }],
    ['/contracts', { body: CONTRACTS }],
  ]);
  await vm.runInContext('openEnrollment(7);', repEnv.ctx);
  for (let i = 0; i < 6; i++) await tick();

  const custEnv = newEnv([
    ['/api/auth/customer-me', { body: {
      enrollment_id: 7, customer: { first_name: 'Johnnie', last_name: 'Testcustomer' },
      workflow_step_key: 'contracts_review',
      workflow_is_terminal: false, workflow_is_blocked: false } }],
    ['/contracts', { body: CONTRACTS }],
  ]);
  await vm.runInContext('openCustomerContracts();', custEnv.ctx);
  for (let i = 0; i < 6; i++) await tick();

  const repRO = vm.runInContext('Agreements.readOnly', repEnv.ctx);
  const custRO = vm.runInContext('Agreements.readOnly', custEnv.ctx);
  const repEnabled = vm.runInContext('Agreements.acceptanceEnabled', repEnv.ctx);
  const custEnabled = vm.runInContext('Agreements.acceptanceEnabled', custEnv.ctx);
  check('rep and customer agree on readOnly', repRO === custRO && repRO === false);
  check('rep and customer agree on acceptanceEnabled',
    repEnabled === custEnabled && repEnabled === true);
  const custHost = custEnv.cache.get('agr-host-customer');
  check('customer also gets the acknowledgement',
    custHost && custHost.innerHTML.includes('id="agr-ack-check"'));

  const failed = results.filter((r) => !r.ok);
  console.log('\n' + '='.repeat(72));
  console.log(`${results.length - failed.length} passed, ${failed.length} failed`);
  console.log('='.repeat(72));
  if (failed.length) { failed.forEach((f) => console.log('  FAILED: ' + f.label)); process.exit(1); }
  process.exit(0);
})();

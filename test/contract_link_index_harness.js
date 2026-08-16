/*
 * Contract link -> contract_index runtime harness.
 *
 * THE BUG THIS EXISTS FOR
 * -----------------------
 * The redesigned screen sent {contract_index: n} while POST /contracts/review
 * reads data.get("index"), so every click returned
 *     "A contract index is required."
 * A parameter-name mismatch between two files that no substring assertion on
 * either file alone could catch.
 *
 * This harness RENDERS the real component, extracts every agreement link from
 * the produced HTML, CLICKS each one, and asserts the exact request body that
 * reaches the network layer - proving the wire contract, not the source text.
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

function makeEl(id) {
  return {
    id, value: '', checked: false, disabled: false, textContent: '', innerHTML: '',
    title: '', type: 'text', style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    appendChild() {}, insertBefore() {}, removeChild() {},
    addEventListener() {}, removeEventListener() {}, focus() {}, click() {},
    closest() { return null; }, querySelector() { return null; },
    querySelectorAll() { return []; },
  };
}
const cache = new Map();
// ids the component renders dynamically into innerHTML
const DYNAMIC_IDS = new Set(['agr-ack-check', 'agr-agree-btn', 'agr-card-error', 'agr-status']);
const doc = {
  getElementById(id) {
    if (!REAL_IDS.has(id) && !DYNAMIC_IDS.has(id)) return null;
    if (!cache.has(id)) cache.set(id, makeEl(id));
    return cache.get(id);
  },
  querySelector() { return null; }, querySelectorAll() { return []; },
  createElement(t) { return makeEl('c-' + t); },
  addEventListener() {}, removeEventListener() {},
  body: Object.assign(makeEl('body'), { style: {} }),
  activeElement: null,
};

// Records every request the component makes.
const sent = [];
const sandbox = {
  console, document: doc,
  sessionStorage: { getItem: () => 'tok', setItem() {}, removeItem() {} },
  localStorage: { getItem: () => 'tok', setItem() {}, removeItem() {} },
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  alert() {}, scrollTo() {},
  fetch(url, opts) {
    let body = null;
    try { body = opts && opts.body ? JSON.parse(opts.body) : null; } catch (e) {}
    sent.push({ url, method: (opts && opts.method) || 'GET', body,
                auth: opts && opts.headers && opts.headers.Authorization });
    if (String(url).includes('/contracts/review')) {
      // Mirror the REAL backend contract: it requires `index`.
      if (!body || typeof body.index !== 'number') {
        return Promise.resolve({ ok: false, status: 400,
          json: () => Promise.resolve({ error: 'A contract index is required.' }) });
      }
      return Promise.resolve({ ok: true, status: 200,
        json: () => Promise.resolve({ review_url: '/api/perch/contract-reviews/tok-' + body.index }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  },
  navigator: { userAgent: 'harness' }, location: { href: '' },
  requestAnimationFrame: () => 0,
  pdfjsLib: { GlobalWorkerOptions: {} }, Tesseract: {},
};
sandbox.window = sandbox;
const ctx = vm.createContext(sandbox);

console.log('='.repeat(72));
console.log('CONTRACT LINK -> INDEX RUNTIME HARNESS');
console.log('='.repeat(72));

try { vm.runInContext(src, ctx, { filename: 'app.js' }); check('app.js evaluates', true); }
catch (e) { check('app.js evaluates — ' + e.message, false); finish(); }

// The exact shape the backend normalizer produces, including the authoritative index.
const PACKET = [
  { index: 0, contract_name: 'Community Distributed Generation Disclosure Form', expires_at: 'x', url_present: true },
  { index: 1, contract_name: 'Community Distributed Generation Credit Sale and Purchase Agreement', expires_at: 'x', url_present: true },
  { index: 2, contract_name: 'Community Solar Agency Agreement', expires_at: 'x', url_present: true },
  { index: 3, contract_name: 'ESIGN Consent Policy', expires_at: 'x', url_present: true },
];

(async () => {
  vm.runInContext(`mountAgreements({actor:'rep', enrollmentId: 42, contracts: ${JSON.stringify(PACKET)},
    acceptanceEnabled: true, readOnly: false, submitted: false,
    summary: {customerName:'Johnnie Testcustomer', email:'j@example.com',
              utility:'national-grid-ny', serviceAddress:'123 Main St, Albany, NY 12207',
              accountNumber:'1234567890'}});`, ctx);

  const host = cache.get('agr-host-rep');
  const rendered = host ? host.innerHTML : '';
  check('the final review card rendered', rendered.length > 0);

  console.log('\n--- ONE acknowledgement, ONE button, NO rail ---');
  check('exactly one acknowledgement checkbox', (rendered.match(/id="agr-ack-check"/g) || []).length === 1);
  check('exactly one submit button', (rendered.match(/id="agr-agree-btn"/g) || []).length === 1);
  check('button reads "Agree & finish"', /Agree &amp; finish|Agree & finish/.test(rendered));
  check('no document rail', !rendered.includes('agr-doc-rail'));
  check('no per-document cards', !rendered.includes('agr-doc-n'));
  check('no "Opened" badges', !/Opened|Tap to open/.test(rendered));
  check('no per-document acceptance controls',
    (rendered.match(/type="checkbox"/g) || []).length === 1);

  console.log('\n--- account details are shown ---');
  for (const v of ['Johnnie Testcustomer', 'j@example.com', 'national-grid-ny',
                   '123 Main St, Albany, NY 12207', '1234567890']) {
    check(`details include ${v}`, rendered.includes(v));
  }

  console.log('\n--- agreement names are inline links carrying the real index ---');
  const links = [...rendered.matchAll(/data-index="(\d+)"[^>]*onclick="openAgreementDoc\((\d+)\)/g)];
  check(`one link per contract (found ${links.length} of ${PACKET.length})`, links.length === PACKET.length);
  let mapped = true;
  PACKET.forEach((c) => {
    if (!rendered.includes(c.contract_name)) mapped = false;
  });
  check('every real Perch agreement name appears', mapped);
  check('data-index matches the onclick index for every link',
    links.every((m) => m[1] === m[2]));
  check('indices are exactly the packet indices, in order',
    links.map((m) => Number(m[1])).join(',') === PACKET.map((c) => c.index).join(','));
  check('names are not hardcoded — they come from the packet',
    rendered.includes('Community Solar Agency Agreement')
    && !rendered.includes('Agreement 1'));

  console.log('\n--- CLICK each link: the correct index must reach the wire ---');
  for (const c of PACKET) {
    sent.length = 0;
    await vm.runInContext(`openAgreementDoc(${c.index});`, ctx);
    await new Promise((r) => setImmediate(r));
    const req = sent.find((s) => String(s.url).includes('/contracts/review'));
    check(`link ${c.index} ("${c.contract_name.slice(0, 34)}…") posts to /contracts/review`, !!req);
    if (!req) continue;
    check(`  ...sends {index: ${c.index}} — the parameter the backend reads`,
      req.body && req.body.index === c.index);
    check('  ...does NOT send the wrong parameter name',
      req.body && !('contract_index' in req.body));
    check('  ...request would be ACCEPTED by the backend contract',
      req.body && typeof req.body.index === 'number');
    const bodyEl = cache.get('agr-doc-body');
    check(`  ...the correct document opens (capability tok-${c.index})`,
      bodyEl && bodyEl.innerHTML.includes('tok-' + c.index));
    check('  ...no Perch presigned URL is used',
      bodyEl && !/amazonaws|X-Amz/.test(bodyEl.innerHTML));
    const errEl = cache.get('agr-error');
    check('  ...no "contract index is required" error',
      !errEl || !String(errEl.textContent).includes('index is required'));
  }

  console.log('\n--- viewing is NOT accepting ---');
  const acceptCalls = sent.filter((s) => String(s.url).includes('/contracts/accept'));
  check('opening documents never called /contracts/accept', acceptCalls.length === 0);
  check('Agreements.submitted is still false',
    vm.runInContext('Agreements.submitted', ctx) === false);
  vm.runInContext('closeAgreements();', ctx);
  check('closing does not accept either',
    vm.runInContext('Agreements.submitted', ctx) === false);

  console.log('\n--- the acknowledgement checkbox is required ---');
  const chk = cache.get('agr-ack-check');
  const btn = cache.get('agr-agree-btn');
  chk.checked = false;
  vm.runInContext('updateAgreeButton();', ctx);
  check('button disabled while unchecked', btn.disabled === true);
  sent.length = 0;
  await vm.runInContext('submitAgreements();', ctx);
  await new Promise((r) => setImmediate(r));
  check('submitting unchecked calls NOTHING',
    !sent.some((s) => String(s.url).includes('/contracts/accept')));
  chk.checked = true;
  vm.runInContext('updateAgreeButton();', ctx);
  check('button enabled once checked', btn.disabled === false);

  console.log('\n--- Agree & finish uses the existing acceptance route ---');
  sent.length = 0;
  await vm.runInContext('submitAgreements();', ctx);
  await new Promise((r) => setImmediate(r));
  const acc = sent.find((s) => String(s.url).includes('/contracts/accept'));
  check('posts to the existing /contracts/accept route', !!acc);
  check('sends customer_confirmed: true', acc && acc.body && acc.body.customer_confirmed === true);
  check('sends no client-side metadata (server builds it)',
    acc && acc.body && !('ip_address' in acc.body) && !('timestamp' in acc.body)
    && !('user_agent' in acc.body));

  console.log('\n--- read-only completed enrollment ---');
  sent.length = 0;
  vm.runInContext(`mountAgreements({actor:'customer', enrollmentId: 42,
    contracts: [{index:0, contract_name:'ESIGN Consent Policy'}],
    acceptanceEnabled: false, readOnly: true, submitted: true,
    summary: {customerName:'A'}});`, ctx);
  const ro = cache.get('agr-host-customer') ? cache.get('agr-host-customer').innerHTML : '';
  check('read-only view made zero requests', sent.length === 0);
  check('read-only shows no acknowledgement checkbox', !ro.includes('agr-ack-check'));
  check('read-only shows no submit button', !ro.includes('agr-agree-btn'));
  check('read-only still lists persisted agreement names', ro.includes('ESIGN Consent Policy'));
  check('read-only exposes no links to open', !ro.includes('openAgreementDoc'));

  console.log('\n--- rep and customer share ONE component ---');
  check('both actors used mountAgreements',
    typeof sandbox.mountAgreements === 'function');
  check('a single renderAgreementCard exists',
    (src.match(/function renderAgreementCard\(/g) || []).length === 1);
  check('a single openAgreementDoc exists',
    (src.match(/function openAgreementDoc\(/g) || []).length === 1);
  check('a single submitAgreements exists',
    (src.match(/function submitAgreements\(/g) || []).length === 1);

  finish();
})();

function finish() {
  const failed = results.filter((r) => !r.ok);
  console.log('\n' + '='.repeat(72));
  console.log(`${results.length - failed.length} passed, ${failed.length} failed`);
  console.log('='.repeat(72));
  if (failed.length) { failed.forEach((f) => console.log('  FAILED: ' + f.label)); process.exit(1); }
  process.exit(0);
}

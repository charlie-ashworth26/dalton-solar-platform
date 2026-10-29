/*
 * RUNTIME PROOF: UBS 2.0 Phase C0.
 *
 * The source checks in test/test_phase_c0_defects.py prove the stylesheet and
 * the structure. They cannot prove that the 403 branch RUNS, that the Export
 * button is actually hidden when it does, or that the rendered state carries
 * none of the server's words. This drives loadSalesReport() against a stubbed
 * 403 and reads the resulting DOM.
 *
 * node test/phase_c0_harness.js
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
    style: {}, dataset: {}, options: [], hidden: false,
    classList: {
      _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle() {}, contains(c) { return this._s.has(c); },
    },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    appendChild() {}, insertBefore() {}, addEventListener() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    focus() {}, click() {}, closest() { return null; }, scrollIntoView() {},
  };
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
  createElement(t) { return makeEl('c-' + t); },
  addEventListener() {}, body: makeEl('body'), documentElement: makeEl('html'),
};
const storageStub = {
  _d: {}, getItem(k) { return this._d[k] ?? null; },
  setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; },
};

// The stubbed server: /api/admin/sales-report answers exactly as the real one
// does for a rep - 403 with the role sentence in the body.
let salesReportResponse = {
  status: 403,
  body: { error: "Role 'sales_rep' is not permitted to access this resource" },
};
const fetchLog = [];
const sandbox = {
  console, document: documentStub, sessionStorage: storageStub, localStorage: storageStub,
  setTimeout(f) { if (typeof f === 'function') f(); return 0; },
  clearTimeout() {}, setInterval() { return 0; }, clearInterval() {},
  alert() {}, confirm() { return true; },
  history: { pushState() {}, replaceState() {} },
  location: { pathname: '/sales-report', href: '' },
  fetch(url, opts) {
    fetchLog.push({ url: String(url), method: (opts && opts.method) || 'GET' });
    if (String(url).includes('/api/admin/sales-report')) {
      const r = salesReportResponse;
      return Promise.resolve({
        ok: r.status < 400, status: r.status,
        json: () => Promise.resolve(r.body),
      });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  },
  FormData: function () { this.append = function () {}; },
  FileReader: function () {}, Blob: function () {},
  URL: { createObjectURL() { return ''; }, revokeObjectURL() {} },
  navigator: { userAgent: 'harness' }, requestAnimationFrame() { return 0; },
  scrollTo() {},
  pdfjsLib: { getDocument() { return { promise: Promise.resolve({ numPages: 0 }) }; }, GlobalWorkerOptions: {} },
  Tesseract: { recognize() { return Promise.resolve({ data: { text: '' } }); } },
};
sandbox.window = sandbox; sandbox.globalThis = sandbox;

console.log('='.repeat(72));
console.log('RUNTIME PROOF - UBS 2.0 PHASE C0');
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
  for (let i = 0; i < 8; i += 1) await new Promise((r) => setImmediate(r));
}

(async function main() {
  // ═══════════════════════════════════════════════════════════════════
  section('1. A REP HITTING /sales-report GETS A DESIGNED STATE');
  elCache.clear(); fetchLog.length = 0;
  storageStub.setItem('dalton_auth_token', 'rep-token');
  // Seed the page as the markup leaves it: export button shown, card holding
  // the loading line.
  el('sales-report-export').style.display = '';
  await run('loadSalesReport()');
  await settle();

  const host = el('sales-report-table').innerHTML;
  const errEl = el('sales-report-error');
  const exportBtn = el('sales-report-export');

  check('the report endpoint was actually called',
        fetchLog.some((f) => f.url.includes('/api/admin/sales-report')));
  check('the Export CSV button is hidden', exportBtn.style.display === 'none');
  check('the bare red error paragraph stays hidden',
        errEl.style.display === 'none' && !errEl.textContent);
  check('the container is not left empty', host.length > 0);
  check('  ...it holds the designed state', /class="state-block"/.test(host));
  check('  ...headed "Admins only"', /Admins only/.test(host));
  check('  ...saying it is limited to admin accounts',
        /limited to admin accounts/.test(host));
  check('  ...and what to do instead', /Ask an admin/.test(host));
  check('  ...with a way back that is not a primary action',
        /showView\('dashboard'\)/.test(host) && /btn-secondary/.test(host)
        && !/btn-primary/.test(host));
  check('no report table is rendered', !/<table/.test(host));

  section('1b. NOTHING THE SERVER SAID REACHES THE SCREEN');
  const text = host.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
  [["the role identifier", /sales_rep/i],
   ["the server's sentence", /not permitted to access/i],
   ['the HTTP status', /403/],
   ["apiFetch's fallback wording", /request failed/i],
   ['any API path', /\/api\//]].forEach(([what, re_]) => {
    check(`  ${what} is absent from the rendered state`, !re_.test(text));
  });

  // ═══════════════════════════════════════════════════════════════════
  section('2. AN ADMIN IS UNAFFECTED');
  elCache.clear(); fetchLog.length = 0;
  salesReportResponse = {
    status: 200,
    body: {
      columns: ['Agent Name', 'Customer Name', 'Enrollment #', 'Customer Email',
                'Status', 'Utility', 'Added', 'Last Modified'],
      rows: [{ agent_name: 'Charlie Mren', customer_name: 'Dana Reyes',
               enrollment_code: 'ENR-2026-000005', customer_email: 'dana@example.com',
               status: 'Complete', utility: 'Central Hudson Gas & Electric',
               created_at: '2026-10-01 12:00:00', updated_at: '2026-10-02 09:30:00' }],
    },
  };
  el('sales-report-export').style.display = 'none';   // left hidden by the rep
  await run('loadSalesReport()');
  await settle();
  const adminHost = el('sales-report-table').innerHTML;
  check('the Export CSV button is restored',
        el('sales-report-export').style.display === '');
  check('the report renders', /<table/.test(adminHost)
        && /ENR-2026-000005/.test(adminHost));
  check('  ...and the admin-only state is gone',
        !/state-block/.test(adminHost) && !/Admins only/.test(adminHost));

  // ═══════════════════════════════════════════════════════════════════
  section('3. A GENUINE FAILURE STILL REPORTS ITSELF');
  // The 403 branch must not swallow real errors - a 500 still surfaces.
  elCache.clear(); fetchLog.length = 0;
  salesReportResponse = { status: 500, body: { error: 'Database unavailable' } };
  el('sales-report-export').style.display = '';
  await run('loadSalesReport()');
  await settle();
  check('a 500 is not treated as a permission refusal',
        !/state-block/.test(el('sales-report-table').innerHTML));
// C2: an error is shown by CLEARING the inline display so the shared
// .alert can be the flex row it is defined as - forcing 'block' would
// stack its icon above the message. "Shown" is asserted as "not
// hidden", which holds whichever way a caller reveals it.
  check('  ...it still surfaces through the error element',
        el('sales-report-error').style.display !== 'none'
        && /Database unavailable/.test(el('sales-report-error').textContent));
  check('  ...and the Export button is not hidden by it',
        el('sales-report-export').style.display === '');

  // ═══════════════════════════════════════════════════════════════════
  section('4. AGREEMENT NAMES ARE NAMED, NOT CANCELLED');
  // The strike came from the stylesheet, but WHICH names take that class is
  // decided here. A contract with no openable URL must still be rendered by
  // name, in the acknowledgement sentence.
  elCache.clear();
  run("Agreements.contracts = ["
      + "{contract_name:'Community Solar Subscription Agreement'},"
      + "{contract_name:'Disclosure Form', index:1, url_present:true}];");
  const links = run('agreementLinksHtml()');
  check('a contract with no openable URL is still named in full',
        /Community Solar Subscription Agreement/.test(links));
  check('  ...marked as not openable', /class="agr-link-off"/.test(links));
  check('  ...and explains why on hover',
        /title="This document is not available to open"/.test(links));
  check('  ...with no inline strike anywhere in the markup',
        !/line-through/.test(links) && !/<s>/.test(links)
        && !/<del/.test(links) && !/text-decoration/.test(links));
  check('an openable contract is still a real link',
        /class="agr-link"/.test(links) && /Disclosure Form/.test(links));

  report();
}());

function report() {
  const failed = results.filter((r) => !r.ok);
  console.log('\n' + '='.repeat(72));
  console.log(`${results.length - failed.length} passed, ${failed.length} failed`);
  console.log('='.repeat(72));
  if (failed.length) {
    failed.forEach((r) => console.log('  FAILED: ' + r.label));
    process.exit(1);
  }
  process.exit(0);
}

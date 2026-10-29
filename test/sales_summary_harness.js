/*
 * RUNTIME PROOF: the Sales Summary panel renders what the server sent, and
 * only what the server sent.
 *
 * WHAT THIS GUARDS THAT THE PYTHON SUITE CANNOT
 *
 *   The Python suite proves the COUNTS are right. This proves the panel does
 *   not quietly invent, re-bucket or re-date anything on the way to the
 *   screen - the classic way a correct API grows a wrong dashboard:
 *
 *     - switching range must re-read the payload, never re-request and never
 *       recompute a total from the rows it happens to have drawn;
 *     - the figures on screen must reconcile with each other, because that is
 *       what an admin will check first;
 *     - a 403 must leave nothing of the panel behind, including from an
 *       earlier admin session in the same tab;
 *     - an empty range must render the composed empty state rather than a
 *       grid of zeroes.
 *
 *   It runs the real static/js/app.js against a DOM stub built from the real
 *   ids in templates/index.html, so a renamed element fails here.
 *
 * node test/sales_summary_harness.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(ROOT, 'templates', 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(ROOT, 'static', 'css', 'app.css'), 'utf8');
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
    offsetWidth: 1, _attrs: {}, _listeners: {},
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, on) {
        if (on === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); }
        else if (on) { this._s.add(c); } else { this._s.delete(c); }
      },
      contains(c) { return this._s.has(c); },
    },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
    removeAttribute(k) { delete this._attrs[k]; },
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
    fetchLog.push({ url: String(url), method: (opts && opts.method) || 'GET' });
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
console.log('RUNTIME PROOF - SALES SUMMARY PANEL');
console.log('='.repeat(72));

const ctx = vm.createContext(sandbox);
vm.runInContext(src, ctx, { filename: 'app.js' });
check('app.js evaluates without throwing', true);

function el(id) { return documentStub.getElementById(id); }
function run(code) { return vm.runInContext(code, ctx); }
async function settle() {
  for (let i = 0; i < 8; i += 1) await new Promise((r) => setImmediate(r));
}
function bodyHtml() { return el('ss-body').innerHTML; }
function summaryCalls() {
  return fetchLog.filter((f) => f.url.includes('/api/admin/sales-summary'));
}

// ── A payload shaped exactly like the endpoint's, with invented figures ─────
function payload() {
  const mk = (range, label, total, utilities, start, end) => ({
    range, label, total,
    utilities_active: utilities.length,
    top_utility: utilities[0] || null,
    top_program: total
      ? (() => {
        const tally = {};
        utilities.forEach((u) => u.programs.forEach((p) => {
          tally[p.program] = (tally[p.program] || 0) + p.count;
        }));
        const best = Object.keys(tally).sort((a, b) => tally[b] - tally[a])[0];
        return { program: best, count: tally[best],
                 share: Math.round(tally[best] * 1000 / total) / 10 };
      })()
      : null,
    by_utility: utilities,
    by_program: total ? [{ program: 'Residential', count: total, share: 100 }] : [],
    window: { timezone: 'America/New_York', start_local: start, end_local: end,
              start_utc: '', end_utc: '' },
  });
  const u = (name, count, programs) => ({
    utility: name, count, share: programs.share || 0, programs,
  });
  const today = [
    { utility: 'National Grid NY', count: 3, share: 60,
      programs: [{ program: 'Residential', count: 2, share: 40 },
                 { program: 'Residential LMI (IRA)', count: 1, share: 20 }] },
    { utility: 'NYSEG', count: 2, share: 40,
      programs: [{ program: 'Residential', count: 1, share: 20 },
                 { program: 'Program not recorded', count: 1, share: 20 }] },
  ];
  const month = [
    { utility: 'National Grid NY', count: 18, share: 42.9,
      programs: [{ program: 'Residential', count: 11, share: 26.2 },
                 { program: 'Residential LMI', count: 7, share: 16.7 }] },
    { utility: 'NYSEG', count: 11, share: 26.2,
      programs: [{ program: 'Residential', count: 11, share: 26.2 }] },
    { utility: 'Rochester Gas & Electric', count: 7, share: 16.7,
      programs: [{ program: 'Residential LMI', count: 7, share: 16.7 }] },
    { utility: 'Dominion Energy', count: 6, share: 14.3,
      programs: [{ program: 'Residential LMI', count: 6, share: 14.3 }] },
  ];
  void u;
  return {
    order: ['today', 'week', 'month'],
    week_starts: 'monday',
    generated_at_local: '2026-10-29T14:00:00-04:00',
    unrecorded_program_label: 'Program not recorded',
    unrecorded_utility_label: 'Utility not recorded',
    ranges: {
      today: mk('today', 'Today', 5, today,
                '2026-10-29T00:00:00-04:00', '2026-10-30T00:00:00-04:00'),
      week: mk('week', 'This Week', 0, [],
               '2026-10-26T00:00:00-04:00', '2026-11-02T00:00:00-04:00'),
      month: mk('month', 'This Month', 42, month,
                '2026-10-01T00:00:00-04:00', '2026-11-01T00:00:00-04:00'),
    },
  };
}

(async () => {
  // ══════════════════════════════════════════════════════════════════════
  section('1. ONE REQUEST CARRIES ALL THREE RANGES');
  // ══════════════════════════════════════════════════════════════════════
  routes = { '/api/admin/sales-summary': { body: payload() } };
  run('loadSalesSummary();');
  await settle();
  check('the panel is shown', el('sales-summary').style.display === '');
  check('exactly one request was made', summaryCalls().length === 1);
  check('it opens on Today',
    el('ss-tab-today').classList.contains('is-on')
    && el('ss-tab-today').getAttribute('aria-selected') === 'true');
  check('  ...and the other two are off',
    !el('ss-tab-week').classList.contains('is-on')
    && el('ss-tab-month').getAttribute('aria-selected') === 'false');

  // ══════════════════════════════════════════════════════════════════════
  section('2. WHAT IS ON SCREEN RECONCILES');
  // ══════════════════════════════════════════════════════════════════════
  let h = bodyHtml();
  check('the total is the range total', /Total completed sales[\s\S]{0,120}>5</.test(h));
  check('both utilities are listed',
    h.includes('National Grid NY') && h.includes('NYSEG'));
  const counts = [...h.matchAll(/class="ss-util-count">(\d+) sales?/g)].map((m) => +m[1]);
  check(`the utility counts on screen sum to the total (${counts.join(' + ')})`,
    counts.reduce((a, b) => a + b, 0) === 5);
  const progN = [...h.matchAll(/class="ss-line-n">(\d+)</g)].map((m) => +m[1]);
  check(`the program counts on screen sum to the total (${progN.join(' + ')})`,
    progN.reduce((a, b) => a + b, 0) === 5);
  check('utilities active is the number of utilities shown',
    /Utilities active[\s\S]{0,120}>2</.test(h));
  check('the top utility card names the leader',
    /Top utility[\s\S]{0,200}National Grid NY/.test(h));
  check('a bar is drawn from the share the server sent, not a measured width',
    h.includes('class="ss-bar-fill" style="width:60%"'));
  check('the IRA program keeps its rep-facing label',
    h.includes('Residential LMI (IRA)') && !h.includes('LMI_IRA'));
  check('a sale with no recorded program is shown, and marked as such',
    h.includes('Program not recorded') && h.includes('is-unrecorded'));
  check('the window names the business timezone, not the browser\'s',
    /Eastern time/.test(el('ss-window').textContent));

  // ══════════════════════════════════════════════════════════════════════
  section('3. SWITCHING RANGE IS INSTANT, AND RE-READS THE PAYLOAD');
  // ══════════════════════════════════════════════════════════════════════
  run("setSalesSummaryRange('month');");
  await settle();
  h = bodyHtml();
  check('no second request was made', summaryCalls().length === 1);
  check('the month total is shown', /Total completed sales[\s\S]{0,120}>42</.test(h));
  check('  ...with all four utilities',
    ['National Grid NY', 'NYSEG', 'Rochester Gas &amp; Electric', 'Dominion Energy']
      .every((n) => h.includes(n)));
  check('  ...and a utility name carrying an ampersand is escaped, not raw',
    h.includes('Rochester Gas &amp; Electric')
    && !h.includes('Rochester Gas & Electric'));
  const mCounts = [...h.matchAll(/class="ss-util-count">(\d+) sales?/g)].map((m) => +m[1]);
  check(`the month counts still reconcile (${mCounts.join(' + ')})`,
    mCounts.reduce((a, b) => a + b, 0) === 42);
  check('the tab moved', el('ss-tab-month').classList.contains('is-on')
    && !el('ss-tab-today').classList.contains('is-on'));
  check('the window line moved to the month',
    /Month of/.test(el('ss-window').textContent));
  run("setSalesSummaryRange('today');");
  await settle();
  check('switching back shows Today again, still with no request',
    /Total completed sales[\s\S]{0,120}>5</.test(bodyHtml())
    && summaryCalls().length === 1);
  run("setSalesSummaryRange('last-quarter');");
  await settle();
  check('an unknown range is ignored rather than blanking the panel',
    el('ss-tab-today').classList.contains('is-on')
    && /Total completed sales/.test(bodyHtml()));

  // ══════════════════════════════════════════════════════════════════════
  section('4. AN EMPTY RANGE IS A COMPOSED ANSWER, NOT A WALL OF ZEROES');
  // ══════════════════════════════════════════════════════════════════════
  run("setSalesSummaryRange('week');");
  await settle();
  h = bodyHtml();
  check('it says there are none yet', h.includes('No completed sales yet this week'));
  check('  ...with a line explaining when one appears',
    h.includes('accepted their agreement'));
  check('  ...and no stat cards or zeroes are drawn',
    !h.includes('ss-grid') && !h.includes('>0<'));
  check('  ...and no empty breakdown panels either',
    !h.includes('ss-panel'));

  // ══════════════════════════════════════════════════════════════════════
  section('5. A VIEWER WHO MAY NOT SEE IT, DOES NOT');
  // ══════════════════════════════════════════════════════════════════════
  run("setSalesSummaryRange('today');");
  await settle();
  check('precondition: the panel is on screen with data',
    el('sales-summary').style.display === '' && /National Grid/.test(bodyHtml()));
  routes = { '/api/admin/sales-summary': { status: 403, body: { error: 'nope' } } };
  run('loadSalesSummary();');
  await settle();
  check('a 403 hides the panel outright',
    el('sales-summary').style.display === 'none');
  check('  ...and drops the data it was holding',
    run('salesSummary') === null);

  // ══════════════════════════════════════════════════════════════════════
  section('6. A FAILURE SAYS SO RATHER THAN SHOWING WRONG NUMBERS');
  // ══════════════════════════════════════════════════════════════════════
  routes = { '/api/admin/sales-summary': {
    status: 503,
    body: { error: 'Sales summary is unavailable because the server\'s timezone '
                 + 'database is missing, so business-day boundaries cannot be '
                 + 'calculated. Install tzdata on the server.' } } };
  run('loadSalesSummary();');
  await settle();
  h = bodyHtml();
  check('the panel reports the failure', h.includes('Summary unavailable'));
  check('  ...in the server\'s own words', h.includes('timezone database is missing'));
  check('  ...and shows no counts at all', !h.includes('ss-grid'));

  // ══════════════════════════════════════════════════════════════════════
  section('7. THE PANEL IS ADDITIVE, AND BUILT FROM THE DESIGN SYSTEM');
  // ══════════════════════════════════════════════════════════════════════
  check('the summary sits ABOVE the detailed table in the markup',
    html.indexOf('id="sales-summary"') < html.indexOf('id="sales-report-table"'));
  check('the existing table and its export are untouched',
    html.includes('id="sales-report-table"')
    && html.includes('id="sales-report-export"'));
  check('loading the report loads the summary beside it',
    /loadSalesSummary\(\);/.test(src.split('async function loadSalesReport')[1].slice(0, 900)));
  check('the panel has an accessible name and a live region',
    html.includes('aria-labelledby="ss-heading"')
    && html.includes('aria-live="polite"'));
  check('the range selector is a tablist with real buttons',
    html.includes('role="tablist"')
    && (html.match(/class="ss-seg-btn[^"]*" role="tab"/g) || []).length === 3);
  // THE COLLISION THIS PANEL ALREADY HIT ONCE. Its heading was .sum-title,
  // which belongs to the agreement summary and carries mono + uppercase +
  // wide tracking; the section heading duly rendered as a label. Every class
  // in the panel is now ss- prefixed, and nothing it uses may be a name that
  // already existed in the stylesheet.
  const sumCss = css.slice(css.indexOf('SALES SUMMARY (admin'));
  const before = css.slice(0, css.indexOf('SALES SUMMARY (admin'));
  const panelClasses = [...new Set([
    ...[...html.matchAll(/class="([^"]+)"/g)].map((m) => m[1]),
  ].join(' ').split(/\s+/).filter((c) => c.startsWith('ss')))];
  const collided = panelClasses.filter(
    (c) => new RegExp('\\.' + c + '(?![\\w-])').test(before));
  check(`no panel class reuses a name the stylesheet already had `
    + `(${panelClasses.length} checked)`, collided.length === 0);
  check('the heading explicitly resets the transform it once inherited',
    /\.ss-title\{[^}]*text-transform:none/.test(sumCss));
  // Comments stripped first: the block EXPLAINS this rule in prose, and a
  // check that reads its own documentation as a selector proves nothing.
  // The slice starts INSIDE the block's opening comment, so the opener is put
  // back before stripping - otherwise the first */ closes a comment that was
  // never opened and the prose survives as if it were a rule.
  const sumRules = ('/*' + sumCss).replace(/\/\*[\s\S]*?\*\//g, '');
  check('the modifiers are only ever compounded onto a panel class',
    !/(^|[\s,{}])\.is-(on|lead|text|unrecorded)/.test(sumRules));
  const literalSizes = [...sumCss.matchAll(/font-size:\s*([^;}]+)/g)]
    .map((m) => m[1].trim()).filter((v) => !v.startsWith('var('));
  check(`every font size is a scale token (${literalSizes.length} literals)`,
    literalSizes.length === 0);
  const literalWeights = [...sumCss.matchAll(/font-weight:\s*([^;}]+)/g)]
    .map((m) => m[1].trim()).filter((v) => !v.startsWith('var('));
  check(`every font weight is a token (${literalWeights.length} literals)`,
    literalWeights.length === 0);
  check('the grid starts at one column and gains them at the breakpoints',
    /\.ss-grid\{display:grid;grid-template-columns:1fr/.test(sumCss)
    && /@media \(min-width:600px\)\{[\s\S]*?\.ss-grid\{grid-template-columns:repeat\(2/.test(sumCss)
    && /@media \(min-width:1200px\)\{[\s\S]*?\.ss-grid\{grid-template-columns:repeat\(4/.test(sumCss));
  check('nothing in the panel is sized in a fixed width that could overflow',
    !/\.ss[^{]*\{[^}]*width:\s*\d{3,}px/.test(sumCss));
  check('long utility and program names wrap instead of pushing the page wide',
    /\.ss-util-name\{[^}]*overflow-wrap:anywhere/.test(sumCss)
    && /\.ss-line-name\{[^}]*overflow-wrap:anywhere/.test(sumCss)
    && /\.ss-v\{[^}]*overflow-wrap:anywhere/.test(sumCss));

  console.log('\n' + '='.repeat(72));
  const failed = results.filter((r) => !r.ok);
  console.log(failed.length
    ? `SALES SUMMARY - ${failed.length} of ${results.length} CHECKS FAILED`
    : `SALES SUMMARY - ALL ${results.length} CHECKS PASSED`);
  console.log('='.repeat(72));
  failed.forEach((r) => console.log('  FAILED: ' + r.label));
  process.exit(failed.length ? 1 : 0);
})();

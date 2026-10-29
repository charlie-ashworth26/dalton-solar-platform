/*
 * RUNTIME PROOF: the Availability utility dropdown is driven by Perch market
 * rules, and auto-selects ONLY when the state has exactly one utility.
 *
 * WHY THIS EXISTS AND WHY IT IS NOT A SOURCE CHECK
 * ------------------------------------------------
 * "The rep is not shown a guess" is a claim about what happens when the code
 * RUNS. A source check for `if(body.auto_select){` cannot prove it: a mutation
 * that preselects options[0] and leaves that branch intact as an `else if`
 * passes every grep while violating the requirement outright. That mutation was
 * written and did pass the source-level suite, which is why this harness exists.
 *
 * METHOD
 *   1. Evaluate app.js in a VM with a DOM stub whose elements RECORD their
 *      event listeners, so the ZIP 'input' handler can actually be fired.
 *   2. Call wireZipDrivenUtilities({key:'service_area'}) as the renderer does.
 *   3. Drive the ZIP field for each case and assert the resulting DOM:
 *        one utility   -> that slug is selected
 *        two utilities -> NOTHING is selected
 *        zero          -> primary action disabled, message shown
 *        cleared ZIP   -> the opening options are restored
 *        lookup error  -> opening options kept, error surfaced
 *   4. Assert the request actually carried the ZIP, and that no request was
 *      ever made that carried a utility slug derived from the ZIP.
 *   5. Drive the real submit path and read the OUTGOING REQUEST BODY, which is
 *      the only thing that proves what Perch actually receives.
 *
 * BOTH FLOWS ARE COVERED
 *   utility-first - on load, before any ZIP, the dropdown lists every utility
 *                   across every enabled market and preselects nothing.
 *   ZIP-first     - a ZIP narrows to its state and preselects only when that
 *                   state has exactly one utility.
 *   together      - a ZIP that contradicts an already-chosen utility clears it
 *                   with a message, and the pair is never submitted.
 *
 * THE DEFECT THIS WAS BUILT FOR
 *   A dropdown can read correctly on screen while submitting something else:
 *   the label is what a human checks and the VALUE is what is transmitted.
 *   Section 8 therefore asserts the request body, not the DOM.
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(ROOT, 'templates', 'index.html'), 'utf8');
const REAL_IDS = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));
// Fields the generic renderer creates at runtime for the service_area step.
['wf-email', 'wf-zip_code', 'wf-utility_name', 'wf-err-utility_name',
 'wf-err-zip_code', 'wf-primary', 'wf-form-error', 'workflow-root']
  .forEach((id) => REAL_IDS.add(id));

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
    _listeners: {},
    classList: {
      _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, on) { if (on === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); } else if (on) { this._s.add(c); } else { this._s.delete(c); } },
      contains(c) { return this._s.has(c); },
    },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    appendChild() {}, insertBefore() {}, removeChild() {},
    // RECORDS listeners, unlike the other harnesses' no-op stub - firing them
    // is the whole point here.
    addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
    removeEventListener() {},
    async fire(ev) {
      for (const fn of (this._listeners[ev] || [])) await fn({ target: this });
    },
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
  setTimeout(fn) { if (typeof fn === 'function') fn(); return 0; },
  clearTimeout() {}, setInterval() { return 0; }, clearInterval() {},
  alert() {}, confirm() { return true; },
  fetch(url, opts) {
    // The BODY is logged, not just the URL: what reaches Perch is the payload,
    // and asserting the DOM alone is exactly how a label/value swap hides.
    fetchLog.push({ url: String(url), method: (opts && opts.method) || 'GET',
                    body: (opts && opts.body) || null });
    for (const key of Object.keys(routes)) {
      if (String(url).includes(key)) {
        const r = routes[key];
        return Promise.resolve({
          ok: (r.status || 200) === 200, status: r.status || 200,
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
console.log('RUNTIME PROOF - AVAILABILITY UTILITY SELECTION');
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

const OPENING_OPTIONS =
  '<option value="">Select a utility</option>' +
  '<option value="nyseg">NYSEG</option>' +
  '<option value="national-grid-ny">National Grid NY</option>';

function el(id) { return documentStub.getElementById(id); }

function fresh() {
  elCache.clear();
  fetchLog.length = 0;
  const zip = el('wf-zip_code');
  const util = el('wf-utility_name');
  const err = el('wf-err-utility_name');
  const primary = el('wf-primary');
  util.innerHTML = OPENING_OPTIONS;
  util.value = '';
  err.style.display = 'none';
  primary.disabled = false;
  vm.runInContext("wireZipDrivenUtilities({key:'service_area'})", ctx);
  return { zip, util, err, primary };
}

// A ZIP handler can now await a fetch that itself awaits another (a cleared
// ZIP falls through to the cross-market load), so one or two turns is no
// longer enough to settle the queue.
async function settle() {
  for (let i = 0; i < 6; i += 1) await new Promise((r) => setImmediate(r));
}

async function typeZip(dom, value) {
  dom.zip.value = value;
  await dom.zip.fire('input');
  await settle();
}

const NY_SLUGS = ['nyseg', 'national-grid-ny', 'central-hudson', 'coned',
                  'rge', 'oru', 'nationalgrid-upstate'];
const NY_LABELS = ['NYSEG', 'National Grid NY', 'Central Hudson', 'Coned',
                   'Rge', 'Oru', 'Nationalgrid Upstate'];
// The utility-first answer: every utility in every enabled market, each tagged
// with the state it serves, and NOTHING preselected.
const UNION_BODY = {
  zip: null, state: null, markets: ['NY', 'VA'], source: 'perch',
  options: NY_SLUGS.map((v, i) => ({ value: v, label: NY_LABELS[i], state: 'NY' }))
    .concat([{ value: 'dominion-energy-va', label: 'Dominion Energy',
               state: 'VA' }]),
  auto_select: null,
};
const VA_ZIP_BODY = {
  zip: '24112', state: 'VA', markets: ['NY', 'VA'], source: 'perch',
  options: [{ value: 'dominion-energy-va', label: 'Dominion Energy',
              state: 'VA' }],
  auto_select: 'dominion-energy-va',
};
const NY_ZIP_BODY = {
  zip: '12401', state: 'NY', markets: ['NY', 'VA'], source: 'perch',
  options: [{ value: 'nyseg', label: 'NYSEG', state: 'NY' },
            { value: 'national-grid-ny', label: 'National Grid NY', state: 'NY' }],
  auto_select: null,
};
// Keys are matched by substring in insertion order, so the ZIP-specific route
// MUST come first - otherwise the bare union route would answer both.
function bothRoutes(zipBody) {
  return { 'utility-options?zip=': { body: zipBody },
           'utility-options': { body: UNION_BODY } };
}

function setServiceAreaStep() {
  // The step descriptor submitCapacity() validates against. MUST be re-set per
  // section: a successful capacity call assigns currentWorkflow = body.workflow,
  // which is the capacity_result step with NO fields - and validating against a
  // zero-field step would let an empty payload through and silently pass.
  vm.runInContext(`currentWorkflow = {step: {key:'service_area', fields:[
      {name:'email', label:'Customer email', type:'text', required:true},
      {name:'zip_code', label:'ZIP code', type:'text', required:true},
      {name:'utility_name', label:'Utility', type:'select', required:true}
    ], panels:[], primary_action:{label:'Check availability',
                                 operation:'check_capacity'}}};
    currentDraft = null;`, ctx);
}

const CAPACITY_OK = {
  body: {
    enrollment_id: 1, enrollment_code: 'H-1',
    result: {
      zip_code: '24112', utility_slug: 'dominion-energy-va',
      utility_display_name: 'Dominion Energy',
      capacity_available: true, project_details: {},
    },
  },
};

function capacityRequests() {
  return fetchLog.filter((f) => f.url.includes('/enrollments/capacity'));
}

function selectedValues(innerHTML) {
  return [...String(innerHTML).matchAll(/value="([^"]*)"/g)].map((m) => m[1]);
}

(async function run() {
  // ───────────────────────────────────────────────────────────────────
  section('0. UTILITY-FIRST: EVERY MARKET IS OFFERED BEFORE ANY ZIP');
  routes = bothRoutes(VA_ZIP_BODY);
  let dom = fresh();
  await settle();
  let vals = selectedValues(dom.util.innerHTML).filter(Boolean);
  check(`on load the dropdown lists every enabled market (${vals.length})`,
        vals.length === 8);
  check('  ...including the Virginia utility, with NO ZIP entered',
        vals.includes('dominion-energy-va'));
  check('  ...and every New York utility',
        NY_SLUGS.every((v) => vals.includes(v)));
  check('  ...with NOTHING preselected from a cross-market union',
        dom.util.value === '');
  check('  ...the primary action is not blocked', dom.primary.disabled === false);
  check('  ...and the request asked for no particular state',
        fetchLog.some((f) => f.url.includes('utility-options')
                          && !f.url.includes('zip=')));

  section('0b. THE OPTION VALUE IS THE SLUG; THE LABEL IS ONLY ON SCREEN');
  check('the slug is the submitted value',
        String(dom.util.innerHTML).includes('value="dominion-energy-va"'));
  check('  ...and the readable name is the option TEXT',
        String(dom.util.innerHTML).includes('>Dominion Energy</option>'));
  check('  ...so no option carries a display name as its value',
        !NY_LABELS.concat(['Dominion Energy'])
          .some((lbl) => vals.includes(lbl)));

  section('0c. A COMPATIBLE ZIP KEEPS THE UTILITY THE REP PICKED FIRST');
  dom.util.value = 'dominion-energy-va';
  await typeZip(dom, '24112');
  check('the rep\'s earlier choice survives a ZIP in the same state',
        dom.util.value === 'dominion-energy-va');
  check('  ...and the list narrowed to that state',
        selectedValues(dom.util.innerHTML).filter(Boolean).length === 1);
  check('  ...with no error shown', dom.err.style.display === 'none');

  section('0d. AN INCOMPATIBLE ZIP CLEARS IT, LOUDLY');
  routes = bothRoutes(NY_ZIP_BODY);
  dom = fresh();
  await settle();
  dom.util.value = 'dominion-energy-va';   // chosen from the union, before a ZIP
  await typeZip(dom, '12401');
  check('a utility that does not serve the ZIP is CLEARED',
        dom.util.value === '');
  check('  ...never left in place to be submitted',
        dom.util.value !== 'dominion-energy-va');
  check('  ...with a message naming the state it does not serve',
        dom.err.style.display === 'block'
        && dom.err.textContent.includes('does not serve')
        && dom.err.textContent.includes('NY'));
  check('  ...in NEUTRAL text, not danger red',
        dom.err.style.color === 'var(--ink-muted)');
  check('  ...and the dropdown is NOT locked',
        dom.util.disabled === false);
  check('  ...with every NY utility still selectable',
        selectedValues(dom.util.innerHTML).includes('nyseg')
        && selectedValues(dom.util.innerHTML).includes('national-grid-ny'));
  // The rep can still override, which is the whole point of not locking it.
  dom.util.value = 'national-grid-ny';
  check('  ...and the rep can pick one manually',
        dom.util.value === 'national-grid-ny');

  check('  ...but the screen is NOT dead-ended: the rep can pick a valid one',
        dom.primary.disabled === false
        && selectedValues(dom.util.innerHTML).includes('nyseg'));

  section('0d2. A ZIP WITH ONE CLEAR UTILITY SWITCHES AUTOMATICALLY');
  // The reported behaviour: National Grid selected, rep types a Dominion ZIP.
  // UBS should simply select Dominion Energy - no red, no dead end.
  routes = bothRoutes(VA_ZIP_BODY);
  dom = fresh();
  await settle();
  dom.util.value = 'national-grid-ny';
  await typeZip(dom, '24112');
  check('the incompatible utility is replaced, not just cleared',
        dom.util.value === 'dominion-energy-va');
  check('  ...so the rep is never left with an empty required field',
        dom.util.value !== '');
  check('  ...and is TOLD it changed', dom.err.style.display === 'block'
        && /switched to/i.test(dom.err.textContent));
  check('  ...naming the utility in human words, not the slug',
        dom.err.textContent.includes('Dominion Energy')
        && !dom.err.textContent.includes('dominion-energy-va'));
  check('  ...neutrally, not in red',
        dom.err.style.color === 'var(--ink-muted)');
  check('  ...without blocking the action',
        dom.primary.disabled === false);
  check('  ...and the dropdown stays editable',
        dom.util.disabled === false);

  section('0e. CLEARING THE ZIP GOES BACK TO THE FULL UNION');
  routes = bothRoutes(VA_ZIP_BODY);
  dom = fresh();
  await settle();
  await typeZip(dom, '24112');
  check('precondition: narrowed to one state',
        selectedValues(dom.util.innerHTML).filter(Boolean).length === 1);
  await typeZip(dom, '');
  check('clearing the ZIP brings back every enabled market',
        selectedValues(dom.util.innerHTML).filter(Boolean).length === 8);
  check('  ...rather than stranding the rep in one state',
        selectedValues(dom.util.innerHTML).some((v) => NY_SLUGS.includes(v)));

  // ───────────────────────────────────────────────────────────────────
  section('1. EXACTLY ONE UTILITY -> AUTO-SELECTED');
  routes = {
    '/api/markets/utility-options': {
      body: {
        zip: '24112', state: 'VA', source: 'perch',
        options: [{ value: 'dominion-energy-va', label: 'Dominion Energy' }],
        auto_select: 'dominion-energy-va',
      },
    },
  };
  dom = fresh();
  await typeZip(dom, '24112');
  check('the utility list was replaced from the API',
        selectedValues(dom.util.innerHTML).includes('dominion-energy-va'));
  check('  ...and the single utility is SELECTED',
        dom.util.value === 'dominion-energy-va');
  check('  ...with its display name on screen',
        String(dom.util.innerHTML).includes('Dominion Energy'));
  check('  ...the primary action stays enabled', dom.primary.disabled === false);
  check('  ...no error is shown', dom.err.style.display === 'none');
  check('the request carried the ZIP',
        fetchLog.some((f) => f.url.includes('utility-options?zip=24112')));
  check('  ...and carried NO utility slug (ZIP resolves state only)',
        !fetchLog.some((f) => /utility(_name)?=/.test(f.url)));

  // ───────────────────────────────────────────────────────────────────
  section('2. TWO UTILITIES -> NOTHING IS GUESSED');
  routes = {
    '/api/markets/utility-options': {
      body: {
        zip: '12401', state: 'NY', source: 'perch',
        options: [{ value: 'nyseg', label: 'NYSEG' },
                  { value: 'national-grid-ny', label: 'National Grid NY' }],
        auto_select: null,
      },
    },
  };
  dom = fresh();
  await typeZip(dom, '12401');
  check('both utilities are offered',
        selectedValues(dom.util.innerHTML).filter(Boolean).length === 2);
  check('  ...NOTHING is selected - this is the requirement',
        dom.util.value === '');
  check('  ...specifically NOT the first option',
        dom.util.value !== 'nyseg');
  check('  ...and the primary action stays enabled', dom.primary.disabled === false);

  section('2b. THE REP’S OWN CHOICE SURVIVES A REFETCH');
  dom.util.value = 'national-grid-ny';
  await typeZip(dom, '12402');
  check('a re-fetch keeps the utility the rep had chosen',
        dom.util.value === 'national-grid-ny');

  // ───────────────────────────────────────────────────────────────────
  section('3. NO SUPPORTED UTILITIES -> AVAILABILITY CANNOT CONTINUE');
  routes = {
    '/api/markets/utility-options': {
      body: {
        zip: '07030', state: 'NJ', source: 'perch', options: [], auto_select: null,
        message: 'No community solar utilities are currently available in NJ. '
               + 'This enrollment cannot continue.',
      },
    },
  };
  dom = fresh();
  await typeZip(dom, '07030');
  check('nothing is selectable', dom.util.value === '');
  check('  ...the primary action is DISABLED', dom.primary.disabled === true);
  check('  ...and the API message is shown to the rep',
        dom.err.style.display === 'block'
        && dom.err.textContent.includes('cannot continue'));

  // ───────────────────────────────────────────────────────────────────
  section('4. A PARTIAL OR CLEARED ZIP FALLS BACK TO THE FULL UNION');
  routes = bothRoutes(VA_ZIP_BODY);
  dom = fresh();
  await typeZip(dom, '24112');
  check('precondition: VA was applied', dom.util.value === 'dominion-energy-va');
  await typeZip(dom, '241');
  // DELIBERATELY CHANGED by the utility-first flow. This used to assert the
  // step descriptor's original options came back. Falling back to the
  // cross-market union is strictly better - the rep gets every supported
  // utility rather than the NY-only set the descriptor was built with - so
  // the assertion is rewritten, not relaxed. The property that matters is
  // that an incomplete ZIP never leaves the dropdown empty or narrowed.
  check('a partial ZIP falls back to the cross-market union',
        selectedValues(dom.util.innerHTML).filter(Boolean).length === 8);
  check('  ...and makes no request for an incomplete ZIP',
        !fetchLog.some((f) => f.url.includes('zip=241&')
                           || f.url.endsWith('zip=241')));
  check('  ...and re-enables the primary action', dom.primary.disabled === false);

  dom = fresh();
  const before = dom.util.innerHTML;
  await typeZip(dom, '');
  check('an empty ZIP offers every market, not an empty list',
        selectedValues(dom.util.innerHTML).filter(Boolean).length === 8);
  check('  ...and never fewer options than the screen opened with',
        selectedValues(dom.util.innerHTML).filter(Boolean).length
          >= selectedValues(before).filter(Boolean).length);

  // ───────────────────────────────────────────────────────────────────
  section('4b. A SLOW RESPONSE CANNOT OVERWRITE A NEWER ZIP');
  // The real race: the rep types a ZIP, keeps typing, and the FIRST lookup
  // lands after the ZIP has already changed. Applying it would show options
  // for a state the rep is no longer entering.
  let release;
  const deferred = new Promise((r) => { release = r; });
  routes = {};
  sandbox.fetch = function (url) {
    fetchLog.push({ url: String(url), method: 'GET' });
    return deferred.then(() => ({
      ok: true, status: 200,
      json: () => Promise.resolve({
        zip: '24112', state: 'VA', source: 'perch',
        options: [{ value: 'dominion-energy-va', label: 'Dominion Energy' }],
        auto_select: 'dominion-energy-va',
      }),
    }));
  };
  dom = fresh();
  dom.zip.value = '24112';
  const inflight = dom.zip.fire('input');      // deliberately NOT awaited
  dom.zip.value = '12401';                     // the rep kept typing
  release();
  await inflight;
  await new Promise((r) => setImmediate(r));
  check('a response for the OLD ZIP is discarded',
        dom.util.value !== 'dominion-energy-va');
  check('  ...and the dropdown is not repopulated from it',
        !String(dom.util.innerHTML).includes('Dominion Energy'));
  // restore the normal routed fetch for the remaining sections
  sandbox.fetch = function (url, opts) {
    fetchLog.push({ url: String(url), method: (opts && opts.method) || 'GET',
                    body: (opts && opts.body) || null });
    for (const key of Object.keys(routes)) {
      if (String(url).includes(key)) {
        const r = routes[key];
        return Promise.resolve({
          ok: (r.status || 200) === 200, status: r.status || 200,
          json: () => Promise.resolve(r.body || {}),
        });
      }
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  };

  // ───────────────────────────────────────────────────────────────────
  section('5. A FAILED LOOKUP DOES NOT EMPTY THE SCREEN');
  routes = {
    '/api/markets/utility-options': {
      status: 503, body: { error: 'Perch market data is temporarily unavailable' },
    },
  };
  dom = fresh();
  await typeZip(dom, '24112');
  check('the opening options are kept rather than cleared',
        dom.util.innerHTML === OPENING_OPTIONS);
  check('  ...and the reason is surfaced', dom.err.style.display === 'block');

  // ───────────────────────────────────────────────────────────────────
  section('6. A LOCKED UTILITY (PROJECT CARD) IS LEFT ALONE');
  routes = {
    '/api/markets/utility-options': {
      body: { zip: '24112', state: 'VA', source: 'perch',
              options: [{ value: 'dominion-energy-va', label: 'Dominion Energy' }],
              auto_select: 'dominion-energy-va' },
    },
  };
  elCache.clear(); fetchLog.length = 0;
  const lockedUtil = el('wf-utility_name');
  lockedUtil.innerHTML = '<option value="national-grid-ny" selected>National Grid NY</option>';
  lockedUtil.value = 'national-grid-ny';
  lockedUtil.disabled = true;
  const lockedZip = el('wf-zip_code');
  vm.runInContext("wireZipDrivenUtilities({key:'service_area'})", ctx);
  lockedZip.value = '24112';
  await lockedZip.fire('input');
  await new Promise((r) => setImmediate(r));
  check('a project-locked utility is not overwritten',
        lockedUtil.value === 'national-grid-ny');
  check('  ...and no lookup is made for it', fetchLog.length === 0);

  // ───────────────────────────────────────────────────────────────────
  section('7. ONLY THE AVAILABILITY STEP IS WIRED');
  elCache.clear(); fetchLog.length = 0;
  const otherZip = el('wf-zip_code');
  vm.runInContext("wireZipDrivenUtilities({key:'enroll'})", ctx);
  otherZip.value = '24112';
  await otherZip.fire('input');
  await new Promise((r) => setImmediate(r));
  check('a different step attaches no ZIP listener', fetchLog.length === 0);
  vm.runInContext('wireZipDrivenUtilities(null)', ctx);
  check('a null step does not throw', true);

  // ───────────────────────────────────────────────────────────────────
  section('8. END TO END: THE SELECTED SLUG REACHES THE CAPACITY PAYLOAD');
  // THE REQUIREMENT THIS WHOLE FILE EXISTS FOR. Everything above asserts the
  // DOM; the DOM is not what Perch receives. This drives the real submit path
  // and reads the OUTGOING REQUEST BODY.
  routes = Object.assign(bothRoutes(VA_ZIP_BODY),
                         { '/enrollments/capacity': CAPACITY_OK });
  setServiceAreaStep();
  dom = fresh();
  await settle();
  await typeZip(dom, '24112');
  el('wf-email').value = 'customer@example.com';
  check('precondition: the auto-selected slug is in the select',
        dom.util.value === 'dominion-energy-va');
  await vm.runInContext('submitCapacity()', ctx);
  await settle();

  let caps = capacityRequests();
  check(`exactly one capacity request was sent (${caps.length})`,
        caps.length === 1);
  let sent = JSON.parse(caps[0].body || '{}');
  check(`utility_name is the exact Perch slug (${JSON.stringify(sent.utility_name)})`,
        sent.utility_name === 'dominion-energy-va');
  check('  ...NOT null', sent.utility_name !== null);
  check('  ...NOT undefined', sent.utility_name !== undefined);
  check('  ...NOT the empty string', sent.utility_name !== '');
  check('  ...NOT the display label, in either spelling',
        sent.utility_name !== 'Dominion Energy'
        && sent.utility_name !== 'Dominion Energy VA');
  check(`  ...and the ZIP went with it (${JSON.stringify(sent.zip_code)})`,
        sent.zip_code === '24112');
  check('  ...along with the email Perch requires for the token',
        sent.email === 'customer@example.com');

  section('8b. A VISIBLE SELECTION CAN NEVER SUBMIT AS EMPTY');
  // The same proof in a state where nothing is auto-selected: the slug that
  // reaches Perch must be the one the rep actually picked.
  routes = Object.assign(bothRoutes(NY_ZIP_BODY),
                         { '/enrollments/capacity': CAPACITY_OK });
  setServiceAreaStep();
  dom = fresh();
  await settle();
  await typeZip(dom, '12401');
  check('precondition: nothing auto-selected for a multi-utility state',
        dom.util.value === '');
  dom.util.value = 'national-grid-ny';     // the rep chooses
  el('wf-email').value = 'customer@example.com';
  await vm.runInContext('submitCapacity()', ctx);
  await settle();
  caps = capacityRequests();
  check('the capacity request was sent', caps.length === 1);
  sent = JSON.parse((caps[0] || {}).body || '{}');
  check(`  ...and carries the rep's chosen slug (${JSON.stringify(sent.utility_name)})`,
        sent.utility_name === 'national-grid-ny');
  check('  ...never null', sent.utility_name !== null);

  section('8c. NO SELECTION -> VALIDATION STOPS IT BEFORE ANY PERCH CALL');
  routes = Object.assign(bothRoutes(NY_ZIP_BODY),
                         { '/enrollments/capacity': CAPACITY_OK });
  setServiceAreaStep();
  dom = fresh();
  await settle();
  await typeZip(dom, '12401');
  el('wf-email').value = 'customer@example.com';
  dom.util.value = '';                     // the rep picked nothing
  await vm.runInContext('submitCapacity()', ctx);
  await settle();
  check('an unselected utility makes NO capacity request at all',
        capacityRequests().length === 0);
  check('  ...so a null utility_name can never reach Perch', true);

  report();
})();

function report() {
  const failed = results.filter((r) => !r.ok);
  console.log('\n' + '='.repeat(72));
  if (failed.length) {
    console.log(`AVAILABILITY UTILITY HARNESS - ${failed.length} FAILED`);
    failed.forEach((f) => console.log('  FAIL ' + f.label));
    console.log('='.repeat(72));
    process.exit(1);
  }
  console.log(`AVAILABILITY UTILITY HARNESS - ALL ${results.length} CHECKS PASSED`);
  console.log('='.repeat(72));
  process.exit(0);
}

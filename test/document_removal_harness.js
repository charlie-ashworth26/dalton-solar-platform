/*
 * RUNTIME PROOF: removing a saved photo persists.
 *
 * WHY THIS HARNESS EXISTS WHEN phase_a_state_harness.js ALREADY TESTS REMOVAL
 * -------------------------------------------------------------------------
 * That harness drives the real remove control and asserts a real DELETE, and
 * it passed throughout the entire life of this bug. It stubs the enrollment
 * detail with `document_sets` ONLY - so only ONE of the browser's two
 * hydration paths ever ran, and the one that caused the defect never did.
 *
 * The real payload carries BOTH:
 *
 *     e.document_sets  the current set per category, WITH its id
 *                      -> hydrateDocSets(), near the top of openEnrollment()
 *     e.documents      every document row, flat
 *                      -> rehydrateDocumentsFromDetail(), 59 lines LATER
 *
 * The second replaced DocSets wholesale with `id: null`, destroying the set id
 * the first had just restored. docSetRemove() needs that id to address the
 * DELETE endpoint and bailed out SILENTLY without one: the x spliced the entry
 * out of the list, repainted, and sent nothing. The row survived and the next
 * reopen listed it again from e.documents.
 *
 * So every scenario below hydrates from a payload containing BOTH sources.
 * That is the single condition the previous harness was missing, and it is the
 * condition a regression would reappear under.
 *
 * node test/document_removal_harness.js
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
    _listeners: {},
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
function byClassSelector(sel) {
  const want = String(sel).replace(/^\./, '').split('[')[0];
  const out = [];
  for (const el of elCache.values()) if (el.classList.contains(want)) out.push(el);
  return out;
}
const documentStub = {
  getElementById(id) {
    if (!REAL_IDS.has(id)) return null;
    if (!elCache.has(id)) elCache.set(id, makeEl(id));
    return elCache.get(id);
  },
  querySelector() { return null; },
  querySelectorAll(sel) { return byClassSelector(sel); },
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
console.log('RUNTIME PROOF - DOCUMENT REMOVAL PERSISTS');
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
  for (let i = 0; i < 6; i += 1) await new Promise((r) => setImmediate(r));
}
function withBase(specific) {
  return Object.assign({}, specific || {}, {
    '/api/enrollments': { body: [] }, '/api/projects': { body: [] },
    '/api/perch': { body: {} },
  });
}
function deletes() { return fetchLog.filter((f) => f.method === 'DELETE'); }

/* A detail payload shaped like the REAL one: both sources, in agreement. */
function payload(category, files, setId) {
  const host = category === 'utility_bill' ? 'utility_bill' : 'lmi_document';
  const sets = {};
  if (files.length) {
    sets[host] = {
      document_set_id: setId,
      file_count: files.length,
      files: files.map((f, i) => ({
        id: f.id, original_filename: f.name, page_order: i, file_size: 16384,
        document_set_id: f.setId === undefined ? setId : f.setId,
      })),
    };
  }
  return {
    id: 9, enrollment_code: 'ENR-9', status: 'Draft',
    customer: { first_name: 'Dana', last_name: 'Reyes', email: 'd@example.com' },
    service_address: { street: '1 Main St', city: 'Albany', state: 'NY', zip: '12207' },
    document_sets: sets,
    // THE SECOND SOURCE. Flat, enrollment-wide, and the thing that used to
    // overwrite the first one with a null set id.
    documents: files.map((f, i) => ({
      id: f.id, doc_category: host, original_filename: f.name,
      document_set_id: f.setId === undefined ? setId : f.setId, page_order: i,
      extraction_confidence: null, created_at: '2026-10-28 12:00:00',
    })),
  };
}

function listed(category) {
  return run(`DocSets.${category}.files.map(function(f){ return f.file.name; }).join(',')`);
}
function removeHandlers(hostId) {
  const inner = el(hostId).innerHTML || '';
  return [...inner.matchAll(/onclick="([^"]+)"/g)]
    .map((m) => m[1]).filter((h) => h.indexOf('docSetRemove(') === 0)
    .map((h) => h.replace(/&quot;/g, '"').replace(/&amp;/g, '&'));
}

(async function main() {
  const FOUR = [{ id: 101, name: 'A.jpg' }, { id: 102, name: 'B.jpg' },
                { id: 103, name: 'C.jpg' }, { id: 104, name: 'D.jpg' }];

  // ───────────────────────────────────────────────────────────────────
  section('1. THE SET ID SURVIVES A PAYLOAD THAT CARRIES BOTH SOURCES');
  elCache.clear(); fetchLog.length = 0;
  storageStub.setItem('dalton_auth_token', 'staff-token');
  routes = withBase({ '/api/enrollments/9': { body: payload('utility_bill', FOUR, 77) } });
  await run('openEnrollment(9)');
  run('goStep(2)');
  await settle();
  check(`four photos restored (${listed('utility_bill')})`,
        listed('utility_bill') === 'A.jpg,B.jpg,C.jpg,D.jpg');
  // THE REGRESSION ASSERTION. This was null for the whole life of the bug.
  check(`the category keeps its set id (${run('DocSets.utility_bill.id')})`,
        run('DocSets.utility_bill.id') === 77);
  check('  ...and every entry carries its own removal handle',
        run('DocSets.utility_bill.files.every(function(f){ return f.setId === 77; })') === true);

  section('1b. IN EITHER CALL ORDER');
  // rehydrateDocumentsFromDetail() must not clobber whatever hydrateDocSets()
  // restored, no matter which runs last. Called directly, in the wrong order.
  const p = payload('utility_bill', FOUR, 77);
  run('docSetResetAll()');
  run(`hydrateDocSets(${JSON.stringify(p.document_sets)})`);
  run(`rehydrateDocumentsFromDetail(${JSON.stringify(p)})`);
  check('document-sets first, then the flat list: id intact',
        run('DocSets.utility_bill.id') === 77);
  check('  ...and no entry was duplicated',
        run('DocSets.utility_bill.files.length') === 4);
  run('docSetResetAll()');
  run(`rehydrateDocumentsFromDetail(${JSON.stringify(p)})`);
  run(`hydrateDocSets(${JSON.stringify(p.document_sets)})`);
  check('flat list first, then document-sets: id intact',
        run('DocSets.utility_bill.id') === 77);
  check('  ...and still four entries',
        run('DocSets.utility_bill.files.length') === 4);

  // ───────────────────────────────────────────────────────────────────
  section('2. PRESSING THE RENDERED x SENDS A REAL DELETE');
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({
    '/document-sets/77/files/102': { body: { removed: 102, remaining: 3 } },
    '/document-sets/77/files/103': { body: { removed: 103, remaining: 2 } },
    '/api/enrollments/9': { body: payload('utility_bill', FOUR, 77) },
  });
  await run('openEnrollment(9)');
  run('goStep(2)');
  await settle();
  fetchLog.length = 0;
  let handlers = removeHandlers('bill-fileset');
  check(`one remove control per photo (${handlers.length})`, handlers.length === 4);
  await run(handlers[1]);                       // B
  await settle();
  handlers = removeHandlers('bill-fileset');
  await run(handlers[1]);                       // C
  await settle();
  const dels = deletes();
  check(`two DELETEs were actually sent (${dels.length})`, dels.length === 2);
  check('  ...addressed to B within its set',
        dels.some((d) => d.url.endsWith('/api/enrollments/9/document-sets/77/files/102')));
  // (dels is a filtered array; .some() is already safe on an empty one.)
  check('  ...and to C', dels.some((d) => d.url.endsWith('/document-sets/77/files/103')));
  check(`the screen shows A and D only (${listed('utility_bill')})`,
        listed('utility_bill') === 'A.jpg,D.jpg');

  section('2b. AND REOPENING DOES NOT BRING THEM BACK');
  // The server now holds A and D, so its payload says so. The point of this
  // step is that the browser accepts that and does not re-add from elsewhere.
  routes['/api/enrollments/9'] = {
    body: payload('utility_bill', [FOUR[0], FOUR[3]], 77) };
  await run('openEnrollment(9)');
  run('goStep(2)');
  await settle();
  check(`reopen lists A and D (${listed('utility_bill')})`,
        listed('utility_bill') === 'A.jpg,D.jpg');
  check('  ...with the set id still intact for the next removal',
        run('DocSets.utility_bill.id') === 77);

  // ───────────────────────────────────────────────────────────────────
  section('3. REPLACEMENT APPENDS TO THE SAME SET, NOT A RIVAL ONE');
  // A null set id made uploadDocumentSet() start a new set, which is how the
  // old photos came back ALONGSIDE the new ones. The request body is the
  // evidence: it must name the set being appended to.
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({
    '/document-sets/77/files/101': { body: { removed: 101, remaining: 1 } },
    '/document-sets': { body: { document_set_id: 77, files: [{ document_id: 105 }],
                                extraction: {}, file_count: 2 } },
    '/api/enrollments/9': { body: payload('utility_bill', [FOUR[0], FOUR[3]], 77) },
  });
  await run('openEnrollment(9)');
  run('goStep(2)');
  await settle();
  const h3 = removeHandlers('bill-fileset');
  await run(h3[0]);                             // remove A
  await settle();
  const appended = [];
  run('DocSets.utility_bill.files.push({file:{name:"E.jpg",size:10,lastModified:1},' +
      'key:"E.jpg:10:1", documentId:null});');
  // The id the upload will be told to append to.
  check('the uploader still knows which set to append to',
        run('DocSets.utility_bill.id') === 77);
  await run('uploadDocumentSet("utility_bill")');
  await settle();
  const posts = fetchLog.filter((f) => f.method === 'POST' && f.url.includes('/document-sets'));
  check(`the replacement was uploaded (${posts.length} POST)`, posts.length === 1);
  check(`the set survived the round trip (${run('DocSets.utility_bill.id')})`,
        run('DocSets.utility_bill.id') === 77);
  check(`only D and the replacement are listed (${listed('utility_bill')})`,
        listed('utility_bill') === 'D.jpg,E.jpg');
  check('  ...and the new entry knows its own set for later removal',
        run('DocSets.utility_bill.files[1].setId') === 77);

  // ───────────────────────────────────────────────────────────────────
  section('4. A LEGACY SET-LESS DOCUMENT IS STILL REMOVABLE');
  // document_set_id NULL: no set route can address it. docSetRemove() used to
  // return silently; it must now use the per-document route.
  elCache.clear(); fetchLog.length = 0;
  const LEGACY = { id: 150, name: 'legacy.jpg', setId: null };
  routes = withBase({
    '/api/enrollments/9/documents/150': { body: { removed: 150 } },
    '/api/enrollments/9': { body: {
      id: 9, enrollment_code: 'ENR-9', status: 'Draft',
      customer: { first_name: 'Dana', last_name: 'Reyes', email: 'd@example.com' },
      document_sets: {},                       // no set exists for it at all
      documents: [{ id: 150, doc_category: 'utility_bill',
                    original_filename: 'legacy.jpg', document_set_id: null,
                    page_order: 0 }],
    } },
  });
  await run('openEnrollment(9)');
  run('goStep(2)');
  await settle();
  check(`the legacy document is listed (${listed('utility_bill')})`,
        listed('utility_bill') === 'legacy.jpg');
  check('  ...with no set id, honestly', run('DocSets.utility_bill.id') === null);
  fetchLog.length = 0;
  const h4 = removeHandlers('bill-fileset');
  check(`it still has a remove control (${h4.length})`, h4.length === 1);
  await run(h4[0]);
  await settle();
  const d4 = deletes();
  check(`a DELETE was sent rather than silently skipped (${d4.length})`, d4.length === 1);
  check(`  ...to the per-document route (${d4[0] && d4[0].url})`,
        !!d4[0] && d4[0].url.endsWith('/api/enrollments/9/documents/150'));
  check('  ...and it left the list', listed('utility_bill') === '');

  // ───────────────────────────────────────────────────────────────────
  section('5. THE LMI PROOF FLOW BEHAVES THE SAME WAY');
  elCache.clear(); fetchLog.length = 0;
  const PROOFS = [{ id: 201, name: 'proof-1.jpg' }, { id: 202, name: 'proof-2.jpg' }];
  routes = withBase({
    '/document-sets/88/files/202': { body: { removed: 202, remaining: 1 } },
    '/api/enrollments/9': { body: payload('lmi_document', PROOFS, 88) },
  });
  await run('openEnrollment(9)');
  await settle();
  check(`both proofs restored (${listed('lmi_document')})`,
        listed('lmi_document') === 'proof-1.jpg,proof-2.jpg');
  check('the proof set keeps its id', run('DocSets.lmi_document.id') === 88);
  fetchLog.length = 0;
  const h5 = removeHandlers('lmi-fileset');
  check(`one remove control per proof (${h5.length})`, h5.length === 2);
  await run(h5[1]);
  await settle();
  const d5 = deletes();
  check(`removing a proof sends a DELETE (${d5.length})`, d5.length === 1);
  check('  ...within the PROOF set, not the bill set',
        d5.length === 1
        && d5[0].url.endsWith('/api/enrollments/9/document-sets/88/files/202'));
  check(`the remaining proof is listed (${listed('lmi_document')})`,
        listed('lmi_document') === 'proof-1.jpg');
  check('  ...and the bill category was not touched',
        listed('utility_bill') === '');

  // ───────────────────────────────────────────────────────────────────
  section('6. A REFUSED REMOVAL IS RESTORED, NOT QUIETLY SWALLOWED');
  // The other direction of the same honesty rule: if the server will not
  // remove it, the screen must not claim it did.
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({
    '/document-sets/77/files/102': { status: 500, body: { error: 'nope' } },
    '/api/enrollments/9': { body: payload('utility_bill', FOUR, 77) },
  });
  await run('openEnrollment(9)');
  run('goStep(2)');
  await settle();
  const h6 = removeHandlers('bill-fileset');
  await run(h6[1]);
  await settle();
  check(`the photo is put back (${listed('utility_bill')})`,
        listed('utility_bill') === 'A.jpg,B.jpg,C.jpg,D.jpg');
  check('  ...in its original position', listed('utility_bill').split(',')[1] === 'B.jpg');
  check('  ...and the rep is told',
        (el('bill-submit-error').textContent || '').indexOf('could not be removed') >= 0);

  section('7. NOTHING IS SENT FOR A PHOTO THAT WAS NEVER UPLOADED');
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({});
  run("resetWizardState(); currentDraft = {enrollment_id: 9, enrollment_code: 'ENR-9'};");
  run('DocSets.utility_bill = {id: null, files: [{file:{name:"pending.jpg",size:10},' +
      'key:"pending.jpg:10:1", documentId: null}]};');
  await run('docSetRemove("utility_bill", "pending.jpg:10:1")');
  await settle();
  check('the pending photo is dropped', run('DocSets.utility_bill.files.length') === 0);
  check('  ...with no request at all, because nothing was stored',
        deletes().length === 0);

  // ───────────────────────────────────────────────────────────────────
  section('8. EVERY PICKED PROOF FILE IS UPLOADED, NOT JUST THE FIRST');
  // handleLmiUpload() listed every selected file via docSetAddFiles() and
  // then uploaded only files[0] through the single-file route. The rep saw
  // three rows and one file left the browser. What is asserted here is the
  // REQUEST: one POST to /document-sets carrying the whole selection, not N
  // posts to /documents and not one file.
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({
    '/document-sets': { body: { document_set_id: 55,
      files: [{ document_id: 301 }, { document_id: 302 }, { document_id: 303 }],
      extraction: {}, file_count: 3, lmi_validation: { classification: 'likely_valid' } } },
    '/api/enrollments/9': { body: payload('lmi_document', [], null) },
  });
  await run('openEnrollment(9)');
  await settle();
  // Perch must be ASKING for proof documents, or the uploader is inert by
  // design - lmiSectionsForCurrentStep() gates it.
  run("perchContext.nextStepKey = 'proof_docs';");
  check('the proof section is the authorised one',
        run('lmiSectionsForCurrentStep().doc') === true);
  fetchLog.length = 0;
  const three = [
    { name: 'proof-a.jpg', size: 1024, lastModified: 1 },
    { name: 'proof-b.jpg', size: 2048, lastModified: 2 },
    { name: 'proof-c.jpg', size: 4096, lastModified: 3 },
  ];
  sandbox.__picked = three;
  await run('handleLmiUpload(__picked)');
  await settle();
  check(`all three are listed (${listed('lmi_document')})`,
        listed('lmi_document') === 'proof-a.jpg,proof-b.jpg,proof-c.jpg');
  const proofPosts = fetchLog.filter((f) => f.method === 'POST');
  const toSets = proofPosts.filter((f) => f.url.includes('/document-sets'));
  const toLegacy = proofPosts.filter((f) => /\/documents$/.test(f.url));
  check(`the whole selection goes to /document-sets in ONE post (${toSets.length})`,
        toSets.length === 1);
  check(`  ...and nothing goes to the single-file route (${toLegacy.length})`,
        toLegacy.length === 0);
  check('every file now has a server document id',
        run('DocSets.lmi_document.files.every(function(f){ return !!f.documentId; })') === true);
  check(`  ...three distinct ones (${run('DocSets.lmi_document.files.map(function(f){ return f.documentId; }).join(",")')})`,
        run('new Set(DocSets.lmi_document.files.map(function(f){ return f.documentId; })).size') === 3);
  check('the primary pointer is the FIRST file, as every caller expects',
        run('state.lmi.documentId') === 301);
  check('  ...and the set id is recorded for later removal',
        run('DocSets.lmi_document.id') === 55);

  section('8b. AND THE REP IS TOLD THE PREVIEW ONLY READ ONE OF THEM');
  check('the prefill scope note is shown for a multi-file proof',
        (el('lmi-prefill-scope').textContent || '').indexOf('All 3') >= 0);

  section('8c. AN OVERSIZED LATER FILE IS CAUGHT, NOT JUST THE FIRST');
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({ '/api/enrollments/9': { body: payload('lmi_document', [], null) } });
  await run('openEnrollment(9)');
  await settle();
  run("perchContext.nextStepKey = 'proof_docs';");
  sandbox.__big = [
    { name: 'ok.jpg', size: 1024, lastModified: 1 },
    { name: 'huge.jpg', size: 5 * 1024 * 1024, lastModified: 2 },
  ];
  fetchLog.length = 0;
  await run('handleLmiUpload(__big)');
  await settle();
  check('nothing is uploaded when any file is over the limit',
        fetchLog.filter((f) => f.method === 'POST').length === 0);
  check('  ...and the oversized file is named in the error',
        (el('lmi-submit-error').textContent || '').indexOf('huge.jpg') >= 0);

  section('8d. removeLmi() PERSISTS A STORED PROOF, LIKE removeBill()');
  // It used to clear the chip and local state only, so a stored proof came
  // straight back on the next reopen.
  elCache.clear(); fetchLog.length = 0;
  routes = withBase({
    '/document-sets/55/files/301': { body: { removed: 301, remaining: 0 } },
    '/api/enrollments/9': { body: payload('lmi_document',
        [{ id: 301, name: 'stored-proof.jpg' }], 55) },
  });
  await run('openEnrollment(9)');
  await settle();
  check('the stored proof is restored', listed('lmi_document') === 'stored-proof.jpg');
  fetchLog.length = 0;
  await run('removeLmi()');
  await settle();
  const lmiDels = deletes();
  check(`removeLmi() issues a real DELETE (${lmiDels.length})`, lmiDels.length === 1);
  check('  ...for the stored proof within its set',
        !!lmiDels[0] && lmiDels[0].url.endsWith('/api/enrollments/9/document-sets/55/files/301'));
  check('  ...and the list is empty afterwards', listed('lmi_document') === '');

  report();
})();

function report() {
  const failed = results.filter((r) => !r.ok);
  console.log('\n' + '='.repeat(72));
  if (failed.length) {
    console.log(`DOCUMENT REMOVAL HARNESS - ${failed.length} FAILED`);
    failed.forEach((f) => console.log('  FAIL ' + f.label));
    console.log('='.repeat(72));
    process.exit(1);
  }
  console.log(`DOCUMENT REMOVAL HARNESS - ALL ${results.length} CHECKS PASSED`);
  console.log('='.repeat(72));
  process.exit(0);
}

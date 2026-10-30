/*
 * RUNTIME PROOF: the dashboard greeting follows the reader's own clock.
 *
 * WHAT WAS WRONG
 *   The greeting was the literal string 'Good afternoon, '. It greeted a rep
 *   who signed in at 7am with the afternoon, and a rep who left the tab open
 *   was still being told it was the afternoon at midnight.
 *
 * WHAT IS ASSERTED
 *   Every hour of the day, both sides of all three boundaries, and the two
 *   ways the greeting has to stay correct without a refresh: the scheduled
 *   boundary update, and the tab coming back into view after the machine
 *   slept through one.
 *
 *   The name is asserted to be untouched - same source, same first-word rule,
 *   same behaviour when nobody is signed in - because that was the one part
 *   of the line that was already right.
 *
 * node test/dashboard_greeting_harness.js
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
    style: {}, dataset: {}, options: [], _attrs: {},
    classList: { _s: new Set(), add() {}, remove() {}, toggle() {}, contains() { return false; } },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    appendChild() {}, removeChild() {}, addEventListener() {}, removeEventListener() {},
    focus() {}, click() {}, closest() { return null; },
    querySelector() { return null; }, querySelectorAll() { return []; },
  };
  return el;
}

const elCache = new Map();
const docListeners = {};
const documentStub = {
  getElementById(id) {
    if (!REAL_IDS.has(id)) return null;
    if (!elCache.has(id)) elCache.set(id, makeEl(id));
    return elCache.get(id);
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement(tag) { return makeEl('created-' + tag); },
  addEventListener(ev, fn) { (docListeners[ev] = docListeners[ev] || []).push(fn); },
  removeEventListener() {},
  body: makeEl('body'), documentElement: makeEl('html'), activeElement: null,
  visibilityState: 'visible',
};
documentStub.body.style = {};
const storageStub = {
  _d: {}, getItem() { return null; }, setItem() {}, removeItem() {}, clear() {},
};

// A controllable clock. setTimeout records rather than runs, so a scheduled
// boundary update can be inspected and fired deliberately.
let fakeNow = new Date(2026, 9, 29, 14, 0, 0);
const timers = [];
let timerSeq = 0;

class FakeDate extends Date {
  constructor(...args) {
    if (args.length === 0) { super(fakeNow.getTime()); } else { super(...args); }
  }
  static now() { return fakeNow.getTime(); }
}

const sandbox = {
  console, document: documentStub, sessionStorage: storageStub, localStorage: storageStub,
  Date: FakeDate,
  setTimeout(fn, ms) { timerSeq += 1; timers.push({ id: timerSeq, fn, ms }); return timerSeq; },
  clearTimeout(id) {
    const i = timers.findIndex((t) => t.id === id);
    if (i !== -1) timers.splice(i, 1);
  },
  setInterval() { return 0; }, clearInterval() {},
  alert() {}, confirm() { return true; },
  fetch() { return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) }); },
  FormData: function () { this.append = function () {}; },
  FileReader: function () { this.readAsDataURL = function () {}; },
  Blob: function () {},
  URL: { createObjectURL() { return ''; }, revokeObjectURL() {} },
  navigator: { userAgent: 'harness' }, location: { href: '', reload() {} },
  requestAnimationFrame() { return 0; },
  pdfjsLib: { getDocument() { return { promise: Promise.resolve({ numPages: 0 }) }; }, GlobalWorkerOptions: {} },
  Tesseract: { recognize() { return Promise.resolve({ data: { text: '' } }); } },
};
sandbox.scrollTo = function () {};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

console.log('='.repeat(72));
console.log('RUNTIME PROOF - DASHBOARD GREETING FOLLOWS THE READER\'S CLOCK');
console.log('='.repeat(72));

const ctx = vm.createContext(sandbox);
vm.runInContext(src, ctx, { filename: 'app.js' });
check('app.js evaluates without throwing', true);

function run(code) { return vm.runInContext(code, ctx); }
function greetingText() { return documentStub.getElementById('dash-greeting').textContent; }
function setClock(h, m) {
  fakeNow = new Date(2026, 9, 29, h, m || 0, 0);
}

run("currentUser = {full_name: 'Charles Ashworth', email: 'charles@daltonsolar.com'};");

// ════════════════════════════════════════════════════════════════════════════
section('1. EVERY HOUR OF THE DAY');
// ════════════════════════════════════════════════════════════════════════════
const EXPECTED = {};
for (let h = 0; h < 24; h += 1) {
  EXPECTED[h] = (h >= 5 && h < 12) ? 'Good morning'
    : (h >= 12 && h < 17) ? 'Good afternoon' : 'Good evening';
}
let wrong = [];
for (let h = 0; h < 24; h += 1) {
  const got = run(`greetingForHour(${h})`);
  if (got !== EXPECTED[h]) wrong.push(`${h}:00 -> ${got}, expected ${EXPECTED[h]}`);
}
check(`all 24 hours map to the right greeting (${wrong.length} wrong)`, wrong.length === 0);
wrong.forEach((w) => console.log('      ' + w));

// ════════════════════════════════════════════════════════════════════════════
section('2. THE BOUNDARIES, ON BOTH SIDES');
// ════════════════════════════════════════════════════════════════════════════
const EDGES = [
  [4, 59, 'Good evening', 'one minute before morning'],
  [5, 0, 'Good morning', 'the moment morning starts'],
  [11, 59, 'Good morning', 'the last minute of morning'],
  [12, 0, 'Good afternoon', 'noon exactly'],
  [16, 59, 'Good afternoon', 'the last minute of afternoon'],
  [17, 0, 'Good evening', 'five o\'clock exactly'],
  [23, 59, 'Good evening', 'just before midnight'],
  [0, 0, 'Good evening', 'midnight'],
];
EDGES.forEach(([h, m, want, why]) => {
  setClock(h, m);
  run('applyDashGreeting();');
  const pad = String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
  check(`${pad} (${why}) -> "${greetingText()}"`,
    greetingText() === want + ', Charles');
});

// ════════════════════════════════════════════════════════════════════════════
section('3. THE NAME IS EXACTLY WHAT IT WAS');
// ════════════════════════════════════════════════════════════════════════════
setClock(9, 0);
run("currentUser = {full_name: 'Charles Ashworth', email: 'charles@daltonsolar.com'};");
run('applyDashGreeting();');
check('a full name gives the first name only',
  greetingText() === 'Good morning, Charles');
run("currentUser = {full_name: '', email: 'dana.reyes@example.com'};");
run('applyDashGreeting();');
check('no full name falls back to the email, first word',
  greetingText() === 'Good morning, dana.reyes@example.com');
run("currentUser = {full_name: 'Prudence', email: 'p@example.com'};");
run('applyDashGreeting();');
check('a single-word name is used whole', greetingText() === 'Good morning, Prudence');
run('currentUser = null;');
run('applyDashGreeting();');
check('nobody signed in leaves the name empty, as before',
  greetingText() === 'Good morning, ');
run("currentUser = {full_name: 'Charles Ashworth', email: 'charles@daltonsolar.com'};");

// ════════════════════════════════════════════════════════════════════════════
section('4. IT CHANGES WITHOUT A REFRESH');
// ════════════════════════════════════════════════════════════════════════════
setClock(16, 30);
timers.length = 0;
run('applyDashGreeting(); scheduleGreetingRefresh();');
check(`at 16:30 it reads "${greetingText()}"`,
  greetingText() === 'Good afternoon, Charles');
check('exactly one update is scheduled, not a poll', timers.length === 1);
const waited = timers[0].ms;
check(`  ...for 30 minutes' time, the next boundary (${Math.round(waited / 60000)} min)`,
  waited === 30 * 60 * 1000);

// Fire it the way the browser would, with the clock now past the boundary.
setClock(17, 0);
const due = timers.shift();
due.fn();
check(`when it fires the greeting reads "${greetingText()}" - no refresh`,
  greetingText() === 'Good evening, Charles');
check('  ...and the next change is already scheduled', timers.length === 1);
check(`  ...for midnight-to-five, i.e. 12 hours (${Math.round(timers[0].ms / 3600000)}h)`,
  timers[0].ms === 12 * 60 * 60 * 1000);

// Evening -> morning across midnight.
setClock(23, 30);
timers.length = 0;
run('applyDashGreeting(); scheduleGreetingRefresh();');
check(`at 23:30 it still reads "${greetingText()}"`,
  greetingText() === 'Good evening, Charles');
check(`  ...with the next change 5.5 hours away, at 05:00 tomorrow `
  + `(${timers[0].ms / 3600000}h)`, timers[0].ms === 5.5 * 60 * 60 * 1000);
setClock(5, 0);
timers.shift().fn();
check(`crossing midnight lands on "${greetingText()}"`,
  greetingText() === 'Good morning, Charles');

// ════════════════════════════════════════════════════════════════════════════
section('5. A MACHINE THAT SLEPT THROUGH A BOUNDARY');
// ════════════════════════════════════════════════════════════════════════════
// The scheduled timeout does not fire while the laptop is asleep, so coming
// back to a visible tab has to re-check rather than wait for the next one.
setClock(15, 0);
timers.length = 0;
run('applyDashGreeting(); scheduleGreetingRefresh();');
check(`asleep at 15:00 showing "${greetingText()}"`,
  greetingText() === 'Good afternoon, Charles');
setClock(19, 0);                       // woke four hours later
check('the greeting is stale until something re-checks',
  greetingText() === 'Good afternoon, Charles');
documentStub.visibilityState = 'visible';
(docListeners.visibilitychange || []).forEach((fn) => fn());
check(`bringing the tab back fixes it: "${greetingText()}"`,
  greetingText() === 'Good evening, Charles');
check('  ...and reschedules from the real time now',
  timers.length === 1 && timers[0].ms === 10 * 60 * 60 * 1000);

// A hidden tab must not thrash the timer.
const before = timers.length;
documentStub.visibilityState = 'hidden';
(docListeners.visibilitychange || []).forEach((fn) => fn());
check('a tab being hidden schedules nothing new', timers.length === before);

// ════════════════════════════════════════════════════════════════════════════
section('6. NOTHING ELSE MOVED');
// ════════════════════════════════════════════════════════════════════════════
// Comments stripped first: the markup EXPLAINS what it used to say, and a
// check that reads its own documentation as content proves nothing.
const markup = html.replace(/<!--[\s\S]*?-->/g, '');
check('no greeting is hardcoded in the markup any more',
  !/Good (morning|afternoon|evening)/.test(markup));
check('  ...and the heading element and its styling are unchanged',
  html.includes('<h1 id="dash-greeting"></h1>')
  && html.includes('Here\'s how your pipeline looks today.'));
check('the dashboard still renders its stats and pipeline',
  /dash-stats/.test(src) && /loadEnrollments\(\)/.test(
    src.split('async function renderDashboard')[1].slice(0, 400)));
check('the greeting reads the browser clock, never a server value',
  /greetingForHour\(new Date\(\)\.getHours\(\)\)/.test(src));
check('one timeout is cleared before another is set, so timers cannot pile up',
  /if\(greetingTimer\)\{ clearTimeout\(greetingTimer\); greetingTimer = null; \}/.test(src));
check('the delay has a floor, so a bad boundary can never become a spin',
  /Math\.max\(1000, msUntilNextGreetingChange/.test(src));
check('a timer that fires synchronously does not chain into recursion',
  /if\(!firedSynchronously\) scheduleGreetingRefresh\(\);/.test(src));

console.log('\n' + '='.repeat(72));
const failed = results.filter((r) => !r.ok);
console.log(failed.length
  ? `DASHBOARD GREETING - ${failed.length} of ${results.length} CHECKS FAILED`
  : `DASHBOARD GREETING - ALL ${results.length} CHECKS PASSED`);
console.log('='.repeat(72));
failed.forEach((r) => console.log('  FAILED: ' + r.label));
process.exit(failed.length ? 1 : 0);

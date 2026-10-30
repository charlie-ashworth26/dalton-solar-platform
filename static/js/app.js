/* ==================== PHASE 2: API CLIENT & AUTH ====================
   Everything in this block is new. It replaces fake in-memory data with
   real calls to the Flask backend built in Phase 1. See backend/README.md
   for the endpoint list this talks to. */

const AuthStore = {
  KEY: 'dalton_auth_token',
  // sessionStorage: cleared when the tab closes, so a stolen/leaked token
  // has a much shorter useful life than localStorage. Still readable by
  // any JS on the page (i.e. vulnerable to XSS) — the real production fix
  // is a server-set httpOnly cookie, which would mean reworking the
  // Bearer-token auth middleware Phase 1 already tested. Deliberately not
  // doing that this phase — see README "Known limitations".
  getToken(){ try { return sessionStorage.getItem(this.KEY); } catch(e){ return null; } },
  setToken(t){ try { sessionStorage.setItem(this.KEY, t); } catch(e){} },
  clear(){ try { sessionStorage.removeItem(this.KEY); } catch(e){} },
};

let currentUser = null; // {id, email, role, full_name} — set after login via GET /api/auth/me

async function apiFetch(path, opts){
  opts = opts || {};
  const headers = Object.assign({}, opts.headers || {});
  const token = AuthStore.getToken();
  if(token) headers['Authorization'] = 'Bearer ' + token;
  if(opts.body && !(opts.body instanceof FormData) && !headers['Content-Type']){
    headers['Content-Type'] = 'application/json';
  }
  let res;
  try {
    res = await fetch(path, Object.assign({}, opts, {headers}));
  } catch(networkErr){
    throw new Error('Could not reach the server. Check your connection and try again.');
  }
  if(res.status === 401){
    AuthStore.clear();
    currentUser = null;
    // The session is gone; do not leave admin controls on screen.
    syncAdminNav();
    if(document.getElementById('app-shell').classList.contains('active')){
      showScreen('screen-login');
    }
    throw new Error('Your session expired — please sign in again.');
  }
  let data = null;
  try { data = await res.json(); } catch(e){ /* empty body, e.g. some 204s */ }
  if(!res.ok){
    // The message is unchanged - every existing caller reads err.message and is
    // unaffected. The status and parsed body are ATTACHED so a caller that
    // needs a server-confirmed fact (e.g. enrollment_discarded) can read it
    // rather than inferring it from the failure alone.
    const apiErr = new Error((data && data.error) || ('Request failed (' + res.status + ')'));
    apiErr.status = res.status;
    apiErr.body = data || {};
    throw apiErr;
  }
  return data;
}

/* ==================== END PHASE 2 API CLIENT ==================== */

/* ==================== THE MOBILE DRAWER (C3) ====================

   Below 900px the sidebar is off-canvas and the top bar's button brings it
   in. There is no second navigation: the drawer IS the sidebar element, so
   syncAdminNav() keeps deciding what a rep may see, and an admin-only link
   cannot leak onto a phone because a mobile menu was not kept in step.

   The state lives in one class on .app-shell. CSS owns the animation and
   owns whether any of this applies at all - above the breakpoint the rules
   simply do not match, so toggling the class on a desktop does nothing.
*/
function navIsOpen(){
  const shell = document.getElementById('app-shell');
  return !!(shell && shell.classList.contains('nav-open'));
}

function setNav(open){
  const shell = document.getElementById('app-shell');
  if(!shell) return;
  const toggle = document.getElementById('nav-toggle');
  const scrim = document.getElementById('nav-scrim');
  shell.classList.toggle('nav-open', !!open);
  if(toggle){
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
  }
  if(scrim) scrim.hidden = !open;
  // The page behind must not scroll under an open drawer.
  document.body.style.overflow = open ? 'hidden' : '';
  if(open){
    // Move focus into the drawer so a keyboard or screen-reader user is
    // taken to what just opened, rather than left behind the scrim.
    const first = document.querySelector('#app-nav .nav-link');
    if(first){ first.setAttribute('tabindex', '0'); first.focus(); }
  } else if(toggle && document.activeElement
            && document.getElementById('app-nav')
            && document.getElementById('app-nav').contains(document.activeElement)){
    toggle.focus();
  }
}

function openNav(){ setNav(true); }
function closeNav(){ if(navIsOpen()) setNav(false); }
function toggleNav(){ setNav(!navIsOpen()); }

document.addEventListener('keydown', function(e){
  if(e.key === 'Escape' && navIsOpen()) closeNav();
});

/* A drawer left open while the viewport grows back past the breakpoint would
   leave body scrolling locked with nothing visibly covering the page.

   Guarded the way every other window listener in this file is (see the
   popstate and pagehide registrations): app.js is loaded into a bare DOM stub
   by the Node test harnesses, which provide `document.addEventListener` but no
   `window.addEventListener`. An unguarded call here throws at load and takes
   every harness with it. */
if(typeof window !== 'undefined' && window.addEventListener){
  window.addEventListener('resize', function(){
    if(window.innerWidth >= 900 && navIsOpen()) closeNav();
  });
}

/* ==================== SHARED UI COMPONENTS (C2) ====================

   Presentation only. Nothing here decides whether something is valid, what
   a request returns, or which step comes next - it decides how the answer
   is shown. Every caller keeps the control flow it had.
*/

/* A backend message is written for whoever is reading the logs. Some of them
   are already fit to show a rep - "Perch accepts utility bills up to 4 MB per
   page" says exactly what to do. Others are not: the Sales Report answered an
   unauthorised rep with `Role 'sales_rep' is not permitted to view this
   report`, naming an internal role string and a permission model the reader
   has no way to act on.

   So this does not blanket-replace backend copy, which would throw away the
   useful messages along with the bad ones. It replaces the messages that
   betray internals, and passes the rest through. The raw message stays on the
   error object for logging and for callers that branch on it. */
/* D-2. The server now writes rep-facing copy deliberately, so nothing here
   should ever match. This stays as the LAST filter before a rep reads a
   sentence: a path nobody thought about degrades to the generic message
   instead of showing plumbing. Vendor names, HTTP statuses, endpoint paths
   and machine_codes are added for the same reason the originals are here. */
const INTERNAL_MARKERS = [
  /\brole\s+'[^']+'/i, /traceback/i, /sqlite|sqlalchemy|psycopg/i,
  /\b(KeyError|TypeError|ValueError|AttributeError|NoneType)\b/,
  /^\s*<!DOCTYPE/i, /^Request failed \(\d+\)$/,
  /\bat line \d+|\.py[:"]/i,
  /\bperch\b|\barcadia\b/i,
  /\b[1-5]\d{2}\b/,
  /\/(enroll|capacity|contracts|status|token|markets|lmi)\b/,
  /\b[a-z]+(?:_[a-z]+)+\b/,
  /\b[A-Za-z]*(?:Error|Exception)\b/
];

function userMessage(err, fallback){
  const raw = (err && err.message ? String(err.message) : '').trim();
  const generic = fallback || 'Something went wrong. Try again, and if it keeps '
                            + 'happening let an admin know.';
  if(!raw) return generic;
  if(err && err.status === 403){
    return 'You do not have access to this. Ask an admin if you need it.';
  }
  if(err && err.status === 404) return 'That is no longer available.';
  if(raw.length > 220) return generic;
  if(INTERNAL_MARKERS.some(function(re){ return re.test(raw); })) return generic;
  return raw;
}

/* Set a shared alert on `host`. The icon and the fill come from the class, so
   this only has to set the class and the words - which is also why an existing
   `el.textContent = msg` call site becomes a real alert just by gaining the
   class in its markup. An empty message clears it. */
function renderAlert(host, variant, message){
  const el = (typeof host === 'string') ? document.getElementById(host) : host;
  if(!el) return;
  el.className = message ? ('alert alert-' + variant) : 'alert';
  el.textContent = message || '';
  el.hidden = !message;
  el.style.display = message ? '' : 'none';
  if(message) el.setAttribute('role', variant === 'error' ? 'alert' : 'status');
}

/* A field-level error: the message AND the field agree. Without the second
   half a rep is told something is wrong but not which box to look at. */
function setFieldError(input, host, message){
  const field = (typeof input === 'string') ? document.getElementById(input) : input;
  const el = (typeof host === 'string') ? document.getElementById(host) : host;
  if(el){
    el.className = 'field-error';
    el.textContent = message || '';
    el.hidden = !message;
    el.style.display = message ? '' : 'none';
  }
  if(field){
    if(message){
      field.classList.add('err');
      field.setAttribute('aria-invalid', 'true');
      if(el && el.id) field.setAttribute('aria-describedby', el.id);
    } else {
      field.classList.remove('err');
      field.removeAttribute('aria-invalid');
    }
  }
}

/* Table states. The shell is the same in all three; only the body differs.

   Before C2 an empty table said something different on every screen and in a
   different style: an inline-styled <td> on the dashboard, a .crm-empty class
   on Customers, a stray <p class="helper"> on Reps. An empty screen is the one
   a new rep sees first, so it is worth saying the same thing the same way -
   and worth saying what to do next, not only that there is nothing here. */
function tableLoadingHtml(rows){
  let s = '<div class="table-skeleton" aria-hidden="true">';
  for(let i = 0; i < (rows || 4); i++) s += '<span></span>';
  return s + '</div>';
}

/* An empty state INSIDE a table, as a full-width row. */
function tableEmptyRowHtml(colspan, title, body){
  return '<tr><td colspan="' + colspan + '" class="cell-wrap">' +
         tableEmptyHtml(title, body) + '</td></tr>';
}

function tableEmptyHtml(title, body){
  return '<div class="table-state" role="status">' +
         '<span class="ts-title">' + esc(title) + '</span>' +
         (body ? '<span class="ts-body">' + esc(body) + '</span>' : '') +
         '</div>';
}

function tableCountHtml(n, noun, total){
  const word = n === 1 ? noun : noun + 's';
  const shown = (total != null && total !== n)
    ? n + ' of ' + total + ' ' + (total === 1 ? noun : noun + 's')
    : n + ' ' + word;
  return '<p class="table-count">' + esc(shown) + '</p>';
}

/* ==================== END SHARED UI COMPONENTS ==================== */

let projects = []; // populated by loadProjects() from GET /api/projects — see below

// The legacy customer-portal demo still reads this local array. The rep-facing
// enrollment wizard does not: its customer/document/Perch data is persisted
// through the real Dalton backend routes below.
let customers = [];

const steps = [1,2,3,4,5];
// Step 3 (Contact & Login) was MERGED into step 2 (Customer & Bill): it only
// repeated the email and collected phone + password, which now live in the
// combined screen. The numbering is preserved so backend workflow keys and the
// existing branch logic keep mapping to the same screens.
const stepIds = {1:'step-project',2:'step-bill',4:'step-lmi',5:'step-agreement'};

// ── Branch-aware wizard navigation ───────────────────────────────────────
// The LMI/eligibility step (4) exists ONLY on the LMI branch. Which branch we
// are on is decided by the BACKEND: the customer_type resolved from the Perch
// capacity response (see services/perch/adapter.resolve_customer_type). This is
// never inferred in JS - selectedProgram is set from the /programs response and
// from the persisted enrollment on resume.
//
// Residential does not merely HIDE step 4: activeSteps() removes it, so Next
// from step 3 goes to 5, Back from 5 goes to 3, and the progress indicator has
// four dots instead of five.
let selectedProgram = null;   // { customer_type, savings_percent, lmi_required }
// True once Perch has accepted /enroll: the program becomes a settled fact.
let programCommitted = false;

function programRequiresLmi(){
  // Absent information is treated as "not yet known", NOT as "no LMI".
  if(!selectedProgram) return null;
  return selectedProgram.lmi_required === true;
}

function activeSteps(){
  const needsLmi = programRequiresLmi();
  // Step 3 no longer exists (merged into step 2).
  //   Residential : Availability -> Customer & Bill -> Agreements
  //   LMI         : Availability -> Customer & Bill -> Eligibility -> Agreements
  // Until the program is known, the Eligibility step is KEPT so nothing is
  // skipped on a guess; it is dropped only once we know it is Residential.
  if(needsLmi === false) return [1,2,5];
  return [1,2,4,5];
}

function stepPosition(n){
  const seq = activeSteps();
  const i = seq.indexOf(n);
  return i === -1 ? 0 : i;
}

function nextStepAfter(n){
  const seq = activeSteps();
  const i = seq.indexOf(n);
  return (i === -1 || i === seq.length - 1) ? n : seq[i + 1];
}

function prevStepBefore(n){
  const seq = activeSteps();
  const i = seq.indexOf(n);
  return i <= 0 ? seq[0] : seq[i - 1];
}
const stepLabels = ['Capacity','Bill','Contact','LMI','Agreement'];

let state = {
  rep:{name:'Charlie Mren'},
  project:{id:'',name:'',utility:''},
  customer:{first:'',last:'',email:'',phone:'',acct:'',meterNumber:'',password:''},
  address:{street:'',unit:'',city:'',state:'NY',zip:''},
  bill:{fileName:'',documentId:null},
  billing:{sameAsService:true,street:'',unit:'',city:'',state:'NY',zip:''},
  lmi:{mode:'doc',docType:'',fileName:'',documentId:null,nameOnDocument:'',relationship:'self',documentFormat:''}
};
let currentCustomerId = null;
let skipProjectStep = false;
let entryMode = null;

function showScreen(id){
  activateScreen(id);
}
function activateScreen(target){
  document.querySelectorAll('.screen').forEach(s=>s.classList.remove('active'));
  document.getElementById('app-shell').classList.remove('active');
  if(target === 'app-shell'){
    document.getElementById('app-shell').classList.add('active');
  } else {
    document.getElementById(target).classList.add('active');
  }
  window.scrollTo({top:0});
}

async function doLogin(){
  const email = document.getElementById('login-email').value.trim();
  const pass = document.getElementById('login-pass').value;
  const errEl = document.getElementById('login-error');
  const btn = document.getElementById('login-submit-btn');
  errEl.style.display = 'none';
  if(!email || !pass){ errEl.textContent = 'Enter your email and password to continue.'; errEl.style.display=''; return; }

  btn.disabled = true;
  btn.textContent = 'Signing in…';

  // ONE endpoint for reps, admins and customers. The BACKEND decides which
  // account type this is and where to send them - the browser never chooses a
  // role, and no "are you a customer?" link is needed for password sign-in.
  let data;
  try {
    data = await apiFetch('/api/auth/signin', {
      method: 'POST',
      body: JSON.stringify({ email, password: pass }),
    });
  } catch(err){
    errEl.textContent = userMessage(err);
    errEl.style.display='';
    btn.disabled = false;
    btn.textContent = 'Continue';
    return;
  }
  btn.disabled = false;
  btn.textContent = 'Continue';

  if(data.account_type === 'customer'){
    // Customer tokens live in their OWN store so the two sessions can never be
    // confused. Clear any stale staff token first.
    AuthStore.clear();
    currentUser = null;
    CustomerAuth.setToken(data.token);
    activateScreen('screen-customer-portal');
    renderPortalGreeting(data.customer);
    await loadCustomerAgreement();
    return;
  }

  // Staff. Clear any stale customer token for the same reason.
  CustomerAuth.clear();
  AuthStore.setToken(data.token);
  currentUser = data.user;
  applyCurrentUserToUI();
  await loadProjects();
  activateScreen('app-shell');
  syncAdminNav();
  // destination is server-authoritative; every current staff role lands on the
  // dashboard, and an unknown role falls back to it rather than guessing.
  // A rep who followed a link and was asked to sign in continues to the place
  // they were going, which the address bar still holds.
  const wanted = Router.parse(window.location ? window.location.pathname : '/');
  if(wanted.view === 'wizard' && wanted.enrollmentId){
    await Router.apply();
  } else {
    showView(data.destination === 'dashboard' ? 'dashboard' : 'dashboard');
  }
}

function applyCurrentUserToUI(){
  // ADMIN NAV IS DERIVED, NOT SET ONCE. This runs BEFORE the null guard
  // below, so losing the session hides the admin controls instead of leaving
  // the last user's nav on screen.
  //
  // ROOT CAUSE THIS FIXES: syncAdminNav() used to be called only from the
  // login path and one admin PATCH. #nav-reps and #nav-sales-report are
  // display:none in index.html, so nothing re-revealed them after a page
  // refresh - tryRestoreSession() verified the JWT, set currentUser and never
  // touched the nav. An admin who refreshed lost both buttons until they
  // logged out and back in, which is exactly the reported symptom.
  syncAdminNav();
  if(!currentUser) return;
  const initial = (currentUser.full_name || currentUser.email || '?').trim().charAt(0).toUpperCase();
  document.getElementById('rep-avatar').textContent = initial;
  document.getElementById('rep-name-label').textContent = currentUser.full_name || currentUser.email;
  document.getElementById('sidebar-role-label').textContent = 'Role: ' + (currentUser.role || '').replace(/_/g, ' ');
  state.rep.name = currentUser.full_name || currentUser.email;
}

// Restores a session on page refresh if a token is already in sessionStorage
// (real JWT verification round-trip against GET /api/auth/me — not just
// "a token exists so trust it" client-side).
async function tryRestoreSession(){
  const token = AuthStore.getToken();
  if(!token) return;
  try {
    currentUser = await apiFetch('/api/auth/me');
  } catch(err){
    AuthStore.clear();
    return;
  }
  // applyCurrentUserToUI() syncs the admin nav. THIS is the path that was
  // broken: a refresh restored the session and the role, but never the nav.
  applyCurrentUserToUI();
  await loadProjects();
  activateScreen('app-shell');
  // Honour the URL the rep actually arrived on, instead of always resetting
  // them to the dashboard. A refresh on an enrollment reopens that
  // enrollment; a pasted link opens it directly.
  await Router.apply();
}
/* Boot: try the STAFF session first, then the CUSTOMER session.

   tryRestoreSession() above is unchanged - it handles staff only and returns
   immediately when there is no staff token. That early return is why a
   magic-link customer landed on the login screen: nothing ever consulted
   dalton_customer_token after /access redirected to /.

   Order matters. Staff is attempted first so an active rep session always wins;
   the customer attempt only runs when no staff session was restored. */
async function bootRestoreSession(){
  await tryRestoreSession();
  // A restored staff session activates #app-shell; if that happened, stop.
  const shell = document.getElementById('app-shell');
  if(shell && shell.classList.contains('active')) return;
  await tryRestoreCustomerSession();
}
document.addEventListener('DOMContentLoaded', bootRestoreSession);

/* doCustomerLogin() was REMOVED with the duplicate customer sign-in screen.
   Customers authenticate through the one unified login; the backend routes
   them. /api/auth/customer-login still exists server-side. */

// Customer tokens are stored separately from the rep token so the two sessions
// can never be confused for one another.
const CustomerAuth = {
  KEY: 'dalton_customer_token',
  getToken(){ try { return sessionStorage.getItem(this.KEY); } catch(e){ return null; } },
  setToken(t){ try { sessionStorage.setItem(this.KEY, t); } catch(e){} },
  clear(){ try { sessionStorage.removeItem(this.KEY); } catch(e){} },
};

/* The customer's own summary (C5).
   This used to write "<project> — <step label>" into #portal-project, an
   element that has never existed in the markup, so the whole response was
   fetched and discarded: the customer saw a single hardcoded "Awaiting your
   signature" and nothing about what they had signed up for. Every value below
   comes from that same response - the utility through the shared resolver
   (never a raw slug), the savings from the persisted-Perch figure the rep
   view uses, and the status from the authoritative workflow key. */
async function loadCustomerAgreement(){
  /* A row shows a STRING or it shows nothing.
     The savings row proved why this guard is worth having: it was handed a
     structure and rendered "[object Object] off your electricity supply" on a
     real customer's page. Any non-string - an object, an array, a number -
     now hides its row instead of being stringified into the page, so a shape
     change in the payload can degrade to a missing line but never to
     [object Object]. */
  const setRow = (rowId, valueId, text) => {
    const row = document.getElementById(rowId);
    const val = document.getElementById(valueId);
    const safe = (typeof text === 'string') ? text.trim() : '';
    if(val) val.textContent = safe;
    if(row) row.hidden = !safe;
  };
  const status = document.getElementById('portal-status');
  try{
    const res = await fetch('/api/auth/customer-me', {
      headers: {Authorization: 'Bearer ' + CustomerAuth.getToken()}
    });
    if(!res.ok) throw new Error('Could not load your enrollment.');
    const me = await res.json();

    setRow('portal-row-utility', 'portal-utility', me.utility_name);
    // SAVINGS IS NOT SHOWN HERE. The row C5 added rendered
    // "[object Object] off your electricity supply": program_savings is a
    // structure, which the rep-facing views pass through formatSavings()
    // before display, and this concatenated it raw. It is removed rather than
    // formatted - the customer's own page is not where that figure belongs.
    // The backend still returns the field; this page simply does not read it.
    setRow('portal-row-address', 'portal-address', me.service_address);

    // The same pill and the same four families as the rest of UBS, from the
    // same resolver - not a hardcoded gold badge that said "Awaiting your
    // signature" whatever the enrollment was actually doing.
    if(status){
      status.innerHTML = '<span class="status-pill ' + statusFamilyFor(me) + '">' +
        esc(customerStatusLabel(me)) + '</span>';
    }
    updatePortalAction(me);
  }catch(err){
    if(status) status.textContent = 'We could not load your enrollment just now.';
  }
}

/* The customer's words for where things stand. The rep-facing short labels
   describe the WORK ("Proof documents needed"); a customer wants to know
   whether anything is waiting on them. Unknown keys fall back to the
   backend's own label rather than to a guess. */
const CUSTOMER_STATUS_LABEL = {
  contracts: 'Ready for you to sign',
  contracts_review: 'Ready for you to sign',
  contracts_accept: 'Ready for you to sign',
  contracts_accepted: 'Signed — you are all set',
  proof_docs: 'We need one more document',
  self_attestation: 'We need one more detail',
  self_attestation_accept: 'Ready for you to confirm',
  no_capacity: 'On hold',
};
function customerStatusLabel(me){
  return CUSTOMER_STATUS_LABEL[me && me.workflow_step_key]
      || (me && me.workflow_step_label)
      || 'In progress';
}

/* The button only offers what the customer can actually do right now. */
function updatePortalAction(me){
  const btn = document.getElementById('portal-open-btn');
  if(!btn) return;
  const terminal = !!(me && me.workflow_is_terminal);
  btn.textContent = terminal ? 'View your agreement' : 'Review & sign agreement';
}


/* ────────────────────────── ROUTING ──────────────────────────
   UBS is one page, and it used to be one URL. Back left the application,
   refresh always landed on the dashboard, and an enrollment could not be
   linked to. This is the smallest thing that fixes all three: a path per
   view, pushed on navigation and read back on load and on popstate.

   DELIBERATELY NOT A FRAMEWORK. There is no route table driving rendering -
   showView() and openEnrollment() still do that. The router only keeps the
   address bar and history in step with them, and translates back the other
   way when the browser moves. That means no render path had to change to
   gain history, and a bug here cannot blank the screen: the worst case is a
   stale URL.

   AUTHORIZATION IS UNCHANGED. The server hands out the same public shell for
   every one of these paths; every piece of data is still fetched through an
   authenticated, authorized API call, so a deep link to an enrollment the rep
   may not see fails exactly as it did before. */
const Router = {
  // Set while a popstate or a boot-time restore is being applied, so the
  // showView() hook below does not push the entry we are already handling.
  applying: false,

  pathFor(view, enrollmentId){
    if(view === 'wizard'){
      return enrollmentId ? '/enrollments/' + enrollmentId : '/enrollments';
    }
    if(view === 'dashboard') return '/dashboard';
    return '/' + view;
  },

  parse(pathname){
    const m = /^\/enrollments\/(\d+)\/?$/.exec(pathname || '');
    if(m) return {view: 'wizard', enrollmentId: Number(m[1])};
    const p = (pathname || '').replace(/\/+$/, '');
    if(p === '/customers') return {view: 'customers'};
    if(p === '/reps') return {view: 'reps'};
    if(p === '/sales-report') return {view: 'sales-report'};
    return {view: 'dashboard'};
  },

  push(view, enrollmentId){
    if(this.applying) return;
    if(typeof history === 'undefined' || !history.pushState) return;
    const path = this.pathFor(view, enrollmentId);
    if(window.location && window.location.pathname === path) return;
    try { history.pushState({view: view, enrollmentId: enrollmentId || null},
                            '', path); } catch(e){ /* non-browser host */ }
  },

  replace(view, enrollmentId){
    if(typeof history === 'undefined' || !history.replaceState) return;
    try { history.replaceState({view: view, enrollmentId: enrollmentId || null},
                               '', this.pathFor(view, enrollmentId)); } catch(e){}
  },

  /* Render whatever the current URL says. Used on boot and on popstate. */
  async apply(){
    const route = this.parse(window.location ? window.location.pathname : '/');
    this.applying = true;
    try {
      if(route.view === 'wizard' && route.enrollmentId){
        // openEnrollment reports its own failure (gone, or not this rep's) and
        // the dashboard is the honest fallback.
        const ok = await openEnrollment(route.enrollmentId);
        if(ok === false){ this.applying = false; this.replace('dashboard'); showView('dashboard'); return; }
        return;
      }
      showView(route.view);
    } finally {
      this.applying = false;
    }
  },
};

if(typeof window !== 'undefined' && window.addEventListener){
  window.addEventListener('popstate', function(){
    // Back/Forward inside UBS. The address bar has already moved; catch up.
    Router.apply();
  });
}

function showView(name){
  // THE ONE PLACE EVERY IN-APP NAVIGATION PASSES THROUGH.
  //
  // The save hook used to live in exitWizard() and goStep(). The sidebar links
  // call showView('dashboard') / showView('customers') directly from inline
  // onclick handlers, so leaving by the nav - the most natural way out - saved
  // nothing. A rep should not have to know which button is the safe one.
  // Putting it here covers the nav, exitWizard, and anything added later.
  const leavingWizard = name !== 'wizard'
    && document.getElementById('view-wizard')
    && document.getElementById('view-wizard').classList.contains('active');
  const alreadySaved = progressSavedByCaller;
  progressSavedByCaller = false;      // consumed either way
  if(leavingWizard && !alreadySaved){
    // Fire and forget: navigation must never wait on a background save.
    captureCustomerFields();
    saveCustomerProgress();
  }
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
  document.getElementById('view-'+name).classList.add('active');
  document.querySelectorAll('.nav-link[data-view]').forEach(n=>n.classList.toggle('active', n.dataset.view===name));
  if(name==='dashboard') renderDashboard();
  if(name==='customers') renderCustomers('');
  window.scrollTo({top:0});

  if(name === 'reps') loadReps();
  if(name === 'sales-report') loadSalesReport();

  // Keep the address bar in step. The wizard pushes its own URL from
  // openEnrollment(), which is the only place that knows the enrollment id.
  if(name !== 'wizard') Router.push(name);

  // Re-derived on every view change. Cheap (two style writes) and it means no
  // future render path can leave the nav disagreeing with currentUser.role.
  // Presentation only: every /api/admin route is still enforced with
  // @require_role("admin") server-side, so a rep who forces these buttons
  // visible gets 403s, not data.
  syncAdminNav();

  // On a phone the drawer is covering the screen the rep just asked for.
  // showView() is the one place every in-app navigation passes through, so
  // closing here cannot be missed by a nav link added later.
  closeNav();
}

async function loadProjects(){
  try {
    const data = await apiFetch('/api/projects');
    projects = data.map(p => ({
      id: p.id, name: p.name, address: p.address, utility: p.utility, location: p.location,
      pct: p.capacity_pct_full, spotsLeft: p.spots_left, payment: p.payment_type, term: p.term,
      savingsPct: p.savings_pct, type: p.savings_pct + '% savings', cancellation: p.cancellation_terms,
      cod: p.commercial_operation_date, full: !!p.is_full, lmiRequired: !!p.lmi_required,
      programType: p.program_type,
    }));
  } catch(err){
    console.error('Failed to load projects:', err.message);
    projects = [];
  }
}

let enrollments = []; // populated by loadEnrollments() from GET /api/enrollments

async function loadEnrollments(){
  try {
    enrollments = await apiFetch('/api/enrollments');
  } catch(err){
    console.error('Failed to load enrollments:', err.message);
    enrollments = [];
  }
}

// Real backend statuses (18 of them, see backend/services/status_machine.py)
// grouped into the same 5-card layout the dashboard already had. This is a
// dashboard-display grouping only — the real status string is always shown
// as-is in the Customers table and everywhere else.
/* Each bucket carries its C1 status FAMILY (C4).
   The four buckets have always mapped one-to-one onto the four semantic
   families the design system defines, but only Complete said so on screen:
   it got a green number while "Needs Attention" - the one bucket that is a
   call to action - rendered in plain ink, indistinguishable from "In
   Progress". Naming the family here means the card, the pill and any future
   surface all colour the same state the same way, from one source. */
const DASH_BUCKETS = [
  { label: 'In Progress',         family: 'neutral',
                                  keys: ['service_area','capacity_result','enroll',
                                         'proof_docs','self_attestation',
                                         'self_attestation_accept','contracts'] },
  { label: 'Awaiting Acceptance', family: 'action-needed',
                                  keys: ['contracts_review','contracts_accept'] },
  { label: 'Complete',            family: 'complete',
                                  keys: ['contracts_accepted'] },
  // Blocked, unrecognised, or unmapped states land here rather than being
  // silently miscounted or dropped.
  { label: 'Needs Attention',     family: 'attention',
                                  keys: ['no_capacity','enroll_outcome_uncertain',
                                         'contracts_accept_uncertain',
                                         'unknown_next_step','status'] },
];

// Counters derive from the PERCH WORKFLOW state, never enrollments.status.
// enrollments.status belongs to the legacy QA/developer pipeline and is
// deliberately never advanced by the Perch flow (see the comments in
// enrollment_routes.py and services/perch/workflow.py), so bucketing it meant a
// COMPLETE enrollment still read "Draft" and counted as In Progress - exactly
// the "5 In Progress / 0 everything else" the dashboard was showing.
// workflow_step_key IS advanced by the Perch flow and is already on every list
// row, so this needs no backend change and writes nothing.
function dashBucketFor(e){
  const key = e.workflow_step_key || null;
  if(e.workflow_is_terminal === true) return 'Complete';
  if(e.workflow_is_blocked === true) return 'Needs Attention';
  for(const b of DASH_BUCKETS){
    if(key && b.keys.indexOf(key) !== -1) return b.label;
  }
  // No workflow row yet (created, not started) is genuinely in progress; an
  // unrecognised key is surfaced rather than hidden.
  return key ? 'Needs Attention' : 'In Progress';
}

/* ═══════════ The dashboard greeting ═══════════

   It used to be the literal string 'Good afternoon, ', so a rep opening UBS at
   seven in the morning was greeted with the afternoon - and a rep who left the
   tab open all day was still greeted with it at midnight.

   THE TIME IS THE BROWSER'S, not the server's. The server runs in UTC (and in
   Eastern for business-day reporting); neither is the hour in front of the
   person reading the screen, which is the only hour a greeting can be about.

   Hours, per the agreed wording:
       05:00-11:59  morning
       12:00-16:59  afternoon
       17:00-04:59  evening                                                  */
const GREETING_BANDS = [
  {from: 5,  until: 12, text: 'Good morning'},
  {from: 12, until: 17, text: 'Good afternoon'},
];
const GREETING_EVENING = 'Good evening';

function greetingForHour(hour){
  const h = Number(hour);
  if(!isFinite(h)) return GREETING_EVENING;
  for(let i = 0; i < GREETING_BANDS.length; i++){
    const b = GREETING_BANDS[i];
    if(h >= b.from && h < b.until) return b.text;
  }
  return GREETING_EVENING;      // 17:00-23:59 and 00:00-04:59 alike
}

/* The name, EXACTLY as it was built before: full_name if there is one, else
   the email, first word either way, and an empty string when nobody is signed
   in. Lifted into its own function so the greeting change could not disturb
   it, not because it needed changing. */
function dashGreetingName(){
  return currentUser ? (currentUser.full_name || currentUser.email).split(' ')[0] : '';
}

function applyDashGreeting(){
  const el = document.getElementById('dash-greeting');
  if(!el) return;
  el.textContent = greetingForHour(new Date().getHours()) + ', ' + dashGreetingName();
}

/* Milliseconds until the next band change, from a given moment.

   Computed as a real instant rather than "an hour from now" so it is right
   across a DST change, and so the update lands ON the boundary instead of up
   to an hour late. */
function msUntilNextGreetingChange(now){
  const at = function(hour){
    const d = new Date(now.getTime());
    d.setHours(hour, 0, 0, 0);
    return d;
  };
  const marks = [at(5), at(12), at(17)];
  const tomorrow5 = at(5);
  tomorrow5.setDate(tomorrow5.getDate() + 1);
  marks.push(tomorrow5);
  for(let i = 0; i < marks.length; i++){
    const delta = marks[i].getTime() - now.getTime();
    if(delta > 0) return delta;
  }
  return 60 * 60 * 1000;        // unreachable; an hour is a safe fallback
}

let greetingTimer = null;

/* Re-greet when the clock crosses a boundary, with no refresh.

   A single timeout to the NEXT boundary, rather than a poll: it fires three
   times a day instead of once a minute. The visibility listener is the other
   half of the same job - a laptop that slept through 5pm wakes with a timeout
   that is overdue rather than fired, and the greeting would otherwise stay
   wrong until the next boundary. */
function scheduleGreetingRefresh(){
  if(greetingTimer){ clearTimeout(greetingTimer); greetingTimer = null; }
  // A floor, so this can never become a spin. If the boundary maths ever
  // returned zero or a negative - a clock moved backwards, a leap second, a
  // bug here - a bare setTimeout(0) chain would re-fire every few
  // milliseconds for as long as the tab stayed open, which on a dashboard
  // reps leave open all day is a flat battery rather than a wrong word.
  const delay = Math.max(1000, msUntilNextGreetingChange(new Date()));
  // Flipped to false once setTimeout RETURNS. A real timer cannot run its
  // callback before that; a test double that calls the function immediately
  // can, and rescheduling from inside such a call recurses until the stack
  // gives out. Caught by phase_a_state_harness.js, whose setTimeout stub does
  // exactly that - the greeting still updates there, it just does not chain.
  let firedSynchronously = true;
  greetingTimer = setTimeout(function(){
    greetingTimer = null;
    applyDashGreeting();
    if(!firedSynchronously) scheduleGreetingRefresh();
  }, delay);
  firedSynchronously = false;
}

document.addEventListener('visibilitychange', function(){
  if(document.visibilityState === 'visible'){
    applyDashGreeting();
    scheduleGreetingRefresh();
  }
});

async function renderDashboard(){
  applyDashGreeting();
  scheduleGreetingRefresh();
  await loadEnrollments();   // Projects UI removed; Perch assigns projects

  document.getElementById('dash-stats').innerHTML = DASH_BUCKETS.map(bucket => {
    const n = enrollments.filter(e => dashBucketFor(e) === bucket.label).length;
    // Every card is built the same way and differs only by family, so the
    // treatment cannot drift one bucket at a time the way it had.
    //
    // stat-verified is GONE. It was a second way of saying "complete" that
    // only this one bucket had, and it disagreed with the family it now
    // belongs to: its rule painted the border --brand while the numeral took
    // --status-complete-ink, so the card's accent and its number were two
    // different greens. One name for one meaning.
    return '<div class="stat-card stat-fam stat-fam-'+bucket.family+'">'
         + '<div class="sc-num">'+n+'</div>'
         + '<div class="sc-label">'+bucket.label+'</div></div>';
  }).join('');


  const recent = [...enrollments].sort((a,b)=> (a.updated_at < b.updated_at ? 1 : -1)).slice(0, 5);
  document.getElementById('dash-recent-body').innerHTML = recent.length
    ? recent.map(enrollmentRowHtml).join('')
    : tableEmptyRowHtml(5, 'No enrollments yet',
        'Start one from the button above and it will appear here.');
}

function renderProjects(targetId, showAllActions){
  const wrap = document.getElementById(targetId);
  const list = showAllActions ? projects : projects.slice(0,2);
  wrap.innerHTML = list.map(p=>{
    const statusWord = p.full ? 'Full' : (p.pct < 15 ? 'Open' : 'Filling');
    const actionsHtml = p.full
      ? '<button class="btn btn-secondary" disabled>Signup disabled</button>'
      : '<button class="btn btn-primary" onclick="startWizardForProject(\'' + p.id + '\')">Add a new customer</button>';
    return '' +
    '<div class="project-card">' +
      '<div class="project-card-head"><div class="pc-name">'+p.name+'</div><div class="pc-addr">'+p.address+'</div></div>' +
      '<div class="project-card-body">' +
        '<div class="gauge" style="background:conic-gradient(var(--brand) '+(p.pct*3.6)+'deg, var(--border) 0deg);"><div class="gauge-inner"><div class="g-pct">'+p.pct+'%</div><div class="g-status">'+statusWord+'</div></div></div>' +
        '<div class="pc-detail-list">' +
          '<div class="pd-row"><div class="pd-val">'+p.payment+'</div><div class="pd-label">Payment accepted</div></div>' +
          '<div class="pd-row"><div class="pd-val">'+p.term+'</div><div class="pd-label">Term</div></div>' +
          '<div class="pd-row"><div class="pd-val">'+p.type+'</div><div class="pd-label">Type</div></div>' +
        '</div>' +
        '<div class="pc-detail-list">' +
          '<div class="pd-row"><div class="pd-val">'+p.location+'</div><div class="pd-label">Project location</div></div>' +
          '<div class="pd-row"><div class="util-wordmark"><span class="u1">national</span><span class="u2">grid</span></div><div class="pd-label">Utility</div></div>' +
          '<div class="pd-row"><div class="pd-val">'+p.spotsLeft.toLocaleString()+' spots left</div><div class="pd-label">Availability</div></div>' +
        '</div>' +
        '<div class="pc-actions">' + actionsHtml +
          '<button class="btn btn-secondary btn-sm" onclick="alert(\'Full project detail pages aren&#92;&#39;t wired up in this demo yet.\')">Project details</button>' +
        '</div>' +
      '</div>' +
    '</div>';
  }).join('');
}

/* ── THE FOUR STATUS FAMILIES (UBS 2.0, C1) ──────────────────────────────
   One meaning, one appearance, everywhere.

   This is PRESENTATION ONLY. The backend decides what a step MEANS -
   workflow_step_key, workflow_step_label, workflow_is_terminal and
   workflow_is_blocked are authoritative and are not second-guessed here.
   This table decides only how that meaning LOOKS, and is keyed on
   workflow_step_key so a label rewrite can never silently change a colour.

     neutral          in motion; nothing is waiting on anyone
     action-needed    waiting on the customer: documents, attestation, signature
     complete         finished
     attention        something is wrong and a human must look

   Why "waiting on the customer" earns its own colour: a rep scanning the
   customer list is looking for the rows they have to chase. Before C1 those
   rows were the same grey as every other in-progress enrollment, so the list
   answered "where is each enrollment" but not "what do I do today".

   SHORT LABELS are for the list and table views only, where a pill sits in a
   fixed column: "Does not qualify for the available program" rendered as a
   40-character sentence in a red pill, which is both too long for the column
   and the wrong colour - it is a settled outcome, not a failure. The full
   workflow_step_label is still what the detail view shows, and it is carried
   on title and aria-label here so nothing is lost to a screen reader or a
   hover. A step with no entry falls back to the backend's own label. */
const STATUS_FAMILY = {
  complete:        'status-complete',
  attention:       'status-attention',
  'action-needed': 'status-action-needed',
  neutral:         'status-neutral'
};

const WORKFLOW_STATUS = {
  // key                        family           short label (lists only)
  service_area:               ['neutral',       'Service area'],
  capacity_result:            ['neutral',       'Capacity confirmed'],
  no_capacity:                ['neutral',       'No capacity'],
  enroll:                     ['neutral',       'Ready to enroll'],
  contracts:                  ['neutral',       'Ready for contracts'],
  status:                     ['neutral',       'Checking status'],
  // A settled local decision, not an error: the customer's income is above
  // the published limit and Perch was never called. Neutral, and short.
  lmi_not_qualified:          ['neutral',       'Not eligible'],

  proof_docs:                 ['action-needed', 'Documents needed'],
  self_attestation:           ['action-needed', 'Attestation needed'],
  self_attestation_accept:    ['action-needed', 'Attestation to accept'],
  contracts_review:           ['action-needed', 'Awaiting signature'],
  contracts_accept:           ['action-needed', 'Awaiting signature'],

  contracts_accepted:         ['complete',      'Complete'],

  contracts_accept_uncertain: ['attention',     'Needs review'],
  enroll_outcome_uncertain:   ['attention',     'Needs review'],
  unknown_next_step:          ['attention',     'Needs review']
};

/* The family for an enrollment. The step key decides it; the backend's own
   terminal/blocked flags are the fallback for a key this table has not seen,
   so a new Perch step degrades to a sensible colour instead of none. */
function statusFamilyFor(e){
  const mapped = WORKFLOW_STATUS[e.workflow_step_key];
  if (mapped) return STATUS_FAMILY[mapped[0]];
  if (e.workflow_is_terminal) return STATUS_FAMILY.complete;
  if (e.workflow_is_blocked) return STATUS_FAMILY.attention;
  return STATUS_FAMILY.neutral;
}

/* The label for a pill in a LIST. Falls back to the backend's label. */
function statusShortLabel(e){
  const mapped = WORKFLOW_STATUS[e.workflow_step_key];
  return (mapped && mapped[1]) || e.workflow_step_label || 'In progress';
}

// enrollments.status (QA/reporting vocabulary), shown on the projects card.
// Same four families, keyed on the status word rather than a workflow step.
function statusPillHtml(status){
  const complete = ['Accepted','Project Assigned','Active'].includes(status);
  const attention = ['Rejected','Needs Work'].includes(status);
  const cls = complete ? 'status-complete'
            : (attention ? 'status-attention' : 'status-neutral');
  return '<span class="status-pill '+cls+'">'+status+'</span>';
}

// Perch workflow state is the accurate rep-facing progress. enrollments.status
// is left alone - it still drives QA/reporting and is not rep-facing here.
/* STATUS IS THE RESUME CONTROL. There is no separate Open column.
   It calls openEnrollment(id) - the same backend-authoritative resume path the
   old Open link used. The visible label is never parsed to decide where to go;
   the backend workflow state does that. Rendered as a real <button> so it is
   keyboard reachable and announced correctly. */
function workflowPillHtml(e){
  const full = e.workflow_step_label || 'In progress';
  const label = statusShortLabel(e);
  const cls = statusFamilyFor(e);
  const verb = e.workflow_is_terminal ? 'View' : 'Resume';
  return '<button type="button" class="status-pill status-action ' + cls + '" ' +
         'onclick="openEnrollment(' + e.id + ')" ' +
         'title="' + esc(full) + '" ' +
         'aria-label="' + esc(verb + ' enrollment ' + (e.enrollment_code || e.id) +
                              ' — ' + full) + '">' +
         esc(label) + '<svg class="sp-go" viewBox="0 0 16 16" aria-hidden="true">' +
         '<path d="M6 3.5L10.5 8L6 12.5" fill="none" stroke="currentColor" ' +
         'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></button>';
}

/* Superseded: the STATUS badge is the resume control now, so no row has a
   separate Open cell. Retained for any external caller. */
function openActionHtml(e){
  const label = e.workflow_is_terminal ? 'View' : 'Open';
  return '<span class="resume-link" onclick="openEnrollment('+e.id+')">'+label+'</span>';
}

function enrollmentRowHtml(e){
  const custName = customerLabel(e);
  const custEmail = e.customer ? e.customer.email : '';
  const projectName = e.project ? e.project.name : '—';
  // data-label drives the <=820px card layout (CSS turns each cell into a
  // labelled row), so the table never scrolls sideways on a phone.
  // The Project column has shown "—" for every enrollment since Perch began
  // assigning projects, so it carries the enrollment code instead - which
  // Phase A asked to surface anyway.
  return '<tr class="crm-row">' +
    // C5: the name and email are clamped to one line each at desktop widths,
    // so the cell carries both in full for a pointer. On a touch device the
    // hover-free rule lets them wrap instead, so neither is ever unreadable.
    '<td data-label="Customer" class="cell-cust" title="' +
      esc([custName, custEmail].filter(Boolean).join(' · ')) + '">' +
      '<span class="cust-name">'+esc(custName)+'</span>' +
      (custEmail ? '<span class="cust-sub">'+esc(custEmail)+'</span>' : '') +
    '</td>' +
    '<td data-label="Enrollment" class="cell-code"><span class="mono">'+esc(e.enrollment_code || '—')+'</span></td>' +
    '<td data-label="Status" class="cell-status">'+workflowPillHtml(e)+'</td>' +
    // C5: the utility cell had no title at all, so once a name was longer
    // than its column - "Central Hudson Gas & Electric" is - there was no way
    // to read the rest on any device. Title for a pointer, and the hover-free
    // rule wraps it in full on touch.
    '<td data-label="Utility" class="cell-util" title="'
      + esc(e.utility_name || '') + '">'+esc(e.utility_name || '—')+'</td>' +
    // Same instant, same precision - date over time instead of one long
    // nowrap line, with the full string kept on the title for hover.
    '<td data-label="Added" class="cell-date" title="'+esc(formatTimestamp(e.created_at))+'">'
      +timestampCellHtml(e.created_at)+'</td>' +
    '<td data-label="Last modified" class="cell-date" title="'+esc(formatTimestamp(e.updated_at))+'">'
      +timestampCellHtml(e.updated_at)+'</td>' +
  '</tr>';
}

/* Backend timestamps are SQLite datetime('now') values: UTC, formatted
   "YYYY-MM-DD HH:MM:SS" with NO timezone marker. Left as-is, JS would parse
   them as LOCAL time and display a value hours adrift. Appending 'Z' is what
   makes them parse as the UTC they actually are, after which toLocaleString
   renders them in the viewer's own timezone.

   These are always real persisted values - nothing here manufactures a "now". */
function parseBackendTimestamp(value){
  if(!value) return null;
  let text = String(value).trim();
  if(!text) return null;
  if(text.indexOf('T') === -1) text = text.replace(' ', 'T');
  // Only stamp UTC when the backend did not already supply an offset.
  if(!/[Zz]$|[+-]\d{2}:?\d{2}$/.test(text)) text += 'Z';
  const d = new Date(text);
  return isNaN(d.getTime()) ? null : d;
}

/* "Aug 18, 2026 at 4:12 PM" - never a raw ISO string. */
function formatTimestamp(value){
  const d = parseBackendTimestamp(value);
  if(!d) return '—';
  try{
    return d.toLocaleString(undefined, {
      month:'short', day:'numeric', year:'numeric',
      hour:'numeric', minute:'2-digit'
    }).replace(', ', ', ').replace(/,([^,]*)$/, ' at$1');
  }catch(e){
    return d.toISOString().slice(0,10);
  }
}

/* Clock time only. Pairs with formatDate so a table cell can put the date on
   one line and the time under it (C4).
   Before this, both date columns rendered the full "Oct 28, 2026 at 12:04 AM"
   on one nowrap line. Two of those are ~400px of a 1130px table, and because
   they could not be compressed the browser took the width from the columns
   that CAN shrink: the utility name collapsed to "Nation..." and the customer
   email was cut mid-address. Splitting the line lets the date columns be
   narrow without losing a character of what they said. */
function formatClock(value){
  const d = parseBackendTimestamp(value);
  if(!d) return '';
  try{
    return d.toLocaleTimeString(undefined, {hour:'numeric', minute:'2-digit'});
  }catch(e){
    return '';
  }
}

/* A table cell's timestamp: date over time, with the full value on hover. */
function timestampCellHtml(value){
  const date = formatDate(value);
  if(date === '—') return '—';
  const clock = formatClock(value);
  return '<span class="ts-date">'+esc(date)+'</span>'
       + (clock ? '<span class="ts-time">'+esc(clock)+'</span>' : '');
}

/* Date only, for compact table cells. */
function formatDate(iso){
  const d = parseBackendTimestamp(iso);
  if(!d) return '—';
  try{
    return d.toLocaleDateString(undefined, {month:'short', day:'numeric', year:'numeric'});
  }catch(e){
    return String(iso).slice(0,10);
  }
}

async function renderCustomers(query){
  const body = document.getElementById('customers-body');
  // The list is fetched on first view, so without this the table is blank
  // while the request is in flight and a rep cannot tell "loading" from
  // "nothing here".
  if(!enrollments.length && body){
    body.innerHTML = '<tr><td colspan="6" class="cell-wrap">' +
                     tableLoadingHtml(4) + '</td></tr>';
  }
  if(!enrollments.length) await loadEnrollments();
  const q = (query||'').toLowerCase();
  const filtered = enrollments.filter(e => {
    const custName = e.customer ? (e.customer.first_name+' '+e.customer.last_name+' '+e.customer.email) : '';
    const projectName = e.project ? e.project.name : '';
    return !q || (custName+' '+projectName+' '+e.enrollment_code).toLowerCase().includes(q);
  });
  const sorted = [...filtered].sort((a,b)=> (a.updated_at < b.updated_at ? 1 : -1));
  // ONE ROW BUILDER (C5). This screen hand-built a row that was the dashboard
  // row with two differences: no hover titles, and timestamps in the long
  // one-line form the dashboard stopped using in C4. Two copies of the same
  // component, drifting. It now calls the shared builder, so the two lists
  // are the same thing and a change to either is a change to both.
  //
  // The empty state also always read "Nothing matches that search", which is
  // the wrong sentence when there is nothing to search yet.
  document.getElementById('customers-body').innerHTML = sorted.length
    ? sorted.map(enrollmentRowHtml).join('')
    : (q
        ? tableEmptyRowHtml(6, 'Nothing matches that search',
            'Try a customer name, an email address, or an enrollment code.')
        : tableEmptyRowHtml(6, 'No customers yet',
            'Start an enrollment and it will appear here.'));
  const count = document.getElementById('customers-count');
  if(count){
    count.textContent = sorted.length
      ? (q ? sorted.length + ' of ' + enrollments.length +
             (enrollments.length === 1 ? ' enrollment' : ' enrollments')
           : sorted.length + (sorted.length === 1 ? ' enrollment' : ' enrollments'))
      : '';
  }
}

/* Blank every Customer & Bill control.

   Called from resetWizardState() so a new enrollment starts from an empty
   form, not from whatever the previous one left on screen. Listed explicitly
   rather than cleared by container, so adding a field is a deliberate decision
   about whether it is enrollment-specific. */
function clearCustomerBillForm(){
  ['c-first','c-last','c-phone','c-email','c-pass','c-pass-confirm',
   'c-acct','c-pod','c-meter',
   'a-street','a-unit','a-city','a-zip',
   'b-street','b-unit','b-city','b-zip'].forEach(function(id){
    const el = document.getElementById(id);
    if(el) el.value = '';
  });
  // Selects and checkboxes go back to their markup defaults, not to blank.
  ['a-state','b-state'].forEach(function(id){
    const el = document.getElementById(id);
    if(el) el.value = 'NY';
  });
  const same = document.getElementById('billing-same');
  if(same) same.checked = true;
  const note = document.getElementById('c-pass-set-note');
  if(note){ note.textContent = ''; note.style.display = 'none'; }
  const help = document.getElementById('meter-rule-help');
  if(help){ help.textContent = ''; help.style.color = ''; }
  const oc = document.getElementById('ocr-container');
  if(oc) oc.innerHTML = '';
}

function resetWizardState(){
  state.customer = {first:'',last:'',email:'',phone:'',acct:'',podId:'',
                    meterNumber:'',password:''};
  state.address = {street:'',unit:'',city:'',state:'NY',zip:''};
  state.bill = {fileName:'',documentId:null};
  state.billing = {sameAsService:true,street:'',unit:'',city:'',state:'NY',zip:''};
  state.lmi = {mode:'doc',docType:'',fileName:'',documentId:null,householdSize:'',incomeBelow:null,nameOnDocument:'',relationship:'self',documentFormat:''};
  billRuntimeFile = null; billUploadPromise = null; billUploadGeneration += 1;
  lmiRuntimeFile = null; lmiUploadPromise = null; lmiUploadGeneration += 1;
  // RC-6: DocSets holds the MULTI-FILE upload state (document_set id, the file
  // list, and the rendered filenames). It was never cleared, so the previous
  // enrollment's filename stayed on screen for the next one - looking like a
  // bill had been uploaded when none had. state.bill was already reset here,
  // which is why Continue still correctly blocked: the visible list and the
  // real state had diverged.
  docSetResetAll();
  perchContracts = [];
  // Enrollment-specific, like everything else here: B must not redraw A's
  // Availability step.
  availabilityWorkflow = null;
  lastBillParse = null;
  meterRule = null;
  // The PREVIOUS market's account rules. applyMarketAccountFields() returns
  // early when there is no slug yet, so without this an NY enrollment opened
  // after one in another market still carried that market's rules - and
  // demanded a meter number no NY utility asks for.
  marketAccountFields = null;
  perchContext = freshPerchContext();
  // The FORM ITSELF. state is reset above, but the inputs keep whatever the
  // last enrollment left in them until hydrateStep(2) happens to repaint them.
  // That is a real leak now that progress is captured FROM the DOM: a rep who
  // started enrollment B, got as far as a capacity check and navigated away
  // without ever opening the Customer & Bill step would have saved
  // enrollment A's customer onto B.
  clearCustomerBillForm();
  currentCustomerId = null;
  // RC-5: enrollment-specific globals that previously survived across
  // enrollments. After this call, Enrollment B must be indistinguishable from
  // Enrollment B on a fresh page load.
  billTouched = false;
  skipProjectStep = false;
  entryMode = null;
  currentWorkflow = null;

  // RC-7: PROGRAM + BRANCH state. These survived every previous reset, so a
  // completed enrollment leaked into the next one:
  //
  //   * programCommitted stayed true, so the new enrollment's program cards
  //     rendered .committed - non-selected options at opacity .55 and disabled.
  //     That is the Residential option "greyed out as if there were no capacity"
  //     on a second Central Hudson enrollment. Capacity was fine; the CONTROL
  //     was locked by the previous enrollment.
  //
  //   * selectedProgram / availablePrograms held the previous enrollment's
  //     programs until the new /programs call resolved, so activeSteps() and
  //     the stepper briefly followed the OLD branch.
  //
  //   * selfAttestation held the previous household answers.
  //
  //   * currentEnrollmentDetail made resolveProgramView() read the PREVIOUS
  //     enrollment's persisted program and savings.
  //
  // None of this is server state - which is why restarting app.py "fixed" it:
  // the restart forced a browser reload, re-evaluating app.js and resetting
  // these module globals and the DOM to their initial markup.
  // The enrollment identity itself. This was previously cleared only by the
  // CALLERS (startWizardFresh, backToDashboard), so any future caller that
  // forgot would carry enrollment A's id into B. openEnrollment sets it AFTER
  // this call, so resuming is unaffected.
  currentDraft = null;
  selectedProgram = null;
  availablePrograms = [];
  programCommitted = false;
  selfAttestation = {reference: null, occupancy: null, county: '', choice: null,
                     countyIncomeLimits: null};
  currentEnrollmentDetail = null;

  // RC-4: undo any read-only lock left by a completed/blocked enrollment.
  unlockEnrollmentControls();
  // ...and its step-5 STATUS WORDING. Same class of leak as the lock: without
  // this, the next enrollment's live Review & finish page would still be
  // headed "Enrollment complete", with its Back button hidden.
  restoreFinalStepFraming();
  const rb = document.getElementById('resume-banner');
  if(rb){ rb.style.display = 'none'; rb.innerHTML = ''; }
  const reqEl = document.getElementById('bill-requirements');
  if(reqEl){ reqEl.style.display = 'none'; reqEl.innerHTML = ''; }

  // RC-7 (DOM): the Eligibility step has TWO mutually exclusive panels. A
  // self-attestation enrollment leaves #sa-panel visible and #lmi-proof-panel
  // hidden, and nothing restored them - so the next enrollment showed the
  // self-attestation UI, and when it turned out to need proof docs the rep saw
  // the proof-doc TEXT with NO uploader, because the uploader lives inside the
  // still-hidden panel. Restore both to their markup defaults; the branch
  // handlers re-show whichever one Perch's next_step selects.
  const saPanel = document.getElementById('sa-panel');
  if(saPanel) saPanel.style.display = 'none';
  const saForm = document.getElementById('sa-form');
  if(saForm) saForm.style.display = '';
  const saSent = document.getElementById('sa-sent');
  if(saSent) saSent.style.display = 'none';
  const saErr = document.getElementById('sa-error');
  if(saErr){ saErr.style.display = 'none'; saErr.textContent = ''; }
  const proofPanel = document.getElementById('lmi-proof-panel');
  if(proofPanel) proofPanel.style.display = '';
  // Rebuilt from the next enrollment's own reference data.
  ['sa-occupancy', 'sa-county'].forEach(function(id){
    const el = document.getElementById(id);
    if(el){ el.innerHTML = ''; el.value = ''; }
  });
  const thr = document.getElementById('sa-threshold-wrap');
  if(thr) thr.style.display = 'none';
  document.querySelectorAll('.sa-opt').forEach(function(b){ b.classList.remove('selected'); });
  const saBtn = document.getElementById('sa-submit');
  if(saBtn){ saBtn.disabled = true; saBtn.textContent = 'Prepare self-attestation'; }
  // The terminal "does not qualify" panel MUST be cleared, or the next
  // enrollment opens showing the previous customer's refusal. Same class of
  // state bleed as the legacy LMI panels this reset already handles above.
  const saNQ = document.getElementById('sa-not-qualified');
  if(saNQ) saNQ.style.display = 'none';
  const saNQText = document.getElementById('sa-not-qualified-text');
  if(saNQText) saNQText.textContent = '';
  // Market-driven account fields: hide the meter input and forget the cached
  // field rules, so the next enrollment renders from ITS utility's rules
  // rather than inheriting the previous utility's.
  const meterField = document.getElementById('meter-number-field');
  if(meterField) meterField.style.display = 'none';
  const meterEl = document.getElementById('c-meter');
  if(meterEl) meterEl.value = '';
  const meterHelp = document.getElementById('meter-rule-help');
  if(meterHelp) meterHelp.textContent = '';
  marketAccountFields = null;

  // The program area is rebuilt by the next enrollment's /programs response.
  const progHost = document.getElementById('program-options');
  if(progHost) progHost.innerHTML = '';
  const progErr = document.getElementById('program-error');
  if(progErr){ progErr.style.display = 'none'; progErr.textContent = ''; }
  // The old #contract-accept-status element was removed with the legacy
  // contract UI. The shared Agreements component owns its own status region
  // and clears it on every mount, so there is nothing to reset here.
  clearWizardForms();
}
function clearWizardForms(){
  ['c-first','c-last','c-email','c-phone','c-acct','c-pod','c-pass','c-pass-confirm','a-street','a-unit','a-city','a-zip','b-street','b-unit','b-city','b-zip','lmi-name-on-doc'].forEach(id=>{ const el=document.getElementById(id); if(el) el.value=''; });
  document.getElementById('bill-file-chip').innerHTML='';
  document.getElementById('bill-file').value='';
  // The DocSets containers are separate elements from the legacy chip and must
  // be emptied too, or a stale filename survives visually.
  ['bill-fileset','lmi-fileset'].forEach(function(id){
    const el = document.getElementById(id); if(el) el.innerHTML = '';
  });
  const lf = document.getElementById('lmi-file'); if(lf) lf.value = '';
  document.getElementById('billing-same').checked=true;
  document.getElementById('billing-address-fields').style.display='none';
  document.getElementById('pod-id-field').style.display='none';
  document.getElementById('bill-submit-error').style.display='none';
  document.getElementById('ocr-container').innerHTML='';
  document.getElementById('lmi-doctype').value='';
  document.getElementById('lmi-file-chip').innerHTML='';
  document.getElementById('lmi-file').value='';
  document.getElementById('lmi-name-on-doc').value='';
  document.getElementById('lmi-relationship').value='self';
  document.getElementById('lmi-format').value='';
  document.getElementById('lmi-submit-error').style.display='none';
  document.getElementById('contact-submit-error').style.display='none';
  applyLmiCopy();
  document.getElementById('lmi-check-container').innerHTML='';
  document.getElementById('lmi-household-size').value='';
  document.getElementById('ami-threshold-display').textContent='';
  document.getElementById('ami-below').classList.remove('selected');
  document.getElementById('ami-above').classList.remove('selected');
  document.getElementById('lmi-mode-doc').classList.remove('selected');
  document.getElementById('lmi-mode-attest').classList.remove('selected');
  document.getElementById('lmi-mode-na').classList.remove('selected');
  // The legacy doc/attest/N.A. toggle row IS the mutually-exclusive mechanism.
  // A reset used to turn all three back on ('flex'), leaving them visible until
  // the next renderLmiSections() happened to run. Resetting them to hidden
  // removes the last code path that can reveal them, so their visibility no
  // longer depends on call ordering.
  document.getElementById('lmi-mode-doc').style.display='none';
  document.getElementById('lmi-mode-attest').style.display='none';
  document.getElementById('lmi-mode-na').style.display='none';
  document.getElementById('lmi-doc-panel').style.display='block';
  document.getElementById('lmi-attest-panel').style.display='none';
  document.getElementById('btn-lmi-next').disabled=true;
  document.getElementById('btn-bill-next').disabled=true;
  document.getElementById('pre-send').style.display='block';
  const agrHostEl=document.getElementById('agr-host-rep'); if(agrHostEl) agrHostEl.innerHTML='';
}

/* ==================== PERCH CAPACITY RENDERER ====================

   The generic descriptor renderer is intentionally limited to the Perch
   service-area/capacity entry point. After capacity succeeds, control returns
   to Dalton's existing Bill -> Contact -> LMI -> Agreement screens. Perch's
   next_step remains authoritative for deciding whether LMI is required and
   when contracts can be generated; it does not replace Dalton's working UX.

   Descriptor contract (see services/perch/workflow.py):
     step.key, .eyebrow, .title, .subtitle
     step.fields[]  {name, label, type, required, value, options[], validation{pattern,message},
                     placeholder, input_mode, max_length, mono}
     step.panels[]  {type: 'capacity_summary'|'notice', ...}
     step.primary_action / .secondary_action  {label, operation, enabled, disabled_reason}
*/

let currentEnrollmentDetail = null;
let currentDraft = null;     // {enrollment_id, enrollment_code} - durable Dalton key
let currentWorkflow = null;  // descriptor used only for service-area/capacity
/* The SERVICE AREA descriptor specifically, remembered so Back can re-render
   the Availability form.

   ROOT CAUSE THIS FIXES: currentWorkflow is two things at once - the latest
   workflow state from the backend, AND whatever Back re-renders. A successful
   capacity call assigns currentWorkflow = body.workflow, which is the
   capacity_result step. backFromCustomer() then rendered THAT, so pressing
   Back from Customer & Bill redrew the old "Capacity confirmed" screen instead
   of the Availability form. It stopped happening after a few navigations
   because a later fetch replaced the descriptor. Keeping a separate reference
   makes Back deterministic without touching Perch's next_step. */
let availabilityWorkflow = null;
let utilityRules = {};
let billRuntimeFile = null, billUploadPromise = null, billUploadGeneration = 0;
let lmiRuntimeFile = null, lmiUploadPromise = null, lmiUploadGeneration = 0;
let perchContracts = [];
let billTouched = false;   // RC-3: has the rep interacted with the bill step yet?
/* The last bill parse, kept so the "Pulled from the bill" panel can be redrawn
   against what is CURRENTLY on the form.

   The panel used to be rendered once, from the OCR result alone, and never
   again. So a bill the parser could not read left four rows reading
   "Not found - enter manually" permanently - including after the rep had
   typed every one of those values in the form directly below. The screen
   contradicted itself: populated fields, and a panel saying they were missing. */
let lastBillParse = null;
function freshPerchContext(){
  return {email:'', capacityZip:'', utilitySlug:'', utilityDisplay:'', projectDetails:null,
          customerHasPassword:false,
          nextStepKey:null, enrollmentSubmitted:false, proofSubmitted:false, contractsGenerated:false,
          acceptanceEnabled:false, acceptanceSubmitted:false, acceptanceInFlight:false,
          agreementBackStep:2};
}
let perchContext = freshPerchContext();

async function loadUtilityRules(){
  if(Object.keys(utilityRules).length) return;
  const data = await apiFetch('/api/perch/utilities');
  (data.utilities || []).forEach(u => { utilityRules[u.slug] = u; });
}

async function startWizardFresh(){
  resetWizardState();
  const rb=document.getElementById('resume-banner'); if(rb) rb.style.display='none';
  state.project = {id:'',name:'',utility:''};
  skipProjectStep = false;
  currentDraft = null;
  currentWorkflow = null;
  showView('wizard');
  Router.push('wizard');          // /enrollments until an id exists
  goStep(1);
  renderWorkflowLoading('Starting a new enrollment...');

  // NOTHING IS PERSISTED HERE. Opening the wizard used to POST /drafts
  // immediately, so a rep who opened the screen and pressed Back left a blank
  // enrollment on every dashboard. The Dalton Enrollment ID is still issued
  // before any Perch call - just at the FIRST REAL SUBMISSION (email + ZIP +
  // utility) in submitCapacity(), not at screen-open.
  currentDraft = null;
  try {
    await loadUtilityRules();
    const body = await apiFetch('/api/perch/workflow/new');
    currentWorkflow = {step: body.step};
    renderWorkflowStep(currentWorkflow);
  } catch(err){
    renderWorkflowError('Could not start a new enrollment: ' + err.message);
  }
}

async function loadWorkflow(){
  if(!currentDraft) return;
  try {
    currentWorkflow = await apiFetch('/api/perch/enrollments/' + currentDraft.enrollment_id + '/workflow');
  } catch(err){
    renderWorkflowError(err.message);
    return;
  }
  renderWorkflowStep(currentWorkflow);
}

/* ---------- generic renderer ---------- */

function esc(v){
  return String(v == null ? '' : v)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function renderWorkflowLoading(msg){
  document.getElementById('workflow-root').innerHTML =
    '<div class="card"><div class="ocr-panel"><div class="ocr-analyzing">' +
    '<div class="spinner"></div>' + esc(msg) + '</div></div></div>';
}

function renderWorkflowError(msg){
  document.getElementById('workflow-root').innerHTML =
    '<div class="card"><p class="card-eyebrow">New enrollment</p>' +
    '<h2>Something went wrong</h2>' +
    '<p class="helper" style="color:var(--danger);">' + esc(msg) + '</p>' +
    '<div class="wizard-actions"><span></span>' +
    '<button class="btn btn-secondary" onclick="exitWizard()">Back to dashboard</button></div></div>';
}

function renderField(f){
  const req = f.required ? '' : ' <span style="font-weight:400;color:var(--ink-faint);">(optional)</span>';
  let control;
  if(f.type === 'select'){
    const opts = ['<option value="">' + esc(f.placeholder || 'Select...') + '</option>']
      .concat((f.options||[]).map(o =>
        '<option value="' + esc(o.value) + '"' + (o.value === f.value ? ' selected' : '') + '>' +
        esc(o.label) + '</option>'));
    control = '<select id="wf-' + esc(f.name) + '" data-field="' + esc(f.name) + '"' + (f.readonly ? ' disabled' : '') + '>' + opts.join('') + '</select>';
  } else {
    control = '<input type="text" id="wf-' + esc(f.name) + '" data-field="' + esc(f.name) + '"' +
      (f.mono ? ' class="mono-field"' : '') +
      (f.input_mode ? ' inputmode="' + esc(f.input_mode) + '"' : '') +
      (f.max_length ? ' maxlength="' + f.max_length + '"' : '') +
      (f.placeholder ? ' placeholder="' + esc(f.placeholder) + '"' : '') +
      ' value="' + esc(f.value || '') + '">';
  }
  const help = f.help ? '<p class="helper">' + esc(f.help) + '</p>' : '';
  // C5: an optional presentation hint from the step descriptor, alongside the
  // placeholder, help and mono hints that already live there. A five-digit ZIP
  // and a utility picker do not each need the full width of the card, and
  // stacking every field regardless of what it holds was what made this step
  // taller and looser than the rest of the wizard.
  const half = f.width === 'half' ? ' field-half' : '';
  return '<div class="field' + half + '"><label for="wf-' + esc(f.name) + '">' + esc(f.label) + req +
    '</label>' + control + help +
    '<p class="field-error wf-field-error" id="wf-err-' + esc(f.name) +
    '" style="display:none;"></p></div>';
}

function renderPanel(p){
  if(p.type === 'capacity_summary'){
    const segs = (p.segments||[]).map(s =>
      '<div class="wf-segment' + (s.available ? ' available' : '') + '">' +
        '<span class="wf-seg-dot"></span>' + esc(s.label) +
        '<span class="wf-seg-state">' + (s.available ? 'Available' : 'Not available') + '</span>' +
      '</div>').join('');
    const mets = (p.metrics||[]).map(m =>
      '<div class="prod-metric"><div class="pm-val">' + esc(m.value) + '</div>' +
      '<div class="pm-label">' + esc(m.label) + '</div></div>').join('');
    const notices = (p.notices||[]).map(n => renderNotice(n)).join('');
    return '<div class="product-card">' +
      '<div class="prod-name" style="margin-bottom:12px;">' + esc(p.title) + '</div>' +
      '<div class="wf-segments">' + segs + '</div>' +
      '<div class="prod-grid" style="margin-top:14px;">' + mets + '</div>' +
      notices + '</div>';
  }
  if(p.type === 'notice'){
    return '<div class="product-card">' +
      (p.title ? '<div class="prod-name" style="margin-bottom:8px;">' + esc(p.title) + '</div>' : '') +
      '<div class="wf-notice ' + esc(p.tone || 'info') + '">' + esc(p.text) + '</div></div>';
  }
  return '';
}

function renderNotice(n){
  return '<div class="wf-notice ' + esc(n.tone || 'info') + '" style="margin-top:12px;">' + esc(n.text) + '</div>';
}

function renderWorkflowStep(wf){
  const step = wf.step;
  // Remember the Availability descriptor whenever one is drawn, so Back always
  // has the right thing to redraw.
  if(step && step.key === 'service_area') availabilityWorkflow = wf;
  const root = document.getElementById('workflow-root');

  const draftBanner = currentDraft
    ? '<div class="draft-banner"><span>Enrollment <strong>' + esc(currentDraft.enrollment_code) + '</strong></span>' +
      (wf.step.perch_next_step && wf.step.perch_next_step.recognized === false
        ? '<span class="badge badge-gold">Unrecognized next step</span>' : '') +
      '</div>'
    : '';

  // Wrapped so the fields share one grid (C5) instead of each being its own
  // full-width block. A field keeps the full row unless the descriptor says
  // otherwise, so nothing changes for a step that gives no hint.
  const fieldList = (step.fields||[]).map(renderField).join('');
  const fields = fieldList ? '<div class="wf-fields">' + fieldList + '</div>' : '';
  // LIVE BUG FIX. This used to inject a SECOND element with id="program-options"
  // into the step-1 workflow root. step-project precedes step-bill in the
  // document, so document.getElementById('program-options') returned THAT one -
  // sitting inside a hidden .wizard-step. Every card rendered into an invisible
  // container while the real one on Customer & Bill stayed empty, which is
  // exactly the "heading with nothing under it" seen in the browser.
  //
  // The slot belonged to the pre-merge layout, when capacity_result was its own
  // screen. Customer & Bill now owns the only #program-options.
  const programSlot = '';
  const panels = (step.panels||[]).map(renderPanel).join('');

  const pa = step.primary_action;
  const sa = step.secondary_action;
  const primaryDisabled = pa && pa.enabled === false;
  const primaryBtn = pa
    ? '<button class="btn btn-primary" id="wf-primary"' + (primaryDisabled ? ' disabled' : '') +
      ' onclick="runWorkflowOperation(\'' + esc(pa.operation) + '\')">' + esc(pa.label) + '</button>'
    : '<span></span>';
  const secondaryBtn = sa
    ? '<button class="btn btn-secondary" onclick="runWorkflowOperation(\'' + esc(sa.operation) + '\')">' +
      esc(sa.label) + '</button>'
    : '<span></span>';
  const disabledNote = (primaryDisabled && pa.disabled_reason)
    ? '<p class="helper" style="margin-top:10px;">' + esc(pa.disabled_reason) + '</p>' : '';

  root.innerHTML =
    '<div class="card">' +
      '<p class="card-eyebrow">' + esc(step.eyebrow || 'New enrollment') + '</p>' +
      '<h2>' + esc(step.title) + '</h2>' +
      (step.subtitle ? '<p class="lead">' + esc(step.subtitle) + '</p>' : '') +
      draftBanner +
      fields +
      programSlot +
      panels +
      '<p class="alert alert-error" id="wf-form-error" style="display:none;"></p>' +

      disabledNote +
      '<div class="wizard-actions">' + secondaryBtn + primaryBtn + '</div>' +
    '</div>';

  applyProgressHint(wf.progress);
  wireZipDrivenUtilities(step);
  wireCountySelection(step);
}

/* COUNTY -> which programs this customer can actually be offered (E2.1).

   Perch v2 can restrict solar capacity to named counties. When the restricted
   capacity offers something the unrestricted capacity does not, the backend
   puts a County field on this step and withholds the program list until it is
   answered - the set of valid programs is not decided yet, and showing a list
   that would silently grow is the stale-state problem the selection rules
   forbid.

   So this does one thing: persist the answer and redraw from the server. The
   filtering, the validation and the controlled list all live server-side;
   nothing here decides what is available, and the rep cannot type a county
   because the control is a <select> built from Perch's own list. */
function wireCountySelection(step){
  if(!step || step.key !== 'capacity_result') return;
  const el = document.getElementById('wf-county');
  if(!el || el.disabled || !currentDraft) return;

  function err(msg){
    const box = document.getElementById('wf-err-county');
    if(!box) return;
    box.textContent = msg || '';
    box.style.display = msg ? '' : 'none';
  }

  el.addEventListener('change', async function(){
    const chosen = el.value;
    err('');
    el.disabled = true;
    try{
      await apiFetch('/api/perch/enrollments/' + currentDraft.enrollment_id + '/county',
                     {method:'POST', body: JSON.stringify({county: chosen})});
    }catch(e){
      el.disabled = false;
      err(userMessage(e, 'Could not save the county. Try again.'));
      return;
    }
    // Redraw from the server rather than patching this screen: the capacity
    // panel, its savings figures and the Continue button all depend on the
    // county, and the backend is what resolved them.
    await loadWorkflow();
    // The program cards live on Customer & Bill and read the same endpoint,
    // so they are refreshed too - otherwise a rep who went back, changed the
    // county and returned would see the previous county's options.
    loadProgramOptions();
  });
}

/* ZIP -> STATE -> the utilities Perch offers in that state.

   WHY THIS EXISTS
   The Availability step has no state field, and the market-rules utility
   endpoint is keyed by state. So the ZIP the rep types is what resolves the
   state, and the state is what fetches the utility list.

   The dropdown arrives from the backend descriptor already populated from the
   local reference table, which is what New York has always shown. That stays
   as the initial and fallback content, so this never leaves the rep with an
   empty screen - it REPLACES the list once a ZIP makes a better answer
   possible.

   ZIP DETERMINES STATE ONLY. It never picks a utility. The choices always come
   from Perch for the resolved state; ZIP-to-utility mapping is not implemented
   here or anywhere. */
function wireZipDrivenUtilities(step){
  if(!step || step.key !== 'service_area') return;
  const zipEl = document.getElementById('wf-zip_code');
  const utilEl = document.getElementById('wf-utility_name');
  if(!zipEl || !utilEl || utilEl.disabled) return;   // locked by a project card

  // The descriptor's own options are kept as a floor: if every lookup fails,
  // the screen still shows what it opened with rather than nothing at all.
  const initialHtml = utilEl.innerHTML;
  const initialValue = utilEl.value;
  let lastZip = null;

  function err(){ return document.getElementById('wf-err-utility_name'); }
  function primary(){ return document.getElementById('wf-primary'); }

  function clearError(){
    const e = err(); if(e){ e.style.display = 'none'; e.textContent = ''; }
    const p = primary(); if(p) p.disabled = false;
  }

  function showError(text, block){
    const e = err();
    if(e){ e.textContent = text; e.style.display = 'block';
           e.style.color = 'var(--danger)'; }
    const p = primary(); if(p && block) p.disabled = true;
  }

  /* Neutral guidance, in the same place as the error but NOT in red.

     A ZIP that changes the market is normal rep behaviour, not a failure. The
     old message - "The utility you selected (National Grid NY) does not serve
     VA." - was rendered in danger red, and reps read red as "the site is
     broken" before reading the words. Nothing has gone wrong here, so nothing
     should look like it has. */
  function showNotice(text){
    const e = err();
    if(e){ e.textContent = text; e.style.display = 'block';
           e.style.color = 'var(--ink-muted)'; }
    const p = primary(); if(p) p.disabled = false;
  }

  /* The visible label currently shown for a slug, read from the select BEFORE
     it is repainted. Avoids turning "national-grid-ny" into "national grid ny". */
  function labelFor(slug){
    const opts = utilEl.innerHTML || '';
    const m = new RegExp('<option value="' + slug.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
                         + '">([^<]*)</option>').exec(opts);
    return m ? m[1] : slug;
  }

  /* Render options and decide what stays selected. `keep` is the slug the rep
     had chosen, which survives whenever it is still offered.

     The <option> VALUE is the Perch slug and its TEXT is the display label.
     That is the whole defect this function was rewritten for: a dropdown can
     show a correct, readable utility name on screen while submitting
     something else, and the only thing that reaches Perch is the value. */
  function paint(options, autoSelect, keep){
    utilEl.innerHTML = '<option value="">Select a utility</option>' +
      options.map(function(o){
        return '<option value="' + esc(o.value) + '">' + esc(o.label) + '</option>';
      }).join('');
    if(keep && options.some(function(o){ return o.value === keep; })){
      utilEl.value = keep;          // the rep's own choice always wins
    } else if(autoSelect){
      // EXACTLY ONE utility in this state. With two or more the API sends
      // auto_select: null and the rep chooses - nothing is guessed.
      utilEl.value = autoSelect;
    } else {
      utilEl.value = '';
    }
  }

  /* Utility-first: every utility across the enabled markets, so the rep can
     pick one before typing a ZIP. */
  async function loadAllMarkets(){
    let body;
    try{
      body = await apiFetch('/api/markets/utility-options');
    }catch(e){
      return;                      // leave the descriptor's options in place
    }
    if((zipEl.value || '').trim().length === 5) return;  // a ZIP already won
    const options = body.options || [];
    if(!options.length) return;    // keep the opening list rather than empty it
    paint(options, null, utilEl.value);
    clearError();
  }

  /* ZIP-first: narrow to the ZIP's state. */
  async function refresh(){
    const zip = (zipEl.value || '').trim();
    if(zip === lastZip) return;
    lastZip = zip;
    const keep = utilEl.value;
    clearError();
    if(zip.length !== 5 || !/^\d{5}$/.test(zip)){
      // Back to the full cross-market list, so clearing a ZIP does not strand
      // the rep with one state's utilities.
      await loadAllMarkets();
      return;
    }
    let body;
    try{
      body = await apiFetch('/api/markets/utility-options?zip=' + encodeURIComponent(zip));
    }catch(e){
      utilEl.innerHTML = initialHtml;
      utilEl.value = initialValue;
      showError(e.message, false);
      return;
    }
    if(zip !== (zipEl.value || '').trim()) return;   // the rep kept typing
    const options = body.options || [];
    if(!options.length){
      // NO SUPPORTED UTILITIES for this ZIP. Availability must not continue.
      utilEl.innerHTML = '<option value="">No utilities available</option>';
      utilEl.value = '';
      showError(body.message
        || 'No supported utilities were found for that ZIP code.', true);
      return;
    }
    const stillOffered = options.some(function(o){ return o.value === keep; });
    const keptLabel = keep ? labelFor(keep) : '';
    // `keep` is passed through as-is: paint() is the ONE place that decides
    // whether a previously chosen slug survives, and it drops any slug the new
    // option list does not contain. Pre-filtering it here as
    // `stillOffered ? keep : null` was a second copy of that decision - it
    // changed no behaviour, so no test could tell the two apart, and a
    // mutation proving exactly that is what flushed it out. stillOffered is
    // still needed below, to decide whether the rep is TOLD why their choice
    // went away.
    paint(options, body.auto_select, keep);

    if(keep && !stillOffered){
      // THE ZIP CHANGED THE MARKET. The rep picked a utility, then typed a ZIP
      // in another state. This is ordinary - a corrected ZIP, a customer in a
      // different territory - so it is handled, not complained about.
      //
      // The dropdown is NEVER disabled here, and every utility for the new
      // state stays in it: a preselection is a default, not a decision. That
      // matters most in New York, where NYSEG / National Grid / RG&E
      // territories cannot be resolved from a ZIP at all (see the market gate
      // in routes/market_routes.py - ZIP determines STATE only).
      if(body.auto_select){
        // Exactly one utility serves that state, so there is nothing to ask:
        // switch to it and say so quietly.
        showNotice('Switched to ' + esc(labelFor(body.auto_select))
          + (body.state ? ' for ' + esc(body.state) : '')
          + '. You can change it below if that is not right.');
      } else {
        // More than one candidate: nothing is guessed, and the rep chooses.
        showNotice(esc(keptLabel || 'That utility')
          + ' does not serve ' + esc(body.state || 'that ZIP code')
          + '. Choose one of the ' + esc(body.state || 'local') + ' utilities below.');
      }
    }
  }

  zipEl.addEventListener('input', refresh);
  zipEl.addEventListener('change', refresh);
  // Clear a stale mismatch message as soon as the rep picks a valid utility.
  utilEl.addEventListener('change', function(){
    if(utilEl.value) clearError();
  });

  if((zipEl.value || '').trim().length === 5){
    // A resumed enrollment arrives with its ZIP already filled in.
    refresh();
  } else {
    loadAllMarkets();
  }
}

// The step labels above the card come from the backend's progress hint, which is
// presentational only - the authoritative sequence is Perch's next_step URLs.
function applyProgressHint(progress){
  if(!progress) return;
  // The backend progress hint is presentational only. The stepper is now
  // rendered from activeSteps() - the authoritative branch - so this hint is
  // deliberately not drawn; drawing it would let a backend hint contradict the
  // branch the rep is actually on.
  return;
}

/* ---------- validation driven by the descriptor, not hardcoded ---------- */

function collectAndValidate(step){
  const values = {};
  let firstError = null;
  (step.fields||[]).forEach(f => {
    const el = document.getElementById('wf-' + f.name);
    const errEl = document.getElementById('wf-err-' + f.name);
    if(errEl){ errEl.style.display = 'none'; }
    const val = el ? el.value.trim() : '';
    values[f.name] = val;

    let msg = null;
    if(f.required && !val){
      // An EMPTY required field is missing, not malformed. This previously
      // reused f.validation.message, so a blank ZIP reported "ZIP code must be
      // exactly 5 digits" - which reads as a rejected valid ZIP.
      msg = (f.label || 'This field') + ' is required.';
    } else if(val && f.validation && f.validation.pattern){
      if(!(new RegExp(f.validation.pattern)).test(val)){
        msg = f.validation.message || ('That ' + f.label + ' does not look right.');
      }
    }
    if(msg){
      if(errEl){ errEl.textContent = msg; errEl.style.display=''; }
      if(!firstError) firstError = msg;
    }
  });
  return {values: values, error: firstError};
}

/* ---------- operations ---------- */

async function runWorkflowOperation(op){
  if(op === 'exit'){ exitWizard(); return; }
  if(op === 'restart_service_area'){
    renderWorkflowLoading('Loading...');
    try {
      const body = await apiFetch('/api/perch/enrollments/' + currentDraft.enrollment_id + '/restart-service-area', {method:'POST', body:JSON.stringify({})});
      // The backend deliberately invalidated the old email-scoped token. Keep
      // the existing Bill/contact state in case the rep is only correcting the
      // email or capacity ZIP; if a later utility choice changes, Bill-screen
      // validation will use the newly authoritative utility rules.
      perchContext.email=''; perchContext.capacityZip=''; perchContext.utilitySlug='';
      perchContext.utilityDisplay=''; perchContext.projectDetails=null; perchContext.nextStepKey=null;
      state.customer.email='';
      currentWorkflow = body.workflow;
      renderWorkflowStep(currentWorkflow);
    } catch(err){ renderWorkflowError(err.message); }
    return;
  }
  if(op === 'check_capacity'){ await submitCapacity(); return; }
  if(op === 'advance'){ advanceFromCapacity(); return; }
  console.warn('Unknown workflow operation:', op);
}

async function submitCapacity(){
  const step = currentWorkflow.step;
  // Any previous field-level rejection is stale the moment this is retried,
  // and a red border on a value the rep has already changed is worse than no
  // border at all.
  setFieldError('wf-email', 'wf-err-email', '');
  const {values, error} = collectAndValidate(step);
  const formErr = document.getElementById('wf-form-error');
  formErr.style.display = 'none';
  if(error) return;

  const btn = document.getElementById('wf-primary');
  const restoreBtn = () => {
    // Always restore, on EVERY exit path. Previously this happened only in the
    // catch block, so a non-error early return left the control latched at
    // "Checking availability…" and disabled - visible after navigating Back.
    if(!btn) return;
    btn.disabled = false;
    btn.textContent = (step.primary_action && step.primary_action.label) || 'Check availability';
  };
  btn.disabled = true;
  btn.textContent = 'Checking availability…';

  let body;
  try {
    // FIRST REAL ACTION. The backend creates the enrollment here - not when the
    // wizard opened - so abandoning an untouched screen persists nothing.
    // Passing an existing enrollment_id makes a retry REUSE that row instead of
    // creating a duplicate.
    const payload = {zip_code: values.zip_code, utility_name: values.utility_name,
                     // email is required by Perch on POST /token, which the
                     // backend issues before the capacity call.
                     email: values.email};
    if(currentDraft && currentDraft.enrollment_id){
      payload.enrollment_id = currentDraft.enrollment_id;
    }
    body = await apiFetch('/api/perch/enrollments/capacity',
                          {method: 'POST', body: JSON.stringify(payload)});
    // Adopt the enrollment the backend created (or confirmed) on this call.
    if(body.enrollment_id && (!currentDraft || !currentDraft.enrollment_id)){
      currentDraft = {enrollment_id: body.enrollment_id,
                      enrollment_code: body.enrollment_code || null};
    }
  } catch(err){
    restoreBtn();
    // PERCH ALREADY KNOWS THIS EMAIL.
    //
    // Perch answers POST /token with a 422 whose message names its own
    // endpoints: "An enrollment request already exists for this email. Use
    // the /status endpoint to check the current status of the enrollment."
    // The backend types that as PerchEnrollmentInProgressError and returns
    // the type alongside the message, so this reads the TYPE - never the
    // words, which Perch is free to rewrite at any time.
    //
    // It belongs on the Email field rather than at the foot of the form: the
    // rep's next action is to look at that one box. The raw message stays on
    // the error object and is logged here for debugging.
    // This branch chooses WHERE the message goes and nothing else. It must not
    // return: the enrollment_discarded reset below is business logic that runs
    // on every failed capacity call, and an early return here skipped it - a
    // server-discarded provisional row left the wizard holding a dead
    // enrollment id, selected program and capacity context. Presentation only
    // means presentation only.
    if(err && err.body && err.body.perch_error === 'PerchEnrollmentInProgressError'){
      if(window.console && console.debug){
        console.debug('[perch] token rejected for this email:', err.message);
      }
      setFieldError('wf-email', 'wf-err-email',
                    "An enrollment already exists for this email address.");
      const emailField = document.getElementById('wf-email');
      if(emailField) emailField.focus();
    } else {
      formErr.textContent = userMessage(err);
      formErr.style.display = '';
    }
    // Reset ONLY when the server confirms it actually discarded the provisional
    // row. enrollment_discarded is true only when this very request created the
    // row AND enrollment_cleanup approved the discard.
    //
    // It is false for a RETRY against an enrollment_id we already held, and
    // false when cleanup refused (real work exists on the row). In both cases
    // the id we hold still refers to a live enrollment, so clearing it would
    // strand a real row - exactly the bug this change set exists to fix.
    // The rep's typed values stay on screen either way.
    if(err && err.body && err.body.enrollment_discarded === true){
      currentDraft = null;
      perchContext.email = '';
      perchContext.capacityZip = '';
      perchContext.utilitySlug = '';
      perchContext.utilityDisplay = '';
      perchContext.nextStepKey = null;
      selectedProgram = null;
      availablePrograms = [];
      currentEnrollmentDetail = null;
    }
    return;
  }
  perchContext.email = values.email;
  perchContext.capacityZip = body.result.zip_code;
  perchContext.utilitySlug = body.result.utility_slug;
  perchContext.utilityDisplay = body.result.utility_display_name || body.result.utility_slug;
  perchContext.projectDetails = body.result.project_details || {};
  perchContext.nextStepKey = (((body.workflow||{}).step||{}).perch_next_step||{}).resolved_step || null;
  state.customer.email = values.email;
  state.project.utility = perchContext.utilityDisplay;
  currentWorkflow = body.workflow;

  // BUG 1 FIX. This previously rendered the backend's capacity_result descriptor
  // into the step-1 workflow root - which IS the standalone "Capacity confirmed"
  // page. The live path showed it while the resume path
  // (WORKFLOW_STEP_TO_WIZARD) went to Customer & Bill: two destinations for one
  // state. The capacity LOOKUP, its validation and the returned data are
  // untouched; we consume the result instead of rendering it as a screen.
  const resolvedKey = ((body.workflow || {}).step || {}).key || null;
  if(resolvedKey === 'no_capacity'){
    // A genuine dead end - still rendered on step 1.
    restoreBtn();
    renderWorkflowStep(currentWorkflow);
    return;
  }
  restoreBtn();                 // step 1 is reusable if the rep navigates Back
  await loadProgramOptions();   // feeds the cards shown at the top of step 2
  goStep(2);                    // straight into Customer & Bill
}

function advanceFromCapacity(){
  if(!currentDraft || !perchContext.email || perchContext.nextStepKey !== 'enroll') return;
  // buildStepLabels() belonged to the removed arc; goStep() renders the stepper.
  configureBillUtilityRules();
  goStep(2);
}

// Dashboard project cards use the same Perch session/capacity entry point, but
// preserve the selected Dalton project instead of creating a second wizard.
// disappear when the dashboard becomes Perch-driven; this keeps them working.
async function startWizardForProject(projId){
  const p = projects.find(x=>String(x.id)===String(projId));
  if(!p){ alert('Could not find that project - try refreshing the page.'); return; }
  resetWizardState();
  state.project = {id:p.id, name:p.name, utility:p.utility, savingsPct:p.savingsPct};
  skipProjectStep = false;
  currentDraft = null; currentWorkflow = null;
  showView('wizard'); goStep(1);
  renderWorkflowLoading('Starting a new enrollment...');
  try {
    await loadUtilityRules();
    currentDraft = await apiFetch('/api/perch/drafts', {method:'POST', body:JSON.stringify({project_id:p.id})});
    await loadWorkflow();
  } catch(err){ renderWorkflowError('Could not start a new enrollment: ' + err.message); }
}
/* ==================== PHASE 4A: OPEN / RESUME AN EXISTING ENROLLMENT ====================
   Operates on the EXISTING enrollment_id. Never calls create_draft, never starts
   a new Perch session, and issues no Perch API call until the rep does something
   that actually needs Perch. All state comes from what Dalton already persisted. */

// Backend workflow key -> wizard screen. Step 3 (Contact & Login) was merged
// into step 2, so the keys that used to resume there now resume on the combined
// Customer & Bill screen. capacity_result also resolves to step 2: the capacity
// LOOKUP is unchanged, but its result is now context at the top of that screen
// rather than a page of its own.
const WORKFLOW_STEP_TO_WIZARD = {
  service_area: 1, no_capacity: 1,
  capacity_result: 2,           // result shown as context on Customer & Bill
  enroll: 2,                    // customer + contact details (merged screen)
  proof_docs: 4, self_attestation: 4, self_attestation_accept: 4,
  contracts: 5, contracts_review: 5, contracts_accept: 5,
  status: 5, unknown_next_step: 5,
  enroll_outcome_uncertain: 2,
  contracts_accepted: 5, contracts_accept_uncertain: 5,
};

/* Returns false when the enrollment could not be opened, so a deep link to
   one that is gone - or that belongs to another rep - falls back to the
   dashboard instead of leaving a blank wizard on screen. */
async function openEnrollment(enrollmentId){
  let e;
  try {
    e = await apiFetch('/api/enrollments/' + enrollmentId);
  } catch(err){
    alert('That enrollment could not be opened. ' +
          userMessage(err, 'Try again in a moment.'));
    return false;
  }

  resetWizardState();
  // Bind to the EXISTING enrollment. No draft is created.
  currentDraft = {enrollment_id: e.id, enrollment_code: e.enrollment_code};
  currentWorkflow = null;
  skipProjectStep = true;

  // Rehydrate from persisted Dalton data rather than asking the rep again.
  if(e.customer){
    state.customer.first = e.customer.first_name || '';
    state.customer.last  = e.customer.last_name  || '';
    state.customer.email = e.customer.email      || '';
    state.customer.phone = e.customer.phone      || '';
  }
  if(e.service_address){
    state.address.street = e.service_address.street || '';
    state.address.unit   = e.service_address.unit   || '';
    state.address.city   = e.service_address.city   || '';
    // THE STATE WAS NEVER RESTORED, and resetWizardState() defaults it to 'NY'.
    // Every later save reads state.address.state, so reopening a Virginia
    // enrollment and pressing Continue rewrote it as a New York one. Data
    // corruption, silent, and only on the market we just added.
    state.address.state  = e.service_address.state  || 'NY';
    state.address.zip    = e.service_address.zip    || '';
  }
  if(e.utility_account){
    state.customer.acct  = e.utility_account.account_number || '';
    state.customer.podId = e.utility_account.secondary_account_identifier || '';
    // Required by some markets and persisted, but not previously restored.
    state.customer.meterNumber = e.utility_account.meter_number || '';
  }
  if(e.billing_address){
    state.billing.sameAsService = e.billing_address.same_as_service !== false;
    state.billing.street = e.billing_address.street || '';
    // unit and state were persisted but never restored, so a separate billing
    // address came back missing its apartment and defaulted to NY.
    state.billing.unit   = e.billing_address.unit   || '';
    state.billing.city   = e.billing_address.city   || '';
    state.billing.state  = e.billing_address.state  || '';
    state.billing.zip    = e.billing_address.zip    || '';
  }
  // Never the password itself - only whether one exists.
  perchContext.customerHasPassword = e.customer_has_password === true;
  hydrateDocSets(e.document_sets);
  // The enrollment now has its own address, so Back, Forward, refresh and a
  // pasted link all land on it.
  Router.push('wizard', e.id);
  state.project = {id:(e.project ? e.project.id : ''), name:(e.project ? e.project.name : ''),
                   // THE PUBLISHED DISPLAY NAME, not utility_accounts.utility_name.
                   // That column holds the raw Perch SLUG, and this field is the
                   // FIRST source rv-utility consults - which is why Review &
                   // finish rendered "dominion-energy-va" on a reopened
                   // enrollment even though the resolved name was in the payload
                   // all along. The live flow already stores the display name
                   // here (see the Availability handler); this makes reopen agree
                   // with it. The slug stays the last-resort fallback.
                   utility: e.utility_name
                            || (e.utility_account ? e.utility_account.utility_name : '')};

  // THE HYDRATION FIX. Each of these reads every place the value is
  // persisted, not just the one that happens to be written last.
  //
  //   email  - customers.email is written by the merged Continue on step 2.
  //            perch_token_email is written at AVAILABILITY. Reading only the
  //            first is why an enrollment abandoned in between reopened with
  //            no email and dead-ended with "Re-run the availability check".
  //   slug   - utility_accounts only exists after the bill step;
  //            enrollments.utility_name holds the slug chosen at Availability.
  //   zip    - the ZIP capacity was CHECKED against, which is not necessarily
  //            the service address ZIP.
  perchContext.email = (e.customer && e.customer.email) || e.perch_token_email || '';
  perchContext.utilitySlug = e.utility_slug
    || (e.utility_account ? e.utility_account.utility_name : '') || '';
  // DISPLAY NAME, hydrated from the BACKEND RESOLVER.
  //
  // ROOT CAUSE THIS FIXES. utilityDisplay is set at Availability and was
  // never restored on reopen, so every 'utilityDisplay || utilitySlug' site
  // in this file fell straight through to the RAW SLUG - which is how
  // "dominion-energy-va" reached the capacity card and Review & finish on a
  // reopened enrollment. e.utility_name is already the published name,
  // resolved server-side by services/perch/utilities.display_name_for_slug.
  //
  // No second formatter is introduced here. The slug remains the last-resort
  // fallback for a legacy row the resolver cannot name, which is honest: a
  // readable slug beats an empty field.
  perchContext.utilityDisplay = e.utility_name || '';
  perchContext.capacityZip = e.service_zip
    || (e.service_address ? e.service_address.zip : '') || '';
  perchContext.nextStepKey = e.workflow_step_key || null;

  currentEnrollmentDetail = e;   // persisted workflow_last_response for resume

  // COMMIT BOUNDARY from the BACKEND. perchContext.enrollmentSubmitted is
  // in-memory and is lost on reload, which is how a reopened enrollment still
  // offered a program switch. e.perch_committed is derived from persisted
  // workflow state and survives reload, resume and reopen.
  if(e.perch_committed === true) perchContext.enrollmentSubmitted = true;

  // RESTORE THE PERSISTED BILL. The document record survives on the server, but
  // nothing rehydrated state.bill, so a resumed enrollment looked as though the
  // upload had vanished and reps re-uploaded a bill Dalton already had.
  // Represent what exists; never fabricate a file object.
  rehydrateDocumentsFromDetail(e);

  // Hydrate the branch from the PERSISTED choice so a resumed LMI enrollment
  // shows 5 steps with Eligibility, and a resumed Residential one shows 4 and
  // skips it. Falls back to null (full sequence) when nothing was chosen, which
  // is the safe default - nothing is skipped on a guess.
  selectedProgram = e.selected_customer_type
    ? {customer_type: e.selected_customer_type,
       // E2: LMI_IRA is income-qualified too, and `=== 'LMI'` is false for it -
       // which would resume an IRA enrollment with 4 steps and no Eligibility.
       lmi_required: isLmiCustomerType(e.selected_customer_type)}
    : null;
  const key = e.workflow_step_key || 'service_area';
  const terminal = e.workflow_is_terminal === true;
  const blocked  = e.workflow_is_blocked === true;

  showView('wizard');
  // The backend workflow may still sit on an LMI step from before the rep
  // switched programs. The PERSISTED selection is authoritative for the branch,
  // so a Residential enrollment never resumes into Eligibility - it returns to
  // Customer & Bill, which is the correct next editable stage for it.
  let target = WORKFLOW_STEP_TO_WIZARD[key] || 1;
  // Kept so the banner can SAY when the landing step is not the one the
  // workflow key names, instead of leaving the rep to wonder.
  const mappedStep = target;
  if(activeSteps().indexOf(target) === -1){
    target = (selectedProgram && !programRequiresLmi()) ? 2 : (activeSteps()[0] || 1);
  }
  // A READ-ONLY ENROLLMENT ALWAYS OPENS ON THE STATUS PAGE.
  //
  // WORKFLOW_STEP_TO_WIZARD describes where the LIVE flow continues, and for
  // two read-only keys that is the wrong destination entirely:
  // enroll_outcome_uncertain opened onto the Customer & Bill FORM and
  // lmi_not_qualified is not in the map at all, so it fell back to
  // Availability. Both are editable-looking screens whose Continue is merely
  // disabled - a settled enrollment presented as unfinished work with a dead
  // button on it.
  //
  // The map is left describing live routing, which is its job; the read-only
  // decision is made here, once, from the authoritative terminal/blocked
  // flags. Step 5 is the status page: its summary renders from the same
  // hydrated state, and no acceptance control is live on it.
  //
  // The re-framing runs BEFORE goStep(), so the live "Final step / Review &
  // finish" wording is never painted for even one frame on a finished
  // enrollment, and fillReview() - which goStep(5) calls - already sees
  // finalStepFramed and leaves the status pill alone.
  if(terminal || blocked){
    target = 5;
    applyFinalStepFraming(key, e.workflow_step_label, terminal);
  }
  goStep(target);
  renderResumeBanner(e, key, terminal, blocked, target, mappedStep);

  // RC-1: straight-through created perchContracts + acceptanceEnabled in memory
  // from the POST /contracts response. Resume previously recreated neither, so
  // the packet rendered empty and Accept could never enable.
  //
  // Repeat POST /contracts is the DOCUMENTED mechanism: "If any URL expires
  // before download, call this endpoint again to receive fresh URLs." The
  // existing one-time review capability already re-calls it on every Review
  // click, and that path is proven live. No presigned URL is persisted here -
  // the response carries only URL-free metadata.
  // BUG 1 FIX: terminal/blocked enrollments must NOT be in this list.
  // Previously contracts_accepted and contracts_accept_uncertain were included,
  // so opening a COMPLETED enrollment POSTed /contracts - which Perch correctly
  // rejects with 422 "Cannot modify this stage because later stages have
  // already started", surfacing as a scary error on a finished enrollment.
  // Only steps that still legitimately need a fresh presigned packet rehydrate.
  const REHYDRATE_CONTRACT_STEPS = ['contracts', 'contracts_review', 'contracts_accept'];

  if(terminal || blocked){
    // Lock FIRST, so no acceptance control is ever briefly live, then render
    // from persisted URL-free metadata. Zero Perch calls: opening a completed
    // enrollment is entirely side-effect-free.
    lockEnrollmentReadOnly(blocked);
    mountAgreements({
      actor: 'rep',
      enrollmentId: e.id,
      contracts: ((e.workflow_last_response || {}).contracts) || [],
      acceptanceEnabled: false,
      readOnly: true,
      submitted: terminal,
      summary: agrSummaryFromEnrollment(e),
      // The real key and the backend's own label, so the card states this
      // enrollment's actual outcome instead of assuming completion.
      statusKey: key,
      statusLabel: e.workflow_step_label || null,
    });
  } else if(REHYDRATE_CONTRACT_STEPS.indexOf(key) !== -1){
    await rehydrateContractPacket(key, terminal, blocked);
  }
}

// Read-only view of a finished (or uncertain) enrollment. Renders only what
// Dalton already persisted - contract NAMES, never URLs, which are deliberately
// never stored. Makes no network request of any kind.
async function rehydrateContractPacket(key, terminal, blocked){
  // RECONCILE FIRST, then decide whether regenerating is even legal.
  //
  // POST /contracts is only repeatable BEFORE acceptance is attempted. Once a
  // customer has submitted acceptance - even one Perch rejected - Perch's stage
  // machine advances and refuses to regenerate:
  //     "Cannot modify this stage because later stages have already started."
  // Dalton's local state does not advance on a rejected acceptance, so the two
  // diverge and the old code called /contracts unconditionally, producing a hard
  // error the rep could not clear by reopening. (Observed live 2026-08-17.)
  //
  // GET /status is documented side-effect free, so asking first costs nothing.
  mountRepAgreements(false, false);
  const cardErr = document.getElementById('agr-card-error');
  if(cardErr) cardErr.style.display = 'none';

  const eid = currentDraft.enrollment_id;
  let status = null;
  try{
    status = await apiFetch('/api/perch/enrollments/' + eid + '/perch-status');
  }catch(err){
    // Status is best-effort. If it fails we fall back to the persisted packet
    // rather than guessing that regeneration is safe.
    status = null;
  }

  const perchStep = status ? (status.next_step_key || null) : null;
  const perchCompleted = status ? status.completed === true : false;

  // Regeneration is valid ONLY while Perch still considers contracts the
  // current stage. Anything at or past acceptance must not touch /contracts.
  const REGENERABLE = ['contracts', 'contracts_review'];
  const statusSaysPastContracts =
    perchCompleted || (perchStep && REGENERABLE.indexOf(perchStep) === -1);
  const canRegenerate = !terminal && !blocked && !statusSaysPastContracts;

  if(canRegenerate){
    try{
      const body = await apiFetch('/api/perch/enrollments/' + eid + '/contracts',
                                  {method:'POST', body:JSON.stringify({})});
      perchContracts = body.contracts || [];
      perchContext.contractsGenerated = true;
      perchContext.contractsNextStepKey = body.next_step_key;
      // Never fabricated - taken from the backend response, exactly as the
      // straight-through path does.
      perchContext.acceptanceEnabled = body.acceptance_enabled === true;
      mountRepAgreements(false, false);
      return;
    }catch(err){
      // Fall through to the persisted packet rather than dead-ending.
      perchContracts = [];
      perchContext.acceptanceEnabled = false;
    }
  }

  // ── Perch has advanced past contracts (or regeneration failed) ──
  // Reuse the agreement names Dalton already persisted at contracts_review.
  // URL-free by design, so documents cannot be reopened - say so honestly
  // instead of implying a retry will help.
  const persisted = (currentEnrollmentDetail
                     && currentEnrollmentDetail.workflow_last_response
                     && currentEnrollmentDetail.workflow_last_response.contracts) || [];
  perchContracts = persisted;
  perchContext.contractsGenerated = persisted.length > 0;

  if(perchCompleted || terminal){
    // Already accepted: read-only, no further action.
    perchContext.acceptanceEnabled = false;
    perchContext.acceptanceSubmitted = true;
    mountRepAgreements(true, true);
    return;
  }

  if(blocked && perchStep !== 'contracts_accept'){
    // Uncertain acceptance that /status has NOT cleared: stay read-only.
    perchContext.acceptanceEnabled = false;
    mountRepAgreements(true, false);
    const el = document.getElementById('agr-card-error');
    if(el){
      el.textContent = 'A previous acceptance could not be confirmed. Check this ' +
                       'enrollment with Perch before trying again.';
      el.style.display = 'block';
    }
    return;
  }

  // Perch says acceptance is still the outstanding step, so ONE attempt is
  // legitimate. Acceptance itself remains server-guarded and non-retrying.
  perchContext.acceptanceEnabled = (perchStep === 'contracts_accept');
  mountRepAgreements(false, false);

  const note = document.getElementById('agr-card-error');
  if(note){
    if(perchStep === 'contracts_accept'){
      note.textContent = 'These agreements were already generated and are awaiting ' +
        'acceptance. The documents cannot be reopened at this stage, but you can ' +
        'complete acceptance below.';
    }else{
      note.textContent = 'The agreements could not be reloaded and we have no ' +
        'saved copy for this enrollment. Check its status with Perch before ' +
        'taking further action.';
    }
    note.style.display = 'block';
  }
}

function renderResumeBanner(e, key, terminal, blocked, landedStep, mappedStep){
  let el = document.getElementById('resume-banner');
  if(!el){
    el = document.createElement('div');
    el.id = 'resume-banner';
    el.className = 'draft-banner';
    const host = document.getElementById('view-wizard');
    if(host) host.insertBefore(el, host.firstChild);
  }
  const badge = terminal ? '<span class="badge badge-green">Complete</span>'
              : (blocked ? '<span class="badge badge-gold">Needs review</span>' : '');
  // "Copy customer link" - shown only when the AUTHORITATIVE workflow key says
  // the customer has something to do. Nothing is sent automatically.
  const linkBtn = customerLinkAvailable(e)
    ? '<button type="button" class="btn btn-secondary btn-sm" id="copy-customer-link" ' +
      'onclick="copyCustomerLink(' + e.id + ', this)">Copy customer link</button>'
    : '';
  // WHERE THE REP HAS ACTUALLY LANDED.
  //
  // The banner named a PERCH STEP ("Proof documents needed") and the screen
  // below it is a UBS SCREEN ("Customer & Bill"). On a partial enrollment
  // those two are often different, and nothing on the page connected them.
  // Phase B's deep links make that worse rather than better: with Back,
  // Forward, refresh and pasted URLs all resolving, a rep can arrive here
  // without having navigated at all, and with no memory of what this
  // enrollment was waiting for.
  //
  // Read-only enrollments are excluded: the status page says what it is, and
  // "Resumed on Agreements" would only add noise to a finished enrollment.
  let where = '';
  if(!terminal && !blocked && STEP_LABEL_TEXT[landedStep]){
    where = '<span class="rb-step">Resumed on ' +
            esc(STEP_LABEL_TEXT[landedStep]) + '</span>';
    // ...and WHY, when it is not the step the workflow key names. The
    // persisted program selection is authoritative for the branch, so a
    // Residential enrollment whose workflow still sits on an LMI step resumes
    // to Customer & Bill. That is correct, and silently surprising.
    if(mappedStep && mappedStep !== landedStep && STEP_LABEL_TEXT[mappedStep]){
      where += '<span class="rb-note">' + esc(STEP_LABEL_TEXT[mappedStep]) +
               ' is not part of the selected program</span>';
    }
  }
  el.innerHTML = '<span>Enrollment <strong>'+esc(e.enrollment_code)+'</strong> — '+
                 esc(e.workflow_step_label || 'In progress')+'</span>'+badge+
                 where+linkBtn;
  el.style.display = 'flex';
}

// RC-4: explicit control list. The previous implementation disabled every
// button in #view-wizard whose TEXT did not match /review|back|dashboard/, which
// (a) depended on wording and (b) poisoned steps 1-4 for the next enrollment
// because nothing re-enabled them. readOnlyLocked lets resetWizardState() undo
// it deterministically.
const READ_ONLY_LOCK_IDS = [
  'btn-project-next', 'btn-bill-next', 'btn-lmi-next',   // btn-contact-next removed with the merge
  // Shared agreement component controls.
  'agr-agree-btn', 'agr-ack-check', 'agr-open-btn',
];
let readOnlyLocked = false;

function unlockEnrollmentControls(){
  READ_ONLY_LOCK_IDS.forEach(function(id){
    const el = document.getElementById(id);
    if(el) el.disabled = false;
  });
  readOnlyLocked = false;
}

function lockEnrollmentReadOnly(blocked){
  readOnlyLocked = true;
  READ_ONLY_LOCK_IDS.forEach(function(id){
    const el = document.getElementById(id);
    if(el) el.disabled = true;
  });
    // READ_ONLY_LOCK_IDS above already disables the shared component's
    // checkbox and agree button. The explanatory copy now lives in
    // renderAgreementCard(), which renders the read-only state directly - the
    // old #contract-* elements this used to poke no longer exist.
}

/* ── Step 5's FRAMING for a read-only enrollment ───────────────────────────

   The step-5 page is written for the live flow: "Final step", "Review &
   finish", "Confirm the enrollment details, then review and accept the
   agreements", and a second "Final step" chip over the agreements block. A
   COMPLETED enrollment reopened straight onto that page - so a finished
   enrollment read as one that still needed finishing, which is exactly what
   the UBS 2.0 item asks us to stop doing.

   The markup strings are the SOURCE OF TRUTH for the live wording, captured
   once on first use rather than duplicated here, so restoring is exact and
   this can never drift from the page. Same reasoning as READ_ONLY_LOCK_IDS:
   whatever a completed enrollment changes, the next enrollment must not
   inherit. resetWizardState() calls the restore.                            */
const FINAL_STEP_IDS = ['agr-eyebrow', 'agr-title', 'agr-subtitle',
                        'agr-section-title', 'agr-section-sub'];
// id -> the markup's own text, captured once. textContent, not innerHTML:
// every one of these nodes holds plain prose, and reading and writing the same
// property means the restore is exactly the inverse of the re-framing.
let finalStepLiveCopy = null;
let finalStepFramed = false;

function captureFinalStepCopy(){
  if(finalStepLiveCopy) return;
  finalStepLiveCopy = {};
  FINAL_STEP_IDS.forEach(function(id){
    const el = document.getElementById(id);
    if(el) finalStepLiveCopy[id] = el.textContent;
  });
}

function setFinalStepText(id, text){
  const el = document.getElementById(id);
  if(el) el.textContent = text;
}

/* Re-frame step 5 as a STATUS page for a terminal or blocked enrollment.
   Driven by the same authoritative key the card uses; an unrecognised key
   falls back to the backend's own step_label and never to "complete". */
function applyFinalStepFraming(statusKey, statusLabel, terminal){
  captureFinalStepCopy();
  finalStepFramed = true;
  const copy = readOnlyStatusCopy(statusKey);
  const label = statusLabel || 'Read-only';

  setFinalStepText('agr-eyebrow', copy ? copy.eyebrow : 'Read-only');
  setFinalStepText('agr-title', copy
    ? copy.repHeading
    : label);
  setFinalStepText('agr-subtitle', copy
    ? copy.lead
    : 'This enrollment is read-only. Its recorded status is shown below.');

  // The agreements block below is a RECORD now, not a task.
  setFinalStepText('agr-section-title', 'Agreements');
  setFinalStepText('agr-section-sub', terminal
    ? 'These agreements were accepted. They are shown for reference and cannot be changed.'
    : 'Shown for reference. This enrollment is read-only and nothing here can be changed.');

  // "Final step" over a finished enrollment is the clearest instance of the
  // defect, so the chip goes entirely.
  const finalLabel = document.getElementById('agr-final-label');
  if(finalLabel) finalLabel.hidden = true;

  // SUCCESS SHOULD LOOK LIKE SUCCESS - but only where it IS success. The pill
  // reads "Complete" for a terminal enrollment and is hidden for a blocked
  // one, which has nothing to celebrate.
  //
  // The text is rewritten in BOTH branches, never only in the one that shows
  // the pill: leaving "Ready to complete" sitting in a hidden element is how a
  // later unrelated unhide would put it back on a finished enrollment.
  const pill = document.getElementById('agr-ready-pill');
  if(pill){
    if(terminal){ pill.textContent = '✓ Complete'; pill.hidden = false; }
    else { pill.textContent = copy ? copy.eyebrow : label; pill.hidden = true; }
  }

  // Back leads into the locked editable steps - a dead end on a finished
  // enrollment. The read-only card renders its own "Back to dashboard".
  const foot = document.getElementById('agr-foot');
  if(foot) foot.style.display = 'none';
}

/* Put step 5 back exactly as the markup had it, for the NEXT enrollment.
   A no-op when nothing was re-framed, so this is safe to call unconditionally
   from resetWizardState(). */
function restoreFinalStepFraming(){
  if(!finalStepFramed) return;
  if(finalStepLiveCopy){
    FINAL_STEP_IDS.forEach(function(id){
      const el = document.getElementById(id);
      if(el && typeof finalStepLiveCopy[id] === 'string'){
        el.textContent = finalStepLiveCopy[id];
      }
    });
  }
  const finalLabel = document.getElementById('agr-final-label');
  if(finalLabel) finalLabel.hidden = false;
  const pill = document.getElementById('agr-ready-pill');
  // Back to the markup default: the live flow's own wording, hidden until
  // fillReview() decides the packet is actually reviewable.
  if(pill){ pill.textContent = '✓ Ready to complete'; pill.hidden = true; }
  const foot = document.getElementById('agr-foot');
  if(foot) foot.style.display = '';
  finalStepFramed = false;
}

/* Persist whatever customer identity the rep has entered, before we leave a
   step or the wizard.

   WHY THIS EXISTS. Customer data had exactly ONE persistence point: the merged
   Continue on the Customer & Bill step, which PATCHes customer, addresses and
   utility account together. A rep who typed a name and backed out lost it, and
   the dashboard showed "(no customer info yet)" for an enrollment that already
   had a customer - because customers.email/first_name were simply never
   written.

   WHAT IT WILL NOT DO:
     * it does not save a single stray character - first AND last name must
       each look like a name, which is also the bar for "enough to identify
       them" on the dashboard
     * it does not touch a COMMITTED enrollment: PATCH answers 409 there by
       design, and an autosave must not surface that as an error
     * it never blocks navigation. A failed autosave is swallowed; the rep's
       explicit Continue still reports its own errors normally.

   Returns true only when something was actually written. */
/* One-shot suppression for the showView() hook.

   Save & Exit and Back-to-dashboard save explicitly and AWAIT it, then
   navigate - and that navigation goes through showView(), whose own hook
   would save a second time. Harmless but wasteful, and it made "saved once"
   impossible to assert. The explicit callers set this; showView consumes it. */
let progressSavedByCaller = false;

function buildCustomerProgress(){
  if(!currentDraft || !currentDraft.enrollment_id) return null;
  if(perchContext.enrollmentSubmitted) return null;

  const first = (state.customer.first || '').trim();
  const last  = (state.customer.last  || '').trim();
  // customers.first_name, last_name and email are all NOT NULL, so all three
  // are needed before a row can exist. The email is nearly always already
  // available: it is the Perch session identity captured at Availability, now
  // restored into perchContext on reopen. Requiring a plausible first AND last
  // name is also the bar for "enough to identify them" on the dashboard - one
  // stray keystroke is not a customer.
  const email = (state.customer.email || perchContext.email || '').trim();
  if(first.length < 2 || last.length < 2 || !email) return null;

  const customer = {first_name: first, last_name: last, email: email};
  const phone = (state.customer.phone || '').trim();
  if(phone) customer.phone = phone;
  // Sent only when one was actually typed and confirmed. Absent means "leave
  // the stored password alone", which is what the PATCH handler does.
  const password = (state.customer.password || '').trim();
  if(password) customer.password = password;

  const payload = {customer: customer};

  // The address goes with it only when it is complete enough to be a real
  // address. A half-typed street is not progress worth persisting.
  const street = (state.address.street || '').trim();
  const city   = (state.address.city   || '').trim();
  const zip    = (state.address.zip    || '').trim();
  const haveAddress = !!(street && city && /^\d{5}$/.test(zip));
  if(haveAddress){
    payload.service_address = {street: street, unit: (state.address.unit || '').trim(),
                               city: city, state: state.address.state || 'NY', zip: zip};

    // Billing rides with the service address, because utility_accounts and
    // billing both hang off it.
    if(state.billing.sameAsService !== false){
      payload.billing_address = {same_as_service: true, street: street,
                                 unit: (state.address.unit || '').trim(), city: city,
                                 state: state.address.state || 'NY', zip: zip};
    } else {
      const bs = (state.billing.street || '').trim();
      const bc = (state.billing.city   || '').trim();
      const bz = (state.billing.zip    || '').trim();
      if(bs && bc && /^\d{5}$/.test(bz)){
        payload.billing_address = {same_as_service: false, street: bs,
                                   unit: (state.billing.unit || '').trim(), city: bc,
                                   state: state.billing.state || state.address.state || 'NY',
                                   zip: bz};
      }
    }
  }

  // UTILITY ACCOUNT. utility_accounts rows hang off the customer AND the
  // service address, so this only goes out once both can exist - otherwise the
  // INSERT has nothing to attach to. The slug is required: the row is
  // meaningless without the utility it belongs to.
  const acct = (state.customer.acct || '').trim();
  const slug = (perchContext.utilitySlug || '').trim();
  if(acct && slug && haveAddress){
    payload.utility_account = {
      utility_name: slug,
      secondary_account_identifier: (state.customer.podId || '').trim() || null,
      meter_number: (state.customer.meterNumber || '').trim() || null,
    };
    // Omitted when it is still the mask we were given: the server then keeps
    // the account number it already holds, which is what the rep meant by not
    // touching the field.
    if(!isMaskedValue(acct)) payload.utility_account.account_number = acct;
  }

  return payload;
}

/* Send whatever buildCustomerProgress() produced. Never throws: a background
   save must not surface an error for something the rep did not do. */
async function saveCustomerProgress(){
  const payload = buildCustomerProgress();
  if(!payload) return false;
  try {
    await apiFetch('/api/enrollments/' + currentDraft.enrollment_id,
                   {method: 'PATCH', body: JSON.stringify(payload)});
    // The credential now exists, so the form must say so without waiting for
    // a reopen to tell it.
    if(payload.customer && payload.customer.password){
      perchContext.customerHasPassword = true;
      renderPasswordState();
    }
    return true;
  } catch(err){
    return false;
  }
}

/* Last-resort save when the PAGE is going away - browser Back out of the app,
   tab close, reload.

   Until Phase B gives UBS real routing there are no in-app history entries, so
   the browser Back button leaves the application entirely and no amount of
   in-app wiring can intercept it. keepalive:true is the one mechanism browsers
   guarantee for in-flight requests during unload, so this is the honest
   best-effort: it is not a substitute for routing, and it is not relied on by
   any normal exit path - all of those save through showView() below. */
function flushCustomerProgressOnUnload(){
  const payload = buildCustomerProgress();
  if(!payload) return;
  try {
    const token = AuthStore.getToken();
    fetch('/api/enrollments/' + currentDraft.enrollment_id, {
      method: 'PATCH',
      headers: Object.assign({'Content-Type': 'application/json'},
                             token ? {Authorization: 'Bearer ' + token} : {}),
      body: JSON.stringify(payload),
      keepalive: true,
    });
  } catch(e){ /* the page is going away; nothing to report to */ }
}

if(typeof window !== 'undefined' && window.addEventListener){
  // pagehide fires on back/forward navigation where unload does not.
  window.addEventListener('pagehide', function(){
    captureCustomerFields();
    flushCustomerProgressOnUnload();
  });
}

/* Read the live form values into `state` so a save can see them.

   hydrateStep() writes state -> DOM. Nothing did the reverse except the
   explicit Continue handler, so a rep who typed a name and pressed Back saved
   nothing: `state` still held the old values. */
function captureCustomerFields(){
  const v = function(id){
    const el = document.getElementById(id);
    return el ? (el.value || '').trim() : '';
  };
  if(!document.getElementById('c-first')) return;

  // ADDITIVE ONLY. An empty input means "nothing was typed here", never
  // "clear what you already know".
  //
  // The step-2 inputs exist in the markup from page load and are empty until
  // hydrateStep(2) fills them. An unconditional copy therefore WIPED state
  // that openEnrollment() had just restored from the backend, whenever the rep
  // left step 2 before its form had been populated - which then re-created the
  // very dead-end this change exists to fix. (Found by
  // test/phase_a_state_harness.js section 3, not by reading the code.)
  //
  // Clearing a field deliberately still works: the explicit Continue handler
  // writes the full payload with its own validation. This function only ever
  // picks up what the rep actually typed.
  const take = function(id, apply){ const got = v(id); if(got) apply(got); };
  take('c-first',  function(x){ state.customer.first = x; });
  take('c-last',   function(x){ state.customer.last  = x; });
  take('c-phone',  function(x){ state.customer.phone = x; });
  take('c-email',  function(x){ state.customer.email = x; });
  // UTILITY ACCOUNT. These were hydrated on reopen but never captured, so a
  // rep who typed an account or meter number and left lost both - they were
  // only ever written by the explicit Continue handler.
  take('c-acct',   function(x){ state.customer.acct        = x; });
  take('c-pod',    function(x){ state.customer.podId       = x; });
  take('c-meter',  function(x){ state.customer.meterNumber = x; });
  take('a-street', function(x){ state.address.street = x; });
  take('a-unit',   function(x){ state.address.unit   = x; });
  take('a-city',   function(x){ state.address.city   = x; });
  take('a-zip',    function(x){ state.address.zip    = x; });
  // A <select> IS NOT ADDITIVE THE WAY AN <input> IS.
  //
  // take() reads "" as "nothing was entered here". A select never has an
  // empty value: #a-state ships as <option value="NY" selected> and
  // clearCustomerBillForm() puts it back to NY on every reset. So the markup
  // DEFAULT was indistinguishable from a deliberate choice by the rep.
  //
  // THE BUG THIS FIXES (found by test/phase_b_readonly_harness.js, not by
  // reading the code). A rep sitting on Customer & Bill for one enrollment
  // opens another that resumes to a DIFFERENT step. goStep() fires this
  // capture on the way out, hydrateStep(2) has not run for the new
  // enrollment, so the select still reads NY - and 'NY' was written straight
  // over the Virginia state openEnrollment() had just restored from the
  // payload. The enrollment silently became a New York one in memory, in
  // every later save, and on Review & finish. Exactly the corruption the
  // hydration comment above describes, re-entering by another door.
  //
  // The typed address fields are the evidence that this form belongs to the
  // CURRENT enrollment: clearCustomerBillForm() empties them on every open
  // and only hydrateStep(2) or the rep fills them. With all three empty the
  // select carries no information and is ignored. A rep who changes only the
  // state and nothing else loses nothing - the explicit Continue handler
  // writes the whole validated payload, selects included.
  const addressFormInUse = !!(v('a-street') || v('a-city') || v('a-zip'));
  if(addressFormInUse) take('a-state', function(x){ state.address.state = x; });
  // BILLING. The checkbox is a boolean, so it is read directly rather than
  // through take() - "unchecked" is a real value, not an absent one.
  // PASSWORD. Taken only when it is COMPLETE and CONFIRMED.
  //
  // THE BUG THIS FIXES: a progress save carried first/last/email/phone and
  // nothing else, so a rep who typed a password and pressed Save & Exit never
  // stored one. customer_has_password came back false on reopen, and Continue
  // then correctly demanded a password - which read as "it still makes me
  // re-enter it" when the real fault was that it had never been saved.
  //
  // Half-typed or mismatched is not a password and is never stored. The boxes
  // are blank on reopen by design, and blank leaves the stored one alone.
  const pw  = v('c-pass');
  const pw2 = v('c-pass-confirm');
  if(pw && pw === pw2 && pw.length >= 6) state.customer.password = pw;

  const same = document.getElementById('billing-same');
  if(same) state.billing.sameAsService = !!same.checked;
  take('b-street', function(x){ state.billing.street = x; });
  take('b-unit',   function(x){ state.billing.unit   = x; });
  take('b-city',   function(x){ state.billing.city   = x; });
  take('b-zip',    function(x){ state.billing.zip    = x; });
  // #b-state is the same kind of control with the same markup default, so it
  // gets the same guard rather than waiting to be found the same way.
  const billingFormInUse = !!(v('b-street') || v('b-city') || v('b-zip'));
  if(billingFormInUse) take('b-state', function(x){ state.billing.state = x; });
}

/* The dashboard's customer label, resolved from whatever we actually have.

   "(no customer info yet)" used to appear whenever customers.first_name was
   unwritten - which, before the autosave above, meant every enrollment the rep
   had not pushed all the way through the Customer & Bill step. It also rendered
   a bare " " for a row whose customer existed with empty names. The placeholder
   now appears only when there is genuinely nothing to show. */
function customerLabel(e){
  const c = e.customer || {};
  const name = [c.first_name, c.last_name]
    .map(function(x){ return (x || '').trim(); })
    .filter(Boolean).join(' ');
  if(name) return name;
  const email = (c.email || e.perch_token_email || '').trim();
  if(email) return email;
  return '(no customer info yet)';
}

/* Say whether a portal password already exists, without ever handling one.

   The browser is told only `customer_has_password`. A password that has been
   set is a committed Dalton credential: it is stored as a hash and cannot be
   read back, so the form CANNOT repopulate those boxes and must not pretend
   to. Leaving them blank keeps the existing password; typing a new one
   replaces it. That is the whole security boundary, stated on screen rather
   than left for the rep to infer from two empty inputs. */
function renderPasswordState(){
  const note = document.getElementById('c-pass-set-note');
  if(!note) return;
  if(perchContext.customerHasPassword){
    note.textContent = 'A password is already set for this customer. '
                     + 'Leave these blank to keep it, or enter a new one to replace it.';
    note.style.display = 'block';
  } else {
    note.textContent = '';
    note.style.display = 'none';
  }
}

/* Save valid progress and return to the dashboard.

   showView() already saves on the way out, so this is a clearer LABEL for
   something the rep could already do - not a new privilege. */
async function saveAndExit(){
  captureCustomerFields();
  await saveCustomerProgress();
  progressSavedByCaller = true;       // showView must not save again
  showView('dashboard');
  renderDashboard();
}

async function exitWizard(){
  // Leaving must not discard valid work. Captured from the DOM first, because
  // `state` is only updated by the Continue handler.
  captureCustomerFields();
  await saveCustomerProgress();
  progressSavedByCaller = true;       // showView must not save again
  showView('dashboard');
  renderDashboard();
}

/* Sent back to Availability instead of being stranded on a later step.

   The rep keeps everything they entered: ZIP, email and utility live in
   `state`/perchContext and hydrateStep() repopulates the step from them, so
   this is a navigation, not a reset. The banner says why they moved, and the
   step's own primary action is relabelled so the thing to press is obvious.
   If a later render resets that label, the button still runs the same capacity
   request - the rep is never stranded either way. */
function requireAvailabilityAgain(reason){
  const banner = document.getElementById('availability-rerun-banner');
  if(banner){
    banner.textContent = reason || 'Availability needs to be checked again before continuing.';
    banner.style.display = 'block';
  }
  goStep(1);
  const primary = document.getElementById('wf-primary');
  if(primary && !primary.disabled) primary.textContent = 'Check Availability Again';
  return false;
}

function clearAvailabilityRerunNotice(){
  const banner = document.getElementById('availability-rerun-banner');
  if(banner){ banner.style.display = 'none'; banner.textContent = ''; }
}

/* Back from Eligibility.

   The button used to call goStep(3) - the Contact & Login step REMOVED in the
   merge. stepIds[3] is undefined, so getElementById(undefined) returned null
   and .classList.add() threw AFTER every .wizard-step had already had 'active'
   removed. The result was a blank page, and because nothing was persisted the
   enrollment resumed to Eligibility again: the trap.

   This navigates via the ACTIVE sequence, so it lands on Customer & Bill with
   all state intact. */
function backFromLmi(){
  goStep(prevStepBefore(4) || 2);
}

function backFromCustomer(){
  // Back goes to AVAILABILITY, so it must render the service_area descriptor -
  // never whatever currentWorkflow happens to hold, which after a capacity
  // check is the capacity_result step. Falls back to currentWorkflow only when
  // no Availability step has been drawn in this session.
  const wf = availabilityWorkflow || currentWorkflow;
  if(wf){
    goStep(1);
    renderWorkflowStep(wf);
    // renderWorkflowStep rebuilds the inputs EMPTY from the step descriptor, so
    // the rep's email/ZIP/utility vanished on Back and an empty ZIP then read as
    // malformed. Restore what was actually submitted.
    rehydrateAvailability();
  } else { exitWizard(); }
}

/* Restore the Availability fields from saved enrollment state. */
function rehydrateAvailability(){
  const put = (name, value) => {
    const el = document.getElementById('wf-' + name);
    if(el && value != null && value !== '') el.value = value;
  };
  put('email', state.customer.email || perchContext.email);
  put('zip_code', perchContext.capacityZip || state.address.zip);
  put('utility_name', perchContext.utilitySlug);
}

let currentStep = 1;
function goStep(n){
  // Defensive: an unknown step must never blank the wizard. Removing 'active'
  // from every screen and then throwing is precisely how the Eligibility Back
  // button produced an empty page.
  const targetId = stepIds[n];
  if(!targetId || !document.getElementById(targetId)){
    const fallback = activeSteps()[0] || 1;
    if(n === fallback) return;
    return goStep(fallback);
  }
  // Moving between steps persists valid customer identity, so progress is
  // never lost by navigating. Fire-and-forget: navigation must not wait on it.
  if(currentStep === 2 && n !== 2){
    captureCustomerFields();
    saveCustomerProgress();
  }
  if(n !== 1) clearAvailabilityRerunNotice();
  currentStep = n;
  document.querySelectorAll('.wizard-step').forEach(s=>s.classList.remove('active'));
  document.getElementById(targetId).classList.add('active');

  // Returning to the availability step must RESTORE the program cards. They
  // were previously loaded only once, right after the capacity submission, so
  // navigating back left the step blank and the rep unable to see or change
  // their choice. loadProgramOptions() re-reads the backend, which also
  // re-hydrates the persisted selection.
  if(n === 1 && currentDraft && currentDraft.enrollment_id){
    loadProgramOptions();
  }
  // One call renders nodes, connectors and states from the ACTIVE sequence.
  renderStepper(n);
  // Mount/refresh "Copy customer link" on EVERY wizard navigation, so it also
  // appears in the live straight-through flow - renderResumeBanner() was the
  // sole mount point and runs only on open/resume.
  refreshCustomerLinkControl();
  hydrateStep(n);
  if(n===5){
    // Populate the persisted detail FIRST so a live enrollment reads the same
    // backend values a resumed one does; fillReview re-runs once it lands.
    fillReview();
    refreshEnrollmentDetail().then(function(){ fillReview(); });
  }
  window.scrollTo({top:0,behavior:'smooth'});
}
function hydrateStep(n){
  if(n===2){
    configureBillUtilityRules();
    document.getElementById('c-first').value = state.customer.first;
    document.getElementById('c-last').value = state.customer.last;
    document.getElementById('c-acct').value = state.customer.acct;
    document.getElementById('c-pod').value = state.customer.podId || '';
    document.getElementById('a-street').value = state.address.street;
    document.getElementById('a-unit').value = state.address.unit;
    document.getElementById('a-city').value = state.address.city;
    document.getElementById('a-state').value = state.address.state || 'NY';
    document.getElementById('a-zip').value = state.address.zip;
      document.getElementById('billing-same').checked = state.billing.sameAsService !== false;
    document.getElementById('b-street').value = state.billing.street || '';
    document.getElementById('b-state').value = state.billing.state || state.address.state || 'NY';
    document.getElementById('b-unit').value = state.billing.unit || '';
    document.getElementById('b-city').value = state.billing.city || '';
    document.getElementById('b-zip').value = state.billing.zip || '';
    toggleBillingAddress(false);
    // The legacy chip is for a file the rep JUST PICKED - it shows one name
    // with a "Remove" that cancels a pending upload. It is NOT drawn for a
    // restored file: the document-set list below already lists every stored
    // page, and drawing the chip too put a SECOND Remove control on screen for
    // a document that is already persisted. That control only cleared the
    // chip, so the rep removed a photo, saved, reopened - and it was still
    // there, because nothing had ever been deleted.
    if(state.bill.fileName && !state.bill.documentId){
      showBillChip(state.bill.fileName);
    }
    // Contact & login moved onto THIS screen, so its hydration moved with it -
    // otherwise phone/password would not restore on Back or resume.
    const email = state.customer.email || perchContext.email || '';
    document.getElementById('c-email').value = email;
    const disp = document.getElementById('c-email-display');
    if(disp) disp.textContent = email || '—';   // element removed with the strip
    document.getElementById('c-phone').value = state.customer.phone || '';
    // The meter number was restored into state but never painted onto the
    // form, so a market that requires it always looked empty on reopen.
    const meterEl = document.getElementById('c-meter');
    if(meterEl) meterEl.value = state.customer.meterNumber || '';
    renderPasswordState();
    document.getElementById('c-pass').value = state.customer.password || '';
    document.getElementById('c-pass-confirm').value = state.customer.password || '';
    renderServiceContext();
    if(currentDraft && currentDraft.enrollment_id) loadProgramOptions();
    checkBillReady();
  }
  if(n===4){
    // ONE screen, sections decided by Perch's step. Both are hydrated - the
    // proof-document fields always, the self-attestation section when Perch
    // has asked for it - so neither is lost by visiting the other.
    const sections = renderLmiSections();
    document.getElementById('lmi-name-on-doc').value = state.lmi.nameOnDocument || (state.customer.first+' '+state.customer.last).trim();
    document.getElementById('lmi-relationship').value = state.lmi.relationship || 'self';
    document.getElementById('lmi-format').value = state.lmi.documentFormat || '';
    if(state.lmi.fileName){
      document.getElementById('lmi-doctype').value = state.lmi.docType;
      showLmiChip(state.lmi.fileName);
    }
    if(perchContext.nextStepKey === 'proof_docs') prepareLmiForPerch();
    if(sections.attest){
      // Read-only: loads the authoritative threshold table, the controlled
      // county list, and any answers this enrollment already submitted.
      Promise.resolve(prepareSelfAttestation()).catch(function(){});
    }
    checkLmiReady();
  }
  if(n===5 && perchContracts.length){ mountRepAgreements(false, perchContext.acceptanceSubmitted === true); }
}

function currentUtilityRule(){ return utilityRules[perchContext.utilitySlug] || null; }

function configureBillUtilityRules(){
  const rule = currentUtilityRule();
  const acct = document.getElementById('c-acct');
  const acctHelp = document.getElementById('acct-rule-help');
  const podField = document.getElementById('pod-id-field');
  const podHelp = document.getElementById('pod-rule-help');
  if(!acct || !acctHelp || !podField) return;
  const expected = rule && rule.account_number_length ? Number(rule.account_number_length) : null;
  acct.maxLength = expected || 20;
  acct.placeholder = expected ? ('•'.repeat(Math.min(expected, 12))) : 'Utility account number';
  // The LOCAL rule writes this first; refreshAcctHint() below replaces it
  // with the market's own wording once Perch's fields have been fetched.
  acctHelp.textContent = expected ? ((rule.display_name || 'This utility') + ' uses a ' + expected + '-digit account number.') : '';
  const needsPod = !!(rule && rule.requires_pod_id);
  podField.style.display = needsPod ? 'block' : 'none';
  podHelp.textContent = needsPod && rule.pod_id ? ('Required: ' + rule.pod_id.description + '.') : '';
  applyMarketAccountFields();
}

/* The fields Perch published for the current utility, or null when unknown. */
let marketAccountFields = null;
let meterRule = null;   // the meter_number field rule, when this market has one

/* MARKET-PUBLISHED ACCOUNT FIELDS.
   Layered on top of configureBillUtilityRules() above, which keeps serving the
   NY rules from perch_utilities exactly as before. This only ADDS the fields
   Perch publishes for the selected utility, and only when it publishes them.

   A field Perch does not list is NOT REQUIRED - that is how the spec expresses
   it ("followed by meter_number and secondary_account_identifier only for
   utilities that require them"). So a utility whose rules list a meter number
   gets a meter-number input and no secondary identifier, while NYSEG keeps its
   POD ID and gains nothing.

   Nothing here names a utility or a state: it renders whatever the response
   lists. If the lookup fails, the form is left exactly as the NY rules set it
   and Perch still validates on submit. */
async function applyMarketAccountFields(){
  const meterField = document.getElementById('meter-number-field');
  const meterHelp = document.getElementById('meter-rule-help');
  if(!meterField) return;
  const slug = perchContext && perchContext.utilitySlug;
  if(!slug){ meterField.style.display = 'none'; return; }
  let fields;
  try{
    const body = await apiFetch('/api/markets/utility-account-fields?utility='
      + encodeURIComponent(slug));
    fields = body.fields || [];
  }catch(e){
    // Market rules unavailable: leave the form as the local NY rules set it.
    // Never guess a field into or out of existence.
    return;
  }
  marketAccountFields = fields;
  const meter = fields.filter(function(f){ return f.field_type === 'meter_number'; })[0];
  meterField.style.display = meter ? 'block' : 'none';
  meterRule = meter || null;
  refreshMeterHint();
  refreshAcctHint();
  // A secondary identifier this utility does NOT require must not be on screen.
  // NY utilities that DO require it are untouched, because they appear in
  // `fields` and so never reach this branch.
  const podField = document.getElementById('pod-id-field');
  const hasSecondary = fields.some(function(f){
    return f.field_type === 'secondary_account_identifier'; });
  if(podField && fields.length && !hasSecondary) podField.style.display = 'none';
  checkBillReady();
}

/* The ACCOUNT NUMBER field's hint, derived from the CURRENT value.

   D-1. The meter number has had this since the market-fields layer landed;
   the account number never did. In a market whose account rule is PUBLISHED
   rather than held in the local utility table, the rep saw "10 digits." under
   the meter and NOTHING under the account - while the published rule said
   "10 or 12 digits" and the value was still validated against it. A wrong
   account number therefore disabled Continue with no stated reason.

   THE RULE IS NOT RESTATED HERE. The text is whatever the market published
   (marketAccountFields[].help_text), exactly as the meter hint does it. When
   Perch publishes nothing for this utility - every NY utility today, since
   the local perch_utilities table is the source there - the sentence
   configureBillUtilityRules() already wrote is left untouched, so NY guidance
   is unchanged. */
function refreshAcctHint(){
  const help = document.getElementById('acct-rule-help');
  const el = document.getElementById('c-acct');
  if(!help) return;
  const field = (marketAccountFields || []).filter(function(f){
    return f.field_type === 'account_number'; })[0];
  if(!field) return;            // no market rule: keep the local NY sentence
  const value = el ? (el.value || '').trim() : '';
  // A value still showing the stored mask is not something to validate - the
  // rep has not touched it and the real number is on the server.
  const problem = (value && !isMaskedValue(value))
    ? validateMarketAccountField('account_number', value) : null;
  if(problem){
    help.textContent = problem;
    help.style.color = 'var(--danger)';
  } else {
    const label = field.field_name || 'Account number';
    help.textContent = field.help_text ? (label + ': ' + field.help_text + '.') : '';
    help.style.color = '';
  }
}

/* The meter field's hint, derived from the CURRENT value.

   It used to be written once as "Required: <help text>." and left there
   forever - so a field containing a perfectly valid ten-digit meter number
   still displayed a red demand to enter a valid ten-digit meter number. The
   text now describes the format while the field is empty or correct, and only
   turns into an error when the value actually fails the market's own pattern. */
function refreshMeterHint(){
  const help = document.getElementById('meter-rule-help');
  const el = document.getElementById('c-meter');
  if(!help) return;
  if(!meterRule){ help.textContent = ''; help.style.color = ''; return; }
  const value = el ? (el.value || '').trim() : '';
  const problem = value ? validateMarketAccountField('meter_number', value) : null;
  if(problem){
    help.textContent = problem;
    help.style.color = 'var(--danger)';
  } else {
    help.textContent = meterRule.help_text ? (meterRule.help_text + '.') : '';
    help.style.color = '';
  }
}

/* Validate a value against the market's patterns: valid when it matches ANY ONE
   of them, per the spec. Returns an error string, or null when valid or when
   the utility does not require the field. account_number is matched
   case-insensitively, the others case-sensitively. */
function validateMarketAccountField(fieldType, value){
  if(!marketAccountFields) return null;
  const field = marketAccountFields.filter(function(f){
    return f.field_type === fieldType; })[0];
  if(!field) return null;                      // not required by this utility
  const raw = (value || '').trim();
  const patterns = field.patterns || [];
  const label = field.field_name || fieldType.replace(/_/g, ' ');
  if(!raw) return label + ' is required for this utility.';
  if(!patterns.length) return null;            // no published format rule
  const flags = fieldType === 'account_number' ? 'i' : '';
  for(let i = 0; i < patterns.length; i++){
    try{
      if(new RegExp('^(?:' + patterns[i] + ')$', flags).test(raw)) return null;
    }catch(e){ /* malformed upstream pattern: skip it, another may match */ }
  }
  return label + ' does not match the format this utility requires'
       + (field.help_text ? ' (' + field.help_text + ').' : '.');
}

function syncBillingFromService(){
  if(!document.getElementById('billing-same').checked) return;
  document.getElementById('b-street').value = document.getElementById('a-street').value;
  document.getElementById('b-unit').value = document.getElementById('a-unit').value;
  document.getElementById('b-city').value = document.getElementById('a-city').value;
  // State is now selectable, so it must be mirrored too - otherwise a
  // same-as-service billing address would silently keep the default NY.
  document.getElementById('b-state').value = document.getElementById('a-state').value;
  document.getElementById('b-zip').value = document.getElementById('a-zip').value;
}

function toggleBillingAddress(runCheck=true){
  const same = document.getElementById('billing-same').checked;
  document.getElementById('billing-address-fields').style.display = same ? 'none' : 'block';
  if(same) syncBillingFromService();
  if(runCheck) checkBillReady();
}

function podLooksValid(rule, value){
  if(!rule || !rule.requires_pod_id) return true;
  value = (value || '').trim();
  const pod = rule.pod_id || {};
  if(!value) return false;
  if(pod.length && value.length !== Number(pod.length)) return false;
  if(pod.prefix && !value.toUpperCase().startsWith(String(pod.prefix).toUpperCase())) return false;
  return true;
}

function checkBillReady(){
  billTouched = true;   // RC-3: reached only via a real field/file interaction
  // Both of these read the SAME inputs this function validates, so the panel
  // and the meter hint can never disagree with the form again.
  renderBillReadPanel();
  refreshMeterHint();
  refreshAcctHint();
  const first = document.getElementById('c-first').value.trim();
  const last = document.getElementById('c-last').value.trim();
  const acct = document.getElementById('c-acct').value.trim();
  const street = document.getElementById('a-street').value.trim();
  const city = document.getElementById('a-city').value.trim();
  const zip = document.getElementById('a-zip').value.trim();
  const rule = currentUtilityRule();
  const expected = rule && rule.account_number_length ? Number(rule.account_number_length) : null;
  // AN UNTOUCHED MASKED FIELD IS AN ACCOUNT THAT IS ALREADY SAVED.
  // The rep is shown "******7890" on reopen; the real number is on the server
  // and is not resent. Judging the mask against the digit rule left a reopened
  // enrollment with a dead Continue and the nonsense reason "must be 10 digits
  // - you entered 10" for a field the rep never touched.
  const acctAlreadyStored = isMaskedValue(acct);
  const acctOk = acctAlreadyStored
    || (/^\d+$/.test(acct) && (!expected || acct.length === expected));
  const podOk = podLooksValid(rule, document.getElementById('c-pod').value);
  const sameBilling = document.getElementById('billing-same').checked;
  const billingOk = sameBilling || (
    document.getElementById('b-street').value.trim() &&
    document.getElementById('b-city').value.trim() &&
    /^\d{5}$/.test(document.getElementById('b-zip').value.trim())
  );
  // RC-3: collect WHY, not just whether. A dead Continue with no explanation is
  // the single most confusing failure a rep hits - especially on NYSEG, where
  // the account number is 11 digits (National Grid is 10) and a POD ID is
  // required. Previously this reduced ~9 conditions to one silent boolean.
  const missing = [];
  // A dual-program location has no branch until the rep chooses, so Continue
  // stays disabled and the reason is listed with the rest.
  if(availablePrograms.length > 1 && !selectedProgram) missing.push('Choose a savings program');
  if(!state.bill.fileName) missing.push('Upload the utility bill');
  if(!first || !last) missing.push('Customer first and last name');
  if(!acct){
    missing.push('Utility account number');
  } else if(!acctOk){
    missing.push(expected
      ? ('Account number must be ' + expected + ' digits for ' +
         ((rule && rule.display_name) || 'this utility') + ' — you entered ' + acct.length)
      : 'Account number must contain digits only');
  }
  if(!podOk){
    const podRule = rule && rule.pod_id;
    missing.push(podRule && podRule.description
      ? ((rule.display_name || 'This utility') + ' requires a POD ID (' + podRule.description + ')')
      : 'Valid POD / secondary identifier');
  }
  if(!street) missing.push('Service street address');
  if(!city) missing.push('Service city');
  if(!/^\d{5}$/.test(zip)) missing.push('5-digit service ZIP');
  if(!billingOk) missing.push('Complete billing address (or tick "same as service")');
  // BILL AMOUNT IS NOT PART OF THIS WORKFLOW. There is no rep field for it,
  // the backend's own missing-field list is account_number / account_holder /
  // service_street, and no Perch payload carries an amount. The check that
  // used to sit here was already inert - `amt` was the literal 'n/a', so the
  // push could never fire - but it kept the requirement looking real.

  const ready = missing.length === 0;
  document.getElementById('btn-bill-next').disabled = !ready;
  renderBillRequirements(missing);
}

// Shows what is still outstanding. Deliberately neutral guidance, not red
// errors, and only after the rep has actually started the step - so it informs
// rather than scolds an untouched form.
function renderBillRequirements(missing){
  const el = document.getElementById('bill-requirements');
  if(!el) return;
  const started = !!(state.bill.fileName || billTouched);
  if(!missing.length || !started){ el.style.display = 'none'; el.innerHTML = ''; return; }
  el.innerHTML = '<strong>Still needed before you can continue:</strong><ul style="margin:6px 0 0 18px;">' +
    missing.map(function(m){ return '<li>' + esc(m) + '</li>'; }).join('') + '</ul>';
  el.style.display = 'block';
}

async function uploadDocumentToDalton(file, category){
  if(!currentDraft) throw new Error('The enrollment session is not ready yet.');
  const fd = new FormData();
  fd.append('file', file, file.name);
  fd.append('category', category);
  return apiFetch('/api/enrollments/' + currentDraft.enrollment_id + '/documents', {method:'POST', body:fd});
}

async function submitBill(){
  const errEl = document.getElementById('bill-submit-error');
  errEl.style.display='none';
  const btn = document.getElementById('btn-bill-next');
  if(btn.disabled) return;

  // Program first: the error belongs beside the choice, and nothing should be
  // submitted before the branch is known. The backend re-validates regardless.
  if(!validateProgramSelection()) return;

  state.customer.first = document.getElementById('c-first').value.trim();
  state.customer.last = document.getElementById('c-last').value.trim();
  state.customer.acct = document.getElementById('c-acct').value.trim();
  state.customer.podId = document.getElementById('c-pod').value.trim();
  // Meter number: only utilities whose market rules list it have the field on
  // screen, so this is an empty string for every NY enrollment and is sent as
  // null (see the payload below), producing no multipart part at all.
  const meterEl = document.getElementById('c-meter');
  state.customer.meterNumber = meterEl ? meterEl.value.trim() : '';
  state.address = {
    street: document.getElementById('a-street').value.trim(),
    unit: document.getElementById('a-unit').value.trim(),
    city: document.getElementById('a-city').value.trim(),
    // Read the SELECTED state. This was hardcoded 'NY' while the field was a
    // readonly NY input, so the new dropdown would have been silently ignored.
    state: (document.getElementById('a-state').value || 'NY').trim(),
    zip: document.getElementById('a-zip').value.trim()
  };
  const sameBilling = document.getElementById('billing-same').checked;
  state.billing = {
    sameAsService: sameBilling,
    street: sameBilling ? state.address.street : document.getElementById('b-street').value.trim(),
    unit: sameBilling ? state.address.unit : document.getElementById('b-unit').value.trim(),
    city: sameBilling ? state.address.city : document.getElementById('b-city').value.trim(),
    state: sameBilling ? state.address.state
                       : (document.getElementById('b-state').value || 'NY').trim(),
    zip: sameBilling ? state.address.zip : document.getElementById('b-zip').value.trim()
  };
  // Contact & login now live on this screen; collect them with the rest.
  state.customer.phone = (document.getElementById('c-phone').value || '').trim();
  state.customer.password = document.getElementById('c-pass').value || '';

  btn.disabled=true; btn.textContent='Saving…';
  try{
    if(billUploadPromise) await billUploadPromise;
    if(!state.bill.documentId) throw new Error('The utility bill has not finished saving. Please try again.');
    // NO PARTIAL BILL REACHES PERCH. Every page the rep selected must have a
    // stored document id; if any upload failed, this stops here with the page
    // named rather than submitting a bill that is quietly missing pages.
    const unsaved = (DocSets.utility_bill.files || []).filter(function(x){ return !x.documentId; });
    if(unsaved.length){
      throw new Error(unsaved.length + ' of ' + DocSets.utility_bill.files.length +
        ' bill pages did not finish uploading (' +
        unsaved.map(function(x){ return x.file.name; }).join(', ') +
        '). Remove or re-add them before continuing.');
    }
    const payload = {
      customer:{first_name:state.customer.first,last_name:state.customer.last,email:state.customer.email,phone:state.customer.phone || null},
      // THE SELECTED STATE, not a literal. These two lines previously sent
      // 'NY' unconditionally, which silently discarded the value read from the
      // dropdown ~30 lines above and persisted every enrollment as New York.
      // 'NY' is still the DEFAULT - set in the initial state and selected in
      // the markup - so an NY enrollment sends exactly what it always did,
      // while a Virginia enrollment now sends VA.
      service_address:{street:state.address.street,unit:state.address.unit,city:state.address.city,state:state.address.state||'NY',zip:state.address.zip},
      billing_address:{same_as_service:state.billing.sameAsService,street:state.billing.street,unit:state.billing.unit,city:state.billing.city,state:state.billing.state||state.address.state||'NY',zip:state.billing.zip},
      utility_account:Object.assign(
        {utility_name:perchContext.utilitySlug,
         secondary_account_identifier:state.customer.podId || null,
         meter_number:state.customer.meterNumber || null},
        // Same rule as the progress save: an untouched masked field is not an
        // edit, so it is left out and the stored value stands.
        isMaskedValue(state.customer.acct)
          ? {} : {account_number:state.customer.acct})
    };
    if(state.project.id) payload.project_id = state.project.id;
    await apiFetch('/api/enrollments/' + currentDraft.enrollment_id, {method:'PATCH', body:JSON.stringify(payload)});
    // ONE merged Continue: Dalton data is saved above, then contact/login is
    // validated and the Perch enrollment created, then the backend decides the
    // branch. No second page, no second button.
    const ok = await submitContactDetails();
    if(!ok) return;
    await continueFromPerchNextStep('contact');
    return;
  }catch(err){
    errEl.textContent=userMessage(err); errEl.style.display='';
  }finally{
    btn.textContent='Continue'; checkBillReady();
  }
}

/* Shows a field-specific validation error and focuses the offending control.
   Top level and fully parameterised - it closes over nothing. */
function wizardFieldError(target, msg, focusId){
  if(target){ target.textContent = msg; target.style.display = 'block'; }
  const el = focusId && document.getElementById(focusId);
  if(el){ el.classList.add('err'); el.focus(); }
  return false;
}

/* Contact + login validation and the Perch enrollment call for the MERGED
   Customer & Bill screen.

   BUG 2 FIX. This used to behave like it owned its own page: it re-read a
   #c-email field, produced a single generic "Fill in every field" message for
   four different problems, and drove #btn-contact-next - a button the merge
   removed, so it threw and the screen simply never advanced.

   Now:
     * the enrollment email comes from perchContext (the authoritative Perch
       session value), not from a hidden DOM field
     * each failure has its OWN message and focuses the offending control
     * the one real Continue button (#btn-bill-next) is driven
   Returns true on success so the caller knows whether to proceed. */
async function submitContactDetails(){
  const errEl = document.getElementById('contact-submit-error');
  if(errEl) errEl.style.display = 'none';

  const email = (perchContext.email || state.customer.email || '').trim();
  const phone = (document.getElementById('c-phone').value || '').trim();
  const pass = document.getElementById('c-pass').value || '';
  const passConfirm = document.getElementById('c-pass-confirm').value || '';

  ['c-phone','c-pass','c-pass-confirm'].forEach(function(id){
    const el = document.getElementById(id); if(el) el.classList.remove('err');
  });

  // The Perch session email is established at the availability step; it cannot
  // be missing here without a broken session.
  if(!email){
    // THE DEAD END THIS REPLACES. This used to render the message and stop,
    // leaving the rep on a step with no way to re-run anything; recovery meant
    // logging out. Now they are returned to Availability with their values
    // intact and an obvious action to press.
    return requireAvailabilityAgain(
      'We need to confirm availability again before continuing. '
      + 'Your ZIP code and utility are still filled in - press '
      + '"Check Availability Again" to continue.');
  }
  if(!phone) return wizardFieldError(errEl, 'Enter the customer\'s phone number.', 'c-phone');
  // A PASSWORD THAT ALREADY EXISTS IS NOT RE-ENTERED.
  //
  // It is stored as a hash and can never be read back, so the form cannot
  // repopulate these boxes - and demanding them again on every reopen would
  // force the rep to invent a new password for a customer who already has one,
  // silently replacing the credential that customer may already be using.
  // Blank + already set means "keep it"; typing one means "replace it", and
  // then the normal rules apply. renderPasswordState() says exactly this on
  // screen, so the behaviour and the label agree.
  const keepExistingPassword = perchContext.customerHasPassword
                               && !pass && !passConfirm;
  if(!keepExistingPassword){
    if(!pass) return wizardFieldError(errEl, 'Create a password for the customer.', 'c-pass');
    if(pass.length < 6) return wizardFieldError(errEl, 'Password should be at least 6 characters.', 'c-pass');
    if(!passConfirm) return wizardFieldError(errEl, 'Confirm the password.', 'c-pass-confirm');
    if(pass !== passConfirm) return wizardFieldError(errEl, 'Passwords don\'t match.', 'c-pass-confirm');
  }

  state.customer.email = email;
  state.customer.phone = phone;
  state.customer.password = pass;

  const customerPayload = {first_name:state.customer.first,
    last_name:state.customer.last, email:state.customer.email,
    phone:state.customer.phone};
  // OMITTED, not sent empty: the PATCH handler only writes a password when one
  // is present, and an absent key is how "leave it alone" is expressed.
  if(!keepExistingPassword) customerPayload.password = state.customer.password;

  await apiFetch('/api/enrollments/' + currentDraft.enrollment_id, {
    method:'PATCH', body:JSON.stringify({customer: customerPayload})
  });

  if(!perchContext.enrollmentSubmitted){
    // customer_type is the rep's SELECTION; the backend re-validates it against
    // this enrollment's persisted capacity response and also reads the
    // persisted value, so this is a request rather than an instruction.
    const enrollBody = {document_id: state.bill.documentId};
    if(selectedProgram && selectedProgram.customer_type){
      enrollBody.customer_type = selectedProgram.customer_type;
    }
    const body = await apiFetch('/api/perch/enrollments/' + currentDraft.enrollment_id + '/enroll',
                                {method:'POST', body:JSON.stringify(enrollBody)});
    perchContext.enrollmentSubmitted = true;
    perchContext.nextStepKey = body.next_step_key;
  }
  return true;
}

/* NOTE: the old submitContact() alias was removed. It had zero callers and
   kept a dead "Contact page" shape alive underneath the merged screen, which is
   exactly what the merge was meant to eliminate. submitBill() -> 
   submitContactDetails() -> continueFromPerchNextStep() is the whole path. */

async function continueFromPerchNextStep(origin){
  if(perchContext.contractsGenerated){
    perchContext.agreementBackStep = origin === 'lmi' ? 4 : 2;
    goStep(5); mountRepAgreements(false, false); return;
  }
  if(perchContext.nextStepKey === 'proof_docs'){
    goStep(4); prepareLmiForPerch(); return;
  }
  if(perchContext.nextStepKey === 'contracts'){
    state.lmi.mode='na'; state.lmi.docType=''; state.lmi.fileName='';
    // backStep 2, not 3: step 3 (Contact & Login) was merged into Customer &
    // Bill, so a non-LMI enrollment must return there.
    await generateContractsAndOpenAgreement(origin === 'lmi' ? 4 : 2); return;
  }
  if(perchContext.nextStepKey === 'self_attestation'
     || perchContext.nextStepKey === 'self_attestation_accept'){
    // CONDITIONAL branch - entered only because PERCH returned it. Rendered
    // inside the existing Eligibility step. The customer accepts the
    // Perch-generated document in the portal, after which Perch returns
    // /contracts and the EXISTING contracts flow runs unchanged.
    // goStep(4) renders the sections and hydrates the self-attestation part
    // itself, so the reference load happens once, from one place.
    goStep(4);
    return;
  }
  throw new Error('This enrollment could not be advanced automatically. It has not changed - please try again, and let an admin know if it continues.');
}

function dzDragOver(e, id){ e.preventDefault(); document.getElementById(id).classList.add('drag'); }
function dzDragLeave(e, id){ document.getElementById(id).classList.remove('drag'); }
function dzDrop(e, id, handler){ e.preventDefault(); document.getElementById(id).classList.remove('drag'); const files=e.dataTransfer.files; if(files&&files.length) handler(files); }

/* ---------------- REAL TEXT EXTRACTION (PDF.js / Tesseract.js) ---------------- */
let pdfWorkerConfigured = false;
async function extractTextFromFile(file){
  const isPdf = file.type === 'application/pdf' || /\.pdf$/i.test(file.name);
  if(isPdf){
    if(!pdfWorkerConfigured){
      pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
      pdfWorkerConfigured = true;
    }
    const buf = await file.arrayBuffer();
    const pdf = await pdfjsLib.getDocument({data: buf}).promise;
    let text = '';
    const maxPages = Math.min(pdf.numPages, 6);
    for(let i=1;i<=maxPages;i++){
      const page = await pdf.getPage(i);
      const content = await page.getTextContent();
      text += content.items.map(it=>it.str).join(' ') + '\n';
    }
    return text;
  }
  const result = await Tesseract.recognize(file, 'eng');
  return result.data.text;
}
function cleanText(t){ return (t||'').replace(/\s+/g,' ').trim(); }

function parseUtilityBill(rawText){
  const t = cleanText(rawText);
  const result = {first:'', last:'', street:'', city:'', zip:'', acct:''};
  let m = t.match(/ACCOUNT NUMBER\s*(\d{5}-\d{5}|\d{10})/i);
  if(m) result.acct = m[1].replace(/-/g,'');
  // Utility PDFs flatten columns/lines into a single text stream. The old
  // pattern let the street stop immediately after the house number, which could
  // turn "123 MAIN ST ALBANY" into street="123", city="MAIN ST ALBANY".
  // Prefer an address boundary at a common street suffix, then fall back to the
  // legacy pattern for unusual addresses rather than refusing to extract.
  m = t.match(/SERVICE FOR\s+([A-Z][A-Z .'-]+?)\s+(\d+[A-Z0-9 .#'-]*?\b(?:ST(?:REET)?|AVE(?:NUE)?|RD|ROAD|BLVD|BOULEVARD|DR|DRIVE|LN|LANE|CT|COURT|PL|PLACE|PKWY|PARKWAY|HWY|HIGHWAY|WAY|TER|TERRACE))\s+([A-Za-z .'-]+?)\s+([A-Z]{2})\s+(\d{5})/i);
  if(!m){
    m = t.match(/SERVICE FOR\s+([A-Z][A-Z .'-]+?)\s+(\d+[A-Z0-9 .'-]*?)\s+([A-Za-z .'-]+?)\s+([A-Z]{2})\s+(\d{5})/);
  }
  if(m){
    const nameParts = m[1].trim().split(/\s+/);
    result.last = nameParts.length>1 ? nameParts[nameParts.length-1] : '';
    result.first = nameParts.length>1 ? nameParts.slice(0,-1).join(' ') : nameParts[0];
    result.street = m[2].trim();
    result.city = m[3].trim();
    result.zip = m[5];
  }
  return result;
}

/* Fill what the SERVER read off the stored bill, and only what is empty.

   THE RULES, in order of precedence:
     1. a field the rep has typed in is never touched;
     2. a field the in-browser National Grid parse already filled is never
        touched, so that path is unchanged;
     3. anything still empty may be filled from the server's reading.

   The server has already validated every identifier against the SAME rule
   manual entry uses, and omits any candidate that failed - so nothing
   arriving here needs re-checking to be safe to show. It is still checked
   again below for the fields with a client-side rule, because two agreeing
   checks cost nothing and a disagreement would mean one of them is wrong. */
const SERVER_FIELD_TO_INPUT = {
  service_street: 'a-street',
  service_unit:   'a-unit',
  service_city:   'a-city',
  service_zip:    'a-zip',
  account_number: 'c-acct',
  pod_id:         'c-pod',
  meter_number:   'c-meter',
};

//: The conditional fields, and the wrapper whose visibility says whether
//: THIS utility asks for them at all. A utility that does not ask for a meter
//: number must not acquire one off the bill: the field is off screen, the rep
//: never sees it, and a value sitting in a hidden input is a value nobody
//: agreed to. (National Grid's own parse reads a meter number off the bill
//: for its own purposes; that must not become form data here.)
const SERVER_FIELD_WRAPPER = {
  pod_id: 'pod-id-field',
  meter_number: 'meter-number-field',
};

//: Which key on lastBillParse each server field answers for, so the
//: "Pulled from the bill" panel can say "Found on the bill" about a field the
//: SERVER read, exactly as it already does for the ones the browser read.
const SERVER_FIELD_TO_PARSE = {
  service_street: 'street',
  service_unit:   'unit',
  service_city:   'city',
  service_zip:    'zip',
  account_number: 'acct',
  pod_id:         'pod',
  meter_number:   'meter',
};

/* Fill one field only if nothing is in it. The emptiness test IS the
   do-not-overwrite rule: whether the value in there was typed by the rep or
   read by the browser a moment ago, it stands. */
function fillIfEmpty(id, value){
  const el = document.getElementById(id);
  if(!el || !value) return false;
  if(String(el.value || '').trim()) return false;
  el.value = value;
  markOcrFilled(id);
  return true;
}

function applyServerBillFields(body){
  const fields = (body && body.extraction && body.extraction.fields) || null;
  if(!fields) return;
  const found = {};
  let filled = 0;

  // The name is stored as one string and the form has two boxes. Split only
  // when BOTH are empty: filling a surname beside a first name the rep typed
  // would be guessing which half of the name they meant.
  const first = document.getElementById('c-first'), last = document.getElementById('c-last');
  if(fields.account_holder && first && last
     && !first.value.trim() && !last.value.trim()){
    const parts = String(fields.account_holder).trim().split(/\s+/);
    if(parts.length > 1){
      if(fillIfEmpty('c-first', parts.slice(0, -1).join(' '))){ filled++; found.first = true; }
      if(fillIfEmpty('c-last', parts[parts.length - 1])){ filled++; found.last = true; }
    } else if(parts.length === 1){
      if(fillIfEmpty('c-first', parts[0])){ filled++; found.first = true; }
    }
  }

  Object.keys(SERVER_FIELD_TO_INPUT).forEach(function(key){
    const id = SERVER_FIELD_TO_INPUT[key], value = fields[key];
    if(!value) return;
    const wrapId = SERVER_FIELD_WRAPPER[key];
    if(wrapId){
      const wrap = document.getElementById(wrapId);
      if(!wrap || wrap.style.display === 'none') return;   // not asked for here
    }
    // The server validated every identifier against the same rule manual entry
    // uses and dropped whatever failed, so nothing arriving here is unchecked.
    // It is checked AGAIN against the browser's own copy of those rules: the
    // two should always agree, and on the day they do not, the field is left
    // for the rep rather than filled on the strength of one of them.
    if(key === 'pod_id' && !podLooksValid(currentUtilityRule(), value)) return;
    if((key === 'meter_number' || key === 'account_number')
       && validateMarketAccountField(key, value)) return;
    if(fillIfEmpty(id, value)){ filled++; found[SERVER_FIELD_TO_PARSE[key]] = true; }
  });

  // THE STATE IS A DROPDOWN, so it is never empty and the emptiness test above
  // cannot protect it. It follows the ADDRESS instead: the state is set only
  // when this pass filled the service address itself, which can only have
  // happened into empty boxes. A rep who typed their own address keeps their
  // own state, and a bill that read no address never moves it.
  const stateEl = document.getElementById('a-state');
  if(fields.service_state && stateEl && found.street && found.zip){
    const want = String(fields.service_state).trim().toUpperCase();
    const known = Array.prototype.some.call(stateEl.options,
      function(o){ return o.value === want; });
    if(known && stateEl.value !== want){ stateEl.value = want; markOcrFilled('a-state'); filled++; }
  }

  if(filled){
    // The panel reports what the BILL gave up, whichever reader found it.
    lastBillParse = Object.assign({}, lastBillParse || {}, found);
    syncBillingFromService();
    checkBillReady();          // also re-renders the panel and the hints
  }
  return filled;
}

/* The legacy single-file chip, for a file the rep has JUST PICKED.

   The second line used to read "Uploaded" the instant a file was chosen -
   before the upload had been sent, let alone stored. A rep who picked a photo
   and immediately left was told it was uploaded when nothing had been. It now
   states the real status, and handleBillUpload() updates it when the upload
   settles. The control and its handler are untouched: this is the pending
   file's chip, and removal still goes through the one real path. */
/* ONE CHIP FOR BOTH UPLOADS.

   The bill upload and the proof-document upload grew separately and built the
   same chip twice, with the same markup typed out twice - and only one of the
   two escaped the filename it printed. The structure and the four states live
   here now; each flow keeps its own handler, its own copy and its own remove
   function, which are passed in.

   THE STATE COMES FROM THE STATUS THE CALLER ALREADY PASSES. Nothing new is
   inferred and no upload logic is consulted: 'Attached' means the backend
   confirmed it, 'Not uploaded' means it did not, and anything else is a file
   that has been chosen but not settled. The callers and their strings are
   unchanged.

   REMOVAL IS NOT FIXED HERE. Deleting a photo or document does not persist
   reliably; that defect is open and tracked in KNOWN_ISSUES.md, and C2 does
   not touch the removal path. The chip reports what the caller tells it. */
const UPLOAD_CHIP_STATE = {
  'Attached': 'is-attached',
  'Not uploaded': 'is-failed'
};

function uploadChipHtml(name, status, removeCall, removeLabel){
  const label = status || 'Selected';
  const state = UPLOAD_CHIP_STATE[label] ||
                (/^Uploading/i.test(label) ? 'is-uploading' : '');
  return '<div class="file-chip upl-chip ' + state + '">' +
    '<div class="fc-left"><div class="fc-icon" aria-hidden="true">\u{1F4C4}</div>' +
    '<div><div class="fc-name">' + esc(name) + '</div>' +
    '<div class="fc-size">' + esc(label) + '</div></div></div>' +
    '<button type="button" class="fc-remove" onclick="' + removeCall + '">' +
    esc(removeLabel || 'Remove') + '</button></div>';
}

function showBillChip(name, status){
  document.getElementById('bill-file-chip').innerHTML =
    uploadChipHtml(name, status, 'removeBill()');
}
/* Redraw "Pulled from the bill" from the LAST PARSE plus the CURRENT fields.

   One source of truth for each row, in this order:
     the bill read it            -> "Found on the bill"
     the rep typed it            -> "Entered manually"   (not a warning)
     neither                     -> "Not found - enter manually"

   The middle case is the one that was missing, and it is the whole of the
   reported problem: a value on screen could never make the panel stop calling
   it missing. */
function renderBillReadPanel(){
  const c = document.getElementById('ocr-container');
  if(!c || !lastBillParse) return;
  const p = lastBillParse;
  const val = function(id){
    const el = document.getElementById(id);
    return el ? (el.value || '').trim() : '';
  };
  const row = function(label, fromBill, current, missingText){
    if(fromBill) return ocrRow(label, true, 'Found on the bill');
    if(current)  return ocrRow(label, true, 'Entered manually');
    return ocrRow(label, false, missingText);
  };
  // A row for each identifier THIS utility asks for, decided by what is on
  // screen rather than by naming a utility here. A required identifier that
  // the bill did not confidently give up says so, which is the whole point:
  // the rep must see that it is still theirs to enter.
  const onScreen = function(wrapId){
    const el = document.getElementById(wrapId);
    return !!el && el.style.display !== 'none';
  };
  let extra = '';
  if(onScreen('pod-id-field'))
    extra += row('POD ID', !!p.pod, val('c-pod'), 'Not found — enter manually');
  if(onScreen('meter-number-field'))
    extra += row('Meter number', !!p.meter, val('c-meter'), 'Not found — enter manually');

  c.innerHTML = '<div class="ocr-panel"><div class="ocr-head">Pulled from the bill</div>'
    + row('Customer name', !!p.first, val('c-first') && val('c-last'),
          'Not found — enter manually')
    + row('Service address', !!p.street, val('a-street') && val('a-city'),
          'Not found — enter manually')
    + row('Account number', !!p.acct, val('c-acct'), 'Not found — enter manually')
    + extra
    + '</div>';
}

function ocrRow(label, ok, valueText){
  return '<div class="ocr-row"><div class="ocr-left"><div class="ocr-check'+(ok?'':' warn')+'"></div>'+label+'</div><span class="ocr-conf">'+valueText+'</span></div>';
}
async function handleBillUpload(files){
  if(!files.length) return;
  // MULTI-FILE: every selected file is tracked and uploaded as ONE document set.
  // The instant in-browser preview below still reads only the FIRST file, so the
  // UI says so explicitly rather than implying all pages were read. The SERVER
  // extracts across the whole set on upload and its result is authoritative.
  docSetAddFiles('utility_bill', files);
  const f = files[0];
  // SIZE IS CHECKED ON EVERY PAGE, not just the first. Perch's 4 MB limit
  // applies per bill file, so a later oversized page must be caught here rather
  // than failing at submission after the rep has moved on.
  for(let i = 0; i < files.length; i++){
    if(files[i].size > 4 * 1024 * 1024){
      removeBill();
      const errEl=document.getElementById('bill-submit-error');
      errEl.textContent='Utility bills must be under 4 MB per page. "' +
        files[i].name + '" is larger than that — choose a smaller PDF or image.';
      errEl.style.display='';
      return;
    }
  }
  const generation = ++billUploadGeneration;
  billRuntimeFile = f;
  state.bill.fileName = f.name;
  state.bill.documentId = null;
  state.bill.uploadError = null;
  showBillChip(f.name);

  // EVERY PAGE IS UPLOADED, not just the one previewed.
  // This used to call uploadDocumentToDalton(files[0]) - a single file - while
  // docSetAddFiles() above listed all of them on screen. The rep saw four pages
  // and only one ever left the browser, which is how bills reached Perch
  // missing their usage pages. uploadDocumentSet() posts the whole set to
  // /document-sets, which stores one row per page with its page_order.
  //
  // state.bill.documentId stays the FIRST page, so every existing caller is
  // unchanged; the backend expands it to the whole set at submission.
  // The generation guard still prevents a slower, older upload from overwriting
  // the document id for a newer bill.
  billUploadPromise = uploadDocumentSet('utility_bill')
    .then(body => {
      const first = (body && body.files && body.files[0]) || null;
      if(generation === billUploadGeneration){
        if(!first) throw new Error('The bill upload did not return a saved document.');
        state.bill.documentId = first.document_id;
        // SAY SO ONLY NOW. The chip reads "Selected" from the moment the file
        // is picked; "Attached" is written here, when the server has actually
        // stored the page and returned its id. The page list repaints for the
        // same reason - its per-row status is the same fact.
        if(state.bill.fileName) showBillChip(state.bill.fileName, 'Attached');
        renderDocSetList('utility_bill');
      }
      // `extraction` rides along so the form can be prefilled from what the
      // SERVER read off the stored set. Reducing the response to these two
      // keys is deliberate - every existing caller reads only those - and the
      // reading was simply being thrown away here.
      return {document_id: first ? first.document_id : null,
              files: (body && body.files) || [],
              extraction: (body && body.extraction) || null};
    })
    .catch(err => {
      if(generation === billUploadGeneration){
        state.bill.uploadError = err.message;
        // An upload that failed must not keep reading as if it were in
        // progress. The existing error panel still carries the detail.
        if(state.bill.fileName) showBillChip(state.bill.fileName, 'Not uploaded');
      }
      throw err;
    });

  const c = document.getElementById('ocr-container');
  c.innerHTML = '<div class="ocr-panel"><div class="ocr-analyzing"><div class="spinner"></div>Reading the bill…</div></div>';
  try{
    const text = await extractTextFromFile(f);
    // RC-2: a stale generation means a NEWER bill/enrollment now owns the DOM.
    // Bail out of applying results, but never skip the finally{} cleanup below.
    if(generation !== billUploadGeneration) return;
    const parsed = parseUtilityBill(text);
    renderPrefillScope('utility_bill');
    // Same assignments as before; markOcrFilled only flashes the field so the
    // rep can SEE what the bill supplied. Values stay fully editable.
    if(parsed.first){ document.getElementById('c-first').value = parsed.first; markOcrFilled('c-first'); }
    if(parsed.last){ document.getElementById('c-last').value = parsed.last; markOcrFilled('c-last'); }
    if(parsed.acct){ document.getElementById('c-acct').value = parsed.acct; markOcrFilled('c-acct'); }
    if(parsed.street){ document.getElementById('a-street').value = parsed.street; markOcrFilled('a-street'); }
    if(parsed.city){ document.getElementById('a-city').value = parsed.city; markOcrFilled('a-city'); }
    if(parsed.zip){ document.getElementById('a-zip').value = parsed.zip; markOcrFilled('a-zip'); }
    // parsed.amount is still produced by the OCR parser; the field it used to
    // populate was removed because nothing consumed the value.
    syncBillingFromService();
    lastBillParse = parsed;
    renderBillReadPanel();
  } catch(err){
    if(generation !== billUploadGeneration) return;
    c.innerHTML = '<div class="ocr-panel"><div class="ocr-analyzing">Couldn\'t read this file automatically — enter the details below by hand.</div></div>';
  } finally {
    // RC-2: cleanup MUST run on every path, including a stale generation.
    // Previously both guards returned before this, leaving the "Reading the
    // bill…" spinner on screen forever and btn-bill-next stuck disabled.
    if(generation === billUploadGeneration){
      // THE SERVER'S READING OF THE STORED BILL.
      //
      // The block above is the National Grid path and is unchanged: it reads
      // the file in the browser and fills what it recognises. It knows only
      // National Grid's labels, which is why NYSEG, RG&E and Dominion filled
      // nothing.
      //
      // The upload response carries what the SERVER read from the stored set
      // - every page, with the OCR fallback for a scan - and it fills only
      // what is still empty, so National Grid is unaffected and a value the
      // rep typed or corrected is never touched.
      //
      // IT RUNS HERE, IN THE finally, ON PURPOSE. When the in-browser read
      // throws - an unreadable PDF, a page the renderer cannot handle - the
      // catch above has just told the rep to type everything by hand. The
      // server may well have read the bill perfectly, and that is exactly the
      // moment its reading is worth most. Inside the try it would have been
      // skipped on the one path that needed it.
      let serverFilled = 0;
      try{
        serverFilled = applyServerBillFields(await billUploadPromise) || 0;
      }catch(e){ /* the upload's own error path already reports this */ }

      // Still the current bill: clear any spinner we may have left behind and
      // recompute the Continue button from real conditions. A panel written by
      // the server's reading stands; only an unresolved spinner is replaced.
      if(!serverFilled && c && c.innerHTML.indexOf('Reading the bill') !== -1){
        c.innerHTML = '<div class="ocr-panel"><div class="ocr-analyzing">Couldn\'t read this file automatically — enter the details below by hand.</div></div>';
      }
      // Avoid an unhandled rejection before Continue; submitBill surfaces the
      // same save error without discarding OCR results.
      if(billUploadPromise) billUploadPromise.catch(()=>{});
      checkBillReady();
    }
    // Stale generation: the newer owner already reset the DOM via
    // clearWizardForms()/removeBill(). Touching it here would corrupt them.
  }
}
/* Clear the bill the chip is showing.

   TWO DIFFERENT THINGS can be on screen here, and they need different
   handling - conflating them is what made a deletion cosmetic:

     a file the rep just picked  -> nothing is stored yet, so dropping the
                                    pending entries IS the removal
     a file that is STORED       -> it has a row in `documents` attached to a
                                    document set, and clearing the chip would
                                    change nothing on the server

   The second case used to fall through to the same "clear the chip" code, so
   the photo vanished from the screen, Save & Exit wrote nothing about it, and
   the next reopen hydrated it straight back. It now goes through the one real
   removal path. */
async function removeBill(){
  billUploadGeneration += 1;
  // RC-2: clear any in-flight spinner so a discarded OCR cannot leave one stuck.
  const oc = document.getElementById('ocr-container');
  if(oc) oc.innerHTML = '';
  billRuntimeFile = null;
  billUploadPromise = null;
  state.bill.uploadError=null;
  document.getElementById('bill-file-chip').innerHTML='';
  const fileInput = document.getElementById('bill-file');
  if(fileInput) fileInput.value='';

  const set = DocSets.utility_bill;
  const stored = set ? set.files.filter(function(x){ return x.documentId; })[0] : null;
  if(stored){
    // Persisted: delete it for real, and let docSetRemove() repoint
    // state.bill and refresh the list.
    await docSetRemove('utility_bill', stored.key);
    return;
  }

  // Pending only. Drop the entries that were never uploaded, so an oversized
  // page does not stay listed after being rejected.
  if(set) set.files = set.files.filter(function(x){ return !!x.documentId; });
  renderDocSetList('utility_bill');
  state.bill.fileName='';
  state.bill.documentId=null;
  checkBillReady();
}

/* ---------------- LMI DOCUMENT CHECKER ---------------- */
const lmiTypes = [
  {label:'Electric bill showing HEAP/LIHEAP/EAP assistance', sourceType:'proof_doc_liheap', defaultDocType:'utility_bill', dac:false, re:/\bHEAP\b|\bLIHEAP\b|Energy Assistance|\bEAP\b|Energy Affordability Credit|Billing Adjustments/i},
  {label:'SNAP award letter', sourceType:'proof_doc_snap', defaultDocType:'letter', dac:false, re:/SNAP.{0,30}(award|notice|eligib|approv)/i},
  {label:'SNAP card', sourceType:'proof_doc_snap', defaultDocType:'card', dac:false, re:/\bSNAP\b/i},
  {label:'Free/reduced school lunch letter', sourceType:'proof_doc_free_reduced_school_lunch_letter', defaultDocType:'letter', dac:false, re:/free.{0,20}reduced.{0,30}(lunch|meal)|school.{0,20}lunch/i},
  {label:'Housing authority certification / Section 8', sourceType:'proof_doc_section_8', defaultDocType:'', dac:false, re:/Section\s*8|Housing Authority|Tenant Eligibility|\bHUD\b/i},
  {label:'SSI', sourceType:'proof_doc_ssi', defaultDocType:'', dac:false, re:/\bSSI\b|Supplemental Security Income/i},
  {label:'Medicaid award letter', sourceType:'proof_doc_medicaid', defaultDocType:'letter', dac:true, re:/Medicaid|NY State of Health|Essential Plan/i},
  {label:'Lifeline qualification', sourceType:'proof_doc_lifeline_usac', defaultDocType:'', dac:true, re:/\bLifeline\b/i},
  // These legacy Dalton labels do not map cleanly to a published Perch proof
  // source type, so they remain selectable for local review but cannot be sent
  // to Perch until an explicit supported source is chosen.
  {label:'Disability benefits letter', sourceType:null, defaultDocType:'letter', dac:false, re:/Disability Benefits|SSDI/i},
  {label:'SLIP', sourceType:null, defaultDocType:'', dac:true, re:/\bSLIP\b/i},
];
function lmiTypeForLabel(value){
  // The select now carries the Perch source_type directly. Legacy labels are
  // still accepted so any stored value keeps resolving.
  if(!value) return null;
  return lmiTypes.find(x => x.sourceType === value)
      || lmiTypes.find(x => x.label === value)
      || null;
}

/* Program changed: nothing is auto-guessed. Format stays whatever the rep (or
   OCR) set - we never fabricate the second dimension. */
function onLmiProgramChange(){ checkLmiReady(); }
function classifyLmiDoc(rawText){
  const t = cleanText(rawText);
  let matched = null;
  for(const type of lmiTypes){ if(type.re.test(t)){ matched = type; break; } }
  const dateMatch = t.match(/Date Printed\s*:?\s*(\d{1,2}\/\d{1,2}\/\d{2,4})/i)
    || t.match(/\b(\d{1,2}\/\d{1,2}\/\d{2,4})\b/)
    || t.match(/\b([A-Z][a-z]+ \d{1,2},? \d{4})\b/);
  let docDate = null;
  if(dateMatch){ const d = new Date(dateMatch[1]); if(!isNaN(d)) docDate = d; }
  let withinYear = null;
  if(docDate){
    const now = new Date();
    const months = (now.getFullYear()-docDate.getFullYear())*12 + (now.getMonth()-docDate.getMonth());
    withinYear = months <= 12 && months >= -2;
  }
  return {matched, docDate, withinYear};
}
function renderLmiCheckPanel(analysis){
  const c = document.getElementById('lmi-check-container');
  const rows = [];
  rows.push(ocrRow('Document type', !!analysis.matched, analysis.matched ? analysis.matched.label : "Couldn't confidently match an accepted type — review manually"));
  if(analysis.docDate){
    const dateStr = analysis.docDate.toLocaleDateString('en-US',{year:'numeric',month:'short',day:'numeric'});
    rows.push(ocrRow('Document date', analysis.withinYear !== false, analysis.withinYear === false ? (dateStr+' — older than 12 months') : dateStr));
  } else {
    rows.push(ocrRow('Document date', false, "No date found — verify it's within 12 months"));
  }
  let dacNote = '';
  if(analysis.matched && analysis.matched.dac){
    dacNote = ocrRow('DAC requirement', false, 'Also confirm the household is in a NYSERDA Designated Disadvantaged Community');
  }
  const banner = (analysis.matched && analysis.withinYear !== false)
    ? '<div class="badge badge-brand" style="margin-bottom:10px;">Looks like acceptable documentation</div>'
    : '<div class="badge badge-gold" style="margin-bottom:10px;">Needs manual review</div>';
  c.innerHTML = banner + '<div class="ocr-panel"><div class="ocr-head">Automated document check</div>' + rows.join('') + dacNote + '</div>';
}
/* The eligibility document's chip. Same honesty fix as showBillChip(): the
   status line used to read "Uploaded" from the moment the file was chosen. */
function showLmiChip(name, status){
  document.getElementById('lmi-file-chip').innerHTML =
    uploadChipHtml(name, status, 'removeLmi()');
}
const amiTable = [
  {size:1, amount:61750},{size:2, amount:70550},{size:3, amount:79350},{size:4, amount:88150},
  {size:5, amount:95250},{size:6, amount:102300},{size:7, amount:109350},{size:8, amount:116400},
];
/* THE ELIGIBILITY SCREEN MIRRORS PERCH'S next_step EXACTLY.
   ──────────────────────────────────────────────────────────────────────
   One LMI path is on screen at a time, and it is the path Perch named.
   UBS never offers an alternative Perch did not ask for, and never gathers
   data for a path that is not the current one.

       next_step = /lmi/proof_docs       -> proof-document upload only
       next_step = /lmi/self_attestation -> self-attestation only
       next_step = /lmi/self_attestation/accept -> self-attestation only
       next_step = /contracts            -> neither
       anything else / unknown           -> neither, and nothing submittable

   `submits` names the single API action authorised right now. It tracks
   the visible section exactly: a section that is not shown cannot submit,
   and nothing is collected or stored for a path that is not shown - the
   proof-document upload does not exist on the self-attestation step, so no
   document is taken in locally there either.

   This replaces an earlier design in which both sections were open at once
   and documents were gathered ahead of Perch asking for them. That is gone
   deliberately: UBS follows the one path Perch returned.

   NOT keyed to any utility, ZIP or project - only to next_step, so every NY
   enrollment behaves the same way for the same Perch state. */
function lmiSectionsForCurrentStep(){
  const key = perchContext.nextStepKey;
  if(key === 'proof_docs'){
    return {doc:true, attest:false, submits:'proof_docs',
            docHint:null,
            attestHint:null};
  }
  if(key === 'self_attestation' || key === 'self_attestation_accept'){
    // SELF-ATTESTATION ONLY. The proof-document section is not shown and no
    // proof document is collected or stored while this is the step. UBS
    // mirrors the single path Perch returned rather than offering both.
    return {doc:false, attest:true, submits:key,
            docHint:null, attestHint:null};
  }
  return {doc:false, attest:false, submits:null, docHint:null, attestHint:null};
}

/* Applies that decision to the DOM. The legacy doc/attest/N.A. toggle row is
   the mutually-exclusive mechanism itself, so it is never shown: the sections
   are driven by Perch's step, not by a rep picking one. The markup is left in
   place rather than deleted, which keeps this patch to one file. */
/* ONE NAME, ONE SENTENCE, ONE PLACE (C5).
   This step called itself "LMI documentation" in a heading while the stepper
   above it said "Eligibility" - the same step with two names, one of them an
   internal acronym. The copy also lived in three places (the markup default,
   resetWizardState and prepareLmiForPerch), so any change had to be made
   three times or they drifted. It is named once here and read from here.

   The resumed wording said "Perch requires proof documentation for this
   enrollment": true, but it tells a rep whose rule it is rather than what to
   do, and names a vendor the customer never sees. */
const LMI_COPY = {
  title: 'Eligibility',
  lead: 'Confirm whether this household qualifies for the low-income discount '
      + '— with a qualifying document, by income, or not applicable.',
  proofLead: 'A qualifying document is needed for this enrollment. Add it '
      + 'below; you will not be asked for it again.',
};

function applyLmiCopy(lead){
  const t = document.getElementById('lmi-title');
  const l = document.getElementById('lmi-lead');
  if(t) t.textContent = LMI_COPY.title;
  if(l) l.textContent = lead || LMI_COPY.lead;
}

function renderLmiSections(){
  const s = lmiSectionsForCurrentStep();
  ['lmi-mode-doc','lmi-mode-attest','lmi-mode-na'].forEach(function(id){
    const el = document.getElementById(id);
    if(el) el.style.display = 'none';
  });
  const legacyAttest = document.getElementById('lmi-attest-panel');
  if(legacyAttest) legacyAttest.style.display = 'none';

  const proofPanel = document.getElementById('lmi-proof-panel');
  if(proofPanel) proofPanel.style.display = s.doc ? '' : 'none';
  const docPanel = document.getElementById('lmi-doc-panel');
  if(docPanel) docPanel.style.display = s.doc ? 'block' : 'none';
  const saPanel = document.getElementById('sa-panel');
  if(saPanel) saPanel.style.display = s.attest ? '' : 'none';

  const hint = document.getElementById('lmi-doc-hint');
  if(hint){
    hint.textContent = s.docHint || '';
    hint.style.display = s.docHint ? 'block' : 'none';
  }
  checkLmiReady();
  return s;
}

/* Retained for the existing handler surface. It no longer switches anything
   exclusive - it records which summary wording to use and re-renders from
   Perch's step. */
function setLmiMode(mode){
  state.lmi.mode = mode;
  renderLmiSections();
}
function prepareLmiForPerch(){
  perchContext.nextStepKey='proof_docs';
  state.lmi.mode='doc';
  applyLmiCopy(LMI_COPY.proofLead);
  state.lmi.mode='doc';
  renderLmiSections();
  if(!state.lmi.nameOnDocument) state.lmi.nameOnDocument=(state.customer.first+' '+state.customer.last).trim();
  document.getElementById('lmi-name-on-doc').value=state.lmi.nameOnDocument;
  document.getElementById('lmi-relationship').value=state.lmi.relationship || 'self';
  document.getElementById('lmi-format').value=state.lmi.documentFormat || '';
  document.getElementById('lmi-submit-error').style.display='none';
  checkLmiReady();
}
function updateAmiThreshold(){
  const sizeVal = document.getElementById('lmi-household-size').value;
  state.lmi.householdSize = sizeVal;
  const row = amiTable.find(r=>String(r.size)===sizeVal);
  document.getElementById('ami-threshold-display').textContent = row ? ('80% State Median Income for '+row.size+' '+(row.size===1?'person':'people')+': $'+row.amount.toLocaleString()) : '';
  checkLmiReady();
}
function setIncomeAnswer(isBelow){
  state.lmi.incomeBelow = isBelow;
  document.getElementById('ami-below').classList.toggle('selected', isBelow===true);
  document.getElementById('ami-above').classList.toggle('selected', isBelow===false);
  checkLmiReady();
}
async function handleLmiUpload(files){
  if(!files.length) return;
  // Nothing is collected for a path Perch did not ask for. Guarded here, not
  // only in the markup, because drag-and-drop reaches this without the control.
  if(!lmiSectionsForCurrentStep().doc) return;
  // MULTI-FILE: front/back or multi-page proofs form ONE document set.
  docSetAddFiles('lmi_document', files);
  const f = files[0];

  // SIZE IS CHECKED ON EVERY FILE, not just the first.
  //
  // Perch's 4 MB limit applies per proof file, so a later oversized page has
  // to be caught here rather than failing at submission after the rep has
  // moved on. This mirrors handleBillUpload, which was corrected the same way.
  for(let i = 0; i < files.length; i++){
    if(files[i].size > 4 * 1024 * 1024){
      removeLmi();
      const errEl=document.getElementById('lmi-submit-error');
      errEl.textContent='Proof documents must be under 4 MB per file. "' +
        files[i].name + '" is larger than that — choose a smaller PDF or image.';
      errEl.style.display='';
      return;
    }
  }
  const generation = ++lmiUploadGeneration;
  lmiRuntimeFile=f;
  state.lmi.fileName = f.name;
  state.lmi.documentId=null;
  state.lmi.uploadError=null;
  showLmiChip(f.name);

  // EVERY SELECTED FILE IS UPLOADED, not just the one previewed.
  //
  // THE BUG THIS FIXES: this called uploadDocumentToDalton(files[0]) - the
  // single-file route - while docSetAddFiles() above had already listed all of
  // them on screen. A rep who picked three pages of an award letter saw three
  // rows and one file left the browser; the other two never existed on the
  // server, and the list showed them as "Uploading" indefinitely. The bill
  // flow had precisely this defect and was fixed by routing through
  // uploadDocumentSet(); the proof flow was simply never moved across.
  //
  // state.lmi.documentId stays the FIRST file, so every existing caller is
  // unchanged - lmi_qualifications.document_id, the /lmi/proof_docs request
  // and checkLmiReady() all still name one document. The server expands that
  // id to the whole set when it submits, which is why no page is dropped.
  lmiUploadPromise = uploadDocumentSet('lmi_document')
    .then(body => {
      const first = (body && body.files && body.files[0]) || null;
      if(generation === lmiUploadGeneration){
        if(!first) throw new Error('The proof upload did not return a saved document.');
        state.lmi.documentId = first.document_id;
        // Only now, with the server's document id in hand.
        if(state.lmi.fileName) showLmiChip(state.lmi.fileName, 'Attached');
        renderDocSetList('lmi_document');
        renderPrefillScope('lmi_document');
      }
      return {document_id: first ? first.document_id : null,
              files: (body && body.files) || []};
    })
    .catch(err => {
      if(generation === lmiUploadGeneration){
        state.lmi.uploadError=err.message;
        if(state.lmi.fileName) showLmiChip(state.lmi.fileName, 'Not uploaded');
      }
      throw err;
    });
  const c = document.getElementById('lmi-check-container');
  c.innerHTML = '<div class="ocr-panel"><div class="ocr-analyzing"><div class="spinner"></div>Reading the document…</div></div>';
  try{
    const text = await extractTextFromFile(f);
    if(generation !== lmiUploadGeneration) return;
    const analysis = classifyLmiDoc(text);
    if(analysis.matched){
      // Populate the two dimensions SEPARATELY from what classifyLmiDoc matched.
      // The classifier is unchanged; only the fields it fills are. A program
      // with no reliable default format leaves Format for the rep - the missing
      // dimension is never fabricated.
      if(analysis.matched.sourceType){
        document.getElementById('lmi-doctype').value = analysis.matched.sourceType;
        markOcrFilled('lmi-doctype');
      }
      if(analysis.matched.defaultDocType){
        document.getElementById('lmi-format').value = analysis.matched.defaultDocType;
        markOcrFilled('lmi-format');
      }
      state.lmi.docType=analysis.matched.label;
      if(analysis.matched.defaultDocType){
        document.getElementById('lmi-format').value=analysis.matched.defaultDocType;
        state.lmi.documentFormat=analysis.matched.defaultDocType;
      }
    }
    renderLmiCheckPanel(analysis);
  } catch(err){
    if(generation !== lmiUploadGeneration) return;
    c.innerHTML = '<div class="ocr-panel"><div class="ocr-analyzing">Couldn\'t read this file automatically — select the document type manually.</div></div>';
  }
  lmiUploadPromise.catch(()=>{});
  checkLmiReady();
}
/* Remove the proof document - and make it STICK, like removeBill().

   THE BUG THIS FIXES: this cleared the chip and the local state and stopped
   there. For a file the rep had just picked that is right, but for one already
   STORED it was cosmetic: the documents row stayed, so the next reopen brought
   it back. removeBill() was corrected for exactly this and routes a stored
   file through docSetRemove(); removeLmi() was left behind. */
async function removeLmi(){
  lmiUploadGeneration += 1;
  lmiRuntimeFile=null; lmiUploadPromise=null;
  state.lmi.uploadError=null;
  document.getElementById('lmi-file-chip').innerHTML='';
  document.getElementById('lmi-check-container').innerHTML='';
  const fileInput = document.getElementById('lmi-file');
  if(fileInput) fileInput.value='';

  const set = DocSets.lmi_document;
  const stored = set ? set.files.filter(function(x){ return x.documentId; })[0] : null;
  if(stored){
    // Persisted: delete it for real, and let docSetRemove() repoint
    // state.lmi and refresh the list.
    await docSetRemove('lmi_document', stored.key);
    renderPrefillScope('lmi_document');
    checkLmiReady();
    return;
  }

  // Pending only. Drop the entries that were never uploaded, so an oversized
  // file does not stay listed after being rejected.
  if(set) set.files = set.files.filter(function(x){ return !!x.documentId; });
  renderDocSetList('lmi_document');
  renderPrefillScope('lmi_document');
  state.lmi.fileName=''; state.lmi.documentId=null;
  checkLmiReady();
}
function checkLmiReady(){
  state.lmi.docType = document.getElementById('lmi-doctype').value;
  state.lmi.nameOnDocument = document.getElementById('lmi-name-on-doc').value.trim();
  state.lmi.relationship = document.getElementById('lmi-relationship').value;
  state.lmi.documentFormat = document.getElementById('lmi-format').value;
  // #btn-lmi-next submits PROOF DOCUMENTS and nothing else. It is armed only
  // when Perch currently authorises that action - so while self-attestation is
  // the authorised step the rep can still gather and upload documents (local
  // storage, no Perch call) but cannot send the wrong thing. The
  // self-attestation section has its own #sa-submit, gated separately by
  // updateSelfAttestationReady().
  const sections = lmiSectionsForCurrentStep();
  let ready = false;
  if(sections.submits === 'proof_docs'){
    const type=lmiTypeForLabel(state.lmi.docType);
    ready=!!(type && type.sourceType && state.lmi.fileName && state.lmi.nameOnDocument && state.lmi.relationship && state.lmi.documentFormat);
  }
  document.getElementById('btn-lmi-next').disabled = !ready;
}
async function submitLmi(){
  const errEl=document.getElementById('lmi-submit-error');
  errEl.style.display='none';
  const btn=document.getElementById('btn-lmi-next');
  if(btn.disabled) return;
  if(perchContext.proofSubmitted){
    await generateContractsAndOpenAgreement(4);
    return;
  }
  // AUTHORIZATION GATE. Perch decides which LMI action may be sent right now.
  // This used to be a mode check, which meant the DOCUMENT validation below ran
  // while the rep was completing self-attestation and raised a document error
  // against an empty #lmi-doctype. Nothing here forces an endpoint Perch has
  // not asked for.
  const sections=lmiSectionsForCurrentStep();
  if(sections.submits !== 'proof_docs'){
    errEl.textContent = sections.submits
      ? 'This enrollment needs the income self-attestation. '
        + 'Complete that section to continue - the documents you upload here stay attached.'
      : 'This enrollment is not on an income-qualification step, so there is '
        + 'nothing to submit here. Use Back to return to the current step.';
    errEl.style.display='';
    return;
  }
  const type=lmiTypeForLabel(document.getElementById('lmi-doctype').value);
  if(!type || !type.sourceType){
    errEl.textContent='That document type is not one we can submit. Choose a supported proof document.';
    errEl.style.display=''; return;
  }
  state.lmi.docType=document.getElementById('lmi-doctype').value;
  state.lmi.nameOnDocument=document.getElementById('lmi-name-on-doc').value.trim();
  state.lmi.relationship=document.getElementById('lmi-relationship').value;
  state.lmi.documentFormat=document.getElementById('lmi-format').value;
  btn.disabled=true; btn.textContent='Submitting…';
  try{
    if(lmiUploadPromise) await lmiUploadPromise;
    if(!state.lmi.documentId) throw new Error('The LMI document has not finished saving. Please try again.');
    await apiFetch('/api/enrollments/' + currentDraft.enrollment_id + '/lmi', {
      method:'POST', body:JSON.stringify({path:'document',qualification_type:state.lmi.docType,document_id:state.lmi.documentId})
    });
    const body=await apiFetch('/api/perch/enrollments/' + currentDraft.enrollment_id + '/lmi/proof_docs', {
      method:'POST', body:JSON.stringify({document_id:state.lmi.documentId,source_type:type.sourceType,
        name_on_document:state.lmi.nameOnDocument,relationship:state.lmi.relationship,document_type:state.lmi.documentFormat})
    });
    perchContext.proofSubmitted=true;
    perchContext.nextStepKey=body.next_step_key;
    // RECONCILE ON WHATEVER PERCH RETURNS. Previously anything other than
    // 'contracts' threw. If Perch advances proof docs to a self-attestation
    // step, that is a legitimate answer: route there, keep the uploaded
    // documents attached, and reveal the self-attestation section. `reconciled`
    // is set when the server reconciled an already-completed step via /status.
    if(body.reconciled || (perchContext.nextStepKey
        && perchContext.nextStepKey !== 'contracts')){
      await continueFromPerchNextStep('lmi');
      return;
    }
    if(perchContext.nextStepKey!=='contracts') throw new Error('The proof document was accepted, but this enrollment could not be advanced automatically. We stopped rather than guessing - please let an admin know.');
    await generateContractsAndOpenAgreement(4);
  }catch(err){
    errEl.textContent=userMessage(err); errEl.style.display='';
  }finally{
    btn.textContent='Continue'; checkLmiReady();
  }
}

function fillReview(){
  const set = (id, txt) => { const el = document.getElementById(id); if(el) el.textContent = txt; };
  const detail = currentEnrollmentDetail || {};

  set('rv-name', [state.customer.first, state.customer.last].filter(Boolean).join(' ') || '—');
  set('rv-email', state.customer.email || perchContext.email || '—');
  set('rv-phone', formatPhone(state.customer.phone) || '—');
  set('rv-utility', state.project.utility || perchContext.utilityDisplay
                    || perchContext.utilitySlug || '—');

  // Address on two lines, as the mockup shows it.
  const addrEl = document.getElementById('rv-address');
  if(addrEl){
    const line1 = state.address.street + (state.address.unit ? ', ' + state.address.unit : '');
    const st = state.address.state || 'NY';
    addrEl.innerHTML = esc(line1) + '<br>' +
      esc([state.address.city, st].filter(Boolean).join(', ') + ' ' + (state.address.zip || '')).trim();
  }
  set('rv-acct', maskAccount(state.customer.acct));
  set('rv-code', (currentDraft && currentDraft.enrollment_code) || detail.enrollment_code || '');

  // PROGRAM + SAVINGS via the ONE resolver, so live, resumed and completed
  // enrollments all read the same way.
  const view = resolveProgramView();
  const isLmi = view.isLmi;
  set('rv-program', view.label || '—');
  const savEl = document.getElementById('rv-savings');
  if(savEl){
    savEl.innerHTML = view.percent === null ? ''
      : '<strong>' + esc(String(view.percent)) + '%</strong><span>savings</span>';
  }

  // LMI PROOF as "<Program> · <Document format>". The uploaded FILENAME is
  // deliberately not shown - the original document is still stored and
  // submitted exactly as before; this is presentation only.
  const lmiRow = document.getElementById('rv-lmi-row');
  if(perchContext.proofSubmitted && isLmi){
    const prg = lmiTypeForLabel(state.lmi.docType);
    const program = prg ? programDisplayName(prg) : (state.lmi.docType || '');
    const format = formatDisplayName(state.lmi.documentFormat);
    const text = [program, format].filter(Boolean).join(' · ');
    set('rv-lmi', text || 'Eligibility document submitted');
    if(lmiRow) lmiRow.style.display = text ? '' : 'none';
  } else if(lmiRow){
    lmiRow.style.display = 'none';
  }

  // "Ready to complete" appears only once the packet is actually reviewable.
  //
  // ONE OWNER. A read-only enrollment's pill belongs to
  // applyFinalStepFraming(), which shows "Complete" for a terminal enrollment
  // and hides it for a blocked one. fillReview() still runs on that page (via
  // goStep(5) and the detail refresh), and unconditionally re-showing the pill
  // here is how a BLOCKED enrollment would end up wearing a completion chip.
  const pill = document.getElementById('agr-ready-pill');
  if(pill && !finalStepFramed){
    pill.hidden = !(Agreements && Agreements.contracts && Agreements.contracts.length);
  }
}

/* Display-only phone formatting. The stored value is never altered. */
function formatPhone(v){
  const d = String(v || '').replace(/\D/g, '');
  if(d.length === 10) return '(' + d.slice(0,3) + ') ' + d.slice(3,6) + '-' + d.slice(6);
  if(d.length === 11 && d[0] === '1') return '(' + d.slice(1,4) + ') ' + d.slice(4,7) + '-' + d.slice(7);
  return String(v || '');
}

/* Masked account: last four only, matching the mockup's treatment. */
/* A DISPLAY MASK IS NOT DATA.

   The enrollment detail is serialised to a rep with account_number masked
   ("******7890"). This form hydrates from that payload, so the mask landed in
   the editable account field and the next save sent it straight back - Save &
   Exit, reopen, Continue overwrote the real account number with the mask.
   The write is now refused server-side as well; this keeps the browser from
   attempting it, and keeps a masked value from failing the market's account
   pattern and dead-ending Continue on a form the rep never touched. */
function isMaskedValue(v){ return String(v == null ? '' : v).indexOf('*') !== -1; }

function maskAccount(acct){
  const v = String(acct || '').trim();
  if(!v) return '—';
  if(v.length <= 4) return v;
  return '•'.repeat(Math.min(v.length - 4, 8)) + v.slice(-4);
}

/* Rep-facing program name for an LMI proof type. The Perch source_type stays
   authoritative underneath; only the label is friendly. */
function programDisplayName(type){
  const MAP = {
    proof_doc_snap: 'SNAP',
    proof_doc_medicaid: 'Medicaid',
    proof_doc_liheap: 'HEAP / EAP / LIHEAP',
    proof_doc_section_8: 'Section 8 / Housing',
    proof_doc_ssi: 'Supplemental Security Income (SSI)',
    proof_doc_lifeline_usac: 'Lifeline',
    proof_doc_free_reduced_school_lunch_letter: 'Free / reduced school lunch',
  };
  if(!type) return '';
  return MAP[type.sourceType] || type.label || '';
}

/* Document format label from the stored Perch document_type value. */
function formatDisplayName(value){
  const MAP = {card:'Card', letter:'Award letter', account_statement:'Account statement',
               utility_bill:'Utility bill', voucher:'Voucher', other:'Other', unknown:''};
  if(!value) return '';
  return MAP[value] || value;
}

async function generateContractsAndOpenAgreement(backStep){
  const sourceErr = backStep===4 ? document.getElementById('lmi-submit-error') : document.getElementById('contact-submit-error');
  if(sourceErr) sourceErr.style.display='none';
  if(!perchContext.contractsGenerated){
    try{
      const body=await apiFetch('/api/perch/enrollments/' + currentDraft.enrollment_id + '/contracts', {method:'POST', body:JSON.stringify({})});
      perchContracts=body.contracts || [];
      perchContext.contractsGenerated=true;
      perchContext.contractsNextStepKey=body.next_step_key;
      perchContext.acceptanceEnabled = body.acceptance_enabled === true;
    }catch(err){
      if(sourceErr){ sourceErr.textContent=userMessage(err); sourceErr.style.display='block'; }
      throw err;
    }
  }
  perchContext.agreementBackStep=backStep || 2;
  goStep(5);
  mountRepAgreements(false, false);
}
function backFromAgreement(){
  // Step 3 is gone; fall back through the ACTIVE sequence so Residential
  // returns to Customer & Bill and LMI returns to Eligibility.
  const target = perchContext.agreementBackStep;
  goStep(activeSteps().indexOf(target) === -1 ? prevStepBefore(5) : target);
}
function todayStr(){ return new Date().toISOString().slice(0,10); }
// REMOVED: the legacy/mock customer contract engine.
//
// This used to render a hardcoded local document packet with per-document
// checkboxes, a drawn-signature canvas and a confetti completion screen, and
// "completing" it updated NOTHING in Perch - a customer could finish the whole
// flow while the real enrollment stayed at contracts_review.
//
// Customers now use the SAME Perch-backed engine as reps
// (openCustomerContracts -> POST /contracts, /contracts/review, /contracts/accept).
// Kept as a hard failure rather than deleted outright so any surviving caller
// is caught loudly instead of silently reopening the mock flow.
/* ---------------- REAL DOCUMENT PACKET (built from Perch/Solstice NY contract templates) ---------------- */
const docPacket = [
  {
    id:'subscription',
    title:'Community Solar Subscription Agreement',
    sub:'Solstice Power Technologies LLC, a Perch Energy company',
    body:(ctx)=>
      '<p>This agreement is between you (&ldquo;Subscriber&rdquo;) and Solstice Power Technologies LLC, a Perch Energy company (&ldquo;Service Provider&rdquo;), covering your community solar subscription through '+ctx.utility+'.</p>'+
      '<p><strong>Key terms</strong></p>'+
      '<ul>'+
        '<li>Service Provider assigns you to an eligible solar project and sets your subscription size based on your historical usage, up to 100% of your annual usage.</li>'+
        '<li>You receive monthly bill credits from '+ctx.utility+' based on your share of the project\'s generation, plus a statement showing your credits and savings.</li>'+
        '<li>Initial term of one year, automatically renewing for one-year terms unless either party gives written notice.</li>'+
        '<li>You may cancel anytime with 90 days\' written notice, with no termination fee.</li>'+
        '<li>Service Provider may terminate with written notice if you no longer meet program eligibility requirements.</li>'+
      '</ul>'+
      '<p>The full agreement (eligibility, payment for credits, dispute resolution, and general provisions) is available from your rep on request.</p>'
  },
  {
    id:'disclosure',
    title:'Community Distributed Generation Disclosure Form',
    sub:'Your specific subscription terms',
    body:(ctx)=>
      '<p><strong>Prepared for:</strong> '+ctx.custName+'<br><strong>Service address:</strong> '+ctx.address+'<br><strong>Utility:</strong> '+ctx.utility+'</p>'+
      '<p><strong>Subscription fee and savings rate</strong> — Your subscription fee is taken automatically from the bill credits you receive. After the fee, you receive savings equal to <strong>'+ctx.savingsPct+'%</strong> of the credits you receive each month. Example: if your credits are $100 for a month, your savings that month would be $'+ctx.exampleSavings+'. You will not be charged any other fees.</p>'+
      '<p><strong>Guarantees</strong> — You\'re guaranteed savings equal to '+ctx.savingsPct+'% of the credits you receive. This doesn\'t guarantee your total utility bill will go up or down in a given month, and doesn\'t guarantee a minimum level of system production.</p>'+
      '<p><strong>Right to cancel</strong> — You can cancel without penalty within 3 business days of signing, and after that with 90 days\' written notice and no termination fee.</p>'+
      '<p><strong>Customer rights</strong> — For unresolved complaints, contact the NY Department of Public Service Helpline at 1-800-342-3377, or file at dps.ny.gov/complaints.html.</p>'
  },
  {
    id:'income',
    title:'Household Income Survey',
    sub:'Determines eligibility for the low-income adder',
    body:(ctx)=> ctx.lmiSummary
  },
  {
    id:'esign',
    title:'ESIGN Consent Disclosure',
    sub:'Getting your documents electronically',
    body:()=>
      '<p>By signing electronically, you agree to receive required notices and disclosures electronically instead of on paper. You can request paper copies or withdraw this consent anytime by emailing customercare@perchenergy.com.</p>'
  },
  {
    id:'consents',
    title:'Credit Check & Contact Consent',
    sub:'Soft credit pull, phone, and text consent',
    body:()=>
      '<p><strong>Credit check</strong> — You authorize a soft credit pull (does not affect your credit score) to help determine program eligibility.</p>'+
      '<p><strong>Phone &amp; text</strong> — You authorize contact by phone, text, or automated dialing about this application. Consent isn\'t required to purchase, and message/data rates may apply.</p>'
  },
  {
    id:'terms',
    title:'Terms & Conditions and Privacy Policy',
    sub:'Standard website and service terms',
    body:()=>
      '<p>Covers acceptable use, intellectual property, disclaimers of warranty, limitation of liability, and how your personal information is collected, used, and protected.</p>'
  }
];
/* Reports what this enrollment ACTUALLY holds, not which tab was last open.
   Both parts can appear together, because both kinds of state can coexist. */
function buildLmiSummary(){
  const parts = [];
  if(state.lmi.fileName || state.lmi.documentId){
    parts.push('<p>You provided documentation (<strong>'+(state.lmi.docType || 'a qualifying document')+'</strong>) to support eligibility for the low-income program adder. Your rep will confirm this meets NY program requirements.</p>');
  }
  const occ = selfAttestation && selfAttestation.occupancy;
  const choice = selfAttestation && selfAttestation.choice;
  if(occ && choice){
    parts.push('<p>You self-attested that your household of <strong>'+occ+'</strong> has income <strong>'+(choice === 'below' ? 'below' : 'above')+'</strong> 80% of the State Median Income for that household size.</p>'+
      '<p style="font-size:var(--t-label);">1: $61,750 &nbsp;2: $70,550 &nbsp;3: $79,350 &nbsp;4: $88,150 &nbsp;5: $95,250 &nbsp;6: $102,300 &nbsp;7: $109,350 &nbsp;8: $116,400</p>'+
      '<p>This information is collected by Arcadia and shared with NYSERDA for program evaluation and incentive determination. It will not be shared or published at the individual customer level.</p>');
  } else if(state.lmi.mode === 'attest' && state.lmi.householdSize){
    // Legacy local attestation, kept so an older enrollment still reads back.
    parts.push('<p>You self-attested that your household of <strong>'+state.lmi.householdSize+'</strong> has income <strong>'+(state.lmi.incomeBelow ? 'below' : 'above')+'</strong> 80% of the State Median Income for that household size.</p>');
  }
  if(!parts.length) return '<p>No low-income program documentation was submitted for this enrollment.</p>';
  return parts.join('');
}
function completeReturnToDashboard(){
  document.getElementById('screen-complete').classList.remove('active');
  document.getElementById('app-shell').classList.add('active');
  document.getElementById('complete-cta').style.display='inline-flex';
  document.getElementById('complete-copy').innerHTML = 'Signed and sent to QA. The rep dashboard will show this as <strong>Customer - Verified</strong> once it clears review.';
  resetWizardState();
  showView('dashboard');
}

function resetAll(){
  AuthStore.clear();
  currentUser = null;
  document.getElementById('app-shell').classList.remove('active');
  document.getElementById('screen-customer-portal').classList.remove('active');
  document.getElementById('login-email').value='';
  document.getElementById('login-pass').value='';
  document.getElementById('login-error').style.display='none';
  resetWizardState();
  showScreen('screen-login');
}


// ─────────────── Password visibility toggle ───────────────
// Toggles ONLY the input type. Never logs, stores, or transmits the value.
function togglePasswordVisibility(inputId, btnId){
  const input = document.getElementById(inputId);
  const btn = document.getElementById(btnId);
  if(!input || !btn) return;
  const showing = input.type === 'text';
  input.type = showing ? 'password' : 'text';
  btn.setAttribute('aria-pressed', String(!showing));
  btn.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
  btn.title = showing ? 'Show password' : 'Hide password';
  btn.textContent = showing ? '\u{1F441}' : '\u{1F441}\u{200D}\u{1F5E8}';
}


/* ==================== CUSTOMER CONTRACT FLOW ====================
   Uses the SAME Perch-backed endpoints the rep flow uses:
     POST /api/perch/enrollments/:id/contracts          (packet + fresh URLs)
     POST /api/perch/enrollments/:id/contracts/review   (one-time capability)
     POST /api/perch/enrollments/:id/contracts/accept   (real acceptance)
   There is deliberately no second contract implementation. Presigned URLs are
   never stored client-side - Review mints a short-lived capability each time. */

let customerEnrollmentId = null;
let customerContracts = [];

async function customerApi(path, opts){
  opts = opts || {};
  const headers = Object.assign({}, opts.headers || {},
    {Authorization: 'Bearer ' + CustomerAuth.getToken()});
  if(opts.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, Object.assign({}, opts, {headers}));
  let data = null;
  try { data = await res.json(); } catch(e){}
  if(res.status === 401){
    // Session rejected. Clear the CUSTOMER token only - the rep session is a
    // separate store and is never touched here - then send them to the ONE
    // unified sign-in screen. They must authenticate again; the backend decides
    // account_type and destination on the next /api/auth/signin.
    CustomerAuth.clear();
    customerEnrollmentId = null;
    activateScreen('screen-login');
    throw new Error('Your session expired — please sign in again.');
  }
  if(!res.ok) throw new Error((data && data.error) || ('Request failed (' + res.status + ')'));
  return data;
}
/* ═══════════════════ AGREEMENT REVIEW — ONE SHARED COMPONENT ═══════════════════
   Rep and customer render the SAME component and call the SAME Perch-backed
   routes. The only difference is which bearer token apiFetch attaches and which
   host element it mounts into - there is deliberately no second implementation.

     POST /api/perch/enrollments/:id/contracts          packet (fresh URLs, server-side)
     POST /api/perch/enrollments/:id/contracts/review   one-time capability URL
     POST /api/perch/enrollments/:id/contracts/accept   the ONLY state change

   Perch presigned URLs never reach this file. Review mints a short-lived
   single-use Dalton URL each time it is clicked.                              */

const Agreements = {
  actor: 'rep',          // 'rep' | 'customer'
  enrollmentId: null,
  contracts: [],
  acceptanceEnabled: false,
  submitted: false,
  readOnly: false,
  inFlight: false,
  opened: {},            // index -> true, for "Opened" hints only. NOT gating.
  summary: null,
  // THE AUTHORITATIVE WORKFLOW KEY for a read-only enrollment, straight from
  // the backend (contracts_accepted, contracts_accept_uncertain,
  // enroll_outcome_uncertain, lmi_not_qualified). The read-only card is
  // rendered FROM this, so a blocked enrollment can never be described as
  // complete. Null during a live enrollment, where readOnly is false anyway.
  statusKey: null,
};

/* What a READ-ONLY enrollment honestly says about itself.

   WHY THIS EXISTS. renderAgreementCard()'s read-only branch was reached by
   BOTH terminal and blocked enrollments and hardcoded "Your enrollment is
   complete ... Nothing further is needed." So reopening an enrollment whose
   acceptance Perch could NOT confirm told the rep it was finished - the exact
   inversion of the truth, and the reason this is driven by the key instead.

   Every key here is a real UBS/Perch workflow key from workflow.py. Nothing
   below invents a status, and an unrecognised key falls back to the backend's
   own step_label rather than to a guess. */
const READ_ONLY_STATUS_COPY = {
  contracts_accepted: {
    eyebrow: 'Complete',
    repHeading: 'Enrollment complete',
    customerHeading: 'Your enrollment is complete',
    lead: 'The agreements were accepted and submitted. Nothing further is needed.',
    tone: 'ok',
  },
  contracts_accept_uncertain: {
    eyebrow: 'Needs review',
    repHeading: 'Acceptance could not be confirmed',
    customerHeading: 'We could not confirm your acceptance',
    lead: 'The agreement acceptance was submitted but Perch did not confirm it. ' +
          'Check this enrollment with Perch before taking further action - do not ' +
          'assume it completed, and do not re-submit.',
    tone: 'warn',
  },
  enroll_outcome_uncertain: {
    eyebrow: 'Needs review',
    repHeading: 'Enrollment outcome uncertain',
    customerHeading: 'Your enrollment is being checked',
    lead: 'The enrollment was sent to Perch but the outcome could not be ' +
          'confirmed. Check its status with Perch before taking further action.',
    tone: 'warn',
  },
  lmi_not_qualified: {
    eyebrow: 'Not eligible',
    repHeading: 'Does not qualify for the available program',
    customerHeading: 'No program is available for this household',
    // Deliberately does NOT say "check with Perch": this outcome is recorded
    // locally precisely BECAUSE the market publishes no status field in which
    // Perch could hold it, and Perch was never called.
    lead: 'The household income answer is above the limit published for this ' +
          'market, so no program is available. This outcome was recorded in UBS ' +
          'and was not sent to Perch.',
    tone: 'warn',
  },
};

/* The copy for a read-only enrollment, or null when its key is unknown.
   Callers fall back to the backend's step_label, which is always present. */
function readOnlyStatusCopy(statusKey){
  return READ_ONLY_STATUS_COPY[statusKey] || null;
}

function agrToken(){
  return Agreements.actor === 'customer'
    ? (typeof CustomerAuth !== 'undefined' ? CustomerAuth.getToken() : null)
    : (typeof AuthStore !== 'undefined' ? AuthStore.getToken() : null);
}

async function agrFetch(path, opts){
  const o = Object.assign({headers:{}}, opts || {});
  o.headers = Object.assign({'Content-Type':'application/json',
                             'Authorization':'Bearer ' + agrToken()}, o.headers || {});
  const res = await fetch(path, o);
  let body = null;
  try { body = await res.json(); } catch(e){ body = null; }
  if(!res.ok){
    throw new Error((body && (body.error || body.detail)) || ('Request failed (' + res.status + ')'));
  }
  return body || {};
}

function agrHost(){
  return document.getElementById(
    Agreements.actor === 'customer' ? 'agr-host-customer' : 'agr-host-rep');
}

function agrEsc(v){ return (typeof esc === 'function') ? esc(v == null ? '' : v) : String(v == null ? '' : v); }

/* ── Mount ─────────────────────────────────────────────────────────────── */

function mountAgreements(opts){
  Agreements.actor = opts.actor || 'rep';
  Agreements.enrollmentId = opts.enrollmentId;
  Agreements.contracts = opts.contracts || [];
  Agreements.acceptanceEnabled = opts.acceptanceEnabled === true;
  Agreements.readOnly = opts.readOnly === true;
  Agreements.submitted = opts.submitted === true;
  Agreements.summary = opts.summary || null;
  // Carried through every mount so the read-only card describes the REAL
  // workflow state. Absent on a live mount, which never reaches that branch.
  Agreements.statusKey = opts.statusKey || null;
  Agreements.statusLabel = opts.statusLabel || null;
  Agreements.opened = {};
  renderAgreementCard();
}
/* ── Overlay ───────────────────────────────────────────────────────────── */


function agrEscKey(e){ if(e.key === 'Escape') closeAgreements(); }
function agrBackdrop(e){ if(e.target && e.target.id === 'agr-overlay') closeAgreements(); }
/* Opening documents NEVER accepts. Only the checkbox + button below does. */
/* "street, unit, city, ST zip" from whichever address object is to hand.

   THE STATE COMES FROM THE ADDRESS. Both summary builders used to append a
   hardcoded ', NY ' before the ZIP, so a Virginia enrollment's agreement card
   read "...,ding Fredericksburg, NY 22401". The address row has carried its own
   state since the enroll-payload fix; this reads it. 'NY' survives only as the
   fallback for a legacy row that never stored one, matching the default the
   wizard's own state object uses. */
function agrAddressLine(a){
  const addr = a || {};
  const line = [addr.street, addr.unit, addr.city].filter(Boolean).join(', ');
  if(!line) return '';
  const st = addr.state || 'NY';
  const tail = [st, addr.zip].filter(Boolean).join(' ');
  return tail ? (line + ', ' + tail) : line;
}

function agrSummaryFromState(){
  const detail = currentEnrollmentDetail || {};
  return {
    customerName: [state.customer.first, state.customer.last].filter(Boolean).join(' '),
    // ONE display-name chain, same order as every other site: the name chosen
    // at Availability, the resolver's name hydrated on reopen, then the raw
    // slug only as a last resort.
    utility: (state.project && state.project.utility) || perchContext.utilityDisplay
             || perchContext.utilitySlug || '',
    serviceAddress: agrAddressLine(state.address),
    savings: formatSavings(detail.program_savings),
    programType: formatProgramType(detail.program_savings, detail.selected_customer_type),
    enrollmentCode: (currentDraft && currentDraft.enrollment_code) || detail.enrollment_code || '',
  };
}

function agrSummaryFromEnrollment(e){
  return {
    customerName: e.customer ? [e.customer.first_name, e.customer.last_name].filter(Boolean).join(' ') : '',
    email: (e.customer && e.customer.email) || e.perch_token_email || '',
    // e.utility_name is the RESOLVED display name; utility_accounts.utility_name
    // holds the raw Perch SLUG, which is why a completed enrollment's card used
    // to read "Electric utility: dominion-energy-va".
    utility: e.utility_name || (e.utility_account ? e.utility_account.utility_name : ''),
    serviceAddress: agrAddressLine(e.service_address),
    savings: formatSavings(e.program_savings),
    programType: formatProgramType(e.program_savings, e.selected_customer_type),
    enrollmentCode: e.enrollment_code || '',
    // Real persisted values, formatted for the viewer's timezone.
    createdAt: formatTimestamp(e.created_at),
    updatedAt: formatTimestamp(e.updated_at),
    // WHEN THE STATUS WAS RECORDED, from perch_workflow_state.updated_at.
    // Shown on the read-only card only, and labelled as what it is - UBS
    // recording the step - never as a Perch completion time, which Perch does
    // not give us.
    // Guarded, because formatTimestamp() returns the em dash placeholder for a
    // missing value and the row filter below keeps any non-empty string - so an
    // unguarded call would add a "Status recorded —" row to a legacy enrollment
    // that has no workflow row at all.
    statusRecordedAt: e.workflow_updated_at ? formatTimestamp(e.workflow_updated_at) : '',
  };
}

// Rep-side: the wizard already holds the packet in perchContracts.
/* Refresh the persisted enrollment detail so Review & Finish sees the same
   backend values during a LIVE enrollment that it sees on resume.
   currentEnrollmentDetail was only ever assigned by openEnrollment(), so mid-flow
   it was null and the savings figure simply vanished. */
async function refreshEnrollmentDetail(){
  if(!currentDraft || !currentDraft.enrollment_id) return null;
  try{
    currentEnrollmentDetail = await apiFetch('/api/enrollments/' + currentDraft.enrollment_id);
  }catch(err){ /* keep the previous value; resolveProgramView degrades safely */ }
  return currentEnrollmentDetail;
}

function mountRepAgreements(readOnly, submitted){
  // The read-only card is rendered from the AUTHORITATIVE workflow key, so
  // every rep-side mount carries it. currentEnrollmentDetail is the persisted
  // detail; perchContext.nextStepKey is the live in-memory key, which is what
  // a straight-through enrollment has before any reload. Neither is invented
  // here, and both are ignored unless readOnly is true.
  const detail = currentEnrollmentDetail || {};
  mountAgreements({
    actor: 'rep',
    enrollmentId: currentDraft && currentDraft.enrollment_id,
    contracts: perchContracts,
    acceptanceEnabled: perchContext.acceptanceEnabled === true,
    readOnly: readOnly === true,
    submitted: submitted === true,
    summary: agrSummaryFromState(),
    statusKey: detail.workflow_step_key || perchContext.nextStepKey || null,
    statusLabel: detail.workflow_step_label || null,
  });
}

// Called by the shared component after a successful acceptance.
function onAgreementsAccepted(){
  if(Agreements.actor === 'rep'){
    perchContext.acceptanceSubmitted = true;
    perchContext.acceptanceEnabled = false;
    return;
  }
  // CUSTOMER. They have finished; there is nothing else for them to do and
  // nowhere in UBS for them to go. This used to leave them on the signing
  // view, and the only way onward was a control pointing at the EMPLOYEE
  // dashboard - which asks a customer to log in as staff.
  renderCustomerComplete();
}

/* THE CUSTOMER'S END STATE.

   A customer's journey ends here: no next step, no employee navigation, and
   nothing that leads to a staff login. The one job of this screen is that an
   ordinary person - including an older or a sceptical one - knows at a glance
   that they are finished and that nothing further is expected of them.

   THE COPY IS APPROVED AND EXACT. It was signed off wholesale, so it is
   written out literally here rather than assembled from fragments, and it
   must not be reworded, abbreviated or "improved" in passing. Two standing
   constraints go with it:

     * the word "solar" does not appear anywhere on this screen - the previous
       version said "the solar project team", which is precisely what was
       removed; and
     * nothing here navigates. No dashboard, no sign-in, no button that could
       be read as "there is one more thing to do".

   VISUAL DIRECTION, adapted rather than copied. The approved reference is a
   centred confirmation: a large green check, generous white space, one
   "What Happens Next" card, and a closing line. It is built here out of the
   existing UBS tokens - brand green, the standard radii, the standard type
   scale - so it reads as part of this product and not as a pasted-in page.
   The mark is drawn inline so it needs no asset and scales cleanly on a
   phone. */
function renderCustomerComplete(){
  const card = document.getElementById('portal-card');
  if(!card) return;
  // The card becomes the confirmation surface: centred, roomier, and without
  // the left-aligned eyebrow/lead rhythm the signed-in portal uses.
  card.classList.add('card-cdone');
  card.innerHTML =
    '<div class="cdone">' +
      // Decorative only: the heading below carries the meaning, so the mark is
      // hidden from screen readers rather than read out as a shape.
      '<div class="cdone-mark" aria-hidden="true">' +
        '<svg viewBox="0 0 120 120" role="presentation" focusable="false">' +
          '<circle class="dm-halo" cx="60" cy="60" r="52"></circle>' +
          '<circle class="dm-disc" cx="60" cy="60" r="38"></circle>' +
          '<path class="dm-tick" d="M44 61.5 L55 72.5 L77 49.5" fill="none" ' +
            'stroke-width="7.5" stroke-linecap="round" stroke-linejoin="round"></path>' +
        '</svg>' +
      '</div>' +
      '<h1 class="cdone-title">You\'re All Set!</h1>' +
      '<p class="cdone-lead">Your enrollment has been submitted successfully.</p>' +
      '<p class="cdone-sub">Thank you for completing your enrollment. ' +
        'No further action is needed at this time.</p>' +
      '<section class="cdone-next">' +
        '<h2 class="cdone-next-head">What Happens Next</h2>' +
        '<p class="cdone-next-body">Your utility and the program team will ' +
          'review your information and handle the next steps. If anything ' +
          'else is needed, they\'ll contact you using the details you ' +
          'provided.</p>' +
      '</section>' +
      '<p class="cdone-close">' +
        '<svg class="cdone-close-ico" viewBox="0 0 20 20" aria-hidden="true" ' +
          'focusable="false"><circle cx="10" cy="10" r="9"></circle>' +
          '<path d="M6 10.3 L8.7 13 L14 7.6" fill="none" stroke="#fff" ' +
          'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path>' +
        '</svg>' +
        'You can safely close this window.</p>' +
    '</div>';
  activateScreen('screen-customer-portal');
}


/* Customer entry point. Resolves the customer's own enrollment, then mounts the
   SAME Agreements component the rep uses - no customer-specific contract code. */
async function openCustomerContracts(){
  const errEl = document.getElementById('portal-error');
  if(errEl) errEl.style.display = 'none';

  let me;
  try{
    me = await customerApi('/api/auth/customer-me');
  }catch(err){
    if(errEl){ errEl.textContent = userMessage(err); errEl.style.display=''; }
    return;
  }
  customerEnrollmentId = me.enrollment_id;
  activateScreen('screen-customer-contracts');

  // THE CUSTOMER'S OWN SUMMARY, from their own payload. Every key here is one
  // renderAgreementCard() actually reads - `project` was not, which is why it
  // is dropped: the card deliberately omits the project.
  const summary = {
    customerName: me.customer ? [me.customer.first_name, me.customer.last_name].filter(Boolean).join(' ') : '',
    email: (me.customer && me.customer.email) || '',
    utility: me.utility_name || '',
    serviceAddress: me.service_address || '',
    savings: formatSavings(me.program_savings),
    enrollmentCode: me.enrollment_code || '',
    statusRecordedAt: me.workflow_updated_at ? formatTimestamp(me.workflow_updated_at) : '',
  };

  // Completed / blocked enrollments are READ-ONLY: no packet is requested, so
  // opening one makes ZERO Perch contract calls.
  // A COMPLETED customer sees the confirmation screen, not an agreement card.
  //
  // They finish in one of two ways - they accept here and now, or they come
  // back later to an enrollment that is already done - and both must land in
  // the same place. Previously only the first did; returning customers got a
  // read-only packet with a "Done" button that signed them out to the staff
  // login, which is the one destination a customer should never be offered.
  //
  // This is TERMINAL ONLY. A blocked enrollment is not complete, so it keeps
  // the status-driven read-only card below: the approved "You're All Set!"
  // copy must never appear over an outcome Perch could not confirm.
  if(me.workflow_is_terminal === true){
    renderCustomerComplete();
    return;
  }
  if(me.workflow_is_blocked === true){
    mountAgreements({
      actor: 'customer', enrollmentId: customerEnrollmentId,
      contracts: (me.workflow_last_response || {}).contracts || [],
      acceptanceEnabled: false, readOnly: true,
      submitted: false, summary: summary,
      // Same authoritative key the rep view uses, so a customer whose
      // acceptance could not be confirmed is NOT told they are finished.
      statusKey: me.workflow_step_key || null,
      statusLabel: me.workflow_step_label || null,
    });
    return;
  }

  mountAgreements({actor:'customer', enrollmentId: customerEnrollmentId, contracts: [],
                   acceptanceEnabled: false, readOnly: false, submitted: false, summary: summary});

  let body;
  try{
    body = await customerApi('/api/perch/enrollments/' + customerEnrollmentId + '/contracts',
                             {method:'POST', body: JSON.stringify({})});
  }catch(err){
    const cardErr = document.getElementById('agr-card-error');
    if(cardErr){
      cardErr.textContent = 'Your agreements could not be loaded. ' +
                          userMessage(err, 'Refresh the page to try again.');
      cardErr.style.display = 'block';
    }
    return;
  }
  mountAgreements({
    actor: 'customer', enrollmentId: customerEnrollmentId,
    contracts: body.contracts || [],
    acceptanceEnabled: body.acceptance_enabled === true,
    readOnly: false, submitted: false, summary: summary,
  });
}


/* ── Final review screen ──────────────────────────────────────────────────
   Modelled on Perch's own National Grid final-review step: concise account
   details, ONE acknowledgement whose text carries the real agreement names as
   inline links, ONE submit button. No document rail, no per-document cards, no
   "opened" badges, no requirement to open anything. */

function renderAgreementCard(){
  const host = agrHost();
  if(!host) return;
  const s = Agreements.summary || {};

  // Deliberately NOT shown: project ("Assigned by Perch" told the rep nothing)
  // and average monthly bill. Savings and the eligibility type replace them,
  // and each row is dropped entirely when its value is absent - the filter
  // below is what guarantees nothing is ever invented to fill a gap.
  const rows = [
    ['Name', s.customerName],
    ['Email', s.email],
    ['Electric utility', s.utility],
    ['Savings', s.savings],                 // omitted unless Perch supplied it
    ['Program', s.programType],             // shown only for LMI enrollments
    ['Service address', s.serviceAddress],
    ['Account number', s.accountNumber],
    ['Enrollment ID', s.enrollmentCode],
    ['Created', s.createdAt],
    ['Last modified', s.updatedAt],
    // Read-only only: when UBS recorded the status shown above. Empty - and so
    // dropped by the filter - on a live enrollment and on a legacy row with no
    // workflow state.
    ['Status recorded', Agreements.readOnly ? s.statusRecordedAt : ''],
  ].filter(function(r){ return r[1]; });

  /* C4. On the REP's live step 5 this card is mounted INSIDE a page that
     already says all of this: #agr-eyebrow reads "Final step", <h1> reads
     "Review & finish", the subtitle explains what to do, and .summary-grid
     lists the customer, service and program. The card then repeated a third
     "Final step", a second "Review &…" heading, its own lead and a <dl> of
     the same enrollment - inside a .card nested in .agr-card. That is the
     duplication C4 was asked to remove.

     Only that one surface changes. The CUSTOMER completion screen has no
     page framing around this card, so it keeps every part: it IS the heading,
     the lead and the only summary there, and it is the quality benchmark the
     rest of this work is measured against. Read-only keeps everything too,
     because its heading and status copy are the whole point of that view. */
  const repLive = Agreements.actor !== 'customer' && !Agreements.readOnly;

  let html = repLive ? '<div class="agr-block">' : '<div class="card">';

  if(Agreements.readOnly){
    // STATUS-DRIVEN, never hardcoded. Only a terminal enrollment may be
    // described as complete; a blocked one states its real outcome and what to
    // do about it. An unrecognised key degrades to the backend's own
    // step_label, so even a future key can never render as "complete".
    const copy = readOnlyStatusCopy(Agreements.statusKey)
      // Perch's /status can report an enrollment completed while UBS's own
      // persisted key still sits at acceptance - the local key deliberately
      // does not advance on an acceptance Perch rejected. `submitted` is only
      // ever passed true where acceptance is CONFIRMED (a terminal key, or
      // /status reporting completed), so that confirmation is enough to
      // describe the enrollment as complete.
      || (Agreements.submitted ? READ_ONLY_STATUS_COPY.contracts_accepted : null);
    const label = Agreements.statusLabel || 'Status recorded';
    const isCustomer = Agreements.actor === 'customer';
    if(copy){
      html += '<p class="card-eyebrow">' + agrEsc(copy.eyebrow) + '</p>' +
              '<h2>' + agrEsc(isCustomer ? copy.customerHeading : copy.repHeading) + '</h2>' +
              '<p class="lead">' + agrEsc(copy.lead) + '</p>';
    } else {
      html += '<p class="card-eyebrow">Read-only</p>' +
              '<h2>' + agrEsc(label) + '</h2>' +
              '<p class="lead">This enrollment is read-only. Its recorded status is ' +
              'shown above; nothing here can be changed.</p>';
    }
  } else if(!repLive){
    html += '<p class="card-eyebrow">Final step</p>' +
            '<h2>Review &amp; complete your enrollment</h2>' +
            '<p class="lead">Check your details below, then agree to your enrollment agreements to finish.</p>';
  }

  // The rep's page shows this same enrollment in .summary-grid immediately
  // above; a second copy of it here is the duplicated summary, not a second
  // source of truth. Everywhere else this list is the only one there is.
  if(rows.length && !repLive){
    html += '<dl class="agr-details">';
    rows.forEach(function(r){
      html += '<dt>' + agrEsc(r[0]) + '</dt><dd>' + agrEsc(r[1]) + '</dd>';
    });
    html += '</dl>';
  }

  if(Agreements.readOnly){
    if(Agreements.contracts.length){
      html += '<p class="agr-docs-label">Agreements</p><ul class="agr-docs-list">';
      Agreements.contracts.forEach(function(c){
        html += '<li>' + agrEsc(c.contract_name || c) + '</li>';
      });
      html += '</ul>';
      // Honest and low-key. The agreements were generated and accepted; Perch
      // simply stops issuing fresh links once the stage advances. Do NOT imply
      // they were deleted or never existed, and do NOT pretend Dalton can
      // fetch them.
      html += '<p class="helper agr-quiet">Perch issues time-limited links, so these ' +
              'documents open from your Perch records rather than from us.</p>';
    }
    // Actor-aware final action. A customer must never be offered the rep
    // dashboard, and "Done" must not sign a rep out.
    html += '<div class="agr-actions agr-final">';
    if(Agreements.actor === 'customer'){
      html += '<button type="button" class="btn btn-primary btn-lg" ' +
              'onclick="finishCustomerEnrollment()">Done</button>';
    } else {
      html += '<button type="button" class="btn btn-primary btn-lg" ' +
              'onclick="backToDashboard()">Back to dashboard</button>';
    }
    html += '</div></div>';
    host.innerHTML = html;
    return;
  }

  html += '<p class="agr-note err" id="agr-card-error" style="display:none;"></p>';

  if(!Agreements.contracts.length){
    html += '<p class="helper">Preparing your agreements…</p></div>';
    host.innerHTML = html;
    return;
  }

  // ONE acknowledgement. The agreement names are real, come from the Perch
  // packet, and each links to its own authoritative contract index.
  html += '<label class="agr-ack" for="agr-ack-check">' +
          '<input type="checkbox" id="agr-ack-check" onchange="updateAgreeButton()">' +
          '<span>By checking this box, I acknowledge that I have reviewed and agree to the ' +
          agreementLinksHtml() +
          '. By continuing, I am submitting my electronic signature.</span></label>';

  html += '<div class="agr-actions">' +
          '<button type="button" class="btn btn-primary btn-lg" id="agr-agree-btn" disabled ' +
          'onclick="submitAgreements()">Agree &amp; finish</button></div>';
  html += '<p class="agr-note" id="agr-status" style="display:none;"></p>';
  html += '</div>';
  host.innerHTML = html;
  updateAgreeButton();
}

/* Real names from the packet, each carrying its authoritative index. */
function agreementLinksHtml(){
  const links = Agreements.contracts.map(function(c){
    const idx = (typeof c.index === 'number') ? c.index : null;
    const name = agrEsc(c.contract_name || ('Agreement ' + ((idx == null ? 0 : idx) + 1)));
    if(idx === null || !c.url_present){
      return '<span class="agr-link-off" title="This document is not available to open">' + name + '</span>';
    }
    return '<a href="#" class="agr-link" data-index="' + idx + '" ' +
           'onclick="openAgreementDoc(' + idx + '); return false;">' + name + '</a>';
  });
  if(links.length === 1) return links[0];
  if(links.length === 2) return links[0] + ' and ' + links[1];
  return links.slice(0, -1).join(', ') + ', and ' + links[links.length - 1];
}

/* ── Single-document viewer ───────────────────────────────────────────── */

let agrLastFocus = null;

async function openAgreementDoc(index){
  const cardErr = document.getElementById('agr-card-error');
  if(cardErr) cardErr.style.display = 'none';

  const contract = Agreements.contracts.find(function(c){ return c.index === index; });
  const title = (contract && contract.contract_name) || 'Agreement';

  agrLastFocus = document.activeElement;
  const ov = document.getElementById('agr-overlay');
  const titleEl = document.getElementById('agr-doc-title');
  const bodyEl = document.getElementById('agr-doc-body');
  const errEl = document.getElementById('agr-error');
  if(titleEl) titleEl.textContent = title;
  if(errEl){ errEl.style.display = 'none'; errEl.textContent = ''; }
  if(bodyEl) bodyEl.innerHTML = '<p class="helper">Opening the document…</p>';
  ov.classList.add('open');
  document.body.style.overflow = 'hidden';
  document.addEventListener('keydown', agrEscKey);
  const close = ov.querySelector('.agr-close');
  if(close) close.focus();

  let body;
  try{
    // The backend parameter is `index`. Send the authoritative index that came
    // from the /contracts response - never a rendering position or a name.
    body = await agrFetch('/api/perch/enrollments/' + Agreements.enrollmentId + '/contracts/review',
                          {method:'POST', body: JSON.stringify({index: index})});
  }catch(err){
    if(bodyEl) bodyEl.innerHTML = '';
    if(errEl){
      errEl.textContent = 'That document could not be opened. ' +
                        userMessage(err, 'Try again in a moment.');
      errEl.style.display='';
    }
    return;
  }

  // A single-use, same-origin Dalton capability URL. Never a Perch presigned URL.
  if(bodyEl){
    bodyEl.innerHTML = '<iframe class="agr-doc-frame" src="' + agrEsc(body.review_url) +
                       '" title="' + agrEsc(title) + '"></iframe>' +
                       '<p class="helper agr-doc-fallback">Trouble viewing it here? ' +
                       '<a href="' + agrEsc(body.review_url) + '" target="_blank" rel="noopener">' +
                       'Open in a new tab</a>.</p>';
  }
}

function closeAgreements(){
  const ov = document.getElementById('agr-overlay');
  if(ov) ov.classList.remove('open');
  document.body.style.overflow = '';
  document.removeEventListener('keydown', agrEscKey);
  // Drop the iframe so the single-use URL is not left loaded behind the page.
  const bodyEl = document.getElementById('agr-doc-body');
  if(bodyEl) bodyEl.innerHTML = '';
  if(agrLastFocus && agrLastFocus.focus) agrLastFocus.focus();
  // Reviewing is NOT accepting - nothing about acceptance changes here.
}

function agrEscKey(e){ if(e.key === 'Escape') closeAgreements(); }
function agrBackdrop(e){ if(e.target && e.target.id === 'agr-overlay') closeAgreements(); }

/* ── Acceptance: one checkbox, one button ─────────────────────────────── */

function updateAgreeButton(){
  const btn = document.getElementById('agr-agree-btn');
  const chk = document.getElementById('agr-ack-check');
  if(!btn || !chk) return;
  if(Agreements.submitted){
    btn.disabled = true; btn.textContent = 'Agreements submitted';
    chk.disabled = true; return;
  }
  if(Agreements.inFlight){ btn.disabled = true; return; }
  btn.disabled = !(Agreements.acceptanceEnabled && chk.checked && !Agreements.readOnly);
}

async function submitAgreements(){
  const btn = document.getElementById('agr-agree-btn');
  const chk = document.getElementById('agr-ack-check');
  const errEl = document.getElementById('agr-card-error');
  const statusEl = document.getElementById('agr-status');
  if(errEl) errEl.style.display = 'none';
  if(statusEl) statusEl.style.display = 'none';

  if(!chk || !chk.checked){
    if(errEl){ errEl.textContent = 'Please check the box to agree before finishing.';
               errEl.style.display=''; }
    return;
  }
  if(Agreements.inFlight || Agreements.submitted) return;
  Agreements.inFlight = true;
  if(btn){ btn.disabled = true; btn.textContent = 'Submitting…'; }

  let body;
  try{
    body = await agrFetch('/api/perch/enrollments/' + Agreements.enrollmentId + '/contracts/accept',
                          {method:'POST', body: JSON.stringify({customer_confirmed: true})});
  }catch(err){
    Agreements.inFlight = false;
    if(btn) btn.textContent = 'Agree & finish';
    if(errEl){ errEl.textContent = userMessage(err); errEl.style.display=''; }
    if(/uncertain|could not be confirmed/i.test(err.message || '')){
      if(btn) btn.disabled = true;   // ambiguous outcome: never retry by clicking
      if(statusEl){
        statusEl.textContent = 'Do not resubmit. We are confirming this with Perch.';
        statusEl.style.display = 'block';
      }
    } else if(chk){
      updateAgreeButton();
    }
    return;
  }

  Agreements.inFlight = false;
  Agreements.submitted = true;
  Agreements.readOnly = true;
  const st = body.perch_status || null;
  let msg = body.message || 'Your agreements were submitted.';
  if(st && st.completed === true) msg += ' Your enrollment is complete.';
  renderAgreementCard();
  const after = document.getElementById('agr-status');
  if(after){ after.textContent = msg; after.style.display = 'block'; }
  if(typeof onAgreementsAccepted === 'function') onAgreementsAccepted(body);
}


/* ═══════════ Multi-file document sets (bill + LMI) ═══════════
   One logical document -> one or more files. The picker and dropzone both
   accept several at once; more can be added later and individual files removed.
   Order of selection is preserved and sent as page order.

   The existing single-file OCR preview still runs on the FIRST file so the
   rep's prefill experience is unchanged; the SERVER extracts across the whole
   set, which is what actually feeds validation. */

const DocSets = { utility_bill: {id:null, files:[]}, lmi_document: {id:null, files:[]} };

/* Show the files that are ALREADY attached to this enrollment.

   The documents and their set persist server-side across leave/reopen; what
   was missing is that the browser never asked. resetWizardState() empties
   DocSets on every open - correctly, so enrollment A's files cannot appear
   under B - and nothing refilled it for the enrollment actually being opened.
   The rep saw an empty uploader for a bill that was still attached, and the
   only way to make the step continue was to upload the same bill again.

   Each restored entry carries its documentId, which is what marks it SAVED:
   docSetNameHtml() renders it as a link to the stored file, and the
   "did every page finish uploading" check in submitBill() skips it. So a
   restored file is never re-uploaded and never counted as missing. */
function hydrateDocSets(sets){
  if(!sets) return;
  Object.keys(DocSets).forEach(function(cat){
    const s = sets[cat];
    if(!s || !s.files || !s.files.length) return;
    DocSets[cat] = {
      id: s.document_set_id,
      files: s.files.map(function(f){
        return {
          // Metadata only - there is no File object for something the rep did
          // not just pick, and nothing needs one: the entry is already saved.
          file: {name: f.original_filename, size: f.file_size},
          key: 'saved:' + f.id,
          documentId: f.id,
          // ITS OWN set id, not the category's. Removal is addressed per
          // entry, so an entry can always be deleted even if something later
          // rewrites the category-level id.
          setId: f.document_set_id || s.document_set_id || null,
        };
      }),
    };
    renderDocSetList(cat);
  });
  // The bill step still gates Continue on this single pointer, so a restored
  // bill must set it or the step blocks on a document that is already there.
  const bill = sets.utility_bill;
  if(bill && bill.files && bill.files.length){
    state.bill.documentId = bill.files[0].id;
    state.bill.fileName = bill.files[0].original_filename;
  }
  const lmi = sets.lmi_document;
  if(lmi && lmi.files && lmi.files.length){
    state.lmi.documentId = lmi.files[0].id;
    state.lmi.fileName = lmi.files[0].original_filename;
  }
}

function docSetReset(category){
  if(DocSets[category]) DocSets[category] = {id:null, files:[]};
  renderDocSetList(category);
}

/* Clear EVERY document-set category. Iterating the object means a category
   added later is reset automatically rather than being silently forgotten -
   which is exactly how the stale-filename bug happened. */
function docSetResetAll(){
  Object.keys(DocSets).forEach(function(cat){
    DocSets[cat] = {id: null, files: []};
    renderDocSetList(cat);
  });
}

function docSetAddFiles(category, fileList){
  const set = DocSets[category];
  if(!set) return [];
  const added = [];
  for(let i = 0; i < fileList.length; i++){
    const f = fileList[i];
    // Same name AND size AND lastModified = the same file picked twice.
    const dup = set.files.some(function(x){
      return x.file.name === f.name && x.file.size === f.size
             && x.file.lastModified === f.lastModified;
    });
    if(dup) continue;
    set.files.push({ file: f, key: (f.name + ':' + f.size + ':' + f.lastModified),
                     documentId: null });
    added.push(f);
  }
  renderDocSetList(category);
  return added;
}

/* Remove one file from a set - and make the removal STICK.

   THE BUG THIS FIXES: this used to filter the entry out of the in-memory list
   and stop there. For a file the rep had just picked that is correct - there
   is nothing on the server yet. For a file that was already STORED it was
   purely cosmetic: the row stayed in `documents`, still attached to the set,
   so the next reopen hydrated it straight back. Remove B, add D, reopen, and
   B was there again next to D. Before attached files were restored at all the
   mistake was invisible; restoring them is what exposed it.

   The removal is applied to the screen first so the rep sees it immediately,
   then persisted. If the server refuses, the entry goes BACK and the rep is
   told - a removal that silently failed is the thing we are fixing, so it
   must not fail silently in the other direction either. */
async function docSetRemove(category, key){
  const set = DocSets[category];
  if(!set) return;
  const idx = set.files.findIndex(function(x){ return x.key === key; });
  if(idx === -1) return;
  const entry = set.files[idx];

  set.files.splice(idx, 1);
  renderDocSetList(category);
  syncPrimaryDocPointer(category);

  // Never uploaded: nothing to tell the server about.
  if(!entry.documentId) return;
  // No enrollment to address. Restore the entry rather than leaving the screen
  // claiming a removal that was never sent anywhere.
  if(!currentDraft || !currentDraft.enrollment_id){
    set.files.splice(idx, 0, entry);
    renderDocSetList(category);
    syncPrimaryDocPointer(category);
    return;
  }

  /* WHICH ENDPOINT. Addressed from the ENTRY first, then the category.
     This used to read `set.id` alone and RETURN SILENTLY when it was null -
     which, because rehydrateDocumentsFromDetail() nulled it on every reopen,
     is precisely how a removal became a repaint and nothing more. A stored
     document is now always removable: through its set when it has one, and
     through the per-document route when it does not. */
  const setId = entry.setId || set.id || null;
  const base = '/api/enrollments/' + currentDraft.enrollment_id;
  const url = setId
    ? base + '/document-sets/' + setId + '/files/' + entry.documentId
    : base + '/documents/' + entry.documentId;

  try {
    await apiFetch(url, {method: 'DELETE'});
  } catch(err){
    // Put it back where it was, so the screen keeps telling the truth.
    set.files.splice(idx, 0, entry);
    renderDocSetList(category);
    syncPrimaryDocPointer(category);
    const host = document.getElementById(
      category === 'utility_bill' ? 'bill-submit-error' : 'lmi-submit-error');
    if(host){
      host.textContent = 'That file could not be removed. ' +
                         userMessage(err, 'Try again in a moment.');
      host.style.display = '';
    } else {
      alert('That file could not be removed. ' +
            userMessage(err, 'Try again in a moment.'));
    }
  }
}

/* Keep the single-document pointers in step with the set they point into.

   state.bill.documentId gates Continue and is what /enroll is told to use, so
   it must never point at a file that has just been removed. */
function syncPrimaryDocPointer(category){
  const set = DocSets[category];
  const first = set && set.files.length ? set.files[0] : null;
  if(category === 'utility_bill'){
    state.bill.documentId = first ? (first.documentId || null) : null;
    state.bill.fileName = first ? first.file.name : '';
  } else if(category === 'lmi_document'){
    state.lmi.documentId = first ? (first.documentId || null) : null;
    state.lmi.fileName = first ? first.file.name : '';
  }
  if(typeof checkBillReady === 'function' && document.getElementById('c-first')){
    checkBillReady();
  }
}

function docSetListEl(category){
  // Its own element - the legacy single-file chip still owns bill-file-chip.
  const hostId = category === 'utility_bill' ? 'bill-fileset' : 'lmi-fileset';
  return document.getElementById(hostId);
}

/* The attached pages, one row each.

   WHAT PHASE B ADDED, AND WHY IT IS NOT COSMETIC. The list showed a number, a
   filename and a size, and said nothing about whether a page had actually
   reached the server. The legacy chip above it, meanwhile, labelled a
   just-picked file "Uploaded" before any upload had resolved. So the two
   things on screen that could have told a rep what is really stored either
   said nothing or said something untrue.

   Each row now states its own persistence: "Attached" once the page has a
   document id from the server, "Uploading" while it has not. A header states
   how many pages are attached, so a multi-page bill can no longer be read as
   the single filename the chip happens to show.

   The remove control is unchanged - one per page, still routed through
   docSetRemove(), which issues the real DELETE. The known photo-removal issue
   in KNOWN_ISSUES.md is deliberately NOT papered over here: nothing in this
   function hides, disables or re-labels removal, and showing which pages are
   genuinely stored makes the symptom easier to see rather than harder. */
function renderDocSetList(category){
  const host = docSetListEl(category);
  if(!host) return;
  const set = DocSets[category];
  if(!set || !set.files.length){ host.innerHTML = ''; return; }
  const stored = set.files.filter(function(x){ return !!x.documentId; }).length;
  const total = set.files.length;
  // Counted, never guessed: "attached" means the server returned a document id
  // for that page.
  const head = '<p class="docset-head">' +
      (stored === total
        ? total + (total === 1 ? ' page attached' : ' pages attached')
        : stored + ' of ' + total + ' pages attached') +
    '</p>';
  host.innerHTML = head + set.files.map(function(x, i){
    const kb = Math.max(1, Math.round(x.file.size / 1024));
    const state = x.documentId
      ? '<span class="docset-state is-stored">Attached</span>'
      : '<span class="docset-state">Uploading</span>';
    return '<div class="docset-file">' +
             '<span class="docset-n">' + (i + 1) + '</span>' +
             docSetNameHtml(x) +
             state +
             '<span class="docset-size">' + kb + ' KB</span>' +
             '<button type="button" class="docset-remove" aria-label="Remove ' +
               esc(x.file.name) + '" onclick="docSetRemove(\'' + category +
               '\', \'' + esc(x.key).replace(/'/g, "\\'") + '\')">&times;</button>' +
           '</div>';
  }).join('');
}

/* Uploads the whole set. Files already stored keep their set id, so adding more
   appends rather than starting a new document. */
async function uploadDocumentSet(category){
  const set = DocSets[category];
  if(!set || !set.files.length) return null;
  // Only files NOT yet stored are sent. Re-sending ones that already have a
  // documentId would append them to the set a second time and duplicate pages,
  // because the server treats every received file as a new page.
  const pending = set.files.filter(function(x){ return !x.documentId; });
  if(!pending.length){
    // Nothing new to send - e.g. the rep re-picked a file already stored.
    // Report what IS stored so callers still resolve a document id.
    return {document_set_id: set.id,
            files: set.files.map(function(x){ return {document_id: x.documentId}; })};
  }
  const fd = new FormData();
  fd.append('category', category);
  if(set.id) fd.append('document_set_id', String(set.id));
  pending.forEach(function(x){ fd.append('files', x.file, x.file.name); });
  const body = await apiFetch('/api/enrollments/' + currentDraft.enrollment_id +
                              '/document-sets', {method:'POST', body: fd});
  set.id = body.document_set_id;
  // Map returned document ids back onto the tracked files, in page order, so
  // each filename can link to its own authorized view route.
  (body.files || []).forEach(function(rec){
    const match = set.files.filter(function(x){ return !x.documentId; })[0];
    // The set id is recorded ON THE ENTRY as well, so every stored file knows
    // its own removal address the moment it is stored - not only after a
    // reopen re-reads it from the server.
    if(match){ match.documentId = rec.document_id; match.setId = set.id; }
  });
  renderDocSetList(category);
  return body;
}


/* Tells the rep exactly which files the visible prefill came from.
   The in-browser preview reads only the first file; the server extracts the
   whole set on upload. Saying so prevents a rep assuming pages 2-3 were read. */
function renderPrefillScope(category){
  const el = document.getElementById(
    category === 'utility_bill' ? 'bill-prefill-scope' : 'lmi-prefill-scope');
  if(!el) return;
  const set = DocSets[category];
  const n = set ? set.files.length : 0;
  if(n <= 1){ el.style.display = 'none'; el.textContent = ''; return; }
  const first = set.files[0].file.name;
  el.textContent = 'Showing details read from "' + first + '" only. All ' + n +
    ' files are attached and will be read together when you continue — ' +
    'please check every field before submitting.';
  el.style.display = 'block';
}


/* Filename link. Opens the ORIGINAL file in a new tab using the browser's own
   viewer - no custom preview window. Stored uploads go through the authorized
   Dalton route; a file not yet uploaded uses a temporary object URL.
   Viewing is read-only: it never changes the set or page order. */
function docSetNameHtml(entry){
  const name = esc(entry.file.name);
  if(entry.documentId && currentDraft && currentDraft.enrollment_id){
    const href = '/api/enrollments/' + currentDraft.enrollment_id +
                 '/documents/' + entry.documentId + '/view';
    return '<a class="docset-name docset-link" href="' + href + '" target="_blank" ' +
           'rel="noopener" title="Open ' + name + ' in a new tab" ' +
           'onclick="return openStoredDocument(event, \'' + href + '\')">' + name + '</a>';
  }
  // Not uploaded yet - open the local file the rep just picked.
  return '<a class="docset-name docset-link" href="#" ' +
         'title="Open ' + name + ' in a new tab" ' +
         'onclick="return openPendingDocument(event, \'' + esc(entry.key) + '\')">' + name + '</a>';
}

/* The view route requires the Authorization header, which a plain <a> cannot
   send. Fetch it authenticated, then hand the browser a blob URL so it renders
   natively (image viewer / PDF viewer) in a new tab. */
async function openStoredDocument(e, href){
  if(e) e.preventDefault();
  const tab = window.open('about:blank', '_blank');   // opened on the user gesture
  try{
    const res = await fetch(href, {headers:{Authorization:'Bearer ' + AuthStore.getToken()}});
    if(!res.ok) throw new Error('Could not open that file (' + res.status + ').');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    if(tab) tab.location = url; else window.location = url;
    // Release once the tab has had time to load; the tab keeps its own copy.
    setTimeout(function(){ URL.revokeObjectURL(url); }, 60000);
  }catch(err){
    if(tab) tab.close();
    alert(userMessage(err, 'That file could not be opened.'));
  }
  return false;
}

function openPendingDocument(e, key){
  if(e) e.preventDefault();
  let entry = null;
  Object.keys(DocSets).forEach(function(cat){
    const hit = DocSets[cat].files.filter(function(x){ return x.key === key; })[0];
    if(hit) entry = hit;
  });
  if(!entry) return false;
  const url = URL.createObjectURL(entry.file);
  window.open(url, '_blank', 'noopener');
  setTimeout(function(){ URL.revokeObjectURL(url); }, 60000);
  return false;
}


/* ═══════════ Admin: rep management ═══════════
   Admin-only. The nav entry is hidden for everyone else, but that is cosmetic -
   every /api/admin route is enforced server-side with @require_role("admin"),
   so hiding the link is convenience, not the security boundary. */

/* The top bar shows the same initial the sidebar avatar does. */
function syncTopbarAvatar(){
  const src = document.getElementById('rep-avatar');
  const dst = document.getElementById('topbar-avatar');
  if(src && dst) dst.textContent = src.textContent;
}

function syncAdminNav(){
  syncTopbarAvatar();
  const isAdmin = !!(currentUser && currentUser.role === 'admin');
  ['nav-reps', 'nav-sales-report'].forEach(function(id){
    const el = document.getElementById(id);
    if(el) el.style.display = isAdmin ? '' : 'none';
  });
}

async function loadReps(){
  const host = document.getElementById('reps-table');
  const err = document.getElementById('rep-list-error');
  if(err) err.style.display = 'none';
  if(!host) return;
  host.innerHTML = ceeLoading('Loading reps');
  let reps;
  try{
    reps = await apiFetch('/api/admin/reps');
  }catch(e){
    host.innerHTML = '';
    if(err){ err.textContent = e.message; err.style.display=''; }
    return;
  }
  adminRepCache = reps;
  // The list is the page now, so it says how long it is - the same row-count
  // treatment C2 gave every other table.
  const countEl = document.getElementById('rep-count');
  if(countEl){
    const active = reps.filter(function(r){ return r.is_active === 1; }).length;
    countEl.textContent = reps.length
      ? reps.length + (reps.length === 1 ? ' rep' : ' reps') + ' · ' + active + ' active'
      : '';
  }
  if(!reps.length){
    host.innerHTML = tableEmptyHtml('No reps yet',
      'Add one with the form above and they can sign in straight away.');
    return;
  }
  host.innerHTML =
    // Wrapped, like every other table in the application. Without the wrap
    // there is nothing to scroll in the 900-1199 band and the table pushes
    // the whole page sideways instead.
    '<div class="table-wrap"><table class="table"><thead><tr>' +
      '<th>Name</th><th>Email</th><th>Rep code</th><th>Phone</th><th>Team</th>' +
      '<th>Enrollments</th><th>Status</th><th style="text-align:right;">Actions</th>' +
    '</tr></thead><tbody>' +
    reps.map(function(r){
      const active = r.is_active === 1;
      return '<tr>' +
        // Eight columns and three actions do not all fit at every width, so
        // the email is the one allowed to ellipsise - and it carries its full
        // value on hover, the same way the enrollment tables carry the full
        // timestamp. Nothing is unreachable.
        '<td data-label="Name" title="' + esc(r.full_name) + '">' + esc(r.full_name) + '</td>' +
        '<td data-label="Email" title="' + esc(r.email) + '">' + esc(r.email) + '</td>' +
        '<td data-label="Rep code"><span class="mono">' + esc(r.rep_code || '') + '</span></td>' +
        '<td data-label="Phone">' + esc(r.phone || '—') + '</td>' +
        '<td data-label="Team">' + esc(r.team || '—') + '</td>' +
        '<td data-label="Enrollments">' + r.enrollment_count + '</td>' +
        // A deactivated rep is a settled administrative state, not a problem to
        // investigate - the same reasoning that put "Not eligible" in neutral
        // in C1. Attention red is reserved for things that need looking at.
        '<td data-label="Status"><span class="status-pill ' +
          (active ? 'status-complete' : 'status-neutral') + '">' +
          (active ? 'Active' : 'Inactive') + '</span></td>' +
        '<td data-label="" class="cell-actions">' +
          // THREE LEVELS, NOT THREE PEERS (C4). Edit is the ordinary action
          // and keeps the secondary button; resetting a password is rarer and
          // steps back to a ghost; deactivating is the one with consequences
          // and stays the outlined danger button - restrained, but the only
          // red thing in the row. Previously the first two were identical, so
          // the row read as three equally likely things to click.
          '<button class="btn btn-secondary btn-sm" onclick="editRep(' + r.user_id + ')">Edit</button> ' +
          '<button class="btn btn-ghost btn-sm" onclick="resetRepPassword(' + r.user_id + ')">Reset password</button> ' +
          '<button class="btn btn-sm ' + (active ? 'btn-danger' : 'btn-secondary') +
            '" onclick="toggleRep(' + r.user_id + ',' + active + ')">' +
            (active ? 'Deactivate' : 'Activate') + '</button>' +
        '</td></tr>';
    }).join('') + '</tbody></table></div>';
}

async function createRep(){
  const btn = document.getElementById('rep-create-btn');
  const err = document.getElementById('rep-form-error');
  const ok  = document.getElementById('rep-form-ok');
  err.style.display = 'none'; ok.style.display = 'none';
  const payload = {
    full_name: document.getElementById('rep-new-name').value.trim(),
    email:     document.getElementById('rep-new-email').value.trim(),
    password:  document.getElementById('rep-new-pass').value,
    rep_code:  document.getElementById('rep-new-code').value.trim(),
    phone:     document.getElementById('rep-new-phone').value.trim(),
    team:      document.getElementById('rep-new-team').value.trim(),
  };
  btn.disabled = true; btn.textContent = 'Adding…';
  let rep;
  try{
    rep = await apiFetch('/api/admin/reps', {method:'POST', body: JSON.stringify(payload)});
  }catch(e){
    err.textContent = e.message; err.style.display='';
    btn.disabled = false; btn.textContent = 'Add rep';
    return;
  }
  btn.disabled = false; btn.textContent = 'Add rep';
  ['rep-new-name','rep-new-email','rep-new-pass','rep-new-code','rep-new-phone','rep-new-team']
    .forEach(function(id){ document.getElementById(id).value = ''; });
  ok.textContent = 'Added ' + rep.full_name + ' (' + rep.rep_code + '). ' +
                   'Give them their password directly — it is not emailed.';
  ok.style.display = 'block';
  loadReps();
}
async function toggleRep(userId, isActive){
  const action = isActive ? 'deactivate' : 'activate';
  if(isActive && !confirm('Deactivate this rep? They will be signed out immediately. ' +
                          'Their enrollments and history are kept.')) return;
  try{
    await apiFetch('/api/admin/reps/' + userId + '/' + action, {method:'POST', body:'{}'});
  }catch(e){ alert(userMessage(e)); return; }
  loadReps();
}


/* Environment warning banner. Fetched unauthenticated so it renders on the
   LOGIN page too - a tester must see it before typing anything. Shown only when
   the backend supplies text, so an unconfigured production environment gets
   nothing. Never blocks the app if the request fails. */
/* The staging warning banner was removed for production readiness. The
   backend /api/environment endpoint and its environment detection are
   deliberately LEFT INTACT - only the visible banner is gone. */


/* Rep: return to the dashboard. Does NOT touch the session. */
function backToDashboard(){
  resetWizardState();
  showView('dashboard');
}

/* Customer: end their enrollment cleanly.

   Clears ONLY the customer token - never the rep session - and returns them to
   the ONE unified sign-in screen. This is NOT "the rep login": there is a
   single UBS login for everyone, and reaching it grants nothing. The rep
   dashboard is never activated here, and signing in again goes through
   /api/auth/signin, where the BACKEND decides the account type. */
function finishCustomerEnrollment(){
  try { CustomerAuth.clear(); } catch(e){}
  customerEnrollmentId = null;
  activateScreen('screen-login');
  const msg = document.getElementById('login-error');
  if(msg){
    msg.textContent = 'Your enrollment is complete. You can close this window.';
    msg.style.color = '';
    msg.style.display='';
  }
}


/* Savings string from the PERSISTED Perch value only.
   program_savings is null whenever Perch did not supply a figure for this
   enrollment's customer type - in that case this returns null and the row is
   dropped. Never hardcode or infer a percentage onto a contract summary. */
function formatSavings(programSavings){
  if(!programSavings || typeof programSavings.percent !== 'number') return null;
  const pct = Number(programSavings.percent);
  if(!isFinite(pct) || pct <= 0) return null;
  const shown = Number.isInteger(pct) ? String(pct) : pct.toFixed(1);
  return shown + '% savings';
}

/* Programme label. Shown ONLY for LMI, because "Residential" on every other
   enrollment is noise. Derived from the persisted Perch basis, not guessed. */
/* Program label for an enrollment row.

   This used to infer the program from programSavings.basis, so an enrollment
   with no savings figure silently read as Residential - which is how a completed
   LMI enrollment displayed as Residential. It now takes the AUTHORITATIVE
   selected type, falling back to basis only for legacy rows that never
   persisted a selection. */
function formatProgramType(programSavings, selectedType){
  const t = String(selectedType || '').toUpperCase();
  // E2: both income-qualified types land here. They are distinguished in the
  // label, because a customer on the IRA program is on a different program -
  // but neither reads as the raw `LMI_IRA` wire value.
  if(t === 'LMI_IRA') return 'Income-eligible (IRA)';
  if(isLmiCustomerType(t)) return 'Income-eligible (LMI)';
  if(t === 'RESIDENTIAL') return null;
  if(!programSavings) return null;
  return programSavings.basis === 'lmi' ? 'Income-eligible (LMI)' : null;
}


const STEP_LABEL_TEXT = {1:'Availability', 2:'Customer & Bill', 4:'Eligibility', 5:'Agreements'};

/* Renders the stepper for the ACTIVE branch.

   Nodes carry three states - done / current / upcoming - and completed nodes
   show a checkmark. The connector between nodes fills as progress advances.
   Eligibility is absent entirely on the Residential branch because
   activeSteps() does not contain it; nothing is hidden or disabled. */
function renderStepper(current){
  const host = document.getElementById('stepper');
  if(!host) return;
  const seq = activeSteps();
  const pos = stepPosition(current);
  host.innerHTML = seq.map(function(n, i){
    const state = i < pos ? 'done' : (i === pos ? 'now' : 'todo');
    const label = esc(STEP_LABEL_TEXT[n] || ('Step ' + n));
    const mark = state === 'done'
      ? '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
      : String(i + 1);
    const connector = i < seq.length - 1
      ? '<span class="stp-line' + (i < pos ? ' filled' : '') + '" aria-hidden="true"></span>'
      : '';
    return '<div class="stp-item ' + state + '"' +
             (state === 'now' ? ' aria-current="step"' : '') + '>' +
             '<span class="stp-node">' + mark + '</span>' +
             '<span class="stp-label">' + label + '</span>' +
           '</div>' + connector;
  }).join('');
}

/* Kept as a thin alias: older call sites still ask for labels only. */
function renderStepLabels(){ renderStepper(currentStep || 1); }


/* ═══════════ Program availability + selection ═══════════
   Consumes GET /api/perch/enrollments/<id>/programs, which returns ONLY the
   programs Perch actually offered for this location, each with its own savings
   percentage from the matching Perch field.

   NO business logic lives here. The frontend renders what the backend returned
   and sends back a choice; services/perch/adapter.resolve_customer_type()
   re-validates that choice against the capacity response, so a tampered client
   cannot enrol a program Perch did not offer. */

let availablePrograms = [];
// E3: the savings rate chosen for the selected program, and what is on offer.
// Perch v2 can publish several rates for one program and then REQUIRES one at
// /enroll, so this is a third decision the rep makes - held here only for
// rendering; enrollments.selected_savings_percent is the authority.
let selectedSavingsPercent = null;
let savingsSelectionRequired = false;
// Reps from the last GET /api/admin/reps, so the edit modal can prefill.
let adminRepCache = [];

function programHostEl(){ return document.getElementById('program-options'); }

async function loadProgramOptions(){
  const host = programHostEl();
  if(!host || !currentDraft) return null;
  host.innerHTML = ceeLoading('Checking available programs');
  let body;
  try{
    body = await apiFetch('/api/perch/enrollments/' + currentDraft.enrollment_id + '/programs');
  }catch(err){
    host.innerHTML = '<p class="helper">Could not load available programs: ' +
                     esc(err.message) + '</p>';
    return null;
  }
  availablePrograms = body.available_programs || [];
  // Hydrated from the backend for the same reason the program is: a JS
  // variable does not survive leaving the screen, and the rate is part of
  // what /enroll will be given.
  selectedSavingsPercent = (typeof body.selected_savings_percent === 'number')
    ? body.selected_savings_percent : null;
  savingsSelectionRequired = body.savings_selection_required === true;

  // HYDRATE FROM THE BACKEND. The persisted choice is authoritative, so the
  // selection survives leaving the screen, navigating back, and reloading -
  // none of which a JS variable can do.
  // Committed enrollments cannot change program - render the choice as a
  // settled fact rather than an interactive control.
  programCommitted = (currentEnrollmentDetail || {}).perch_committed === true
                     || perchContext.enrollmentSubmitted === true;
  const persisted = body.selected_customer_type || null;
  if(persisted){
    selectedProgram = availablePrograms.filter(function(p){
      return p.customer_type === persisted; })[0] || null;
  } else if(availablePrograms.length === 1){
    // Exactly one option is unambiguous - but it must still be PERSISTED.
    // Previously this only set the local variable, so selected_customer_type
    // stayed NULL for every single-program location. The server then fell back
    // to workflow state (which no longer carries customer_type), decided the
    // enrollment was Residential, and read the wrong savings field - 0% for an
    // LMI-only ZIP like 12901. On reopen selectedProgram was null too, so an
    // LMI enrollment rendered as "Residential".
    selectedProgram = availablePrograms[0];
    if(!persisted && currentDraft && currentDraft.enrollment_id){
      persistSelectedProgram(selectedProgram.customer_type);
    }
  } else if(selectedProgram){
    // Drop a stale selection that is no longer offered.
    const still = availablePrograms.filter(function(p){
      return p.customer_type === selectedProgram.customer_type; })[0];
    selectedProgram = still || null;
  } else {
    // Two programs and no persisted choice: NOTHING is auto-selected.
    selectedProgram = null;
  }
  renderProgramOptions(body);
  // The branch may have just become known (single option auto-selected, or a
  // persisted choice hydrated). Re-render the stepper so Eligibility appears or
  // disappears via the CSS transition rather than being flashed on a later nav.
  renderStepper(currentStep || 1);
  return body;
}

/* THE income-qualified test. Perch has TWO income-qualified customer types
   from v2 on - LMI and LMI_IRA - and `=== 'LMI'` is false for the second one,
   which would route an IRA enrollment down the standard-residential branch and
   show it the residential savings figure. Asked here, once, so adding a third
   type later is one edit rather than a search. Mirrors
   adapter.is_lmi_customer_type() on the server. */
const LMI_CUSTOMER_TYPES = ['LMI', 'LMI_IRA'];
function isLmiCustomerType(t){
  return LMI_CUSTOMER_TYPES.indexOf(String(t || '').trim().toUpperCase()) !== -1;
}

/* Labels come from the SERVER (adapter.PROGRAM_RULES), so Perch vocabulary -
   `lmi_ira`, `mass_market`, `commercial_and_industrial` - cannot reach a
   screen because someone forgot a case here. The local fallback covers a
   response from a pre-E2 backend only. */
function programLabel(p){
  if(p && p.label) return p.label;
  return isLmiCustomerType(p && p.customer_type) ? 'Residential LMI' : 'Residential';
}

function programBlurb(p){
  if(p && p.description) return p.description;
  return p.lmi_required
    ? 'Income-eligible program. Requires proof of participation in a qualifying program.'
    : 'Standard residential program. No eligibility documentation needed.';
}

/* Savings as the rep reads it. Perch can offer ONE rate, SEVERAL, or NONE for
   a program, so there is not always a single number to show.

   One rate renders exactly as it did before E2. Several render as a range -
   never averaged, never quietly reduced to the best-looking one, because the
   rate the customer actually gets is chosen at enroll time (E3) and quoting a
   single figure here would promise something Perch has not agreed to. */
function programSavingsHtml(p){
  const rates = (p && p.savings_percents) || [];
  if(rates.length === 1){
    return '<span class="pg-save">' + esc(String(rates[0])) +
           '<span class="pg-pct">% savings</span></span>';
  }
  if(rates.length > 1){
    const lo = Math.min.apply(null, rates), hi = Math.max.apply(null, rates);
    return '<span class="pg-save">' + esc(String(lo)) + '&ndash;' + esc(String(hi)) +
           '<span class="pg-pct">% savings</span></span>';
  }
  // A pre-E2 backend sends only the scalar; honour it rather than printing
  // "not published" over a rate that is right there.
  if(p && typeof p.savings_percent === 'number' && p.savings_percent > 0){
    return '<span class="pg-save">' + esc(String(p.savings_percent)) +
           '<span class="pg-pct">% savings</span></span>';
  }
  return '<span class="pg-save pg-save-none">Savings not published</span>';
}

function renderProgramOptions(body){
  const host = programHostEl();
  if(!host) return;

  if(!body || body.capacity_checked !== true){
    host.innerHTML = '';
    return;
  }
  // E2.1: an empty list with the county outstanding is NOT "no capacity".
  // Some capacity here is limited to particular counties, so which programs
  // apply is not decided until the rep says where the customer lives - and
  // telling them nothing is available would be wrong AND would send them off
  // to re-check a ZIP that is perfectly fine.
  if(body.awaiting_county === true){
    host.innerHTML = '<div class="prog-none"><strong>Select the customer\'s ' +
      'county to see the available programs.</strong><p class="helper">' +
      'Some solar capacity at this address is limited to particular counties, ' +
      'so the programs available depend on where the customer lives. The ' +
      'County field is on the Availability step.</p></div>';
    return;
  }
  if(!availablePrograms.length){
    // No invented programme, no fake choice.
    host.innerHTML = '<div class="prog-none"><strong>No community solar capacity ' +
      'is available for this address right now.</strong><p class="helper">' +
      'Perch did not return an eligible program for this ZIP and utility. ' +
      'Check the ZIP and utility, or try again later.</p></div>';
    return;
  }

  const multiple = availablePrograms.length > 1;
  // E2: there can now be three programs, not only two, so the count is read
  // rather than spelled out. "Two programs are available" was correct while
  // Residential and LMI were the only possibilities and becomes a lie the
  // moment LMI_IRA appears beside them.
  let html = '<div class="prog-head">' +
    (programCommitted
      ? 'This enrollment has already been submitted. The savings program can no longer be changed.'
      : (multiple ? availablePrograms.length + ' programs are available at this address. Choose one.'
                  : 'Available program')) + '</div>';

  html += '<div class="prog-grid">' + availablePrograms.map(function(p){
    const selected = selectedProgram && selectedProgram.customer_type === p.customer_type;
    // Savings renders ONLY from what Perch supplied - one rate, a range, or
    // "not published". See programSavingsHtml().
    const savings = programSavingsHtml(p);
    // Committed: still shown, still legible - just no longer a control.
    return '<button type="button" class="pg' + (selected ? ' selected' : '') +
      (programCommitted ? ' committed' : '') +
      '" role="radio" aria-checked="' + (selected ? 'true' : 'false') + '"' +
      (programCommitted ? ' disabled aria-disabled="true"' : '') +
      ' onclick="selectProgram(\'' + esc(p.customer_type) + '\')">' +
      '<span class="pg-check" aria-hidden="true"><svg viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg></span>' +
      '<span class="pg-name">' + esc(programLabel(p)) + '</span>' +
      savings +
      '<span class="pg-note">' + esc(p.lmi_required
        ? 'Eligibility documentation required'
        : (isLmiCustomerType(p.customer_type)
            ? 'Income-eligible enrollment'
            : 'Standard residential enrollment')) + '</span>' +
      '</button>';
  }).join('') + '</div>';

  html += savingsChoiceHtml();

  host.innerHTML = html;
  updateProgramContinueState();
  renderRailProgram();
}

/* THE SAVINGS RATE CHOICE (E3).

   Perch v2 can publish several rates for one program, and /enroll then
   REQUIRES one: "Optional when only one rate applies, required when more than
   one does." One rate needs no choice and renders nothing - which is every v1
   location and most v2 ones, so this is invisible unless it is needed.

   Deliberately NOT a new screen and not a new component: it reuses the same
   .pg card row the programs themselves use, directly under them, so it reads
   as part of the same decision rather than a second step. */
function savingsChoiceHtml(){
  if(!selectedProgram) return '';
  const program = availablePrograms.filter(function(p){
    return p.customer_type === selectedProgram.customer_type; })[0];
  const rates = (program && program.savings_percents) || [];
  if(rates.length < 2) return '';   // nothing to choose

  const head = programCommitted
    ? 'Savings rate for this enrollment'
    : 'This program offers more than one savings rate. Choose one.';

  return '<div class="prog-head prog-rate-head">' + esc(head) + '</div>' +
    '<div class="prog-grid prog-rate-grid" role="radiogroup" ' +
    'aria-label="Savings rate">' + rates.map(function(r){
      const on = (selectedSavingsPercent === r);
      return '<button type="button" class="pg pg-rate' + (on ? ' selected' : '') +
        (programCommitted ? ' committed' : '') +
        '" role="radio" aria-checked="' + (on ? 'true' : 'false') + '"' +
        (programCommitted ? ' disabled aria-disabled="true"' : '') +
        ' onclick="selectSavingsRate(' + esc(String(r)) + ')">' +
        '<span class="pg-check" aria-hidden="true"><svg viewBox="0 0 16 16">' +
        '<path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" ' +
        'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>' +
        '</svg></span>' +
        '<span class="pg-save">' + esc(String(r)) +
        '<span class="pg-pct">% savings</span></span>' +
        '</button>';
    }).join('') + '</div>';
}

async function selectSavingsRate(rate){
  if(programCommitted){
    const err = document.getElementById('program-error');
    if(err){
      err.textContent = 'This enrollment has already been submitted and the '
                      + 'savings rate can no longer be changed.';
      err.style.display = '';
    }
    return;
  }
  const err = document.getElementById('program-error');
  if(err){ err.style.display = 'none'; err.textContent = ''; }
  try{
    const body = await apiFetch(
      '/api/perch/enrollments/' + currentDraft.enrollment_id + '/savings-rate',
      {method:'POST', body: JSON.stringify({savings_percent: rate})});
    selectedSavingsPercent = body.selected_savings_percent;
    savingsSelectionRequired = false;
  }catch(e){
    if(err){
      err.textContent = userMessage(e, 'Could not save the savings rate.');
      err.style.display = '';
    }
    return;
  }
  // Re-read rather than patch: the backend is what decides whether anything
  // is still outstanding, and it may have cleared something.
  await loadProgramOptions();
}

async function selectProgram(customerType){
  if(programCommitted){
    // Backend refuses this too (409); this only avoids a pointless round trip
    // and states the reason where the rep is looking.
    const err = document.getElementById('program-error');
    if(err){
      err.textContent = 'This enrollment has already been submitted and the '
                      + 'savings program can no longer be changed.';
      err.style.display='';
    }
    return;
  }
  const match = availablePrograms.filter(function(p){
    return p.customer_type === customerType; })[0];
  if(!match) return;             // never select something not offered

  // Optimistic paint so the card responds instantly...
  selectedProgram = match;
  renderProgramOptions({capacity_checked: true, selected_customer_type: customerType});

  // ...then PERSIST. The server re-validates against this enrollment's capacity
  // response, so this is a request rather than an instruction.
  if(!currentDraft || !currentDraft.enrollment_id) return;
  try{
    const saved = await apiFetch('/api/perch/enrollments/' + currentDraft.enrollment_id + '/program',
                                 {method:'POST', body: JSON.stringify({customer_type: customerType})});
    const confirmed = availablePrograms.filter(function(p){
      return p.customer_type === saved.selected_customer_type; })[0] || null;
    selectedProgram = confirmed;
  }catch(err){
    // Roll the paint back rather than leave the rep believing a rejected
    // choice was saved.
    selectedProgram = null;
    const host = programHostEl();
    if(host){
      renderProgramOptions({capacity_checked: true});
      const note = document.createElement('p');
      note.className = 'helper';
      note.style.color = 'var(--danger)';
      note.textContent = userMessage(err, 'That program could not be selected.');
      host.appendChild(note);
    }
  }
  renderProgramOptions({capacity_checked: true});
  renderStepper(currentStep || 1);
}

/* Blocks Continue only when the rep genuinely has an unmade choice. The backend
   enforces this too - this is convenience, not the security boundary. */
/* Continue state has ONE owner: checkBillReady(), which weighs every
   requirement together. This used to drive #wf-primary - the step-1 button -
   so on the merged screen it silently controlled nothing. */
function updateProgramContinueState(){
  const stepOne = document.getElementById('wf-primary');
  if(stepOne && availablePrograms.length){
    // E3: a rate that /enroll will REQUIRE is as blocking as an unchosen
    // program. Stopping here costs a click; letting it through costs a
    // 422 at the end of the wizard.
    stepOne.disabled = (availablePrograms.length > 1 && !selectedProgram)
                       || savingsNeededNow();
  }
  if(document.getElementById('btn-bill-next')) checkBillReady();
}

/* Whether a savings rate is still owed for the CURRENTLY selected program. */
function savingsNeededNow(){
  if(!selectedProgram) return false;
  const program = availablePrograms.filter(function(p){
    return p.customer_type === selectedProgram.customer_type; })[0];
  const rates = (program && program.savings_percents) || [];
  return rates.length > 1 && selectedSavingsPercent === null;
}


/* ═══════════ Admin modal (replaces prompt()) ═══════════
   One reusable in-app dialog. Every action still calls the SAME admin APIs with
   the same permissions - this changes how the values are collected, nothing
   about what is allowed. No deletion, no role mutation. */

let adminModalSubmit = null;

function openAdminModal(title, bodyHtml, onSubmit, saveLabel){
  document.getElementById('admin-modal-title').textContent = title;
  document.getElementById('admin-modal-body').innerHTML = bodyHtml;
  const err = document.getElementById('admin-modal-error');
  if(err){ err.style.display = 'none'; err.textContent = ''; }
  const save = document.getElementById('admin-modal-save');
  save.textContent = saveLabel || 'Save';
  save.disabled = false;
  adminModalSubmit = onSubmit;
  document.getElementById('admin-modal').classList.add('open');
  document.body.style.overflow = 'hidden';
  document.addEventListener('keydown', adminModalEsc);
  const first = document.querySelector('#admin-modal-body input, #admin-modal-body select');
  if(first) first.focus();
}

function closeAdminModal(){
  document.getElementById('admin-modal').classList.remove('open');
  document.body.style.overflow = '';
  document.removeEventListener('keydown', adminModalEsc);
  adminModalSubmit = null;
}
function adminModalEsc(e){ if(e.key === 'Escape') closeAdminModal(); }
function adminModalBackdrop(e){ if(e.target && e.target.id === 'admin-modal') closeAdminModal(); }

function adminModalError(message){
  const err = document.getElementById('admin-modal-error');
  if(err){ err.textContent = message; err.style.display=''; }
  const save = document.getElementById('admin-modal-save');
  if(save){ save.disabled = false; save.textContent = save.dataset.label || 'Save'; }
}

async function submitAdminModal(){
  if(!adminModalSubmit) return;
  const save = document.getElementById('admin-modal-save');
  save.dataset.label = save.textContent;
  save.disabled = true; save.textContent = 'Saving…';
  try{
    await adminModalSubmit();
    closeAdminModal();
    loadReps();
  }catch(err){
    adminModalError(err.message || 'Could not save.');
  }
}

function adminField(id, label, value, opts){
  opts = opts || {};
  return '<div class="field"><label for="' + id + '">' + esc(label) + '</label>' +
    '<input id="' + id + '" type="' + (opts.type || 'text') + '" value="' + esc(value || '') +
    '"' + (opts.placeholder ? ' placeholder="' + esc(opts.placeholder) + '"' : '') + '></div>';
}

/* Edit phone / team / rep code - exactly the fields PATCH /api/admin/reps
   already accepts. Role, email and active state are deliberately absent. */
function editRep(userId){
  const rep = (adminRepCache || []).filter(function(r){ return r.user_id === userId; })[0] || {};
  openAdminModal('Edit ' + (rep.full_name || 'rep'),
    adminField('adm-phone', 'Phone', rep.phone) +
    adminField('adm-team', 'Team', rep.team) +
    adminField('adm-code', 'Rep code', rep.rep_code) +
    '<p class="helper">Role, email and active status are managed separately.</p>',
    async function(){
      const payload = {
        phone: document.getElementById('adm-phone').value,
        team: document.getElementById('adm-team').value,
      };
      const code = document.getElementById('adm-code').value.trim();
      if(code && code !== (rep.rep_code || '')) payload.rep_code = code;
      await apiFetch('/api/admin/reps/' + userId, {method:'PATCH', body: JSON.stringify(payload)});
    });
}

function resetRepPassword(userId){
  const rep = (adminRepCache || []).filter(function(r){ return r.user_id === userId; })[0] || {};
  openAdminModal('Reset password for ' + (rep.full_name || 'rep'),
    '<div class="field"><label for="adm-pw">New password</label>' +
      '<span class="pw-wrap"><input id="adm-pw" type="password" placeholder="At least 10 characters">' +
      '<button type="button" class="pw-eye" id="adm-pw-eye" aria-label="Show password" ' +
      'aria-pressed="false" onclick="togglePasswordVisibility(\'adm-pw\',\'adm-pw-eye\')">&#128065;</button>' +
      '</span></div>' +
    '<p class="helper">Give the new password to the rep directly &mdash; it is not emailed.</p>',
    async function(){
      const pw = document.getElementById('adm-pw').value;
      await apiFetch('/api/admin/reps/' + userId + '/password',
                     {method:'POST', body: JSON.stringify({password: pw})});
    }, 'Set password');
}

/* Admin edits their OWN display name via PATCH /api/auth/me/profile. */
function editOwnProfile(){
  openAdminModal('Your profile',
    adminField('adm-name', 'Display name', currentUser ? currentUser.full_name : '') +
    '<p class="helper">This is the name shown across the app. Your role and email cannot be changed here.</p>',
    async function(){
      const name = document.getElementById('adm-name').value;
      const body = await apiFetch('/api/auth/me/profile',
                                  {method:'PATCH', body: JSON.stringify({full_name: name})});
      if(currentUser) currentUser.full_name = body.full_name;
      syncAdminNav();
    });
}


/* Loading affordances.
   A spinner tells the rep the system is working; a bare sentence does not.
   Both respect prefers-reduced-motion via the CSS token overrides. */
function ceeLoading(message){
  return '<div class="cee-loading"><span class="cee-spinner" aria-hidden="true"></span>' +
         '<span>' + esc(message || 'Loading') + '…</span></div>';
}

function ceeSkeletonRows(count){
  let out = '';
  for(let i = 0; i < (count || 3); i++){
    out += '<div class="cee-skel" style="height:52px;margin-bottom:10px;"></div>';
  }
  return out;
}

/* Briefly highlights a field the OCR just populated, then fades out. The value
   stays fully editable - this marks provenance, it does not lock anything. */
function markOcrFilled(id){
  const el = document.getElementById(id);
  if(!el) return;
  el.classList.remove('ocr-filled');
  void el.offsetWidth;          // restart the animation
  el.classList.add('ocr-filled');
}


/* Quiet service context at the top of Customer & Bill.
   Replaces the dedicated "Capacity confirmed" screen. The capacity LOOKUP and
   its validation are unchanged - only this presentation is new. Raw
   available/not-available diagnostics are deliberately not shown; the program
   cards below carry the decision the rep actually needs. */
function renderServiceContext(){
  // The mockup shows a utility treatment in the program section head. It is
  // bound to the utility ACTUALLY chosen at Availability - never hardcoded.
  // If no logo asset exists the chip renders the name alone, and if no utility
  // is known yet the chip is hidden entirely rather than showing a placeholder.
  const chip = document.getElementById('util-chip');
  if(chip){
    const name = perchContext.utilityDisplay || perchContext.utilitySlug || '';
    if(!name){
      chip.hidden = true; chip.innerHTML = '';
    } else {
      const initials = String(name).replace(/[^A-Za-z ]/g,'').split(/\s+/)
        .filter(Boolean).slice(0,2).map(w=>w[0].toUpperCase()).join('') || '·';
      chip.hidden = false;
      chip.innerHTML = '<span class="util-logo" aria-hidden="true">' + esc(initials) + '</span>' +
        '<span class="util-txt"><strong>' + esc(name) + '</strong>' +
        '<small>' + (perchContext.capacityZip ? 'ZIP ' + esc(perchContext.capacityZip)
                                              : 'Current utility') + '</small></span>';
      chip.setAttribute('aria-label', 'Current utility ' + name);
    }
  }
  // The old context strip was removed with the mockup layout; these writers
  // stay tolerant so nothing throws if the element is absent.
  const host = document.getElementById('svc-context');
  if(host){
    const bits = [];
    const utility = perchContext.utilityDisplay || perchContext.utilitySlug;
    if(utility) bits.push(['Utility', utility]);
    if(perchContext.capacityZip) bits.push(['Service area', 'ZIP ' + perchContext.capacityZip]);
    if(currentDraft && currentDraft.enrollment_code) bits.push(['Enrollment', currentDraft.enrollment_code]);
    host.innerHTML = bits.map(function(b){
      return '<span class="svc-item"><span class="svc-k">' + esc(b[0]) +
             '</span><span class="svc-v">' + esc(b[1]) + '</span></span>';
    }).join('');
  }
}



/* Concise completion summary. Real values only - a row is omitted when its
   value is absent, so nothing is invented to fill the shape. */
function renderCompleteSummary(){
  const host = document.getElementById('complete-summary');
  if(!host) return;
  const detail = currentEnrollmentDetail || {};
  // Same resolver as Review & Finish - one source of truth, not a second
  // heuristic. (This supersedes the local patch that read
  // selectedProgram.savings_percent directly: right for a live enrollment, but
  // it left the resumed and completed cases reading a different source.)
  const view = resolveProgramView();
  const savings = view.percent === null ? '' : view.percent + '% savings';
  const rows = [
    ['Customer', [state.customer.first, state.customer.last].filter(Boolean).join(' ')],
    ['Utility', state.project.utility || perchContext.utilityDisplay || perchContext.utilitySlug],
    ['Program', (view.label || '—') + (savings ? ' · ' + savings : '')],
    ['Enrollment', (currentDraft && currentDraft.enrollment_code) || detail.enrollment_code],
  ].filter(function(r){ return r[1]; });
  host.innerHTML = '<section class="sum-group"><dl class="sum-dl">' +
    rows.map(function(r){
      return '<div><dt>' + esc(r[0]) + '</dt><dd>' + esc(r[1]) + '</dd></div>';
    }).join('') + '</dl></section>';
}


/* Rail summary values. Real data only - "Not selected" is an honest state, not
   a placeholder standing in for a guess. */
function renderRailProgram(){
  const el = document.getElementById('rail-program');
  if(el){
    if(selectedProgram){
      const s = formatSavings({percent: selectedProgram.savings_percent, basis: null});
      el.textContent = programLabel(selectedProgram) + (s ? ' · ' + s : '');
      el.classList.remove('rail-empty');
    } else {
      el.textContent = availablePrograms.length > 1 ? 'Choose a program' : 'Not selected';
      el.classList.add('rail-empty');
    }
  }
  const cust = document.getElementById('rail-customer');
  if(cust){
    const name = [state.customer.first, state.customer.last].filter(Boolean).join(' ');
    cust.textContent = name || '—';
  }
}

/* Program selection must be resolved BEFORE anything is submitted, and the
   error belongs beside the choice. Previously the only signal was the backend's
   422 surfacing at the bottom of the page, far from the control. */
function validateProgramSelection(){
  const err = document.getElementById('program-error');
  if(err){ err.style.display = 'none'; err.textContent = ''; }
  if(availablePrograms.length > 1 && !selectedProgram){
    if(err){
      err.textContent = 'Choose which program this customer is enrolling in.';
      err.style.display='';
    }
    const host = programHostEl();
    if(host && host.scrollIntoView) host.scrollIntoView({block:'center'});
    return false;
  }
  return true;
}


/* Persist a program choice. Fire-and-forget: the server re-validates against
   this enrollment's capacity response, and a failure must not block the rep -
   the value is re-sent on the next explicit selection. */
function persistSelectedProgram(customerType){
  if(!customerType || !currentDraft || !currentDraft.enrollment_id) return Promise.resolve(null);
  return apiFetch('/api/perch/enrollments/' + currentDraft.enrollment_id + '/program',
                  {method:'POST', body: JSON.stringify({customer_type: customerType})})
    .catch(function(){ return null; });
}

/* SINGLE SOURCE OF TRUTH for the Review/Completion program + savings.

   Resolution order, identical for live, resumed and completed enrollments:
     1. the persisted selection (enrollments.selected_customer_type)
     2. the live selection held in selectedProgram
   The percentage is then taken for THAT type only:
     Residential -> the residential/commercial figure
     LMI         -> the LMI figure
   No cross-fallback, and nothing is inferred from program_savings.basis. When
   no figure exists for the selected type the percent is null and the caller
   omits it rather than showing a wrong number. */
function resolveProgramView(){
  const detail = currentEnrollmentDetail || {};
  const type = detail.selected_customer_type
            || (selectedProgram && selectedProgram.customer_type)
            || null;
  // E2: LMI_IRA is income-qualified, so it reads the LMI figure. `=== 'LMI'`
  // would have sent it to the residential one - the same class of bug this
  // function exists to prevent.
  const isLmi = isLmiCustomerType(type);

  let percent = null;
  // Backend value is authoritative when present - it is keyed to the persisted
  // selection server-side.
  const ps = detail.program_savings;
  if(ps && typeof ps.percent === 'number'
     && (!ps.basis || (ps.basis === 'lmi') === isLmi)){
    percent = ps.percent;
  }
  if(percent === null && availablePrograms.length){
    // Live fallback: the capacity response for the SELECTED type only.
    const match = availablePrograms.filter(function(p){
      return String(p.customer_type).toUpperCase() === (isLmi ? 'LMI' : 'RESIDENTIAL'); })[0];
    if(match && typeof match.savings_percent === 'number') percent = match.savings_percent;
  }
  if(percent === null && selectedProgram
     && String(selectedProgram.customer_type).toUpperCase() === String(type || '').toUpperCase()
     && typeof selectedProgram.savings_percent === 'number'){
    percent = selectedProgram.savings_percent;
  }
  return {type: type, isLmi: isLmi, percent: percent,
          label: type ? (isLmi ? 'Residential LMI' : 'Residential') : null};
}


/* Rebuild the upload UI from documents the SERVER already holds.

   This restores identity only - filename and document id. It does not
   reconstruct a File object, because the bytes live on the server and are
   re-fetched on demand; the existing view/remove controls operate on the
   document id, which is what they already used. */
/* THE BUG THIS FUNCTION CAUSED, and what it is now allowed to do.

   THIS WAS THE ROOT CAUSE of "a removed photo comes back". There are two
   sources of attached documents in the detail payload:

     e.document_sets  - the current set per category, WITH its set id.
                        hydrateDocSets() reads it at the top of openEnrollment.
     e.documents      - every document row for the enrollment, flat.
                        This function reads it, 59 lines LATER.

   This function then REPLACED DocSets wholesale with `id: null`, destroying
   the set id hydrateDocSets() had just restored correctly. docSetRemove()
   needs that id to call the DELETE endpoint and bailed out silently without
   one - so pressing the x spliced the entry out of the list, repainted, and
   sent NOTHING. The row survived, and the next reopen listed it again from
   e.documents. Adding replacements made it worse: uploadDocumentSet() with a
   null set id starts a NEW set instead of appending, so the old rows stayed
   and the new ones arrived beside them - four removed-and-replaced photos
   reopening as six.

   Three attempted fixes missed it because every one of them looked at the
   removal path, which was correct, and at hydrateDocSets(), which was also
   correct. The damage happened between them.

   WHAT IT LEGITIMATELY DOES NOW. e.documents is still the only source that
   can see a LEGACY document - one uploaded through the old single-file route,
   with document_set_id NULL, which no document_sets entry will ever mention.
   So this function now fills a category only when hydrateDocSets() left it
   EMPTY, and never overwrites a restored set. Each entry carries its own
   set id (now serialized by the detail route), so removal addresses the right
   endpoint whichever source listed it. */
function rehydrateDocumentsFromDetail(detail){
  const docs = (detail && detail.documents) || [];
  if(!docs.length) return;
  const byCat = {utility_bill: [], lmi_document: []};
  docs.forEach(function(d){
    const cat = d.doc_category || d.category;
    if(byCat[cat]) byCat[cat].push(d);
  });

  function restore(category, rows, pointer){
    if(!rows.length) return;
    const existing = DocSets[category];
    // ALREADY RESTORED BY hydrateDocSets(). Leave it exactly as it is: that
    // source knows which set is current and carries its id. Overwriting it
    // here is the defect described above.
    if(existing && existing.files && existing.files.length) return;
    DocSets[category] = {
      // The set id of the rows we are showing, when they have one. Null only
      // for genuinely set-less legacy documents - and those are removable
      // through the per-document route, so null no longer disables removal.
      id: rows[0].document_set_id || null,
      files: rows.map(function(d){
        return {key: 'srv-' + d.id, documentId: d.id, persisted: true,
                setId: d.document_set_id || null,
                file: {name: d.original_filename || ('Document ' + d.id), size: null}};
      })
    };
    pointer.fileName = rows[0].original_filename || 'Uploaded document';
    pointer.documentId = rows[0].id;
    renderDocSetList(category);
  }

  restore('utility_bill', byCat.utility_bill, state.bill);
  restore('lmi_document', byCat.lmi_document, state.lmi);
  if(typeof checkBillReady === 'function') checkBillReady();
}


/* ══════════════════════════════════════════════════════════════════════
   LMI SELF-ATTESTATION (rep side)
   Mirrors the official Perch form: household size -> the threshold for that
   size -> above/below. Exact income is never collected, and no attestation
   language is invented here - the legal document is generated by Perch.
   ══════════════════════════════════════════════════════════════════════ */
let selfAttestation = {reference: null, occupancy: null, county: '', choice: null,
                       /* Per-county income limits for markets that publish them
                          (VA). Null until a county is chosen; never a default. */
                       countyIncomeLimits: null};

async function prepareSelfAttestation(){
  // The proof-document section is NO LONGER HIDDEN here. It used to be, which
  // is what made the two look mutually exclusive: revealing self-attestation
  // took the uploaded documents off the screen. They coexist now, and
  // renderLmiSections() owns visibility for both.
  renderLmiSections();
  const err = document.getElementById('sa-error');
  if(err){ err.textContent = ''; err.style.display = 'none'; }
  try{
    selfAttestation.reference = await apiFetch('/api/perch/enrollments/'
      + currentDraft.enrollment_id + '/lmi/self-attestation/reference');
  }catch(e){
    if(err){ err.textContent = e.message; err.style.display=''; }
    return;
  }
  // RESUME: restore the rep's own previous answers before rendering, so a
  // returning enrollment shows what was already entered rather than a blank
  // form. Values come from the server's record, never from a local guess.
  const prior = selfAttestation.reference.submitted;
  if(prior){
    if(prior.occupancy !== null && prior.occupancy !== undefined){
      selfAttestation.occupancy = String(prior.occupancy);
      const occSel = document.getElementById('sa-occupancy');
      if(occSel) occSel.value = String(prior.occupancy);
    }
    if(prior.county){
      selfAttestation.county = prior.county;
      const ctySel = document.getElementById('sa-county');
      if(ctySel) ctySel.value = prior.county;
    }
    if(prior.status === 'accepted' || prior.status === 'rejected'){
      selfAttestation.choice = prior.status === 'accepted' ? 'below' : 'above';
    }
  }
  if(selfAttestation.reference.already_generated) renderSelfAttestationSent();
  renderSelfAttestation();
}

function renderSelfAttestation(){
  const ref = selfAttestation.reference;
  if(!ref) return;
  const occSel = document.getElementById('sa-occupancy');
  if(occSel && !occSel.options.length){
    // Household sizes. A market with a STATEWIDE table (NY) names them in its
    // thresholds, so those are used verbatim as before. A market with
    // PER-COUNTY limits (VA) ships no table, so the documented 1-8 range is
    // used - that range is the same in every market per the API contract, and
    // the dollar figure still comes from the county lookup, never from here.
    const sizes = (ref.thresholds && ref.thresholds.length)
      ? ref.thresholds.map(function(t){ return t.occupancy; })
      : [1,2,3,4,5,6,7,8];
    occSel.innerHTML = '<option value="">Select…</option>' + sizes.map(function(n){
      return '<option value="' + n + '">' + n
           + (n === 1 ? ' person' : ' people') + '</option>'; }).join('');
  }
  const ctySel = document.getElementById('sa-county');
  const counties = ref.counties || [];
  // E3.1: a v2 enrollment submitted with a county is BOUND to it. The income
  // limits here have to be read for the county the customer was actually
  // enrolled under, so the control shows that county and nothing else - a
  // picker would be an invitation to make the eligibility check disagree with
  // the enrollment. The backend refuses a different county regardless; this
  // is so the rep never reaches that refusal.
  if(ctySel && ref.county_locked && ref.county){
    ctySel.innerHTML = '<option value="' + esc(ref.county) + '">' +
                       esc(ref.county) + '</option>';
    ctySel.value = ref.county;
    ctySel.disabled = true;
    selfAttestation.county = ref.county;
    const cHelp = document.getElementById('sa-county-help');
    if(cHelp){
      cHelp.textContent = 'This enrollment was submitted for ' + ref.county +
                          ', so the income limit is checked for that county.';
      cHelp.style.display = '';
    }
  } else if(ctySel && !ctySel.options.length && counties.length){
    // CONTROLLED list from the backend - never free text, which the API
    // would reject.
    ctySel.innerHTML = '<option value="">Select a county…</option>' + counties.map(
      function(n){ return '<option value="' + esc(n) + '">' + esc(n) + '</option>'; }).join('');
  }
  if(!counties.length){
    // County is required and no list came back. Say so rather than rendering an
    // empty required field the rep cannot satisfy. No county is ever invented.
    const cErr = document.getElementById('sa-error');
    if(cErr){
      cErr.textContent = 'The county list is unavailable, so this attestation '
        + 'cannot be completed right now. The enrollment is unchanged - retry, '
        + 'or contact support if it persists.';
      cErr.style.display='';
    }
  }
  // Re-apply any restored answers AFTER the options exist - setting .value on
  // an empty <select> does not stick.
  if(occSel && selfAttestation.occupancy) occSel.value = String(selfAttestation.occupancy);
  if(ctySel && selfAttestation.county) ctySel.value = selfAttestation.county;
  if(selfAttestation.choice){
    document.querySelectorAll('.sa-opt').forEach(function(b){
      b.classList.toggle('selected', b.getAttribute('data-choice') === selfAttestation.choice);
    });
  }
  // A locked county fires no change event, so the per-county limits are
  // fetched here instead - once, and only when they are not already loaded.
  if(ref.county_locked && ref.county && ref.income_basis === 'per_county'
     && !selfAttestation.countyIncomeLimits){
    onSelfAttestationCounty();
  }

  const cap = document.getElementById('sa-caption');
  // A per-county market has no statewide table caption. Say where the figure
  // comes from instead of leaving the caption blank or showing NY's wording.
  if(cap) cap.textContent = ref.table_caption
    || (selfAttestation.county
          ? 'Income limit for ' + selfAttestation.county
          : 'Select a county to see the income limit that applies.');
  const wrap = document.getElementById('sa-threshold-wrap');
  const elThreshold = document.getElementById('sa-threshold');
  if(ref.income_basis === 'per_county'){
    // VA: the limit depends on the county AND the household size, and it comes
    // from Perch, never from a local table. countyIncomeLimits is populated by
    // onSelfAttestationCounty() below.
    const limits = selfAttestation.countyIncomeLimits || [];
    const hit = limits.filter(function(l){
      return String(l.house_occupancy) === String(selfAttestation.occupancy); })[0];
    if(hit && elThreshold){
      elThreshold.textContent = formatUsdDollars(hit.household_income);
      if(wrap) wrap.style.display = '';
    } else if(wrap){ wrap.style.display = 'none'; }
  } else {
    const row = (ref.thresholds || []).filter(function(t){
      return String(t.occupancy) === String(selfAttestation.occupancy); })[0];
    if(row){
      elThreshold.textContent = formatUsd(row.income_level_cents);
      if(wrap) wrap.style.display = '';
    } else if(wrap){ wrap.style.display = 'none'; }
  }
  updateSelfAttestationReady();
}

function formatUsd(cents){
  return '$' + String(Math.round((cents || 0) / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/* Perch's market income limits arrive as DOLLARS (49150.0), not cents. Kept
   separate from formatUsd so neither unit can be passed to the wrong one. */
function formatUsdDollars(dollars){
  return '$' + String(Math.round(dollars || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

function onSelfAttestationOccupancy(){
  selfAttestation.occupancy = document.getElementById('sa-occupancy').value || null;
  selfAttestation.choice = null;
  document.querySelectorAll('.sa-opt').forEach(function(b){ b.classList.remove('selected'); });
  renderSelfAttestation();
}

async function onSelfAttestationCounty(){
  const ref0 = selfAttestation.reference;
  // A locked county is the enrollment's, not the control's - read it from the
  // reference so a disabled or not-yet-populated <select> cannot blank it.
  selfAttestation.county = (ref0 && ref0.county_locked && ref0.county)
    ? ref0.county
    : (document.getElementById('sa-county').value || '');
  const ref = selfAttestation.reference;
  // PER-COUNTY MARKETS: the income limit is a property of the county, so it is
  // fetched when the county changes. Read from the market-rules endpoint, never
  // from a local table - UBS holds no Virginia income figures.
  if(ref && ref.income_basis === 'per_county'){
    selfAttestation.countyIncomeLimits = null;
    selfAttestation.choice = null;   // a new county means a new limit to answer against
    document.querySelectorAll('.sa-opt').forEach(function(b){ b.classList.remove('selected'); });
    const err = document.getElementById('sa-error');
    if(selfAttestation.county){
      try{
        const body = await apiFetch('/api/markets/county-income-limits?state='
          + encodeURIComponent(ref.state || '') + '&county='
          + encodeURIComponent(selfAttestation.county));
        selfAttestation.countyIncomeLimits = body.income_limits || [];
        if(err) err.style.display = 'none';
      }catch(e){
        // No invented figure. The rep is told the limit is unavailable rather
        // than being shown a number from the wrong market.
        selfAttestation.countyIncomeLimits = null;
        if(err){
          err.textContent = 'The income limit for ' + selfAttestation.county
            + ' could not be loaded, so this attestation cannot be completed '
            + 'right now. The enrollment is unchanged.';
          err.style.display='';
        }
      }
    }
    renderSelfAttestation();
    return;
  }
  updateSelfAttestationReady();
}

/* below -> accepted, above -> rejected, per the documented status field. */
function selectSelfAttestationChoice(choice){
  selfAttestation.choice = choice;
  document.querySelectorAll('.sa-opt').forEach(function(b){
    b.classList.toggle('selected', b.getAttribute('data-choice') === choice); });
  updateSelfAttestationReady();
}

function updateSelfAttestationReady(){
  const btn = document.getElementById('sa-submit');
  if(!btn) return;
  btn.disabled = !(selfAttestation.occupancy && selfAttestation.county && selfAttestation.choice);
}

/* THE SELF-ATTESTATION PAYLOAD IS MARKET-DRIVEN.

   Built from sends_status on the reference response, which the backend derives
   from Perch's own self_attestation_options - never from a state name here.

     sends_status true  (NY) -> status: "accepted" | "rejected", as before
     sends_status false (VA) -> NO status key at all. The answer travels as
                                `qualifies`, which Perch never sees, because the
                                spec says status "cannot be used to signal that
                                the customer does not qualify". A negative
                                answer is therefore a UBS-side stop, enforced by
                                the backend, not something Perch is told. */
function buildSelfAttestationPayload(){
  const ref = selfAttestation.reference || {};
  const payload = {
    occupancy: Number(selfAttestation.occupancy),
    county: selfAttestation.county,
  };
  if(ref.sends_status === false){
    payload.qualifies = selfAttestation.choice === 'below';
  } else {
    payload.status = selfAttestation.choice === 'below' ? 'accepted' : 'rejected';
  }
  return payload;
}

async function submitSelfAttestation(){
  const btn = document.getElementById('sa-submit');
  const err = document.getElementById('sa-error');
  if(!btn || btn.disabled) return;
  if(err) err.style.display = 'none';
  btn.disabled = true; btn.textContent = 'Preparing eligibility…';
  try{
    const body = await apiFetch('/api/perch/enrollments/' + currentDraft.enrollment_id
      + '/lmi/self-attestation',
      {method:'POST', body: JSON.stringify(buildSelfAttestationPayload())});
    // HARD STOP: above the limit, in a market with no non-LMI fallback. No
    // Perch call was made and none will be.
    if(body.not_qualified){
      perchContext.nextStepKey = body.next_step_key || 'lmi_not_qualified';
      renderLmiNotQualified(body.message);
      return;
    }
    perchContext.nextStepKey = body.next_step_key;
    perchContext.selfAttestationGenerated = true;
    // The backend now generates AND accepts in one call, so Perch has already
    // advanced to /contracts. Continue into the EXISTING contracts flow exactly
    // as the proof-doc branch does - the customer signs in ONCE and sees only
    // the normal agreement package.
    if(body.next_step_key === 'contracts'){
      await generateContractsAndOpenAgreement(4);
      return;
    }
    // Perch advanced somewhere other than contracts - most usefully to
    // /lmi/proof_docs. Route there rather than stopping. The documents already
    // attached to this enrollment are reused; the rep is not asked to upload
    // them again. No endpoint is forced: we follow Perch's own next step.
    if(body.next_step_key === 'proof_docs'){
      await continueFromPerchNextStep('lmi');
      return;
    }
    renderSelfAttestationSent();
  }catch(e){
    if(err){ err.textContent = e.message; err.style.display=''; }
    btn.disabled = false; btn.textContent = 'Prepare self-attestation';
  }
}

function renderSelfAttestationSent(){
  const form = document.getElementById('sa-form');
  const done = document.getElementById('sa-sent');
  if(form) form.style.display = 'none';
  if(done) done.style.display = '';
}

/* TERMINAL: the customer does not qualify.
   Reached only in a market that publishes no `status` values - so Perch has no
   field for "does not qualify" - when the household income is above the
   published limit. Perch was never called and will not be.
   Shows the reason and removes every route onward: the attestation form is
   hidden, no contracts action is offered, and the backend independently
   refuses contracts for this enrollment (workflow state lmi_not_qualified), so
   this screen is a presentation of a decision already enforced server-side,
   not the enforcement itself. */
function renderLmiNotQualified(message){
  const form = document.getElementById('sa-form');
  if(form) form.style.display = 'none';
  const sent = document.getElementById('sa-sent');
  if(sent) sent.style.display = 'none';
  const box = document.getElementById('sa-not-qualified');
  const text = document.getElementById('sa-not-qualified-text');
  if(text) text.textContent = message
    || 'This customer does not qualify for the community solar program '
     + 'currently available in their area. The enrollment cannot continue.';
  if(box) box.style.display = '';
  // Nothing further is offered on this screen, and the wizard's own Continue
  // control is disabled so the rep cannot advance past a settled stop.
  const err = document.getElementById('sa-error');
  if(err) err.style.display = 'none';
  const btn = document.getElementById('sa-submit');
  if(btn){ btn.disabled = true; btn.textContent = 'Does not qualify'; }
}

/* NOTE: there is deliberately NO customer-side self-attestation screen.
   Self-attestation is a REP-SIDE eligibility step - the rep attests on the
   customer's behalf, which Perch supports - and the generate call now accepts
   in the same request. The customer signs in once and sees only the normal
   agreement package. */



/* UBS login password reveal. Presentation only - it toggles the input type and
   the label, and touches no auth logic, no IDs the app depends on, and no
   stored value. */
function toggleLoginPassword(){
  const input = document.getElementById('login-pass');
  const button = document.getElementById('login-pass-toggle');
  if(!input || !button) return;
  const isText = input.type === 'text';
  input.type = isText ? 'password' : 'text';
  button.textContent = isText ? 'Show' : 'Hide';
  button.setAttribute('aria-pressed', isText ? 'false' : 'true');
}


/* ══════════════════════════════════════════════════════════════════════
   MAGIC LINK — customer session restore + rep-side copy control.
   Appended at end of file so no existing code moves.
   ══════════════════════════════════════════════════════════════════════ */

/* Restore a customer session from the EXISTING CustomerAuth store.

   This is the missing half of the /access -> / handoff: access.html stores the
   JWT that issue_customer_token() minted, redirects here, and this validates it
   and opens the customer portal. No password, no second auth model, no new
   token type.

   An invalid or expired token falls back to the ONE unified login screen - never
   to any retired customer-login screen. */
async function tryRestoreCustomerSession(){
  const token = CustomerAuth.getToken();
  if(!token) return false;

  let me;
  try{
    const res = await fetch('/api/auth/customer-me', {
      headers: {Authorization: 'Bearer ' + token}
    });
    if(!res.ok) throw new Error('invalid customer session');
    me = await res.json();
  }catch(err){
    // Expired/revoked/invalid: drop the dead token and leave the unified login
    // showing. #screen-login is already active from the markup, so there is
    // nothing to activate - and nothing else is disturbed.
    CustomerAuth.clear();
    return false;
  }

  // A customer session must never leave a stale staff session behind - the same
  // separation unified login already enforces.
  AuthStore.clear();
  currentUser = null;

  customerEnrollmentId = me.enrollment_id || null;
  renderPortalGreeting(me.customer);
  activateScreen('screen-customer-portal');
  await loadCustomerAgreement();
  return true;
}

/* Availability is decided by the AUTHORITATIVE workflow key, never the
   human-readable label, and never once the enrollment is complete. */
function customerLinkAvailable(e){
  if(!e) return false;
  if(e.workflow_is_terminal === true) return false;
  if(!e.customer || !e.customer.email) return false;
  return ['proof_docs','self_attestation','self_attestation_accept',
          'contracts','contracts_review','contracts_accept']
         .indexOf(e.workflow_step_key || '') !== -1;
}

/* Rep creates a link and copies it. Nothing is sent automatically. */
async function copyCustomerLink(enrollmentId, btn){
  const button = btn || document.getElementById('copy-customer-link');
  if(button && button.disabled) return;
  const original = button ? button.textContent : 'Copy customer link';
  if(button){ button.disabled = true; button.textContent = 'Creating…'; }
  try{
    const body = await apiFetch('/api/enrollments/' + enrollmentId + '/customer-link',
                                {method: 'POST'});
    const copied = await copyTextToClipboard(body.url);
    if(button){
      button.textContent = copied ? 'Copied' : 'Copy manually below';
      setTimeout(function(){
        button.textContent = original;
        button.disabled = false;
      }, 2000);
    }
  }catch(err){
    if(button){ button.textContent = original; button.disabled = false; }
    const el = document.getElementById('resume-banner');
    if(el){
      let e2 = document.getElementById('customer-link-error');
      if(!e2){
        e2 = document.createElement('p');
        e2.id = 'customer-link-error';
        e2.className = 'helper';
        e2.style.color = 'var(--danger)';
        el.appendChild(e2);
      }
      e2.textContent = userMessage(err, 'The customer link could not be created.');
    }
  }
}

/* Clipboard with a real fallback - never a silent no-op, and never prompt(),
   which this UI deliberately removed. */
async function copyTextToClipboard(text){
  try{
    if(navigator.clipboard && window.isSecureContext){
      await navigator.clipboard.writeText(text);
      return true;
    }
  }catch(e){ /* fall through */ }
  try{
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    if(ok) return true;
  }catch(e){ /* fall through */ }
  showManualCopyField(text);
  return false;
}

/* Last resort: the link in a pre-selected read-only field. */
function showManualCopyField(text){
  const host = document.getElementById('resume-banner') || document.body;
  let box = document.getElementById('manual-copy-box');
  if(!box){
    box = document.createElement('div');
    box.id = 'manual-copy-box';
    box.className = 'manual-copy';
    host.appendChild(box);
  }
  box.innerHTML = '<label for="manual-copy-input">Copy this link manually</label>' +
                  '<input id="manual-copy-input" type="text" readonly>';
  const input = document.getElementById('manual-copy-input');
  input.value = text;
  input.focus();
  input.select();
}


/* ══════════════════════════════════════════════════════════════════════
   PORTAL GREETING + LIVE "COPY CUSTOMER LINK" MOUNT
   ══════════════════════════════════════════════════════════════════════ */

/* One greeting rule for BOTH customer entry paths - unified login and magic
   link. Previously only doLogin() wrote it, producing "Hi  - welcome back" when
   the first name was blank, and the magic-link path never wrote it at all, so
   those customers kept the static "Hi there". Layout and styling untouched. */
function renderPortalGreeting(customer){
  const hello = document.getElementById('portal-hello');
  if(!hello) return;
  const first = ((customer && customer.first_name) || '').trim();
  hello.textContent = first ? ('Hi, ' + first) : 'Welcome back';
}

/* Workflow keys at which the customer has something to do. AUTHORITATIVE keys
   only - never a human-readable label. Shared by both availability checks so
   they cannot drift apart. */
const CUSTOMER_ACTION_STEP_KEYS = ['proof_docs','self_attestation',
  'self_attestation_accept','contracts','contracts_review','contracts_accept'];

/* Live-flow availability: the same rule as customerLinkAvailable(), evaluated
   against the in-flight enrollment instead of a fetched detail row. */
function customerLinkAvailableLive(){
  if(!currentDraft || !currentDraft.enrollment_id) return false;
  // A completed enrollment never offers a new link.
  const detail = currentEnrollmentDetail;
  if(detail && detail.workflow_is_terminal === true) return false;
  if(perchContext.nextStepKey === 'contracts_accepted') return false;
  const email = (state.customer.email || perchContext.email || '').trim();
  if(!email) return false;
  return CUSTOMER_ACTION_STEP_KEYS.indexOf(perchContext.nextStepKey || '') !== -1;
}

/* Mounts, updates or removes the control in a dedicated slot inside the wizard.

   A dedicated slot rather than the resume banner: that banner is created only by
   renderResumeBanner() on open/resume, so a live straight-through enrollment had
   no host for the button at all. */
function refreshCustomerLinkControl(){
  const host = document.getElementById('view-wizard');
  if(!host) return;
  let slot = document.getElementById('customer-link-slot');
  if(!customerLinkAvailableLive()){
    if(slot && slot.parentNode) slot.parentNode.removeChild(slot);
    return;
  }
  if(!slot){
    slot = document.createElement('div');
    slot.id = 'customer-link-slot';
    slot.className = 'customer-link-slot';
    host.insertBefore(slot, host.firstChild);
  }
  // Rebuild only when the button is absent, so an in-progress "Copied" state is
  // not wiped by an unrelated navigation.
  if(!document.getElementById('copy-customer-link')){
    slot.innerHTML = '<button type="button" class="btn btn-secondary btn-sm" ' +
      'id="copy-customer-link" onclick="copyCustomerLink(' +
      currentDraft.enrollment_id + ', this)">Copy customer link</button>';
  }
}


/* ═══════════ Admin: sales summary ═══════════

   A small analytics panel above the Sales Report table. It counts COMPLETED
   SALES ONLY, and it does not decide what that means - /api/admin/sales-summary
   answers from the workflow's own terminal step, in the business timezone.
   Nothing here recomputes, re-buckets or re-dates anything the server sent;
   this file renders it.

   ALL THREE RANGES ARRIVE IN ONE RESPONSE, so Today / This Week / This Month
   switch with no network call at all - which is what makes the selector feel
   instant - and the three can never disagree with each other, because they
   were measured against one clock reading.

   Admin-only: the route enforces it, and a 403 hides the panel outright. */

let salesSummary = null;            // the whole payload, all three ranges
let salesSummaryRange = 'today';    // which one is on screen

const SUM_RANGES = ['today', 'week', 'month'];

function hideSalesSummary(){
  const host = document.getElementById('sales-summary');
  if(host) host.style.display = 'none';
  salesSummary = null;
}

async function loadSalesSummary(){
  const host = document.getElementById('sales-summary');
  const body = document.getElementById('ss-body');
  if(!host || !body) return;
  host.style.display = '';
  body.innerHTML = '<p class="ss-skeleton">Counting completed sales…</p>';
  let data;
  try{
    data = await apiFetch('/api/admin/sales-summary');
  }catch(e){
    // A viewer who may not see this must not see a half-drawn version of it.
    if(e && e.status === 403){ hideSalesSummary(); return; }
    // Anything else - including the server telling us its timezone database
    // is missing - is reported in the panel's own words. Showing counts built
    // on the wrong day boundary would be worse than showing none.
    body.innerHTML = '<div class="ss-empty">'
      + '<p class="ss-empty-title">Summary unavailable</p>'
      + '<p class="ss-empty-body">' + esc(e && e.message ? e.message :
          'The completed-sales summary could not be loaded.') + '</p></div>';
    return;
  }
  salesSummary = data;
  if(SUM_RANGES.indexOf(salesSummaryRange) === -1) salesSummaryRange = 'today';
  renderSalesSummary();
}

function setSalesSummaryRange(range){
  if(SUM_RANGES.indexOf(range) === -1) return;
  salesSummaryRange = range;
  renderSalesSummary();
}

/* The window a range covers, in the words an admin would use. The dates come
   from the server's own business-timezone boundaries, so what is printed here
   is the window the counts were actually taken over - not a second opinion
   formed from the browser's clock, which may be in another timezone entirely. */
function sumWindowText(view, payload){
  const w = (view && view.window) || null;
  if(!w) return '';
  const day = function(iso){
    const d = new Date(iso);
    if(isNaN(d.getTime())) return '';
    return d.toLocaleDateString(undefined,
      {weekday:'short', month:'short', day:'numeric'});
  };
  const start = day(w.start_local);
  if(!start) return '';
  if(view.range === 'today') return 'Completed on ' + start + ', Eastern time.';
  // The end boundary is the START of the next period, so the last day the
  // window actually covers is the day before it.
  const end = new Date(w.end_local);
  end.setDate(end.getDate() - 1);
  const label = view.range === 'week'
    ? 'Week of ' : 'Month of ';
  return label + start + ' – ' + day(end.toISOString()) + ', Eastern time.';
}

function sumCard(key, value, sub, isText, lead){
  return '<div class="ss-card' + (lead ? ' is-lead' : '') + '">'
    + '<p class="ss-k">' + esc(key) + '</p>'
    + '<p class="ss-v' + (isText ? ' is-text' : '') + '">' + esc(value) + '</p>'
    + (sub ? '<p class="ss-sub">' + esc(sub) + '</p>' : '')
    + '</div>';
}

function sumPlural(n, one, many){
  return n + ' ' + (n === 1 ? one : many);
}

function renderSalesSummary(){
  const host = document.getElementById('sales-summary');
  const body = document.getElementById('ss-body');
  const windowEl = document.getElementById('ss-window');
  if(!host || !body || !salesSummary) return;
  const view = (salesSummary.ranges || {})[salesSummaryRange];

  SUM_RANGES.forEach(function(r){
    const tab = document.getElementById('ss-tab-' + r);
    if(!tab) return;
    const on = r === salesSummaryRange;
    tab.classList.toggle('is-on', on);
    tab.setAttribute('aria-selected', on ? 'true' : 'false');
  });

  if(!view){ body.innerHTML = ''; return; }
  if(windowEl) windowEl.textContent = sumWindowText(view, salesSummary);

  // AN EMPTY RANGE IS AN ANSWER. A wall of zeroes reads as a broken panel;
  // one sentence reads as "nothing yet", which is what it means.
  if(!view.total){
    const when = view.range === 'today' ? 'yet today'
               : (view.range === 'week' ? 'yet this week' : 'yet this month');
    body.innerHTML = '<div class="ss-empty">'
      + '<svg class="ss-empty-ico" viewBox="0 0 24 24" fill="none" '
      + 'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" '
      + 'stroke-linejoin="round" aria-hidden="true">'
      + '<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/></svg>'
      + '<p class="ss-empty-title">No completed sales ' + esc(when) + '</p>'
      + '<p class="ss-empty-body">Sales appear here once the customer has '
      + 'accepted their agreement.</p></div>';
    return;
  }

  const topUtil = view.top_utility;
  const topProg = view.top_program;
  const cards = sumCard('Total completed sales', String(view.total),
                        view.label, false, true)
    + sumCard('Utilities active', String(view.utilities_active),
              view.utilities_active === 1 ? 'utility with a sale'
                                          : 'utilities with sales')
    + sumCard('Top utility', topUtil ? topUtil.utility : '—',
              topUtil ? sumPlural(topUtil.count, 'sale', 'sales')
                        + ' · ' + topUtil.share + '%' : '', true)
    + sumCard('Top program', topProg ? topProg.program : '—',
              topProg ? sumPlural(topProg.count, 'sale', 'sales')
                        + ' · ' + topProg.share + '%' : '', true);

  const unrecordedUtility = salesSummary.unrecorded_utility_label;
  const unrecordedProgram = salesSummary.unrecorded_program_label;

  const utilRows = (view.by_utility || []).map(function(u){
    const odd = u.utility === unrecordedUtility;
    return '<div class="ss-util">'
      + '<span class="ss-util-name' + (odd ? ' is-unrecorded' : '') + '">'
        + esc(u.utility) + '</span>'
      + '<span class="ss-util-count">' + sumPlural(u.count, 'sale', 'sales')
        + '<span class="ss-util-share">' + u.share + '%</span></span>'
      + '<span class="ss-bar"><span class="ss-bar-fill" style="width:'
        + u.share + '%"></span></span>'
      + '</div>';
  }).join('');

  const progGroups = (view.by_utility || []).map(function(u){
    const lines = (u.programs || []).map(function(p){
      const odd = p.program === unrecordedProgram;
      return '<div class="ss-line">'
        + '<span class="ss-line-name' + (odd ? ' is-unrecorded' : '') + '">'
          + esc(p.program) + '</span>'
        + '<span class="ss-line-n">' + p.count + '</span></div>';
    }).join('');
    return '<div class="ss-group"><p class="ss-group-head">' + esc(u.utility)
      + '</p>' + lines + '</div>';
  }).join('');

  body.innerHTML = '<div class="ss-grid">' + cards + '</div>'
    + '<div class="ss-panel"><div class="ss-panel-head">'
      + '<h3 class="ss-panel-title">By utility</h3>'
      + '<p class="ss-panel-note">' + sumPlural(view.total, 'completed sale',
          'completed sales') + '</p></div>'
      + utilRows + '</div>'
    + '<div class="ss-panel"><div class="ss-panel-head">'
      + '<h3 class="ss-panel-title">By program</h3>'
      + '<p class="ss-panel-note">Grouped by the utility they were sold on</p>'
      + '</div><div class="ss-groups">' + progGroups + '</div></div>';
}


/* ═══════════ Admin: sales report ═══════════
   Admin-only, the same way rep management is: the nav entry is hidden for
   everyone else, and /api/admin/sales-report enforces the role server-side.
   Hiding the link is convenience, not the security boundary.

   Agent Name comes from the enrollment's own sales_rep_id, resolved on the
   backend through sales_reps -> users.full_name. Nothing is typed by a rep and
   nothing is inferred from the customer. */

async function loadSalesReport(){
  const host = document.getElementById('sales-report-table');
  const err = document.getElementById('sales-report-error');
  const exportBtn = document.getElementById('sales-report-export');
  if(err) err.style.display = 'none';
  // Restored on every load, so an admin signing in after a rep gets the
  // control back. The button is only ever HIDDEN here - the server still
  // decides who may export, and that check is untouched.
  if(exportBtn) exportBtn.style.display = '';
  // The summary is a SEPARATE request and is deliberately not awaited: the
  // detailed table must not wait on it, and if the summary fails the table
  // still renders. Its own route enforces the same admin check.
  loadSalesSummary();
  if(!host) return;
  host.innerHTML = '<p class="helper">Loading sales report…</p>';
  let data;
  try{
    data = await apiFetch('/api/admin/sales-report');
  }catch(e){
    // NOT PERMITTED is a legitimate state, not a failure (C0 fix).
    //
    // Phase B made /sales-report reachable by URL, and a rep who follows one
    // used to get the backend's own sentence - "Role 'sales_rep' is not
    // permitted to access this resource" - as bare red text, under a page
    // heading, beside a live Export CSV button, above an empty white card.
    // Three things wrong at once: an internal role identifier on screen, an
    // offer the rep cannot accept, and no way onward.
    //
    // apiFetch attaches the HTTP status, so this branches on 403 rather than
    // matching the message text. 401 never arrives here - apiFetch handles an
    // expired session itself.
    if(e && e.status === 403){
      if(exportBtn) exportBtn.style.display = 'none';
      // Nothing of the summary may linger on a screen the viewer is not
      // entitled to - including from an earlier admin session in this tab.
      hideSalesSummary();
      renderSalesReportAdminOnly(host);
      return;
    }
    host.innerHTML = '';
    if(err){ err.textContent = e.message; err.style.display=''; }
    return;
  }
  const rows = (data && data.rows) || [];
  if(!rows.length){
    host.innerHTML = '<p class="helper">No enrollments yet.</p>';
    return;
  }
  const head = (data.columns || []).map(function(c){
    return '<th>' + esc(c) + '</th>'; }).join('');
  const body = rows.map(function(r){
    return '<tr>'
      + '<td data-label="Agent Name">'     + esc(r.agent_name || '—') + '</td>'
      + '<td data-label="Customer Name">'  + esc(r.customer_name || '—') + '</td>'
      + '<td data-label="Enrollment #">'   + esc(r.enrollment_code || '—') + '</td>'
      // Email and utility are the two cells allowed to ellipsise here, so
      // both carry their full value for a pointer; on a touch device the
      // hover-free rule wraps them in full instead.
      + '<td data-label="Customer Email" class="cell-trunc" title="'
        + esc(r.customer_email || '') + '">' + esc(r.customer_email || '—') + '</td>'
      // C5: the same semantic pill every other list uses, from the same
      // resolver and the same authoritative key - not bare text, and not a
      // colour guessed from the label. Read-only here: unlike the dashboard
      // and Customers pills this one does not open the enrollment, because
      // the Sales Report is a report, not a work queue.
      + '<td data-label="Status" class="cell-status">'
        + '<span class="status-pill ' + statusFamilyFor(r) + '">'
        + esc(statusShortLabel(r)) + '</span></td>'
      + '<td data-label="Utility" class="cell-util" title="'
        + esc(r.utility || '') + '">' + esc(r.utility || '—') + '</td>'
      // C5: date over time, as in every other table, instead of a third
      // date format that only this screen used.
      + '<td data-label="Added" class="cell-date" title="'
        + esc(formatReportDate(r.created_at)) + '">'
        + timestampCellHtml(r.created_at) + '</td>'
      + '<td data-label="Last Modified" class="cell-date" title="'
        + esc(formatReportDate(r.updated_at)) + '">'
        + timestampCellHtml(r.updated_at) + '</td>'
      + '</tr>';
  }).join('');
  host.innerHTML = '<div class="table-wrap"><table class="data-table"><thead><tr>'
                 + head + '</tr></thead><tbody>' + body + '</tbody></table></div>'
                 + '<p class="helper" style="margin-top:12px;">'
                 + rows.length + (rows.length === 1 ? ' enrollment' : ' enrollments') + '.</p>';
}

/* What a rep sees where the report would be.

   States what the screen is, why it is closed to them, and where to go next -
   in the product's own words. No role identifier, no HTTP status, no hint of
   the server's phrasing: the backend's refusal is the SERVER's business and is
   still logged there, while the rep gets a sentence written for a person. The
   one control is the way out, and it is secondary, because nothing here is an
   action the rep is being invited to take. */
function renderSalesReportAdminOnly(host){
  host.innerHTML =
    '<div class="state-block">' +
      '<svg class="state-ico" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
        '<rect x="4.5" y="10.5" width="15" height="9.5" rx="2.2" fill="none" ' +
          'stroke="currentColor" stroke-width="1.6"></rect>' +
        '<path d="M8.4 10.5V7.8a3.6 3.6 0 0 1 7.2 0v2.7" fill="none" ' +
          'stroke="currentColor" stroke-width="1.6" stroke-linecap="round"></path>' +
      '</svg>' +
      '<h2 class="state-title">Admins only</h2>' +
      // The page subtitle above already says what this report contains, so
      // this says the one thing it does not: who it is for, and what to do
      // about it. Repeating the description here was the same duplication the
      // audit flagged on the agreements screen.
      '<p class="state-body">This report is limited to admin accounts. Ask an ' +
        'admin for the figures you need.</p>' +
      '<button type="button" class="btn btn-secondary" ' +
        'onclick="showView(\'dashboard\')">Back to dashboard</button>' +
    '</div>';
}

/* ONE DATE STRATEGY (C5).
   UBS had three. formatTimestamp gave "Oct 28, 2026 at 12:04 AM" in the
   enrollment tables and on the agreements card; formatDate/formatClock gave
   the date-over-time cells C4 introduced; and this one called
   toLocaleString(), which renders "10/28/2026, 12:04:00 AM" - a different
   order, a different separator and seconds nobody asked for, on the one
   screen an admin exports from. Same instant, three appearances.

   It now delegates to the shared formatter, so there is one way UBS writes a
   moment and one place to change it. The parsing stays here because the
   report's rows come from a different query than parseBackendTimestamp was
   written for, and it has always tolerated both shapes. */
function formatReportDate(value){
  if(!value) return '—';
  const shared = formatTimestamp(value);
  if(shared && shared !== '—') return shared;
  const d = new Date(String(value).replace(' ', 'T') + (String(value).endsWith('Z') ? '' : 'Z'));
  if(isNaN(d.getTime())) return String(value);
  return formatTimestamp(d.toISOString());
}

/* The CSV is generated by the backend from the same query the table uses, so
   it holds every enrollment rather than the rendered rows. The request carries
   the bearer token, so it cannot be a plain link - the blob is fetched and
   handed to the browser as a download. */
async function exportSalesReport(){
  const btn = document.getElementById('sales-report-export');
  const err = document.getElementById('sales-report-error');
  if(err) err.style.display = 'none';
  const restore = btn ? btn.textContent : '';
  if(btn){ btn.disabled = true; btn.textContent = 'Exporting…'; }
  try{
    const token = AuthStore.getToken();
    const res = await fetch('/api/admin/sales-report.csv',
                            {headers: token ? {'Authorization': 'Bearer ' + token} : {}});
    if(!res.ok) throw new Error('The export could not be generated (' + res.status + ').');
    const blob = await res.blob();
    // Filename comes from the server's Content-Disposition when present, so the
    // UBS_Sales_Report_YYYY-MM-DD.csv format is defined in one place.
    const disposition = res.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="([^"]+)"/);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = match ? match[1] : 'UBS_Sales_Report.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }catch(e){
    if(err){ err.textContent = e.message; err.style.display=''; }
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = restore || 'Export CSV'; }
  }
}

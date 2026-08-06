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
    if(document.getElementById('app-shell').classList.contains('active')){
      showScreen('screen-login');
    }
    throw new Error('Your session expired — please sign in again.');
  }
  let data = null;
  try { data = await res.json(); } catch(e){ /* empty body, e.g. some 204s */ }
  if(!res.ok){
    throw new Error((data && data.error) || ('Request failed (' + res.status + ')'));
  }
  return data;
}

/* ==================== END PHASE 2 API CLIENT ==================== */

let projects = []; // populated by loadProjects() from GET /api/projects — see below

// NOTE (Phase 2): the wizard below still reads/writes this local `customers`
// array — the enrollment wizard itself is not yet connected to the backend
// (that's Phase 2 steps 10-15). It starts empty now instead of seeded with
// fake demo people; the Dashboard and Customers views below no longer read
// from this array at all — they load real enrollments from the API.
let customers = [];

const steps = [1,2,3,4,5];
const stepIds = {1:'step-project',2:'step-bill',3:'step-contact',4:'step-lmi',5:'step-agreement'};
const stepLabels = ['Project','Bill','Contact','LMI','Agreement'];

let state = {
  rep:{name:'Charlie Mren'},
  project:{id:'',name:'',utility:''},
  customer:{first:'',last:'',email:'',phone:'',acct:'',password:''},
  address:{street:'',unit:'',city:'',state:'NY',zip:''},
  bill:{fileName:'',amount:''},
  lmi:{na:false,docType:'',fileName:''}
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
  if(!email || !pass){ errEl.textContent = 'Enter your email and password to continue.'; errEl.style.display = 'block'; return; }

  btn.disabled = true;
  btn.textContent = 'Signing in…';
  try {
    const data = await apiFetch('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password: pass }),
    });
    AuthStore.setToken(data.token);
    currentUser = data.user;
  } catch(err){
    errEl.textContent = err.message;
    errEl.style.display = 'block';
    btn.disabled = false;
    btn.textContent = 'Sign in';
    return;
  }
  btn.disabled = false;
  btn.textContent = 'Sign in';
  applyCurrentUserToUI();
  await loadProjects();
  activateScreen('app-shell');
  showView('dashboard');
}

function applyCurrentUserToUI(){
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
  applyCurrentUserToUI();
  await loadProjects();
  activateScreen('app-shell');
  showView('dashboard');
}
document.addEventListener('DOMContentLoaded', tryRestoreSession);

function doCustomerLogin(){
  const email = document.getElementById('cust-login-email').value.trim().toLowerCase();
  const pass = document.getElementById('cust-login-pass').value;
  const errEl = document.getElementById('cust-login-error');
  if(!email || !pass){ alert('Enter your email and password to continue.'); return; }
  const match = customers.find(c => c.email.toLowerCase() === email && c.status === 'Opportunity - Review');
  if(!match || match.password !== pass){ errEl.style.display='block'; return; }
  errEl.style.display='none';
  loadRecordIntoState(match);
  activateScreen('screen-customer-portal');
  document.getElementById('portal-hello').textContent = 'Hi ' + match.first + ' — welcome back';
  document.getElementById('portal-project').textContent = match.projectName;
}

function showView(name){
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
  document.getElementById('view-'+name).classList.add('active');
  document.querySelectorAll('.nav-link[data-view]').forEach(n=>n.classList.toggle('active', n.dataset.view===name));
  if(name==='dashboard') renderDashboard();
  if(name==='projects') renderProjects('projects-list', true);
  if(name==='customers') renderCustomers('');
  window.scrollTo({top:0});
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
const DASH_BUCKETS = [
  { label: 'In Progress', statuses: ['Draft','Information Needed','Utility Bill Uploaded','Utility Validation','LMI Review','Agreement Ready'] },
  { label: 'Signature Pending', statuses: ['Signature Pending'] },
  { label: 'In Review', statuses: ['Signed','Internal Review','Needs Work','Rejected'] },
  { label: 'Submitted', statuses: ['Verified','Submitted','Developer Review'] },
  { label: 'Active', statuses: ['Accepted','Project Assigned','Active'] },
];

async function renderDashboard(){
  document.getElementById('dash-greeting').textContent = 'Good afternoon, ' + (currentUser ? (currentUser.full_name || currentUser.email).split(' ')[0] : '');
  await Promise.all([loadProjects(), loadEnrollments()]);

  document.getElementById('dash-stats').innerHTML = DASH_BUCKETS.map(bucket => {
    const n = enrollments.filter(e => bucket.statuses.includes(e.status)).length;
    return '<div class="stat-card'+(bucket.label==='Active' ? ' stat-verified' : '')+'"><div class="sc-num">'+n+'</div><div class="sc-label">'+bucket.label+'</div></div>';
  }).join('');

  renderProjects('dash-projects', false);

  const recent = [...enrollments].sort((a,b)=> (a.updated_at < b.updated_at ? 1 : -1)).slice(0, 5);
  document.getElementById('dash-recent-body').innerHTML = recent.length
    ? recent.map(enrollmentRowHtml).join('')
    : '<tr><td colspan="5" style="text-align:center;color:var(--ink-faint);padding:26px;">No enrollments yet.</td></tr>';
}

function renderProjects(targetId, showAllActions){
  const wrap = document.getElementById(targetId);
  const list = showAllActions ? projects : projects.slice(0,2);
  wrap.innerHTML = list.map(p=>{
    const statusWord = p.full ? 'Full' : (p.pct < 15 ? 'Open' : 'Filling');
    const actionsHtml = p.full
      ? '<button class="btn btn-ghost" disabled>Signup disabled</button>'
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
          '<button class="btn btn-ghost btn-sm" onclick="alert(\'Full project detail pages aren&#92;&#39;t wired up in this demo yet.\')">Project details</button>' +
        '</div>' +
      '</div>' +
    '</div>';
  }).join('');
}

// Real backend status -> pill color. Terminal-success statuses read as
// "verified" green, anything flagged for attention reads as danger, and
// everything mid-pipeline keeps the existing gold "in progress" look.
function statusPillHtml(status){
  const verified = ['Accepted','Project Assigned','Active'].includes(status);
  const attention = ['Rejected','Needs Work'].includes(status);
  const cls = verified ? 'status-verified' : (attention ? 'status-danger' : 'status-opp');
  return '<span class="status-pill '+cls+'">'+status+'</span>';
}

function enrollmentRowHtml(e){
  const custName = e.customer ? (e.customer.first_name+' '+e.customer.last_name) : '(no customer info yet)';
  const custEmail = e.customer ? e.customer.email : '';
  const projectName = e.project ? e.project.name : '—';
  return '<tr><td><div class="cust-name">'+custName+'</div><div class="cust-sub">'+custEmail+'</div></td><td>'+projectName+'</td><td>'+statusPillHtml(e.status)+'</td><td>'+formatDate(e.updated_at)+'</td><td style="text-align:right;"><span style="color:var(--ink-faint);font-size:12px;">'+e.enrollment_code+'</span></td></tr>';
}

function formatDate(iso){
  if(!iso) return '—';
  return iso.slice(0,10);
}

async function renderCustomers(query){
  if(!enrollments.length) await loadEnrollments();
  const q = (query||'').toLowerCase();
  const filtered = enrollments.filter(e => {
    const custName = e.customer ? (e.customer.first_name+' '+e.customer.last_name+' '+e.customer.email) : '';
    const projectName = e.project ? e.project.name : '';
    return !q || (custName+' '+projectName+' '+e.enrollment_code).toLowerCase().includes(q);
  });
  const sorted = [...filtered].sort((a,b)=> (a.updated_at < b.updated_at ? 1 : -1));
  document.getElementById('customers-body').innerHTML = sorted.length
    ? sorted.map(e => {
        const custName = e.customer ? (e.customer.first_name+' '+e.customer.last_name) : '(no customer info yet)';
        const custEmail = e.customer ? e.customer.email : '';
        const projectName = e.project ? e.project.name : '—';
        return '<tr><td><div class="cust-name">'+custName+'</div><div class="cust-sub">'+custEmail+'</div></td><td>'+projectName+'</td><td>'+statusPillHtml(e.status)+'</td><td>'+formatDate(e.created_at)+'</td><td>'+formatDate(e.updated_at)+'</td><td style="text-align:right;"><span style="color:var(--ink-faint);font-size:12px;">'+e.enrollment_code+'</span></td></tr>';
      }).join('')
    : '<tr><td colspan="6" style="text-align:center;color:var(--ink-faint);padding:26px;">No enrollments match that search.</td></tr>';
}

function resetWizardState(){
  state.customer = {first:'',last:'',email:'',phone:'',acct:'',password:''};
  state.address = {street:'',unit:'',city:'',state:'NY',zip:''};
  state.bill = {fileName:'',amount:''};
  state.lmi = {mode:'doc',docType:'',fileName:'',householdSize:'',incomeBelow:null};
  currentCustomerId = null;
  clearWizardForms();
}
function clearWizardForms(){
  ['c-first','c-last','c-email','c-phone','c-acct','c-pass','c-pass-confirm','a-street','a-unit','a-city','a-zip'].forEach(id=>{ const el=document.getElementById(id); if(el) el.value=''; });
  document.getElementById('bill-amount').value='';
  document.getElementById('bill-file-chip').innerHTML='';
  document.getElementById('ocr-container').innerHTML='';
  document.getElementById('lmi-doctype').value='';
  document.getElementById('lmi-file-chip').innerHTML='';
  document.getElementById('lmi-check-container').innerHTML='';
  document.getElementById('lmi-household-size').value='';
  document.getElementById('ami-threshold-display').textContent='';
  document.getElementById('ami-below').classList.remove('selected');
  document.getElementById('ami-above').classList.remove('selected');
  document.getElementById('lmi-mode-doc').classList.remove('selected');
  document.getElementById('lmi-mode-attest').classList.remove('selected');
  document.getElementById('lmi-mode-na').classList.remove('selected');
  document.getElementById('lmi-doc-panel').style.display='block';
  document.getElementById('lmi-attest-panel').style.display='none';
  document.getElementById('btn-lmi-next').disabled=true;
  document.getElementById('btn-bill-next').disabled=true;
  document.getElementById('pre-send').style.display='block';
  document.getElementById('post-send').style.display='none';
}

/* ==================== MILESTONE 2: WORKFLOW RENDERER ====================

   The frontend is a RENDERER, not a page sequence.

   It asks the backend "what step am I on?" and draws whatever descriptor comes
   back: fields, validations, panels, actions. It contains no knowledge of
   community solar, no hardcoded step order, and no business rules. Perch owns
   the workflow (via its next_step URLs); the backend translates that into a
   descriptor; this code draws it.

   Adding Milestone 3's enrollment step requires ZERO changes in this file -
   it's a new step builder in services/perch/workflow.py.

   Descriptor contract (see services/perch/workflow.py):
     step.key, .eyebrow, .title, .subtitle
     step.fields[]  {name, label, type, required, value, options[], validation{pattern,message},
                     placeholder, input_mode, max_length, mono}
     step.panels[]  {type: 'capacity_summary'|'notice', ...}
     step.primary_action / .secondary_action  {label, operation, enabled, disabled_reason}
*/

let currentDraft = null;     // {enrollment_id, enrollment_code} - created BEFORE any Perch call
let currentWorkflow = null;  // last descriptor from GET .../workflow

async function startWizardFresh(){
  resetWizardState();
  state.project = {id:'',name:'',utility:''};
  skipProjectStep = false;
  currentDraft = null;
  currentWorkflow = null;
  showView('wizard');
  goStep(1);
  renderWorkflowLoading('Starting a new enrollment...');

  // A Dalton Enrollment ID is issued before any Perch call. Perch's enrollment
  // token is session-scoped and expires in 30 minutes, so it can never be the
  // durable key for an enrollment.
  try {
    currentDraft = await apiFetch('/api/perch/drafts', {method: 'POST'});
  } catch(err){
    renderWorkflowError('Could not start a new enrollment: ' + err.message);
    return;
  }
  await loadWorkflow();
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
    '<button class="btn btn-ghost" onclick="exitWizard()">Back to dashboard</button></div></div>';
}

function renderField(f){
  const req = f.required ? '' : ' <span style="font-weight:400;color:var(--ink-faint);">(optional)</span>';
  let control;
  if(f.type === 'select'){
    const opts = ['<option value="">' + esc(f.placeholder || 'Select...') + '</option>']
      .concat((f.options||[]).map(o =>
        '<option value="' + esc(o.value) + '"' + (o.value === f.value ? ' selected' : '') + '>' +
        esc(o.label) + '</option>'));
    control = '<select id="wf-' + esc(f.name) + '" data-field="' + esc(f.name) + '">' + opts.join('') + '</select>';
  } else {
    control = '<input type="text" id="wf-' + esc(f.name) + '" data-field="' + esc(f.name) + '"' +
      (f.mono ? ' class="mono-field"' : '') +
      (f.input_mode ? ' inputmode="' + esc(f.input_mode) + '"' : '') +
      (f.max_length ? ' maxlength="' + f.max_length + '"' : '') +
      (f.placeholder ? ' placeholder="' + esc(f.placeholder) + '"' : '') +
      ' value="' + esc(f.value || '') + '">';
  }
  const help = f.help ? '<p class="helper">' + esc(f.help) + '</p>' : '';
  return '<div class="field"><label>' + esc(f.label) + req + '</label>' + control + help +
    '<p class="helper wf-field-error" id="wf-err-' + esc(f.name) + '" style="display:none;color:var(--danger);"></p></div>';
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
  const root = document.getElementById('workflow-root');

  const draftBanner = currentDraft
    ? '<div class="draft-banner"><span>Enrollment <strong>' + esc(currentDraft.enrollment_code) + '</strong></span>' +
      (wf.step.perch_next_step && wf.step.perch_next_step.recognized === false
        ? '<span class="badge badge-gold">Unrecognized next step</span>' : '') +
      '</div>'
    : '';

  const fields = (step.fields||[]).map(renderField).join('');
  const panels = (step.panels||[]).map(renderPanel).join('');

  const pa = step.primary_action;
  const sa = step.secondary_action;
  const primaryDisabled = pa && pa.enabled === false;
  const primaryBtn = pa
    ? '<button class="btn btn-primary" id="wf-primary"' + (primaryDisabled ? ' disabled' : '') +
      ' onclick="runWorkflowOperation(\'' + esc(pa.operation) + '\')">' + esc(pa.label) + '</button>'
    : '<span></span>';
  const secondaryBtn = sa
    ? '<button class="btn btn-ghost" onclick="runWorkflowOperation(\'' + esc(sa.operation) + '\')">' +
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
      panels +
      '<p class="helper" id="wf-form-error" style="display:none;color:var(--danger);margin-top:12px;"></p>' +
      disabledNote +
      '<div class="wizard-actions">' + secondaryBtn + primaryBtn + '</div>' +
    '</div>';

  applyProgressHint(wf.progress);
}

// The step labels above the card come from the backend's progress hint, which is
// presentational only - the authoritative sequence is Perch's next_step URLs.
function applyProgressHint(progress){
  if(!progress) return;
  const wrap = document.getElementById('step-labels');
  if(!wrap) return;
  wrap.innerHTML = progress.map(p =>
    '<div class="step-label ' + (p.state === 'done' ? 'done' : (p.state === 'current' ? 'now' : '')) + '">' +
    esc(p.label) + '</div>').join('');
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
      msg = (f.validation && f.validation.message) || (f.label + ' is required.');
    } else if(val && f.validation && f.validation.pattern){
      if(!(new RegExp(f.validation.pattern)).test(val)){
        msg = f.validation.message || ('That ' + f.label + ' does not look right.');
      }
    }
    if(msg){
      if(errEl){ errEl.textContent = msg; errEl.style.display = 'block'; }
      if(!firstError) firstError = msg;
    }
  });
  return {values: values, error: firstError};
}

/* ---------- operations ---------- */

async function runWorkflowOperation(op){
  if(op === 'exit'){ exitWizard(); return; }
  if(op === 'restart_service_area'){
    // Re-checking capacity is always a fresh Perch call - stored checks are
    // audit records, never a cache, because Perch enforces live rates at enroll.
    currentWorkflow.step = null;
    renderWorkflowLoading('Loading...');
    await loadWorkflow();
    return;
  }
  if(op === 'check_capacity'){ await submitCapacity(); return; }
  if(op === 'advance'){ return; }  // disabled until Milestone 3
  console.warn('Unknown workflow operation:', op);
}

async function submitCapacity(){
  const step = currentWorkflow.step;
  const {values, error} = collectAndValidate(step);
  const formErr = document.getElementById('wf-form-error');
  formErr.style.display = 'none';
  if(error) return;

  const btn = document.getElementById('wf-primary');
  btn.disabled = true;
  btn.textContent = 'Checking with Perch...';

  let body;
  try {
    body = await apiFetch('/api/perch/enrollments/' + currentDraft.enrollment_id + '/capacity', {
      method: 'POST',
      // email is sent because Perch requires it on POST /token, which the
      // backend issues before the capacity call.
      body: JSON.stringify({zip_code: values.zip_code, utility_name: values.utility_name,
                            email: values.email}),
    });
  } catch(err){
    btn.disabled = false;
    btn.textContent = step.primary_action.label;
    formErr.textContent = err.message;
    formErr.style.display = 'block';
    return;
  }
  currentWorkflow = body.workflow;
  renderWorkflowStep(currentWorkflow);
}

// Dashboard project cards still use the legacy /api/projects list. Those cards
// disappear when the dashboard becomes Perch-driven; this keeps them working.
function startWizardForProject(projId){
  const p = projects.find(x=>String(x.id)===String(projId));
  if(!p){ alert('Could not find that project - try refreshing the page.'); return; }
  resetWizardState();
  state.project = {id:p.id, name:p.name, utility:p.utility};
  skipProjectStep = true;
  showView('wizard');
  goStep(2);
}
function exitWizard(){ showView('dashboard'); }

function backFromCustomer(){ if(skipProjectStep){ exitWizard(); } else { goStep(1); } }

function buildStepLabels(){
  const wrap = document.getElementById('step-labels');
  wrap.innerHTML = '';
  stepLabels.forEach(l=>{ const s=document.createElement('div'); s.className='step-label'; s.textContent=l; wrap.appendChild(s); });
}
function quadPoint(t){
  const p0={x:20,y:66}, p1={x:200,y:4}, p2={x:380,y:66};
  const x = (1-t)*(1-t)*p0.x + 2*(1-t)*t*p1.x + t*t*p2.x;
  const y = (1-t)*(1-t)*p0.y + 2*(1-t)*t*p1.y + t*t*p2.y;
  return {x,y};
}
function goStep(n){
  document.querySelectorAll('.wizard-step').forEach(s=>s.classList.remove('active'));
  document.getElementById(stepIds[n]).classList.add('active');
  const t = (n-1)/(steps.length-1);
  const pt = quadPoint(t);
  document.getElementById('arc-sun').setAttribute('cx', pt.x);
  document.getElementById('arc-sun').setAttribute('cy', pt.y);
  document.getElementById('arc-progress').setAttribute('stroke-dashoffset', 1-t);
  document.querySelectorAll('.step-label').forEach((el,i)=>{
    el.classList.remove('done','now');
    if(i < n-1) el.classList.add('done');
    if(i === n-1) el.classList.add('now');
  });
  hydrateStep(n);
  if(n===5) fillReview();
  window.scrollTo({top:0,behavior:'smooth'});
}
function hydrateStep(n){
  if(n===2){
    document.getElementById('c-first').value = state.customer.first;
    document.getElementById('c-last').value = state.customer.last;
    document.getElementById('c-acct').value = state.customer.acct;
    document.getElementById('a-street').value = state.address.street;
    document.getElementById('a-unit').value = state.address.unit;
    document.getElementById('a-city').value = state.address.city;
    document.getElementById('a-zip').value = state.address.zip;
    document.getElementById('bill-amount').value = state.bill.amount || '';
    if(state.bill.fileName){ showBillChip(state.bill.fileName); }
    checkBillReady();
  }
  if(n===3){
    document.getElementById('c-email').value = state.customer.email;
    document.getElementById('c-phone').value = state.customer.phone;
    document.getElementById('c-pass').value = state.customer.password || '';
    document.getElementById('c-pass-confirm').value = state.customer.password || '';
  }
  if(n===4){
    const mode = state.lmi.mode || 'doc';
    setLmiMode(mode);
    if(mode === 'doc' && state.lmi.fileName){ document.getElementById('lmi-doctype').value = state.lmi.docType; showLmiChip(state.lmi.fileName); }
    if(mode === 'attest'){
      document.getElementById('lmi-household-size').value = state.lmi.householdSize || '';
      updateAmiThreshold();
      if(state.lmi.incomeBelow === true || state.lmi.incomeBelow === false) setIncomeAnswer(state.lmi.incomeBelow);
    }
    checkLmiReady();
  }
}

function checkBillReady(){
  const first = document.getElementById('c-first').value.trim();
  const last = document.getElementById('c-last').value.trim();
  const acct = document.getElementById('c-acct').value.trim();
  const street = document.getElementById('a-street').value.trim();
  const city = document.getElementById('a-city').value.trim();
  const zip = document.getElementById('a-zip').value.trim();
  const amt = document.getElementById('bill-amount').value;
  const ready = first && last && acct.length===10 && street && city && zip.length===5 && amt;
  document.getElementById('btn-bill-next').disabled = !ready;
}
function submitBill(){
  state.customer.first = document.getElementById('c-first').value.trim();
  state.customer.last = document.getElementById('c-last').value.trim();
  state.customer.acct = document.getElementById('c-acct').value.trim();
  state.address = {
    street: document.getElementById('a-street').value.trim(),
    unit: document.getElementById('a-unit').value.trim(),
    city: document.getElementById('a-city').value.trim(),
    state: 'NY',
    zip: document.getElementById('a-zip').value.trim()
  };
  state.bill.amount = document.getElementById('bill-amount').value;
  upsertCustomerRecord({status:'Opportunity - Address'});
  goStep(3);
}
function submitContact(){
  const email = document.getElementById('c-email').value.trim();
  const phone = document.getElementById('c-phone').value.trim();
  const pass = document.getElementById('c-pass').value;
  const passConfirm = document.getElementById('c-pass-confirm').value;
  if(!email || !phone || !pass || !passConfirm){ alert('Fill in every field before continuing.'); return; }
  if(pass.length < 6){ alert('Password should be at least 6 characters.'); return; }
  if(pass !== passConfirm){ alert('Passwords don\'t match.'); return; }
  state.customer.email = email;
  state.customer.phone = phone;
  state.customer.password = pass;
  upsertCustomerRecord({status:'Opportunity - Utility'});
  goStep(4);
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
  const result = {first:'', last:'', street:'', city:'', zip:'', acct:'', amount:null};
  let m = t.match(/ACCOUNT NUMBER\s*(\d{5}-\d{5}|\d{10})/i);
  if(m) result.acct = m[1].replace(/-/g,'');
  m = t.match(/AMOUNT DUE\D{0,25}?\$?\s*([\d,]+\.\d{2})/i);
  if(m) result.amount = parseFloat(m[1].replace(/,/g,''));
  m = t.match(/SERVICE FOR\s+([A-Z][A-Z .'-]+?)\s+(\d+[A-Z0-9 .'-]*?)\s+([A-Za-z .'-]+?)\s+([A-Z]{2})\s+(\d{5})/);
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
function amountToBracket(amount){
  if(amount==null) return '';
  if(amount < 75) return 'Less than $75';
  if(amount < 150) return '$75 – $149';
  if(amount < 250) return '$150 – $249';
  return '$250+';
}

function showBillChip(name){
  document.getElementById('bill-file-chip').innerHTML = '<div class="file-chip"><div class="fc-left"><div class="fc-icon">📄</div><div><div class="fc-name">'+name+'</div><div class="fc-size">Uploaded</div></div></div><button class="fc-remove" onclick="removeBill()">Remove</button></div>';
}
function ocrRow(label, ok, valueText){
  return '<div class="ocr-row"><div class="ocr-left"><div class="ocr-check'+(ok?'':' warn')+'"></div>'+label+'</div><span class="ocr-conf">'+valueText+'</span></div>';
}
async function handleBillUpload(files){
  if(!files.length) return;
  const f = files[0];
  state.bill.fileName = f.name;
  showBillChip(f.name);
  const c = document.getElementById('ocr-container');
  c.innerHTML = '<div class="ocr-panel"><div class="ocr-analyzing"><div class="spinner"></div>Reading the bill…</div></div>';
  try{
    const text = await extractTextFromFile(f);
    const parsed = parseUtilityBill(text);
    if(parsed.first) document.getElementById('c-first').value = parsed.first;
    if(parsed.last) document.getElementById('c-last').value = parsed.last;
    if(parsed.acct) document.getElementById('c-acct').value = parsed.acct;
    if(parsed.street) document.getElementById('a-street').value = parsed.street;
    if(parsed.city) document.getElementById('a-city').value = parsed.city;
    if(parsed.zip) document.getElementById('a-zip').value = parsed.zip;
    if(parsed.amount != null) document.getElementById('bill-amount').value = amountToBracket(parsed.amount);
    c.innerHTML = '<div class="ocr-panel"><div class="ocr-head">Pulled from the bill</div>' +
      ocrRow('Customer name', !!parsed.first, parsed.first ? 'Found' : 'Not found — enter manually') +
      ocrRow('Service address', !!parsed.street, parsed.street ? 'Found' : 'Not found — enter manually') +
      ocrRow('Account number', !!parsed.acct, parsed.acct ? 'Found' : 'Not found — enter manually') +
      ocrRow('Bill amount', parsed.amount!=null, parsed.amount!=null ? ('$'+parsed.amount.toFixed(2)) : 'Not found — select manually') +
      '</div>';
  } catch(err){
    c.innerHTML = '<div class="ocr-panel"><div class="ocr-analyzing">Couldn\'t read this file automatically — enter the details below by hand.</div></div>';
  }
  checkBillReady();
}
function removeBill(){
  state.bill.fileName='';
  document.getElementById('bill-file-chip').innerHTML='';
  document.getElementById('ocr-container').innerHTML='';
  document.getElementById('bill-file').value='';
  checkBillReady();
}

/* ---------------- LMI DOCUMENT CHECKER ---------------- */
const lmiTypes = [
  {label:'Electric bill showing HEAP/LIHEAP/EAP assistance', dac:false, re:/\bHEAP\b|\bLIHEAP\b|Energy Assistance|\bEAP\b|Energy Affordability Credit|Billing Adjustments/i},
  {label:'SNAP award letter', dac:false, re:/SNAP.{0,30}(award|notice|eligib|approv)/i},
  {label:'SNAP card', dac:false, re:/\bSNAP\b/i},
  {label:'Housing authority certification / Section 8', dac:false, re:/Section\s*8|Housing Authority|Tenant Eligibility|\bHUD\b/i},
  {label:'Disability benefits letter', dac:false, re:/Disability Benefits|SSDI/i},
  {label:'SSI', dac:false, re:/\bSSI\b|Supplemental Security Income/i},
  {label:'Medicaid award letter', dac:true, re:/Medicaid|NY State of Health|Essential Plan/i},
  {label:'Lifeline qualification', dac:true, re:/\bLifeline\b/i},
  {label:'SLIP', dac:true, re:/\bSLIP\b/i},
];
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
function showLmiChip(name){
  document.getElementById('lmi-file-chip').innerHTML = '<div class="file-chip"><div class="fc-left"><div class="fc-icon">📄</div><div><div class="fc-name">'+name+'</div><div class="fc-size">Uploaded</div></div></div><button class="fc-remove" onclick="removeLmi()">Remove</button></div>';
}
const amiTable = [
  {size:1, amount:61750},{size:2, amount:70550},{size:3, amount:79350},{size:4, amount:88150},
  {size:5, amount:95250},{size:6, amount:102300},{size:7, amount:109350},{size:8, amount:116400},
];
function setLmiMode(mode){
  state.lmi.mode = mode;
  document.getElementById('lmi-mode-doc').classList.toggle('selected', mode==='doc');
  document.getElementById('lmi-mode-attest').classList.toggle('selected', mode==='attest');
  document.getElementById('lmi-mode-na').classList.toggle('selected', mode==='na');
  document.getElementById('lmi-doc-panel').style.display = mode==='doc' ? 'block' : 'none';
  document.getElementById('lmi-attest-panel').style.display = mode==='attest' ? 'block' : 'none';
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
  if(state.lmi.mode !== 'doc') setLmiMode('doc');
  const f = files[0];
  state.lmi.fileName = f.name;
  showLmiChip(f.name);
  const c = document.getElementById('lmi-check-container');
  c.innerHTML = '<div class="ocr-panel"><div class="ocr-analyzing"><div class="spinner"></div>Reading the document…</div></div>';
  try{
    const text = await extractTextFromFile(f);
    const analysis = classifyLmiDoc(text);
    if(analysis.matched){ document.getElementById('lmi-doctype').value = analysis.matched.label; }
    renderLmiCheckPanel(analysis);
  } catch(err){
    c.innerHTML = '<div class="ocr-panel"><div class="ocr-analyzing">Couldn\'t read this file automatically — select the document type manually.</div></div>';
  }
  checkLmiReady();
}
function removeLmi(){
  state.lmi.fileName='';
  document.getElementById('lmi-file-chip').innerHTML='';
  document.getElementById('lmi-check-container').innerHTML='';
  document.getElementById('lmi-file').value='';
  checkLmiReady();
}
function checkLmiReady(){
  state.lmi.docType = document.getElementById('lmi-doctype').value;
  let ready = false;
  if(state.lmi.mode === 'na') ready = true;
  else if(state.lmi.mode === 'attest') ready = !!(state.lmi.householdSize && (state.lmi.incomeBelow === true || state.lmi.incomeBelow === false));
  else ready = !!(state.lmi.docType && state.lmi.fileName);
  document.getElementById('btn-lmi-next').disabled = !ready;
}
function submitLmi(){ upsertCustomerRecord({status:'Opportunity - Contract'}); goStep(5); }

function fillReview(){
  document.getElementById('rv-name').textContent = state.customer.first+' '+state.customer.last;
  document.getElementById('rv-email').textContent = state.customer.email;
  document.getElementById('rv-phone').textContent = state.customer.phone;
  document.getElementById('rv-acct').textContent = state.customer.acct;
  document.getElementById('rv-project').textContent = state.project.name;
  document.getElementById('rv-utility').textContent = state.project.utility;
  document.getElementById('rv-address').textContent = state.address.street+(state.address.unit ? ', '+state.address.unit : '')+', '+state.address.city+', NY '+state.address.zip;
  document.getElementById('rv-bill').textContent = state.bill.amount;
  document.getElementById('rv-lmi').textContent =
    state.lmi.mode === 'na' ? 'N/A' :
    state.lmi.mode === 'attest' ? ('Self-attested — household of '+state.lmi.householdSize+', income '+(state.lmi.incomeBelow ? 'below' : 'above')+' 80% AMI') :
    (state.lmi.docType+' — '+state.lmi.fileName);
}
function sendAgreement(){
  document.getElementById('pre-send').style.display = 'none';
  document.getElementById('post-send').style.display = 'block';
  document.getElementById('post-send-contact').textContent = state.customer.phone || state.customer.email;
  const token = Math.random().toString(36).slice(2,10);
  document.getElementById('magic-link-text').textContent = 'https://sign.daltonsolar.com/a/'+token;
  upsertCustomerRecord({status:'Opportunity - Review'});
}

function todayStr(){ return new Date().toISOString().slice(0,10); }
function upsertCustomerRecord(patch){
  const base = {
    first: state.customer.first, last: state.customer.last, email: state.customer.email,
    phone: state.customer.phone, acct: state.customer.acct, password: state.customer.password,
    projectId: state.project.id, projectName: state.project.name, utility: state.project.utility,
    address: Object.assign({}, state.address), bill: Object.assign({}, state.bill), lmi: Object.assign({}, state.lmi),
    lastModified: todayStr()
  };
  if(currentCustomerId){
    const idx = customers.findIndex(c=>c.id===currentCustomerId);
    if(idx>-1){ customers[idx] = Object.assign({}, customers[idx], base, patch); return; }
  }
  const id = 'c' + Math.random().toString(36).slice(2,9);
  customers.push(Object.assign({id:id, dateAdded: todayStr(), status:'Opportunity - Address'}, base, patch));
  currentCustomerId = id;
}
function loadRecordIntoState(rec){
  currentCustomerId = rec.id;
  state.project = {id: rec.projectId, name: rec.projectName, utility: rec.utility};
  state.customer = {first:rec.first,last:rec.last,email:rec.email,phone:rec.phone,acct:rec.acct,password:rec.password};
  state.address = Object.assign({}, rec.address);
  state.bill = Object.assign({}, rec.bill);
  state.lmi = Object.assign({}, rec.lmi);
}
function resumeCustomer(id){
  const rec = customers.find(c=>c.id===id);
  if(!rec) return;
  loadRecordIntoState(rec);
  skipProjectStep = true;
  clearWizardForms();
  showView('wizard');
  const target = resumeStepFor(rec.status);
  goStep(target || 2);
  if(rec.status === 'Opportunity - Review'){
    document.getElementById('pre-send').style.display='none';
    document.getElementById('post-send').style.display='block';
    document.getElementById('post-send-contact').textContent = rec.phone || rec.email;
    document.getElementById('magic-link-text').textContent = 'https://sign.daltonsolar.com/a/'+Math.random().toString(36).slice(2,10);
  }
}

function enterCustomerSign(mode){
  entryMode = mode;
  activateScreen('screen-customer');
  document.getElementById('cust-hello').textContent = 'Hi '+state.customer.first+' — one step left';
  document.getElementById('cv-name').textContent = state.customer.first+' '+state.customer.last;
  document.getElementById('cv-address').textContent = state.address.street+(state.address.unit ? ', '+state.address.unit : '')+', '+state.address.city+', NY '+state.address.zip;
  document.getElementById('cv-utility').textContent = state.project.utility;
  document.getElementById('cv-acct').textContent = state.customer.acct;
  renderDocPacket();
  initSigCanvas();
}

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
let docReviewed = {};
function buildLmiSummary(){
  if(state.lmi.mode === 'attest'){
    return '<p>You self-attested that your household of <strong>'+state.lmi.householdSize+'</strong> has income <strong>'+(state.lmi.incomeBelow ? 'below' : 'above')+'</strong> 80% of the State Median Income for that household size.</p>'+
      '<p style="font-size:11.5px;">1: $61,750 &nbsp;2: $70,550 &nbsp;3: $79,350 &nbsp;4: $88,150 &nbsp;5: $95,250 &nbsp;6: $102,300 &nbsp;7: $109,350 &nbsp;8: $116,400</p>'+
      '<p>This information is collected by Arcadia and shared with NYSERDA for program evaluation and incentive determination. It will not be shared or published at the individual customer level.</p>';
  }
  if(state.lmi.mode === 'doc'){
    return '<p>You provided documentation (<strong>'+(state.lmi.docType || 'a qualifying document')+'</strong>) to support eligibility for the low-income program adder. Your rep will confirm this meets NY program requirements.</p>';
  }
  return '<p>No low-income program documentation was submitted for this enrollment.</p>';
}
function renderDocPacket(){
  const wrap = document.getElementById('doc-packet');
  const savingsPct = state.project.savingsPct != null ? state.project.savingsPct : 5;
  const ctx = {
    utility: state.project.utility || 'your utility',
    custName: state.customer.first+' '+state.customer.last,
    address: state.address.street+(state.address.unit ? ', '+state.address.unit : '')+', '+state.address.city+', NY '+state.address.zip,
    savingsPct: savingsPct,
    exampleSavings: savingsPct.toFixed(2),
    lmiSummary: buildLmiSummary()
  };
  docReviewed = {};
  wrap.innerHTML = docPacket.map(d=>{
    docReviewed[d.id] = false;
    return '<details class="doc-item" id="doc-'+d.id+'">'+
      '<summary><span class="di-chevron">›</span><span><span class="di-title">'+d.title+'</span><div class="di-sub">'+d.sub+'</div></span><span class="di-status" id="di-status-'+d.id+'">Not reviewed</span></summary>'+
      '<div class="doc-item-body">'+d.body(ctx)+'</div>'+
      '<label class="doc-item-check"><input type="checkbox" onchange="markDocReviewed(\''+d.id+'\', this.checked)"> I\'ve reviewed this document</label>'+
    '</details>';
  }).join('');
  checkCustomerReady();
}
function markDocReviewed(id, val){
  docReviewed[id] = val;
  const statusEl = document.getElementById('di-status-'+id);
  if(statusEl){ statusEl.textContent = val ? 'Reviewed' : 'Not reviewed'; statusEl.classList.toggle('done', val); }
  checkCustomerReady();
}
function allDocsReviewed(){
  const keys = Object.keys(docReviewed);
  return keys.length>0 && keys.every(k=>docReviewed[k]);
}

let sigCtx, isDrawing = false, hasSigned = false;
function initSigCanvas(){
  const canvas = document.getElementById('sig-canvas');
  sigCtx = canvas.getContext('2d');
  sigCtx.clearRect(0,0,canvas.width,canvas.height);
  sigCtx.strokeStyle = '#122720';
  sigCtx.lineWidth = 3;
  sigCtx.lineCap = 'round';
  sigCtx.lineJoin = 'round';
  hasSigned = false;
  document.getElementById('sig-canvas-wrap').classList.remove('signed');
  function pos(e){
    const r = canvas.getBoundingClientRect();
    const clientX = e.touches ? e.touches[0].clientX : e.clientX;
    const clientY = e.touches ? e.touches[0].clientY : e.clientY;
    const scaleX = canvas.width / r.width;
    const scaleY = canvas.height / r.height;
    return {x: (clientX - r.left) * scaleX, y: (clientY - r.top) * scaleY};
  }
  function start(e){ isDrawing=true; hasSigned=true; document.getElementById('sig-canvas-wrap').classList.add('signed'); const p=pos(e); sigCtx.beginPath(); sigCtx.moveTo(p.x,p.y); sigCtx.lineTo(p.x+0.1,p.y+0.1); sigCtx.stroke(); checkCustomerReady(); e.preventDefault(); }
  function move(e){ if(!isDrawing) return; const p=pos(e); sigCtx.lineTo(p.x,p.y); sigCtx.stroke(); e.preventDefault(); }
  function end(){ isDrawing=false; }
  canvas.onmousedown=start; canvas.onmousemove=move; window.onmouseup=end;
  canvas.ontouchstart=start; canvas.ontouchmove=move; canvas.ontouchend=end;
}
function clearSignature(){
  const canvas = document.getElementById('sig-canvas');
  if(sigCtx) sigCtx.clearRect(0,0,canvas.width,canvas.height);
  hasSigned = false;
  document.getElementById('sig-canvas-wrap').classList.remove('signed');
  checkCustomerReady();
}
document.addEventListener('change', function(e){ if(e.target.id==='cv-agree') checkCustomerReady(); });
function checkCustomerReady(){
  const agree = document.getElementById('cv-agree').checked;
  document.getElementById('cv-submit').disabled = !(hasSigned && agree && allDocsReviewed());
}
function completeSign(){
  upsertCustomerRecord({status:'Customer - Verified'});
  document.getElementById('screen-customer').classList.remove('active');
  document.getElementById('screen-complete').classList.add('active');
  const cta = document.getElementById('complete-cta');
  if(entryMode === 'preview'){
    cta.style.display = 'inline-flex';
    cta.textContent = 'Return to dashboard';
    cta.onclick = completeReturnToDashboard;
  } else {
    cta.style.display = 'none';
    document.getElementById('complete-copy').innerHTML = 'Signed and submitted. You can close this window — Dalton Solar will be in touch if anything else is needed.';
  }
  setTimeout(fireConfetti, 150);
}
function completeReturnToDashboard(){
  document.getElementById('screen-complete').classList.remove('active');
  document.getElementById('app-shell').classList.add('active');
  document.getElementById('complete-cta').style.display='inline-flex';
  document.getElementById('complete-copy').innerHTML = 'Signed and sent to QA. The rep dashboard will show this as <strong>Customer - Verified</strong> once it clears review.';
  resetWizardState();
  showView('dashboard');
}
function fireConfetti(){
  const canvas = document.getElementById('confetti-canvas');
  if(!canvas) return;
  const ctx = canvas.getContext('2d');
  canvas.width = window.innerWidth; canvas.height = window.innerHeight;
  const colors = ['#1F6E52','#D8A31A','#8AC7A1','#F4D35E','#134432'];
  const particles = [];
  for(let i=0;i<140;i++){
    particles.push({x:Math.random()*canvas.width, y:-20-Math.random()*canvas.height*0.6, w:6+Math.random()*6, h:8+Math.random()*10,
      color:colors[Math.floor(Math.random()*colors.length)], speed:2+Math.random()*3, drift:-1+Math.random()*2, rot:Math.random()*360, rotSpeed:-6+Math.random()*12});
  }
  let frame=0;
  function loop(){
    frame++;
    ctx.clearRect(0,0,canvas.width,canvas.height);
    particles.forEach(function(p){
      p.y+=p.speed; p.x+=p.drift; p.rot+=p.rotSpeed;
      ctx.save(); ctx.translate(p.x,p.y); ctx.rotate(p.rot*Math.PI/180); ctx.fillStyle=p.color; ctx.fillRect(-p.w/2,-p.h/2,p.w,p.h); ctx.restore();
    });
    if(frame<220){ requestAnimationFrame(loop); } else { ctx.clearRect(0,0,canvas.width,canvas.height); }
  }
  loop();
}

function resetAll(){
  AuthStore.clear();
  currentUser = null;
  document.getElementById('app-shell').classList.remove('active');
  document.getElementById('screen-customer-portal').classList.remove('active');
  document.getElementById('screen-customer-login').classList.remove('active');
  document.getElementById('login-email').value='';
  document.getElementById('login-pass').value='';
  document.getElementById('login-error').style.display='none';
  document.getElementById('cust-login-email').value='';
  document.getElementById('cust-login-pass').value='';
  resetWizardState();
  showScreen('screen-login');
}

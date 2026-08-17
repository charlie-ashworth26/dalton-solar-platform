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

// The legacy customer-portal demo still reads this local array. The rep-facing
// enrollment wizard does not: its customer/document/Perch data is persisted
// through the real Dalton backend routes below.
let customers = [];

const steps = [1,2,3,4,5];
const stepIds = {1:'step-project',2:'step-bill',3:'step-contact',4:'step-lmi',5:'step-agreement'};
const stepLabels = ['Capacity','Bill','Contact','LMI','Agreement'];

let state = {
  rep:{name:'Charlie Mren'},
  project:{id:'',name:'',utility:''},
  customer:{first:'',last:'',email:'',phone:'',acct:'',password:''},
  address:{street:'',unit:'',city:'',state:'NY',zip:''},
  bill:{fileName:'',amount:'',documentId:null},
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
  syncAdminNav();
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

async function doCustomerLogin(){
  const email = document.getElementById('cust-login-email').value.trim();
  const pass = document.getElementById('cust-login-pass').value;
  const errEl = document.getElementById('cust-login-error');
  const btn = document.getElementById('cust-login-btn');
  errEl.style.display = 'none';
  if(!email || !pass){
    errEl.textContent = 'Enter your email and password to continue.';
    errEl.style.display = 'block';
    return;
  }
  // BUG 2 FIX: this used to search a legacy in-memory array (empty since the
  // Perch refactor) and compare plaintext passwords, never calling the backend
  // at all - so it could never succeed. Real authentication now happens
  // server-side against customers.password_hash.
  if(btn){ btn.disabled = true; btn.textContent = 'Signing in…'; }
  let body;
  try{
    body = await apiFetch('/api/auth/customer-login', {
      method:'POST', body: JSON.stringify({email: email, password: pass})
    });
  }catch(err){
    errEl.textContent = err.message || 'Invalid email or password.';
    errEl.style.display = 'block';
    if(btn){ btn.disabled = false; btn.textContent = 'Sign in'; }
    return;
  }
  if(btn){ btn.disabled = false; btn.textContent = 'Sign in'; }
  CustomerAuth.setToken(body.token);
  activateScreen('screen-customer-portal');
  document.getElementById('portal-hello').textContent =
    'Hi ' + (body.customer.first_name || '') + ' — welcome back';
  await loadCustomerAgreement();
}

// Customer tokens are stored separately from the rep token so the two sessions
// can never be confused for one another.
const CustomerAuth = {
  KEY: 'dalton_customer_token',
  getToken(){ try { return sessionStorage.getItem(this.KEY); } catch(e){ return null; } },
  setToken(t){ try { sessionStorage.setItem(this.KEY, t); } catch(e){} },
  clear(){ try { sessionStorage.removeItem(this.KEY); } catch(e){} },
};

async function loadCustomerAgreement(){
  const el = document.getElementById('portal-project');
  try{
    const res = await fetch('/api/auth/customer-me', {
      headers: {Authorization: 'Bearer ' + CustomerAuth.getToken()}
    });
    if(!res.ok) throw new Error('Could not load your agreement.');
    const me = await res.json();
    if(el){
      el.textContent = (me.project_name || 'Your enrollment') +
                       ' — ' + (me.workflow_step_label || '');
    }
  }catch(err){
    if(el) el.textContent = 'Could not load your agreement.';
  }
}


function showView(name){
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
  document.getElementById('view-'+name).classList.add('active');
  document.querySelectorAll('.nav-link[data-view]').forEach(n=>n.classList.toggle('active', n.dataset.view===name));
  if(name==='dashboard') renderDashboard();
  if(name==='projects') renderProjects('projects-list', true);
  if(name==='customers') renderCustomers('');
  window.scrollTo({top:0});

  if(name === 'reps') loadReps();
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

// Perch workflow state is the accurate rep-facing progress. enrollments.status
// is left alone - it still drives QA/reporting and is not rep-facing here.
function workflowPillHtml(e){
  const label = e.workflow_step_label || 'In progress';
  const cls = e.workflow_is_terminal ? 'status-verified'
            : (e.workflow_is_blocked ? 'status-danger' : 'status-opp');
  return '<span class="status-pill '+cls+'">'+esc(label)+'</span>';
}

function openActionHtml(e){
  const label = e.workflow_is_terminal ? 'View' : 'Open';
  return '<span class="resume-link" onclick="openEnrollment('+e.id+')">'+label+'</span>';
}

function enrollmentRowHtml(e){
  const custName = e.customer ? (e.customer.first_name+' '+e.customer.last_name) : '(no customer info yet)';
  const custEmail = e.customer ? e.customer.email : '';
  const projectName = e.project ? e.project.name : '—';
  return '<tr><td><div class="cust-name">'+esc(custName)+'</div><div class="cust-sub">'+esc(custEmail)+'</div></td><td>'+esc(projectName)+'</td><td>'+workflowPillHtml(e)+'</td><td>'+formatDate(e.updated_at)+'</td><td style="text-align:right;">'+openActionHtml(e)+'</td></tr>';
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
        return '<tr><td><div class="cust-name">'+esc(custName)+'</div><div class="cust-sub">'+esc(custEmail)+'</div></td><td>'+esc(projectName)+'</td><td>'+workflowPillHtml(e)+'</td><td>'+formatDate(e.created_at)+'</td><td>'+formatDate(e.updated_at)+'</td><td style="text-align:right;">'+openActionHtml(e)+'</td></tr>';
      }).join('')
    : '<tr><td colspan="6" style="text-align:center;color:var(--ink-faint);padding:26px;">No enrollments match that search.</td></tr>';
}

function resetWizardState(){
  state.customer = {first:'',last:'',email:'',phone:'',acct:'',podId:'',password:''};
  state.address = {street:'',unit:'',city:'',state:'NY',zip:''};
  state.bill = {fileName:'',amount:'',documentId:null};
  state.billing = {sameAsService:true,street:'',unit:'',city:'',state:'NY',zip:''};
  state.lmi = {mode:'doc',docType:'',fileName:'',documentId:null,householdSize:'',incomeBelow:null,nameOnDocument:'',relationship:'self',documentFormat:''};
  billRuntimeFile = null; billUploadPromise = null; billUploadGeneration += 1;
  lmiRuntimeFile = null; lmiUploadPromise = null; lmiUploadGeneration += 1;
  perchContracts = [];
  perchContext = freshPerchContext();
  currentCustomerId = null;
  // RC-5: enrollment-specific globals that previously survived across
  // enrollments. After this call, Enrollment B must be indistinguishable from
  // Enrollment B on a fresh page load.
  billTouched = false;
  skipProjectStep = false;
  entryMode = null;
  currentWorkflow = null;
  // RC-4: undo any read-only lock left by a completed/blocked enrollment.
  unlockEnrollmentControls();
  const rb = document.getElementById('resume-banner');
  if(rb){ rb.style.display = 'none'; rb.innerHTML = ''; }
  const reqEl = document.getElementById('bill-requirements');
  if(reqEl){ reqEl.style.display = 'none'; reqEl.innerHTML = ''; }
  // The old #contract-accept-status element was removed with the legacy
  // contract UI. The shared Agreements component owns its own status region
  // and clears it on every mount, so there is nothing to reset here.
  clearWizardForms();
}
function clearWizardForms(){
  ['c-first','c-last','c-email','c-phone','c-acct','c-pod','c-pass','c-pass-confirm','a-street','a-unit','a-city','a-zip','b-street','b-unit','b-city','b-zip','lmi-name-on-doc'].forEach(id=>{ const el=document.getElementById(id); if(el) el.value=''; });
  document.getElementById('bill-amount').value='';
  document.getElementById('bill-file-chip').innerHTML='';
  document.getElementById('bill-file').value='';
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
  const lmiTitle=document.getElementById('lmi-title');
  const lmiLead=document.getElementById('lmi-lead');
  if(lmiTitle) lmiTitle.textContent='LMI documentation';
  if(lmiLead) lmiLead.textContent='Qualify the customer for the low-income adder — by document, by income self-attestation, or mark it N/A.';
  document.getElementById('lmi-check-container').innerHTML='';
  document.getElementById('lmi-household-size').value='';
  document.getElementById('ami-threshold-display').textContent='';
  document.getElementById('ami-below').classList.remove('selected');
  document.getElementById('ami-above').classList.remove('selected');
  document.getElementById('lmi-mode-doc').classList.remove('selected');
  document.getElementById('lmi-mode-attest').classList.remove('selected');
  document.getElementById('lmi-mode-na').classList.remove('selected');
  document.getElementById('lmi-mode-doc').style.display='flex';
  document.getElementById('lmi-mode-attest').style.display='flex';
  document.getElementById('lmi-mode-na').style.display='flex';
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

let currentDraft = null;     // {enrollment_id, enrollment_code} - durable Dalton key
let currentWorkflow = null;  // descriptor used only for service-area/capacity
let utilityRules = {};
let billRuntimeFile = null, billUploadPromise = null, billUploadGeneration = 0;
let lmiRuntimeFile = null, lmiUploadPromise = null, lmiUploadGeneration = 0;
let perchContracts = [];
let billTouched = false;   // RC-3: has the rep interacted with the bill step yet?
function freshPerchContext(){
  return {email:'', capacityZip:'', utilitySlug:'', utilityDisplay:'', projectDetails:null,
          nextStepKey:null, enrollmentSubmitted:false, proofSubmitted:false, contractsGenerated:false,
          acceptanceEnabled:false, acceptanceSubmitted:false, acceptanceInFlight:false,
          agreementBackStep:3};
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
  goStep(1);
  renderWorkflowLoading('Starting a new enrollment...');

  // A Dalton Enrollment ID is issued before any Perch call. Perch's enrollment
  // token is session-scoped and expires in 1 hour, so it can never be the
  // durable key for an enrollment.
  try {
    await loadUtilityRules();
    currentDraft = await apiFetch('/api/perch/drafts', {method: 'POST', body: JSON.stringify({})});
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
  perchContext.email = values.email;
  perchContext.capacityZip = body.result.zip_code;
  perchContext.utilitySlug = body.result.utility_slug;
  perchContext.utilityDisplay = body.result.utility_display_name || body.result.utility_slug;
  perchContext.projectDetails = body.result.project_details || {};
  perchContext.nextStepKey = (((body.workflow||{}).step||{}).perch_next_step||{}).resolved_step || null;
  state.customer.email = values.email;
  state.project.utility = perchContext.utilityDisplay;
  if(!state.project.name) state.project.name = 'Assigned by Perch';
  currentWorkflow = body.workflow;
  renderWorkflowStep(currentWorkflow);
}

function advanceFromCapacity(){
  if(!currentDraft || !perchContext.email || perchContext.nextStepKey !== 'enroll') return;
  buildStepLabels();
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

const WORKFLOW_STEP_TO_WIZARD = {
  service_area: 1, capacity_result: 1, no_capacity: 1,
  enroll: 3,                    // customer/contact details
  proof_docs: 4, self_attestation: 4, self_attestation_accept: 4,
  contracts: 5, contracts_review: 5, contracts_accept: 5,
  status: 5, unknown_next_step: 5,
  enroll_outcome_uncertain: 3,
  contracts_accepted: 5, contracts_accept_uncertain: 5,
};

async function openEnrollment(enrollmentId){
  let e;
  try {
    e = await apiFetch('/api/enrollments/' + enrollmentId);
  } catch(err){
    alert('Could not open that enrollment: ' + err.message);
    return;
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
    state.address.zip    = e.service_address.zip    || '';
  }
  if(e.utility_account){
    state.customer.acct  = e.utility_account.account_number || '';
    state.customer.podId = e.utility_account.secondary_account_identifier || '';
  }
  if(e.billing_address){
    state.billing.sameAsService = e.billing_address.same_as_service !== false;
    state.billing.street = e.billing_address.street || '';
    state.billing.city   = e.billing_address.city   || '';
    state.billing.zip    = e.billing_address.zip    || '';
  }
  state.project = {id:(e.project ? e.project.id : ''), name:(e.project ? e.project.name : ''),
                   utility: e.utility_account ? e.utility_account.utility_name : ''};

  perchContext.email = (e.customer && e.customer.email) || '';
  perchContext.utilitySlug = e.utility_account ? e.utility_account.utility_name : '';
  perchContext.nextStepKey = e.workflow_step_key || null;

  const key = e.workflow_step_key || 'service_area';
  const terminal = e.workflow_is_terminal === true;
  const blocked  = e.workflow_is_blocked === true;

  showView('wizard');
  goStep(WORKFLOW_STEP_TO_WIZARD[key] || 1);
  renderResumeBanner(e, key, terminal, blocked);

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
    });
  } else if(REHYDRATE_CONTRACT_STEPS.indexOf(key) !== -1){
    await rehydrateContractPacket(key, terminal, blocked);
  }
}

// Read-only view of a finished (or uncertain) enrollment. Renders only what
// Dalton already persisted - contract NAMES, never URLs, which are deliberately
// never stored. Makes no network request of any kind.
async function rehydrateContractPacket(key, terminal, blocked){
  // Mount the shared component FIRST so the review screen is present (and the
  // error region exists) before the network call. The old #contract-review-error
  // element was removed with the legacy UI; errors now surface in the
  // component's own #agr-card-error, which renderAgreementCard() renders.
  mountRepAgreements(false, false);
  const cardErr = document.getElementById('agr-card-error');
  if(cardErr) cardErr.style.display = 'none';
  try{
    const body = await apiFetch('/api/perch/enrollments/' + currentDraft.enrollment_id + '/contracts',
                                {method:'POST', body:JSON.stringify({})});
    perchContracts = body.contracts || [];
    perchContext.contractsGenerated = true;
    perchContext.contractsNextStepKey = body.next_step_key;
    // Never fabricated - taken from the backend response, exactly as the
    // straight-through path does.
    perchContext.acceptanceEnabled = body.acceptance_enabled === true;
  }catch(err){
    perchContracts = [];
    perchContext.acceptanceEnabled = false;
    mountRepAgreements(false, false);
    const failEl = document.getElementById('agr-card-error');
    if(failEl){
      failEl.textContent = 'Could not load the agreements: ' + err.message +
                           ' Return to the dashboard and reopen this enrollment to retry.';
      failEl.style.display = 'block';
    }
    return;
  }
  if(terminal || blocked){
    // Already accepted or uncertain: show the packet for reference only.
    perchContext.acceptanceEnabled = false;
    perchContext.acceptanceSubmitted = terminal;
  }
  mountRepAgreements(false, false);
}

function renderResumeBanner(e, key, terminal, blocked){
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
  el.innerHTML = '<span>Enrollment <strong>'+esc(e.enrollment_code)+'</strong> — '+
                 esc(e.workflow_step_label || 'In progress')+'</span>'+badge;
  el.style.display = 'flex';
}

// RC-4: explicit control list. The previous implementation disabled every
// button in #view-wizard whose TEXT did not match /review|back|dashboard/, which
// (a) depended on wording and (b) poisoned steps 1-4 for the next enrollment
// because nothing re-enabled them. readOnlyLocked lets resetWizardState() undo
// it deterministically.
const READ_ONLY_LOCK_IDS = [
  'btn-project-next', 'btn-bill-next', 'btn-contact-next', 'btn-lmi-next',
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

function exitWizard(){ showView('dashboard'); }

function backFromCustomer(){
  if(currentWorkflow){
    goStep(1);
    renderWorkflowStep(currentWorkflow);
  } else { exitWizard(); }
}

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
    configureBillUtilityRules();
    document.getElementById('c-first').value = state.customer.first;
    document.getElementById('c-last').value = state.customer.last;
    document.getElementById('c-acct').value = state.customer.acct;
    document.getElementById('c-pod').value = state.customer.podId || '';
    document.getElementById('a-street').value = state.address.street;
    document.getElementById('a-unit').value = state.address.unit;
    document.getElementById('a-city').value = state.address.city;
    document.getElementById('a-zip').value = state.address.zip;
    document.getElementById('bill-amount').value = state.bill.amount || '';
    document.getElementById('billing-same').checked = state.billing.sameAsService !== false;
    document.getElementById('b-street').value = state.billing.street || '';
    document.getElementById('b-unit').value = state.billing.unit || '';
    document.getElementById('b-city').value = state.billing.city || '';
    document.getElementById('b-zip').value = state.billing.zip || '';
    toggleBillingAddress(false);
    if(state.bill.fileName){ showBillChip(state.bill.fileName); }
    checkBillReady();
  }
  if(n===3){
    document.getElementById('c-email').value = state.customer.email || perchContext.email;
    document.getElementById('c-phone').value = state.customer.phone;
    document.getElementById('c-pass').value = state.customer.password || '';
    document.getElementById('c-pass-confirm').value = state.customer.password || '';
  }
  if(n===4){
    const mode = state.lmi.mode || 'doc';
    setLmiMode(mode);
    document.getElementById('lmi-name-on-doc').value = state.lmi.nameOnDocument || (state.customer.first+' '+state.customer.last).trim();
    document.getElementById('lmi-relationship').value = state.lmi.relationship || 'self';
    document.getElementById('lmi-format').value = state.lmi.documentFormat || '';
    if(mode === 'doc' && state.lmi.fileName){
      document.getElementById('lmi-doctype').value = state.lmi.docType;
      showLmiChip(state.lmi.fileName);
    }
    if(mode === 'attest'){
      document.getElementById('lmi-household-size').value = state.lmi.householdSize || '';
      updateAmiThreshold();
      if(state.lmi.incomeBelow === true || state.lmi.incomeBelow === false) setIncomeAnswer(state.lmi.incomeBelow);
    }
    if(perchContext.nextStepKey === 'proof_docs') prepareLmiForPerch();
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
  acctHelp.textContent = expected ? ((rule.display_name || 'This utility') + ' uses a ' + expected + '-digit account number.') : '';
  const needsPod = !!(rule && rule.requires_pod_id);
  podField.style.display = needsPod ? 'block' : 'none';
  podHelp.textContent = needsPod && rule.pod_id ? ('Required: ' + rule.pod_id.description + '.') : '';
}

function syncBillingFromService(){
  if(!document.getElementById('billing-same').checked) return;
  document.getElementById('b-street').value = document.getElementById('a-street').value;
  document.getElementById('b-unit').value = document.getElementById('a-unit').value;
  document.getElementById('b-city').value = document.getElementById('a-city').value;
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
  const first = document.getElementById('c-first').value.trim();
  const last = document.getElementById('c-last').value.trim();
  const acct = document.getElementById('c-acct').value.trim();
  const street = document.getElementById('a-street').value.trim();
  const city = document.getElementById('a-city').value.trim();
  const zip = document.getElementById('a-zip').value.trim();
  const amt = document.getElementById('bill-amount').value;
  const rule = currentUtilityRule();
  const expected = rule && rule.account_number_length ? Number(rule.account_number_length) : null;
  const acctOk = /^\d+$/.test(acct) && (!expected || acct.length === expected);
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
  if(!amt) missing.push('Average monthly bill amount');

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

  state.customer.first = document.getElementById('c-first').value.trim();
  state.customer.last = document.getElementById('c-last').value.trim();
  state.customer.acct = document.getElementById('c-acct').value.trim();
  state.customer.podId = document.getElementById('c-pod').value.trim();
  state.address = {
    street: document.getElementById('a-street').value.trim(),
    unit: document.getElementById('a-unit').value.trim(),
    city: document.getElementById('a-city').value.trim(),
    state: 'NY',
    zip: document.getElementById('a-zip').value.trim()
  };
  const sameBilling = document.getElementById('billing-same').checked;
  state.billing = {
    sameAsService: sameBilling,
    street: sameBilling ? state.address.street : document.getElementById('b-street').value.trim(),
    unit: sameBilling ? state.address.unit : document.getElementById('b-unit').value.trim(),
    city: sameBilling ? state.address.city : document.getElementById('b-city').value.trim(),
    state: 'NY',
    zip: sameBilling ? state.address.zip : document.getElementById('b-zip').value.trim()
  };
  state.bill.amount = document.getElementById('bill-amount').value;

  btn.disabled=true; btn.textContent='Saving…';
  try{
    if(billUploadPromise) await billUploadPromise;
    if(!state.bill.documentId) throw new Error('The utility bill has not finished saving. Please try again.');
    const payload = {
      customer:{first_name:state.customer.first,last_name:state.customer.last,email:state.customer.email,phone:state.customer.phone || null},
      service_address:{street:state.address.street,unit:state.address.unit,city:state.address.city,state:'NY',zip:state.address.zip},
      billing_address:{same_as_service:state.billing.sameAsService,street:state.billing.street,unit:state.billing.unit,city:state.billing.city,state:'NY',zip:state.billing.zip},
      utility_account:{utility_name:perchContext.utilitySlug,account_number:state.customer.acct,secondary_account_identifier:state.customer.podId || null}
    };
    if(state.project.id) payload.project_id = state.project.id;
    await apiFetch('/api/enrollments/' + currentDraft.enrollment_id, {method:'PATCH', body:JSON.stringify(payload)});
    goStep(3);
  }catch(err){
    errEl.textContent=err.message; errEl.style.display='block';
  }finally{
    btn.textContent='Continue'; checkBillReady();
  }
}

async function submitContact(){
  const errEl = document.getElementById('contact-submit-error');
  errEl.style.display='none';
  const email = document.getElementById('c-email').value.trim();
  const phone = document.getElementById('c-phone').value.trim();
  const pass = document.getElementById('c-pass').value;
  const passConfirm = document.getElementById('c-pass-confirm').value;
  if(!email || !phone || !pass || !passConfirm){ errEl.textContent='Fill in every field before continuing.'; errEl.style.display='block'; return; }
  if(pass.length < 6){ errEl.textContent='Password should be at least 6 characters.'; errEl.style.display='block'; return; }
  if(pass !== passConfirm){ errEl.textContent='Passwords don\'t match.'; errEl.style.display='block'; return; }
  if(email.toLowerCase() !== (perchContext.email || '').toLowerCase()){
    errEl.textContent='Email must match the address used for the Perch availability check. Go back to change it.'; errEl.style.display='block'; return;
  }
  state.customer.email = email;
  state.customer.phone = phone;
  state.customer.password = pass;
  const btn=document.getElementById('btn-contact-next');
  btn.disabled=true; btn.textContent=perchContext.enrollmentSubmitted ? 'Continuing…' : 'Submitting to Perch…';
  try{
    await apiFetch('/api/enrollments/' + currentDraft.enrollment_id, {
      method:'PATCH',
      body:JSON.stringify({customer:{first_name:state.customer.first,last_name:state.customer.last,email:state.customer.email,phone:state.customer.phone,password:state.customer.password}})
    });
    if(!perchContext.enrollmentSubmitted){
      const body = await apiFetch('/api/perch/enrollments/' + currentDraft.enrollment_id + '/enroll', {method:'POST', body:JSON.stringify({document_id:state.bill.documentId})});
      perchContext.enrollmentSubmitted = true;
      perchContext.nextStepKey = body.next_step_key;
    }
    await continueFromPerchNextStep('contact');
  }catch(err){
    errEl.textContent=err.message; errEl.style.display='block';
  }finally{
    btn.disabled=false; btn.textContent='Continue';
  }
}

async function continueFromPerchNextStep(origin){
  if(perchContext.contractsGenerated){
    perchContext.agreementBackStep = origin === 'lmi' ? 4 : 3;
    goStep(5); mountRepAgreements(false, false); return;
  }
  if(perchContext.nextStepKey === 'proof_docs'){
    goStep(4); prepareLmiForPerch(); return;
  }
  if(perchContext.nextStepKey === 'contracts'){
    state.lmi.mode='na'; state.lmi.docType=''; state.lmi.fileName='';
    await generateContractsAndOpenAgreement(origin === 'lmi' ? 4 : 3); return;
  }
  if(perchContext.nextStepKey === 'self_attestation' || perchContext.nextStepKey === 'self_attestation_accept'){
    throw new Error('Perch requires its self-attestation branch for this enrollment. That branch is intentionally not wired into this milestone, so Dalton will not guess or skip it.');
  }
  throw new Error('Perch returned a next step Dalton does not recognize. The enrollment was not advanced.');
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
  // MULTI-FILE: every selected file is tracked and uploaded as ONE document set.
  // The instant in-browser preview below still reads only the FIRST file, so the
  // UI says so explicitly rather than implying all pages were read. The SERVER
  // extracts across the whole set on upload and its result is authoritative.
  docSetAddFiles('utility_bill', files);
  const f = files[0];
  if(f.size > 4 * 1024 * 1024){
    removeBill();
    const errEl=document.getElementById('bill-submit-error');
    errEl.textContent='Perch accepts utility bills up to 4 MB. Choose a smaller PDF or image.';
    errEl.style.display='block';
    return;
  }
  const generation = ++billUploadGeneration;
  billRuntimeFile = f;
  state.bill.fileName = f.name;
  state.bill.documentId = null;
  state.bill.uploadError = null;
  showBillChip(f.name);

  // Persist the exact same file selected for OCR. The generation guard prevents
  // a slower, older upload from overwriting the document ID for a newer bill.
  billUploadPromise = uploadDocumentToDalton(f, 'utility_bill')
    .then(saved => {
      if(generation === billUploadGeneration) state.bill.documentId = saved.document_id;
      return saved;
    })
    .catch(err => {
      if(generation === billUploadGeneration) state.bill.uploadError = err.message;
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
    if(parsed.first) document.getElementById('c-first').value = parsed.first;
    if(parsed.last) document.getElementById('c-last').value = parsed.last;
    if(parsed.acct) document.getElementById('c-acct').value = parsed.acct;
    if(parsed.street) document.getElementById('a-street').value = parsed.street;
    if(parsed.city) document.getElementById('a-city').value = parsed.city;
    if(parsed.zip) document.getElementById('a-zip').value = parsed.zip;
    if(parsed.amount != null) document.getElementById('bill-amount').value = amountToBracket(parsed.amount);
    syncBillingFromService();
    c.innerHTML = '<div class="ocr-panel"><div class="ocr-head">Pulled from the bill</div>' +
      ocrRow('Customer name', !!parsed.first, parsed.first ? 'Found' : 'Not found — enter manually') +
      ocrRow('Service address', !!parsed.street, parsed.street ? 'Found' : 'Not found — enter manually') +
      ocrRow('Account number', !!parsed.acct, parsed.acct ? 'Found' : 'Not found — enter manually') +
      ocrRow('Bill amount', parsed.amount!=null, parsed.amount!=null ? ('$'+parsed.amount.toFixed(2)) : 'Not found — select manually') +
      '</div>';
  } catch(err){
    if(generation !== billUploadGeneration) return;
    c.innerHTML = '<div class="ocr-panel"><div class="ocr-analyzing">Couldn\'t read this file automatically — enter the details below by hand.</div></div>';
  } finally {
    // RC-2: cleanup MUST run on every path, including a stale generation.
    // Previously both guards returned before this, leaving the "Reading the
    // bill…" spinner on screen forever and btn-bill-next stuck disabled.
    if(generation === billUploadGeneration){
      // Still the current bill: clear any spinner we may have left behind and
      // recompute the Continue button from real conditions.
      if(c && c.innerHTML.indexOf('Reading the bill') !== -1){
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
function removeBill(){
  billUploadGeneration += 1;
  // RC-2: clear any in-flight spinner so a discarded OCR cannot leave one stuck.
  const oc = document.getElementById('ocr-container');
  if(oc) oc.innerHTML = '';
  billRuntimeFile = null;
  billUploadPromise = null;
  state.bill.fileName='';
  state.bill.documentId=null;
  state.bill.uploadError=null;
  document.getElementById('bill-file-chip').innerHTML='';
  document.getElementById('ocr-container').innerHTML='';
  document.getElementById('bill-file').value='';
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
function lmiTypeForLabel(label){ return lmiTypes.find(x=>x.label===label) || null; }
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
  document.getElementById('lmi-file-chip').innerHTML = '<div class="file-chip"><div class="fc-left"><div class="fc-icon">📄</div><div><div class="fc-name">'+esc(name)+'</div><div class="fc-size">Uploaded</div></div></div><button class="fc-remove" onclick="removeLmi()">Remove</button></div>';
}
const amiTable = [
  {size:1, amount:61750},{size:2, amount:70550},{size:3, amount:79350},{size:4, amount:88150},
  {size:5, amount:95250},{size:6, amount:102300},{size:7, amount:109350},{size:8, amount:116400},
];
function setLmiMode(mode){
  if(perchContext.nextStepKey === 'proof_docs' && mode !== 'doc'){
    const errEl=document.getElementById('lmi-submit-error');
    errEl.textContent='Perch requires proof documentation for this enrollment, so self-attestation and N/A cannot replace this step.';
    errEl.style.display='block';
    mode='doc';
  }
  state.lmi.mode = mode;
  document.getElementById('lmi-mode-doc').classList.toggle('selected', mode==='doc');
  document.getElementById('lmi-mode-attest').classList.toggle('selected', mode==='attest');
  document.getElementById('lmi-mode-na').classList.toggle('selected', mode==='na');
  document.getElementById('lmi-doc-panel').style.display = mode==='doc' ? 'block' : 'none';
  document.getElementById('lmi-attest-panel').style.display = mode==='attest' ? 'block' : 'none';
  checkLmiReady();
}
function prepareLmiForPerch(){
  perchContext.nextStepKey='proof_docs';
  state.lmi.mode='doc';
  const lmiTitle=document.getElementById('lmi-title');
  const lmiLead=document.getElementById('lmi-lead');
  if(lmiTitle) lmiTitle.textContent='LMI documentation';
  if(lmiLead) lmiLead.textContent='Perch requires proof documentation for this enrollment. Use the existing Dalton upload below; you will not be asked to upload it again.';
  document.getElementById('lmi-mode-attest').style.display='none';
  document.getElementById('lmi-mode-na').style.display='none';
  document.getElementById('lmi-mode-doc').style.display='flex';
  setLmiMode('doc');
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
  if(state.lmi.mode !== 'doc') setLmiMode('doc');
  // MULTI-FILE: front/back or multi-page proofs form ONE document set.
  docSetAddFiles('lmi_document', files);
  const f = files[0];
  if(f.size > 4 * 1024 * 1024){
    removeLmi();
    const errEl=document.getElementById('lmi-submit-error');
    errEl.textContent='Perch accepts proof documents up to 4 MB. Choose a smaller file.';
    errEl.style.display='block';
    return;
  }
  const generation = ++lmiUploadGeneration;
  lmiRuntimeFile=f;
  state.lmi.fileName = f.name;
  state.lmi.documentId=null;
  state.lmi.uploadError=null;
  showLmiChip(f.name);
  lmiUploadPromise = uploadDocumentToDalton(f, 'lmi_document')
    .then(saved => {
      if(generation === lmiUploadGeneration) state.lmi.documentId=saved.document_id;
      return saved;
    })
    .catch(err => {
      if(generation === lmiUploadGeneration) state.lmi.uploadError=err.message;
      throw err;
    });
  const c = document.getElementById('lmi-check-container');
  c.innerHTML = '<div class="ocr-panel"><div class="ocr-analyzing"><div class="spinner"></div>Reading the document…</div></div>';
  try{
    const text = await extractTextFromFile(f);
    if(generation !== lmiUploadGeneration) return;
    const analysis = classifyLmiDoc(text);
    if(analysis.matched){
      document.getElementById('lmi-doctype').value = analysis.matched.label;
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
function removeLmi(){
  lmiUploadGeneration += 1;
  lmiRuntimeFile=null; lmiUploadPromise=null;
  state.lmi.fileName=''; state.lmi.documentId=null; state.lmi.uploadError=null;
  document.getElementById('lmi-file-chip').innerHTML='';
  document.getElementById('lmi-check-container').innerHTML='';
  document.getElementById('lmi-file').value='';
  checkLmiReady();
}
function checkLmiReady(){
  state.lmi.docType = document.getElementById('lmi-doctype').value;
  state.lmi.nameOnDocument = document.getElementById('lmi-name-on-doc').value.trim();
  state.lmi.relationship = document.getElementById('lmi-relationship').value;
  state.lmi.documentFormat = document.getElementById('lmi-format').value;
  let ready = false;
  if(perchContext.nextStepKey === 'proof_docs'){
    const type=lmiTypeForLabel(state.lmi.docType);
    ready=!!(state.lmi.mode==='doc' && type && type.sourceType && state.lmi.fileName && state.lmi.nameOnDocument && state.lmi.relationship && state.lmi.documentFormat);
  } else if(state.lmi.mode === 'na') ready = true;
  else if(state.lmi.mode === 'attest') ready = !!(state.lmi.householdSize && (state.lmi.incomeBelow === true || state.lmi.incomeBelow === false));
  else ready = !!(state.lmi.docType && state.lmi.fileName);
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
  const type=lmiTypeForLabel(document.getElementById('lmi-doctype').value);
  if(!type || !type.sourceType){
    errEl.textContent='That legacy Dalton document label does not map to a published Perch proof source type. Choose a supported proof document instead of guessing.';
    errEl.style.display='block'; return;
  }
  state.lmi.docType=document.getElementById('lmi-doctype').value;
  state.lmi.nameOnDocument=document.getElementById('lmi-name-on-doc').value.trim();
  state.lmi.relationship=document.getElementById('lmi-relationship').value;
  state.lmi.documentFormat=document.getElementById('lmi-format').value;
  btn.disabled=true; btn.textContent='Submitting to Perch…';
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
    if(perchContext.nextStepKey!=='contracts') throw new Error('Perch accepted the proof document but returned an unexpected next step. Dalton stopped rather than guessing.');
    await generateContractsAndOpenAgreement(4);
  }catch(err){
    errEl.textContent=err.message; errEl.style.display='block';
  }finally{
    btn.textContent='Continue'; checkLmiReady();
  }
}

function fillReview(){
  document.getElementById('rv-name').textContent = state.customer.first+' '+state.customer.last;
  document.getElementById('rv-email').textContent = state.customer.email;
  document.getElementById('rv-phone').textContent = state.customer.phone;
  document.getElementById('rv-acct').textContent = state.customer.acct;
  document.getElementById('rv-project').textContent = state.project.name || 'Assigned by Perch';
  document.getElementById('rv-utility').textContent = state.project.utility || perchContext.utilityDisplay || perchContext.utilitySlug;
  document.getElementById('rv-address').textContent = state.address.street+(state.address.unit ? ', '+state.address.unit : '')+', '+state.address.city+', NY '+state.address.zip;
  document.getElementById('rv-bill').textContent = state.bill.amount;
  document.getElementById('rv-lmi').textContent = perchContext.proofSubmitted
    ? (state.lmi.docType+' — '+state.lmi.fileName)
    : 'Not required by Perch for this enrollment';
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
      if(sourceErr){ sourceErr.textContent=err.message; sourceErr.style.display='block'; }
      throw err;
    }
  }
  perchContext.agreementBackStep=backStep || 3;
  goStep(5);
  mountRepAgreements(false, false);
}
function backFromAgreement(){ goStep(perchContext.agreementBackStep || 3); }
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
  document.getElementById('screen-customer-login').classList.remove('active');
  document.getElementById('login-email').value='';
  document.getElementById('login-pass').value='';
  document.getElementById('login-error').style.display='none';
  document.getElementById('cust-login-email').value='';
  document.getElementById('cust-login-pass').value='';
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
    CustomerAuth.clear();
    activateScreen('screen-customer-login');
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
};

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
  Agreements.opened = {};
  renderAgreementCard();
}
/* ── Overlay ───────────────────────────────────────────────────────────── */


function agrEscKey(e){ if(e.key === 'Escape') closeAgreements(); }
function agrBackdrop(e){ if(e.target && e.target.id === 'agr-overlay') closeAgreements(); }
/* Opening documents NEVER accepts. Only the checkbox + button below does. */
function agrSummaryFromState(){
  const addr = state.address || {};
  const line = [addr.street, addr.unit, addr.city].filter(Boolean).join(', ');
  return {
    customerName: [state.customer.first, state.customer.last].filter(Boolean).join(' '),
    utility: (state.project && state.project.utility) || perchContext.utilitySlug || '',
    serviceAddress: line ? (line + (addr.zip ? ', NY ' + addr.zip : '')) : '',
    project: (state.project && state.project.name) || '',
  };
}

function agrSummaryFromEnrollment(e){
  const a = e.service_address || {};
  const line = [a.street, a.unit, a.city].filter(Boolean).join(', ');
  return {
    customerName: e.customer ? [e.customer.first_name, e.customer.last_name].filter(Boolean).join(' ') : '',
    utility: e.utility_account ? e.utility_account.utility_name : '',
    serviceAddress: line ? (line + (a.zip ? ', NY ' + a.zip : '')) : '',
    project: e.project ? e.project.name : '',
  };
}

// Rep-side: the wizard already holds the packet in perchContracts.
function mountRepAgreements(readOnly, submitted){
  mountAgreements({
    actor: 'rep',
    enrollmentId: currentDraft && currentDraft.enrollment_id,
    contracts: perchContracts,
    acceptanceEnabled: perchContext.acceptanceEnabled === true,
    readOnly: readOnly === true,
    submitted: submitted === true,
    summary: agrSummaryFromState(),
  });
}

// Called by the shared component after a successful acceptance.
function onAgreementsAccepted(){
  if(Agreements.actor === 'rep'){
    perchContext.acceptanceSubmitted = true;
    perchContext.acceptanceEnabled = false;
  }
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
    if(errEl){ errEl.textContent = err.message; errEl.style.display = 'block'; }
    return;
  }
  customerEnrollmentId = me.enrollment_id;
  activateScreen('screen-customer-contracts');

  const summary = {
    customerName: me.customer ? [me.customer.first_name, me.customer.last_name].filter(Boolean).join(' ') : '',
    utility: me.utility_name || '',
    serviceAddress: me.service_address || '',
    project: me.project_name || '',
  };

  // Completed / blocked enrollments are READ-ONLY: no packet is requested, so
  // opening one makes ZERO Perch contract calls.
  if(me.workflow_is_terminal === true || me.workflow_is_blocked === true){
    mountAgreements({
      actor: 'customer', enrollmentId: customerEnrollmentId,
      contracts: (me.workflow_last_response || {}).contracts || [],
      acceptanceEnabled: false, readOnly: true,
      submitted: me.workflow_is_terminal === true, summary: summary,
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
      cardErr.textContent = 'Could not load your agreements: ' + err.message;
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

  const rows = [
    ['Name', s.customerName],
    ['Email', s.email],
    ['Electric utility', s.utility],
    ['Service address', s.serviceAddress],
    ['Account number', s.accountNumber],
  ].filter(function(r){ return r[1]; });

  let html = '<div class="card">';

  if(Agreements.readOnly){
    html += '<p class="card-eyebrow">Enrollment complete</p>' +
            '<h2>Your enrollment is complete</h2>' +
            '<p class="lead">These agreements were accepted and submitted. Nothing further is needed.</p>';
  } else {
    html += '<p class="card-eyebrow">Final step</p>' +
            '<h2>Review &amp; complete your enrollment</h2>' +
            '<p class="lead">Check your details below, then agree to your enrollment agreements to finish.</p>';
  }

  if(rows.length){
    html += '<dl class="agr-details">';
    rows.forEach(function(r){
      html += '<dt>' + agrEsc(r[0]) + '</dt><dd>' + agrEsc(r[1]) + '</dd>';
    });
    html += '</dl>';
  }

  if(Agreements.readOnly){
    if(Agreements.contracts.length){
      html += '<p class="agr-docs-label">Agreements on file</p><ul class="agr-docs-list">';
      Agreements.contracts.forEach(function(c){
        html += '<li>' + agrEsc(c.contract_name || c) + '</li>';
      });
      html += '</ul>';
    }
    html += '<p class="helper">Your agreements are held by Perch and are no longer ' +
            'retrievable through Dalton once an enrollment is complete.</p>';
    html += '</div>';
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
      errEl.textContent = 'Could not open that document: ' + err.message;
      errEl.style.display = 'block';
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
               errEl.style.display = 'block'; }
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
    if(errEl){ errEl.textContent = err.message; errEl.style.display = 'block'; }
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

function docSetReset(category){
  if(DocSets[category]) DocSets[category] = {id:null, files:[]};
  renderDocSetList(category);
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

function docSetRemove(category, key){
  const set = DocSets[category];
  if(!set) return;
  set.files = set.files.filter(function(x){ return x.key !== key; });
  renderDocSetList(category);
}

function docSetListEl(category){
  // Its own element - the legacy single-file chip still owns bill-file-chip.
  const hostId = category === 'utility_bill' ? 'bill-fileset' : 'lmi-fileset';
  return document.getElementById(hostId);
}

function renderDocSetList(category){
  const host = docSetListEl(category);
  if(!host) return;
  const set = DocSets[category];
  if(!set || !set.files.length){ host.innerHTML = ''; return; }
  host.innerHTML = set.files.map(function(x, i){
    const kb = Math.max(1, Math.round(x.file.size / 1024));
    return '<div class="docset-file">' +
             '<span class="docset-n">' + (i + 1) + '</span>' +
             docSetNameHtml(x) +
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
  const fd = new FormData();
  fd.append('category', category);
  if(set.id) fd.append('document_set_id', String(set.id));
  set.files.forEach(function(x){ fd.append('files', x.file, x.file.name); });
  const body = await apiFetch('/api/enrollments/' + currentDraft.enrollment_id +
                              '/document-sets', {method:'POST', body: fd});
  set.id = body.document_set_id;
  // Map returned document ids back onto the tracked files, in page order, so
  // each filename can link to its own authorized view route.
  (body.files || []).forEach(function(rec){
    const match = set.files.filter(function(x){ return !x.documentId; })[0];
    if(match) match.documentId = rec.document_id;
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
    alert(err.message || 'Could not open that file.');
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

function syncAdminNav(){
  const el = document.getElementById('nav-reps');
  if(!el) return;
  el.style.display = (currentUser && currentUser.role === 'admin') ? '' : 'none';
}

async function loadReps(){
  const host = document.getElementById('reps-table');
  const err = document.getElementById('rep-list-error');
  if(err) err.style.display = 'none';
  if(!host) return;
  host.innerHTML = '<p class="helper">Loading reps…</p>';
  let reps;
  try{
    reps = await apiFetch('/api/admin/reps');
  }catch(e){
    host.innerHTML = '';
    if(err){ err.textContent = e.message; err.style.display = 'block'; }
    return;
  }
  if(!reps.length){ host.innerHTML = '<p class="helper">No reps yet.</p>'; return; }
  host.innerHTML =
    '<table class="table"><thead><tr>' +
      '<th>Name</th><th>Email</th><th>Rep code</th><th>Phone</th><th>Team</th>' +
      '<th>Enrollments</th><th>Status</th><th style="text-align:right;">Actions</th>' +
    '</tr></thead><tbody>' +
    reps.map(function(r){
      const active = r.is_active === 1;
      return '<tr>' +
        '<td>' + esc(r.full_name) + '</td>' +
        '<td>' + esc(r.email) + '</td>' +
        '<td>' + esc(r.rep_code || '') + '</td>' +
        '<td>' + esc(r.phone || '—') + '</td>' +
        '<td>' + esc(r.team || '—') + '</td>' +
        '<td>' + r.enrollment_count + '</td>' +
        '<td><span class="status-pill ' + (active ? 'status-verified' : 'status-danger') + '">' +
          (active ? 'Active' : 'Inactive') + '</span></td>' +
        '<td style="text-align:right;white-space:nowrap;">' +
          '<button class="btn btn-ghost btn-sm" onclick="editRep(' + r.user_id + ')">Edit</button> ' +
          '<button class="btn btn-ghost btn-sm" onclick="resetRepPassword(' + r.user_id + ')">Reset password</button> ' +
          '<button class="btn btn-ghost btn-sm" onclick="toggleRep(' + r.user_id + ',' + active + ')">' +
            (active ? 'Deactivate' : 'Activate') + '</button>' +
        '</td></tr>';
    }).join('') + '</tbody></table>';
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
    err.textContent = e.message; err.style.display = 'block';
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

async function editRep(userId){
  const phone = prompt('Phone (leave blank to clear):');
  if(phone === null) return;
  const team = prompt('Team (leave blank to clear):');
  if(team === null) return;
  const code = prompt('Rep code (leave blank to keep unchanged):');
  if(code === null) return;
  const payload = {phone: phone, team: team};
  if(code.trim()) payload.rep_code = code.trim();
  try{
    await apiFetch('/api/admin/reps/' + userId, {method:'PATCH', body: JSON.stringify(payload)});
  }catch(e){ alert(e.message); return; }
  loadReps();
}

async function resetRepPassword(userId){
  const pw = prompt('New password (at least 10 characters):');
  if(pw === null) return;
  try{
    await apiFetch('/api/admin/reps/' + userId + '/password',
                   {method:'POST', body: JSON.stringify({password: pw})});
  }catch(e){ alert(e.message); return; }
  alert('Password updated. Give it to the rep directly — it is not emailed.');
}

async function toggleRep(userId, isActive){
  const action = isActive ? 'deactivate' : 'activate';
  if(isActive && !confirm('Deactivate this rep? They will be signed out immediately. ' +
                          'Their enrollments and history are kept.')) return;
  try{
    await apiFetch('/api/admin/reps/' + userId + '/' + action, {method:'POST', body:'{}'});
  }catch(e){ alert(e.message); return; }
  loadReps();
}


/* Environment warning banner. Fetched unauthenticated so it renders on the
   LOGIN page too - a tester must see it before typing anything. Shown only when
   the backend supplies text, so an unconfigured production environment gets
   nothing. Never blocks the app if the request fails. */
async function loadEnvironmentBanner(){
  const el = document.getElementById('env-banner');
  if(!el) return;
  try{
    const res = await fetch('/api/environment');
    if(!res.ok) return;
    const info = await res.json();
    if(info && info.banner){
      el.textContent = info.banner;
      el.style.display = 'block';
    }
  }catch(e){ /* never block the app on the banner */ }
}
document.addEventListener('DOMContentLoaded', loadEnvironmentBanner);

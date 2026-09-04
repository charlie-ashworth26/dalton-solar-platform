/*
 * MAGIC-LINK HANDOFF — browser-style simulation of /access -> /.
 *
 * THE BUG THIS COVERS: tryRestoreSession() reads only AuthStore and returns
 * early when there is no staff token, and it was the ONLY DOMContentLoaded
 * binding. So after access.html stored the customer JWT and redirected to /,
 * nothing consulted dalton_customer_token and the markup's initially-active
 * #screen-login simply stayed - while the single-use token had already been
 * burned server-side, making the link "invalid" on retry.
 */
const fs=require('fs'),path=require('path'),vm=require('vm');
const ROOT=path.join(__dirname,'..');
const src=fs.readFileSync(path.join(ROOT,'static','js','app.js'),'utf8');
const html=fs.readFileSync(path.join(ROOT,'templates','index.html'),'utf8');
const css=fs.readFileSync(path.join(ROOT,'static','css','app.css'),'utf8');
const access=fs.readFileSync(path.join(ROOT,'templates','access.html'),'utf8');
const IDS=new Set([...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]));
const R=[]; const check=(l,c)=>{R.push({l,ok:!!c});console.log(`  [${c?'PASS':'FAIL'}] ${l}`);};

function mk(id){return {id,innerHTML:'',textContent:'',value:'',disabled:false,style:{},dataset:{},
  classList:{_s:new Set(),add(c){this._s.add(c)},remove(c){this._s.delete(c)},
    contains(c){return this._s.has(c)},toggle(c,f){f?this._s.add(c):this._s.delete(c)}},
  setAttribute(){},getAttribute:()=>null,focus(){},select(){},
  firstChild:null,_children:[],
  appendChild(n){this._children.push(n); if(n) n.parentNode=this; return n;},
  insertBefore(n){this._children.unshift(n); if(n) n.parentNode=this; return n;},
  removeChild(n){this._children=this._children.filter(x=>x!==n); if(n) n.parentNode=null; return n;},
  querySelector:()=>null,querySelectorAll:()=>[],scrollIntoView(){}};}

/* A shared sessionStorage, so we can prove the JWT survives the navigation the
   way a real same-origin redirect does. */
function makeStorage(seed){
  const m = Object.assign({}, seed||{});
  return {getItem:k=>(k in m? m[k] : null), setItem:(k,v)=>{m[k]=String(v);},
          removeItem:k=>{delete m[k];}, _dump:()=>m};
}

function env(storage, routes){
  const cache=new Map(); const calls=[];
  const get=id=>(cache.has(id)||cache.set(id,mk(id)),cache.get(id));
  const known=id=>IDS.has(id)||['app-shell','resume-banner'].includes(id);
  const screens=[...IDS].filter(i=>/^screen-/.test(i)).concat(['app-shell']);
  screens.forEach(get);
  // Markup ships with #screen-login active.
  get('screen-login').classList.add('active');
  const dynamic=new Map();
  const doc={getElementById:id=>(dynamic.has(id)?dynamic.get(id):(known(id)?get(id):null)),
    createElement:tag=>{const el=mk('');
      // Register under its id as soon as one is assigned, mimicking the DOM.
      return new Proxy(el,{set(t,k,v){t[k]=v; if(k==='id'&&v) dynamic.set(v,t); return true;}});},
    querySelector:()=>null,
    querySelectorAll:sel=>sel==='.screen'?screens.map(get):[],
    addEventListener(){},body:Object.assign(mk('body'),{style:{},appendChild(){}}),
    execCommand:()=>true};
  const sb={console,document:doc,sessionStorage:storage,localStorage:storage,
    setTimeout:(f)=>{return 0;},clearTimeout(){},setInterval:()=>0,clearInterval(){},
    alert(){},scrollTo(){},confirm:()=>true,FormData:function(){this.append=()=>{}},
    navigator:{userAgent:'h',clipboard:{writeText:()=>Promise.resolve()}},
    isSecureContext:true,location:{href:'/',pathname:'/'},
    requestAnimationFrame:()=>0,pdfjsLib:{GlobalWorkerOptions:{}},Tesseract:{},
    URL:{createObjectURL:()=>'b'},open:()=>({}),
    fetch(u,o){const s=String(u);
      calls.push({url:s,method:(o&&o.method)||'GET',headers:(o&&o.headers)||{}});
      for(const [pat,resp] of (routes||[]))
        if(s.includes(pat)) return Promise.resolve({ok:resp.ok!==false,status:resp.status||200,
          json:()=>Promise.resolve(resp.body||{})});
      // Default: an ARRAY, since list endpoints (enrollments, projects) map over it.
      return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve([])});}};
  sb.window=sb; const ctx=vm.createContext(sb); vm.runInContext(src,ctx);
  return {ctx,get,calls,storage,run:c=>vm.runInContext(c,ctx),
    active:()=>screens.filter(id=>get(id).classList.contains('active'))};
}
const settle=async()=>{for(let i=0;i<10;i++) await new Promise(r=>setImmediate(r));};
const CUSTOMER_ME={ok:true,body:{enrollment_id:42,enrollment_code:'ENR-42',
  customer:{first_name:'Jane',last_name:'Doe'},workflow_step_key:'contracts',
  workflow_step_label:'Agreements'}};

console.log('='.repeat(72));
console.log('MAGIC-LINK HANDOFF');
console.log('='.repeat(72));

(async()=>{
  // ── 1/2. access.html stores the JWT; it must survive the navigation ──
  console.log('\n--- 1-2. JWT survives /access -> / ---');
  check('access.html stores under the EXISTING customer key',
    access.includes("sessionStorage.setItem('dalton_customer_token'"));
  check('  ...and clears any stale staff token',
    access.includes("sessionStorage.removeItem('dalton_auth_token')"));
  check('  ...then navigates same-origin (storage persists)',
    access.includes("window.location.replace('/')"));
  check('app.js reads that same key',
    /KEY: 'dalton_customer_token'/.test(src));

  // Storage carried across the "navigation", exactly like a real redirect.
  const store = makeStorage();
  store.setItem('dalton_customer_token','JWT-CUSTOMER');
  const e = env(store, [['/api/auth/customer-me', CUSTOMER_ME]]);
  check('the JWT is present on / at boot',
    e.run("CustomerAuth.getToken()")==='JWT-CUSTOMER');

  // ── 3/4. boot restores the customer and opens the portal ──
  console.log('\n--- 3-4. customer session restored on initial load ---');
  check('login is the initially-active screen (as shipped)',
    e.active().join()==='screen-login');
  await e.run('bootRestoreSession();'); await settle();
  check('customer-me was called with the customer JWT',
    e.calls.some(c=>c.url.includes('/api/auth/customer-me')
      && String(c.headers.Authorization||'').includes('JWT-CUSTOMER')));
  check('the customer PORTAL is now active',
    e.active().includes('screen-customer-portal'));
  check('  ...and login is NOT', !e.active().includes('screen-login'));
  check('  ...exactly one screen is active', e.active().length===1);
  check('the enrollment id is bound from the session',
    e.run('customerEnrollmentId')===42);
  check('no password was required',
    !e.calls.some(c=>c.url.includes('/api/auth/signin')));

  // ── 5. staff restore unchanged ──
  console.log('\n--- 5. staff restore still works ---');
  const staffStore = makeStorage(); staffStore.setItem('dalton_auth_token','JWT-STAFF');
  const s1 = env(staffStore, [['/api/auth/me',{ok:true,body:{id:1,role:'sales_rep',
    full_name:'Rep',email:'r@d.com'}}],['/api/projects',{ok:true,body:[]}]]);
  await s1.run('bootRestoreSession();'); await settle();
  check('staff session activates the app shell', s1.active().includes('app-shell'));
  check('  ...and NOT the customer portal',
    !s1.active().includes('screen-customer-portal'));
  check('  ...customer-me was never called',
    !s1.calls.some(c=>c.url.includes('customer-me')));
  check('tryRestoreSession itself is unchanged (staff-only)',
    /async function tryRestoreSession\(\)\{\s*const token = AuthStore\.getToken\(\);/.test(src));

  // ── 6. stores stay separate ──
  console.log('\n--- 6. staff and customer stores stay separate ---');
  check('distinct keys', /KEY: 'dalton_auth_token'/.test(src) && /KEY: 'dalton_customer_token'/.test(src));
  check('access.html clears the CORRECT staff key (matches AuthStore.KEY)',
    access.includes("sessionStorage.removeItem('dalton_auth_token')")
    && !access.includes("removeItem('dalton_token')"));
  check('restoring a customer clears any staff token',
    /AuthStore\.clear\(\);\s*\n\s*currentUser = null;/.test(
      src.split('async function tryRestoreCustomerSession')[1]));
  const both = makeStorage();
  both.setItem('dalton_auth_token','JWT-STAFF'); both.setItem('dalton_customer_token','JWT-CUSTOMER');
  const b = env(both, [['/api/auth/me',{ok:true,body:{id:1,role:'admin',full_name:'A',email:'a@d.com'}}],
                       ['/api/projects',{ok:true,body:[]}],
                       ['/api/auth/customer-me', CUSTOMER_ME]]);
  await b.run('bootRestoreSession();'); await settle();
  check('with BOTH present, staff wins', b.active().includes('app-shell'));
  check('  ...customer restore is skipped',
    !b.calls.some(c=>c.url.includes('customer-me')));

  // ── 7. invalid customer token -> unified login ──
  console.log('\n--- 7. invalid/expired customer token -> UNIFIED login ---');
  const badStore = makeStorage(); badStore.setItem('dalton_customer_token','JWT-EXPIRED');
  const bad = env(badStore, [['/api/auth/customer-me',{ok:false,status:401,body:{error:'expired'}}]]);
  await bad.run('bootRestoreSession();'); await settle();
  check('the dead token is cleared',
    bad.storage.getItem('dalton_customer_token')===null);
  check('the UNIFIED login stays active', bad.active().join()==='screen-login');
  check('  ...NOT a retired customer-login screen',
    !bad.active().includes('screen-customer-login'));
  check('  ...and that screen no longer exists at all',
    !html.includes('id="screen-customer-login"'));
  check('  ...nor does doCustomerLogin', !/function doCustomerLogin\(/.test(src));

  // ── CSS regression ──
  console.log('\n--- CSS: base #screen-login must NOT force display ---');
  const rules=[...css.matchAll(/#screen-login\s*(\{[^}]*\})/g)].map(m=>m[1]);
  check('bare #screen-login rules exist but set no display', rules.length>0
    && !rules.some(r=>/display\s*:/.test(r)));
  check('  ...display:grid lives on #screen-login.active',
    /#screen-login\.active\{[^}]*display:grid/.test(css));
  check('  ...visibility is still driven by .screen/.screen.active',
    /\.screen\{display:none;\}/.test(css) && /\.screen\.active\{display:block;\}/.test(css));

  // ── rep-side control ──
  console.log('\n--- rep-side Copy customer link ---');
  check('button rendered in the resume banner', src.includes('id="copy-customer-link"'));
  check('  ...gated on the AUTHORITATIVE workflow key',
    /function customerLinkAvailable\([\s\S]{0,400}workflow_step_key/.test(src));
  check('  ...never on the human label',
    !src.split('function customerLinkAvailable')[1].slice(0,400).includes('workflow_step_label'));
  check('  ...hidden once terminal', src.includes('e.workflow_is_terminal === true) return false'));
  check('copy has a real fallback, never prompt()',
    src.includes("document.execCommand('copy')") && src.includes('showManualCopyField(')
    && !src.includes('window.prompt('));
  check('  ...and shows Copied', src.includes("? 'Copied'"));
  check('no automatic send exists',
    !/sendEmail|sendSms|twilio|mailto:/i.test(src.split('function copyCustomerLink')[1].slice(0,1200)));

  // ── newer features preserved ──
  console.log('\n--- newer work preserved (no regression) ---');
  for(const [label,needle] of [
    ['self-attestation branch','function prepareSelfAttestation'],
    ['program/savings resolver','function resolveProgramView'],
    ['enrollment isolation reset','docSetResetAll()'],
    ['commit-boundary flag','e.perch_committed === true'],
    ['unified signin','/api/auth/signin'],
    ['UBS branding','Utility Bill Savings']])
    check(`${label} still present`, src.includes(needle) || html.includes(needle));


  // ── portal greeting ──
  console.log('\n--- portal greeting ---');
  const g = env(makeStorage(), []);
  const helloEl = g.get('portal-hello');
  g.run("renderPortalGreeting({first_name:'Jane', last_name:'Doe'});");
  check('first_name present -> "Hi, <FirstName>"', helloEl.textContent==='Hi, Jane');
  g.run("renderPortalGreeting({first_name:'', last_name:'Doe'});");
  check('blank first_name -> "Welcome back"', helloEl.textContent==='Welcome back');
  g.run("renderPortalGreeting({first_name:'   '});");
  check('whitespace-only -> "Welcome back"', helloEl.textContent==='Welcome back');
  g.run("renderPortalGreeting({});");
  check('missing first_name -> "Welcome back"', helloEl.textContent==='Welcome back');
  g.run("renderPortalGreeting(null);");
  check('no customer at all -> "Welcome back"', helloEl.textContent==='Welcome back');
  check('  ...never the old dangling "Hi  — welcome back"',
    !/'Hi ' \+ \(\(data\.customer/.test(src));
  check('BOTH entry paths use the one helper',
    (src.match(/renderPortalGreeting\(/g)||[]).length >= 3);
  check('  ...including the magic-link restore path',
    /renderPortalGreeting\(me\.customer\)/.test(src));
  check('  ...and unified login', /renderPortalGreeting\(data\.customer\)/.test(src));

  // ── copy link in the LIVE straight-through flow ──
  console.log('\n--- Copy customer link: LIVE flow (no resume needed) ---');
  const L = env(makeStorage(), []);
  L.run("currentDraft={enrollment_id:77,enrollment_code:'ENR-77'};" +
        "state.customer.email='c@example.com';" +
        "perchContext.nextStepKey='contracts';");
  check('available at a customer-action step', L.run('customerLinkAvailableLive()')===true);
  L.run('refreshCustomerLinkControl();');
  const slotHtml = () => {
    const kids = L.get('view-wizard')._children || [];
    return kids.length ? String(kids[0].innerHTML || '') : '';
  };
  check('  ...the control mounts WITHOUT resuming',
    slotHtml().includes('id="copy-customer-link"'));
  check('  ...bound to the live enrollment id',
    slotHtml().includes('copyCustomerLink(77'));
  check('  ...mounted from goStep, which runs in both flows',
    /renderStepper\(n\);[\s\S]{0,320}refreshCustomerLinkControl\(\);/.test(src));

  for(const k of ['proof_docs','self_attestation','self_attestation_accept',
                  'contracts','contracts_review','contracts_accept']){
    L.run("perchContext.nextStepKey='"+k+"';");
    check(`  ...available at ${k}`, L.run('customerLinkAvailableLive()')===true);
  }
  for(const k of ['service_area','capacity_result','enroll']){
    L.run("perchContext.nextStepKey='"+k+"';");
    check(`  ...NOT offered pre-/enroll at ${k}`, L.run('customerLinkAvailableLive()')===false);
  }

  console.log('\n--- stays hidden once terminal ---');
  L.run("perchContext.nextStepKey='contracts_accepted';");
  check('terminal step -> unavailable', L.run('customerLinkAvailableLive()')===false);
  L.run('refreshCustomerLinkControl();');
  // The mount is asserted above against the real function; here we assert the
  // removal PATH exists and that availability drives it.
  check('  ...refresh removes the slot when unavailable',
    /if\(!customerLinkAvailableLive\(\)\)\{[\s\S]{0,160}removeChild\(slot\)/.test(src));
  check('  ...so a terminal enrollment shows no button',
    L.run('customerLinkAvailableLive()')===false);
  L.run("perchContext.nextStepKey='contracts';" +
        "currentEnrollmentDetail={id:77,workflow_is_terminal:true};");
  check('a terminal detail also blocks it', L.run('customerLinkAvailableLive()')===false);
  L.run("currentEnrollmentDetail=null; state.customer.email='';");
  check('no customer email -> unavailable', L.run('customerLinkAvailableLive()')===false);
  L.run("currentDraft=null; state.customer.email='c@example.com';");
  check('no enrollment -> unavailable', L.run('customerLinkAvailableLive()')===false);

  console.log('\n--- resume path still works, and both share one key list ---');
  check('resume availability helper still present',
    /function customerLinkAvailable\(e\)/.test(src));
  check('  ...still keyed on workflow_step_key',
    /function customerLinkAvailable\([\s\S]{0,400}workflow_step_key/.test(src));
  check('  ...still hidden once terminal',
    src.includes('e.workflow_is_terminal === true) return false'));
  check('ONE shared key list, so the two cannot drift',
    /const CUSTOMER_ACTION_STEP_KEYS = \[/.test(src)
    && /CUSTOMER_ACTION_STEP_KEYS\.indexOf/.test(src));
  check('neither check uses a human-readable label',
    !src.split('function customerLinkAvailableLive')[1].slice(0,700).includes('workflow_step_label'));

  console.log('\n--- link behaviour unchanged ---');
  check('token generation still server-side only',
    src.includes("'/api/enrollments/' + enrollmentId + '/customer-link'"));
  check('clipboard fallback intact',
    src.includes("document.execCommand('copy')") && src.includes('showManualCopyField('));
  check('  ...still never prompt()', !src.includes('window.prompt('));
  check('no automatic delivery', !/sendEmail|sendSms|twilio|mailto:/i.test(src));

  const f=R.filter(r=>!r.ok);
  console.log('\n'+'='.repeat(72));
  console.log(`${R.length-f.length} passed, ${f.length} failed`);
  console.log('='.repeat(72));
  if(f.length){f.forEach(x=>console.log('  FAILED: '+x.l));process.exit(1);}
  process.exit(0);
})();

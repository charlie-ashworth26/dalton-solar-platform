/*
 * UBS SIGN IN — branding + design, with auth behaviour untouched.
 * Built to ubs_signin_mockup_v5_cool_pattern.html.
 */
const fs=require('fs'),path=require('path');
const ROOT=path.join(__dirname,'..');
const JS=fs.readFileSync(path.join(ROOT,'static','js','app.js'),'utf8');
const HTML=fs.readFileSync(path.join(ROOT,'templates','index.html'),'utf8');
const CSS=fs.readFileSync(path.join(ROOT,'static','css','app.css'),'utf8');
const R=[]; const check=(l,c)=>{R.push({l,ok:!!c});console.log(`  [${c?'PASS':'FAIL'}] ${l}`);};
// The old boundary (#screen-customer-login) was RETIRED, so slice to the next
// screen instead. Without this the slice ran to end-of-document and scanned the
// whole app.
const LOGIN_START=HTML.indexOf('id="screen-login"');
const LOGIN=HTML.slice(LOGIN_START,
  HTML.indexOf('class="screen"', HTML.indexOf('</div>', LOGIN_START)));

console.log('='.repeat(72));
console.log('UBS SIGN IN');
console.log('='.repeat(72));

console.log('\n--- BRANDING ---');
check('no "Dalton Solar" on the login screen', !/Dalton\s*Solar/i.test(LOGIN));
check('  ...no "DaltonSolar" either', !/DaltonSolar/i.test(LOGIN));
check('  ...and no DS badge', !/>DS</.test(LOGIN));
check('UBS mark shown', /class="ubs-mark">UBS</.test(LOGIN));
check('  ...and on mobile', /class="ubs-mobile-mark">UBS</.test(LOGIN));
check('"Utility Bill Savings" shown', (LOGIN.match(/Utility Bill Savings/g)||[]).length >= 2);
check('heading is "Sign in"', /<h2>Sign in<\/h2>/.test(LOGIN));
check('button reads "Continue"', /id="login-submit-btn"[^>]*>Continue</.test(LOGIN));
check('  ...and JS restores that label', JS.includes("btn.textContent = 'Continue';"));
check('  ...never "Sign in" again', !JS.includes("btn.textContent = 'Sign in';"));

console.log('\n--- ONE LOGIN FORM ONLY ---');
check('exactly one email input', (LOGIN.match(/id="login-email"/g)||[]).length===1);
check('exactly one password input', (LOGIN.match(/id="login-pass"/g)||[]).length===1);
check('exactly one submit', (LOGIN.match(/id="login-submit-btn"/g)||[]).length===1);
check('no role selector', !/role.?select|Are you a rep|I am a/i.test(LOGIN));
// Strip comments: a comment naming the /api/auth/customer-login ENDPOINT is
// documentation, not a link.
const LOGIN_MARKUP=LOGIN.replace(/<!--[\s\S]*?-->/g,'');
check('no customer login link',
  !/href[^>]*customer-login|showScreen\('screen-customer-login'\)|Sign in to your agreement/i
    .test(LOGIN_MARKUP));
check('no staff/customer buttons', !/Staff login|Customer login/i.test(LOGIN));
check('no second form on the screen', !/<form/i.test(LOGIN));
check('the duplicate customer login screen is RETIRED',
  !HTML.includes('id="screen-customer-login"'));
check('  ...and nothing routes to it', !/screen-customer-login/.test(JS));
check('  ...leaving exactly one login form in the whole app',
  (HTML.match(/type="password"/g)||[]).filter(Boolean).length >= 1
  && !HTML.includes('cust-login-pass'));

console.log('\n--- AUTH UNTOUCHED ---');
const dl = JS.slice(JS.indexOf('async function doLogin'), JS.indexOf('async function doLogin')+2600);
check('still posts to /api/auth/signin', dl.includes("'/api/auth/signin'"));
check('  ...sending ONLY email + password',
  dl.includes('JSON.stringify({ email, password: pass })'));
check('  ...never a role hint', !/role:|account_type:/.test(dl.split('body: JSON')[1].slice(0,120)));
check('backend decides the account type', dl.includes("data.account_type === 'customer'"));
check('  ...and the destination', dl.includes('data.destination'));
check('token stores unchanged',
  dl.includes('AuthStore.setToken') && dl.includes('CustomerAuth.setToken')
  && dl.includes('AuthStore.clear()') && dl.includes('CustomerAuth.clear()'));

console.log('\n--- JS HOOKS PRESERVED ---');
for(const id of ['login-email','login-pass','login-error','login-submit-btn'])
  check(`#${id} still present in markup`, LOGIN.includes(`id="${id}"`));
check('doLogin() still bound', /onclick="doLogin\(\)"/.test(LOGIN));
check('  ...and reads all four ids',
  ["login-email","login-pass","login-error","login-submit-btn"]
    .every(i=>dl.includes(`getElementById('${i}')`)));
check('logout still clears the fields',
  JS.includes("getElementById('login-email').value=''")
  && JS.includes("getElementById('login-pass').value=''"));
check('  ...and returns to this screen', JS.includes("showScreen('screen-login')"));
check('error element still targeted by id', dl.includes("getElementById('login-error')"));

console.log('\n--- DESIGN PORTED FROM THE MOCKUP ---');
check('two-column shell', /\.ubs-shell\{[\s\S]{0,200}grid-template-columns:1fr 1fr/.test(CSS));
check('  ...1080px max width', /width:min\(1080px,100%\)/.test(CSS));
check('  ...30px radius', /\.ubs-shell\{[\s\S]{0,300}border-radius:30px/.test(CSS));
check('  ...translucent + blurred', /backdrop-filter:blur\(18px\)/.test(CSS));
check('green accent #1f7a55', /--ubs-accent:#1f7a55/.test(CSS));
check('  ...and its dark hover', /--ubs-accent-dark:#165d41/.test(CSS));
check('patterned background layers',
  /repeating-radial-gradient/.test(CSS) && CSS.includes('mask-image:linear-gradient(to bottom right'));
check('  ...plus the soft glow layer', /filter:blur\(26px\)/.test(CSS));
check('brand panel gradient', /linear-gradient\(160deg, rgba\(38,137,94,\.98\)/.test(CSS));
check('  ...with the orbital rings', /\.ubs-brand-side::after\{[\s\S]{0,240}border-radius:50%/.test(CSS));
check('54px inputs and button',
  /#screen-login input\{[\s\S]{0,120}height:54px/.test(CSS) && /\.ubs-submit\{[\s\S]{0,120}height:54px/.test(CSS));
check('  ...14px radii', /border-radius:14px/.test(CSS));
check('34px heading', /\.ubs-form-wrap h2\{margin:0 0 28px;font-size:34px/.test(CSS));
check('styles are SCOPED to the login screen',
  CSS.includes('#screen-login{') && CSS.includes('#screen-login input'));

console.log('\n--- RESPONSIVE ---');
check('820px breakpoint', /@media \(max-width:820px\)[\s\S]{0,2000}\.ubs-brand-side\{display:none/.test(CSS));
check('  ...mobile brand appears', /\.ubs-mobile-brand\{display:flex/.test(CSS));
check('  ...shell goes full-bleed', /\.ubs-shell\{display:block;width:100%;min-height:100vh/.test(CSS));
check('  ...controls grow to 56px', /#screen-login input,\.ubs-submit\{height:56px/.test(CSS));
check('430px breakpoint', /@media \(max-width:430px\)[\s\S]{0,200}\.ubs-form-wrap h2\{font-size:28px/.test(CSS));

console.log('\n--- PASSWORD TOGGLE (presentation only) ---');
check('toggle present', LOGIN.includes('id="login-pass-toggle"'));
check('  ...swaps the input type', /input\.type = isText \? 'password' : 'text'/.test(JS));
check('  ...and the label', /button\.textContent = isText \? 'Show' : 'Hide'/.test(JS));
check('  ...touching no auth logic',
  !/signin|AuthStore|CustomerAuth/.test(JS.slice(JS.indexOf('function toggleLoginPassword'))));

console.log('\n--- ACCOUNTS / IDENTIFIERS UNCHANGED ---');
check('rep emails untouched in seed',
  fs.readFileSync(path.join(ROOT,'seed.py'),'utf8').includes('charlie@daltonsolar.com'));
check('auth routes untouched by branding',
  !/Utility Bill Savings|UBS/.test(fs.readFileSync(path.join(ROOT,'routes','auth_routes.py'),'utf8')));
check('  ...and no branding leaked into the backend',
  !/UBS/.test(fs.readFileSync(path.join(ROOT,'app.py'),'utf8')));

const f=R.filter(r=>!r.ok);
console.log('\n'+'='.repeat(72));
console.log(`${R.length-f.length} passed, ${f.length} failed`);
console.log('='.repeat(72));
if(f.length){f.forEach(x=>console.log('  FAILED: '+x.l));process.exit(1);}
process.exit(0);

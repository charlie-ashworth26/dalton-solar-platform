/*
 * CUSTOMER & BILL rebuild — proves the composition genuinely changed AND that
 * the functional contract survived. The grayscale test is yours to make in the
 * browser; this asserts the structural facts behind it.
 */
const fs=require('fs'),path=require('path');
const ROOT=path.join(__dirname,'..');
const html=fs.readFileSync(path.join(ROOT,'templates','index.html'),'utf8');
const css=fs.readFileSync(path.join(ROOT,'static','css','app.css'),'utf8');
const js=fs.readFileSync(path.join(ROOT,'static','js','app.js'),'utf8');
const R=[]; const check=(l,c)=>{R.push({l,ok:!!c});console.log(`  [${c?'PASS':'FAIL'}] ${l}`);};
const block=(()=>{const i=html.indexOf('id="step-bill"');const s=html.lastIndexOf('<div',i);
  let d=0,j=s;const re=/<div\b|<\/div>/g;re.lastIndex=s;let m;
  while((m=re.exec(html))){d+=m[0]==='</div>'?-1:1;if(d===0){j=re.lastIndex;break;}}
  return html.slice(s,j);})();

console.log('='.repeat(72));
console.log('CUSTOMER & BILL — rebuilt composition');
console.log('='.repeat(72));

console.log('\n--- the old composition is GONE ---');
check('no .card wrapper on this screen', !/class="card"/.test(block));
check('no legacy .field wrappers', !/class="field"/.test(block));
check('no .row2 / .row3 grids', !/class="row2"/.test(block) && !/class="row3/.test(block));
check('no .form-section', !/form-section/.test(block));
check('no .lead paragraph', !/class="lead"/.test(block));
check('no .card-eyebrow', !/card-eyebrow/.test(block));
check('no .wizard-actions', !/wizard-actions/.test(block));
check('no .dropzone (replaced by the centrepiece)', !/class="dropzone"/.test(block));
check('no .ctx-line', !/ctx-line/.test(block));

console.log('\n--- a genuine page shell, not a centred card ---');
check('workbench root', /class="screen wizard-step wb"/.test(block));
check('POD ID preserved as a conditional field',
  block.includes('id="pod-id-field"') && /style="display:none;"/.test(
    (block.match(/<div class="fd full" id="pod-id-field"[^>]*>/)||[''])[0]));
check('  ...uses the mockup canvas width', /\.wb\{max-width:1120px/.test(css));
check('page header with title and sub (mockup has no eyebrow)',
  /wb-title/.test(block) && /wb-sub/.test(block) && !/wb-eyebrow/.test(block));
check('  ...with the fluid hero title',
  /\.wb \.wb-title\{font-size:var\(--t-hero\)/.test(css));
check('the 620px shell cap that crushed this screen is lifted',
  /\.wizard-inner\{width:100%;max-width:1200px/.test(css));
check('  ...upload is its own full-width section, capped at 620px',
  block.includes('class="upload"') && /\.upload\{[\s\S]{0,200}max-width:620px/.test(css));
// The rail was REMOVED - it did not earn its column. Its content is now a
// compact metadata strip under the header, giving full width to the work.
check('  ...no rail; the utility chip carries context',
  !block.includes('wb-rail') && block.includes('id="util-chip"'));
/* RE-PINNED (Phase C4). This asserted the mockup's three separate grids -
   .access at 1fr 1fr 1fr, .address at 2fr 1fr 110px, .fields at repeat(2).
   Having three grids on one form WAS the alignment defect C4 was asked to
   fix: measured at 1440px the screen showed seven field widths and seven left
   edges, and the address needed five rows. One twelve-column grid now serves
   all three, which is what makes the rows line up and cut the screen from
   1965px to 1744px. The check is for the single grid, and for the spans that
   put three fields on a row - the thing the old repeat(2) could not express. */
const flatCss = css.replace(/\s+/g, '');
check('  ...one shared twelve-column grid, not three ad-hoc ones',
  /\.access,\.fields,\.address\{display:grid;grid-template-columns:repeat\(12,minmax\(0,1fr\)\)/
    .test(flatCss)
  && !/\.address\{display:grid;grid-template-columns:2fr1fr110px/.test(flatCss));
check('  ...with thirds for the name/account and access rows',
  /\.access>\.fd,\.fields>\.fd\{grid-column:span4/.test(flatCss));
check('  ...and the address spanning 8+4 then 6+3+3',
  /\.address\.street\{grid-column:span8/.test(flatCss)
  && /\.address\.unit\{grid-column:span4/.test(flatCss)
  && /\.address\.city\{grid-column:span6/.test(flatCss)
  && /\.address\.state\{grid-column:span3/.test(flatCss)
  && /\.address\.zip\{grid-column:span3/.test(flatCss));

console.log('\n--- sections separated by hierarchy, not card chrome ---');
check('four sections, in the approved order',
  (block.match(/class="sec"/g)||[]).length===4);
// Numbered mini-steps removed by design; headings carry the structure.
check('  ...no numbered mini-steps', !block.includes('class="blk-num"'));
// Service address became its own full-width row, so there are five headings.

check('  ...separated by a top rule, not a bordered box',
  /\.sec\{padding:var\(--s5\) 0;border-top:1px solid/.test(css));
check('  ...no background fill on sections', !/\.sec\{[^}]*background:/.test(css));
check('section heads pair a title with supporting copy', /\.sec-head h2\{/.test(css));
// Approved order: program -> customer access -> utility bill -> review.
check('order matches the approved mockup',
  block.indexOf('Choose a savings program') < block.indexOf('Customer access') &&
  block.indexOf('Customer access') < block.indexOf('Utility bill') &&
  block.indexOf('Utility bill') < block.indexOf('Review account details'));
check('OCR upload comes BEFORE the fields it fills',
  block.indexOf('bill-dropzone') < block.indexOf('id="c-first"'));

console.log('\n--- upload is a centrepiece ---');
check('upload zone present', /class="upload"/.test(block));
check('  ...150px min-height per the mockup', /\.upload\{[\s\S]{0,200}min-height:150px/.test(css));
check('  ...with an icon, headline and sub copy',
  /up-ico/.test(block) && /<strong>Drop the bill/.test(block) && /<small>/.test(block));
check('  ...not nested inside a card', !/class="card"/.test(block));

console.log('\n--- FUNCTIONAL CONTRACT preserved ---');
// svc-context and c-email-display belonged to the removed context strip; the
// mockup surfaces the same live values through #util-chip. Their writers are
// null-guarded, verified below.
const NEED=['program-options','bill-file','bill-file-chip','ocr-container',
 'c-first','c-last','c-acct','acct-rule-help','pod-id-field','c-pod','pod-rule-help',
 'a-street','a-unit','a-city','a-state','a-zip','billing-same','billing-address-fields',
 'b-street','b-unit','b-city','b-state','b-zip','bill-requirements','bill-submit-error',
 'c-email','c-phone','c-pass','c-pass-confirm','contact-submit-error',
 'btn-bill-next','bill-dropzone','bill-fileset','bill-prefill-scope','c-pass-eye','c-pass-confirm-eye'];
const missing=NEED.filter(i=>!html.includes(`id="${i}"`));
check(`all ${NEED.length} required ids reattached (missing: ${missing.join(',')||'none'})`, !missing.length);
check('removed strip elements are written defensively',
  /const disp = document\.getElementById\('c-email-display'\);\s*\n\s*if\(disp\)/.test(js)
  && /const host = document\.getElementById\('svc-context'\);\s*\n\s*if\(host\)/.test(js));
for(const h of ['handleBillUpload','checkBillReady','syncBillingFromService',
                'toggleBillingAddress','togglePasswordVisibility','submitBill','backFromCustomer',
                'dzDragOver','dzDragLeave','dzDrop'])
  check(`handler ${h} still wired`, block.includes(h+'('));
check('state dropdowns populated (both)',
  (block.match(/<option value="NY" selected>NY<\/option>/g)||[]).length===2);
check('  ...and still drive billing sync', /id="a-state" onchange="syncBillingFromService\(\); checkBillReady\(\)"/.test(block));
check('ONE Continue button', (block.match(/id="btn-bill-next"/g)||[]).length===1);
check('  ...still disabled until valid', /id="btn-bill-next"[^>]*disabled/.test(block));
check('no btn-contact-next resurrected', !block.includes('btn-contact-next'));
check('email stays hidden context, not a re-asked field',
  /<input type="email" id="c-email" readonly hidden>/.test(block));
check('multi-file upload preserved', /id="bill-file"[^>]*multiple/.test(block));
check('  ...accepting pdf/jpg/png/heic', /accept="\.pdf,\.jpg,\.jpeg,\.png,\.heic"/.test(block));

console.log('\n--- responsive intent ---');

// RE-PINNED (Phase C3): the mockup's intent was ONE breakpoint for this
// screen, not the number 900. C3 consolidated the thirteen scattered
// breakpoints into four (430/599/899/1199), which moved this screen's rules
// from a 900px band to the shared 899px touch band - one band still, and now
// the same one as the rest of the application rather than a near-duplicate
// one pixel away. What is asserted is the intent: the screen's responsive
// rules live in a single band, and it is one of the four documented ones.
// C4: the single-column collapse is now declared on the shared grid
// (.access,.fields,.address) rather than on .fields alone, so this looks for
// that rule instead. The property - ONE band carries this screen's responsive
// rules, and it is one of the four documented breakpoints - is unchanged.
const screenBands = [...new Set(
  (css.match(/@media[^{]*?max-width:\s*(\d+)px\s*\)\s*\{[\s\S]*?\.access,\.fields,\.address\{grid-template-columns:repeat\(6/g)
   || []).map((s) => +/(\d+)px/.exec(s)[1]))];
check(`single breakpoint per the mockup (${screenBands.join(', ') || 'none'})`,
  screenBands.length === 1 && [430, 599, 899, 1199].includes(screenBands[0]));
check('actions row is a bordered footer', /\.actions\{display:flex;justify-content:space-between/.test(css));

console.log('\n--- untouched elsewhere ---');
check('protected agreement block intact', js.includes('agreementLinksHtml'));
check('one acknowledgement checkbox', (js.match(/id="agr-ack-check"/g)||[]).length===1);
check('branch logic intact', js.includes('if(needsLmi === false) return [1,2,5];'));
check('merged submit intact', js.includes('submitContactDetails'));

const f=R.filter(r=>!r.ok);
console.log('\n'+'='.repeat(72));
console.log(`${R.length-f.length} passed, ${f.length} failed`);
console.log('='.repeat(72));
if(f.length){f.forEach(x=>console.log('  FAILED: '+x.l));process.exit(1);}
process.exit(0);

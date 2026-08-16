/* Multi-file document set: FRONTEND runtime behaviour. */
const fs=require('fs'),path=require('path'),vm=require('vm');
const ROOT=path.join(__dirname,'..');
const src=fs.readFileSync(path.join(ROOT,'static','js','app.js'),'utf8');
const html=fs.readFileSync(path.join(ROOT,'templates','index.html'),'utf8');
const IDS=new Set([...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]));
const R=[]; const check=(l,c)=>{R.push({l,ok:!!c});console.log(`  [${c?'PASS':'FAIL'}] ${l}`);};

const mk=id=>({id,innerHTML:'',textContent:'',value:'',files:null,style:{},dataset:{},
  classList:{add(){},remove(){},toggle(){}},addEventListener(){},setAttribute(){},
  querySelector:()=>null,querySelectorAll:()=>[],focus(){},appendChild(){}});
const cache=new Map(); const sent=[];
const sb={console,
  document:{getElementById:id=>IDS.has(id)?(cache.has(id)||cache.set(id,mk(id)),cache.get(id)):null,
    createElement:mk,querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){},
    body:Object.assign(mk('body'),{style:{}}),activeElement:null},
  sessionStorage:{getItem:()=>'t',setItem(){},removeItem(){}},
  localStorage:{getItem:()=>'t',setItem(){},removeItem(){}},
  setTimeout:()=>0,clearTimeout(){},setInterval:()=>0,clearInterval(){},alert(){},scrollTo(){},
  FormData:function(){this.parts=[];this.append=(k,v,n)=>this.parts.push([k,n||v]);},
  navigator:{userAgent:'h'},location:{href:''},requestAnimationFrame:()=>0,
  URL:{createObjectURL:()=>'blob:stub',revokeObjectURL(){}},
  open:()=>({location:'',close(){}}),AuthStore:{getToken:()=>'tok'},
  Blob:function(){},alert(){},
  pdfjsLib:{GlobalWorkerOptions:{}},Tesseract:{},
  fetch:(u,o)=>{sent.push({url:String(u),body:o&&o.body});
    if(String(u).includes('/view')){
      return Promise.resolve({ok:true,status:200,blob:()=>Promise.resolve({size:1})});
    }
    return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve(
      {document_set_id:77,file_count:(o&&o.body&&o.body.parts?o.body.parts.filter(p=>p[0]==='files').length:0),
       files:[],extraction:{status:'ocr_unavailable',usable:false,fields:{},confidence:null},
       manual_entry_required:true,message:"We could not reliably read this bill."})});}};
sb.window=sb; const ctx=vm.createContext(sb);
vm.runInContext(src,ctx);
vm.runInContext('currentDraft={enrollment_id:5};',ctx);

const F=(n,s,t)=>({name:n,size:s,lastModified:t||1});

console.log('='.repeat(72));
console.log('MULTI-FILE DOCUMENT SETS (frontend runtime)');
console.log('='.repeat(72));

(async()=>{
  console.log('\n--- markup supports multi-select ---');
  check('bill input accepts multiple', /id="bill-file"[^>]*multiple/.test(html));
  check('LMI input accepts multiple', /id="lmi-file"[^>]*multiple/.test(html));
  check('HEIC is accepted by the picker', /id="bill-file"[^>]*heic/i.test(html));
  check('dropzone forwards ALL dropped files',
    /dzDrop[\s\S]{0,220}dataTransfer\.files[\s\S]{0,80}handler\(files\)/.test(src));

  console.log('\n--- selecting several files at once ---');
  vm.runInContext("docSetReset('utility_bill');",ctx);
  vm.runInContext("docSetAddFiles('utility_bill',[{name:'p1.jpg',size:10,lastModified:1},{name:'p2.jpg',size:11,lastModified:2},{name:'p3.jpg',size:12,lastModified:3}]);",ctx);
  check('all three tracked as ONE set',
    vm.runInContext("DocSets.utility_bill.files.length",ctx)===3);
  check('selection order preserved',
    vm.runInContext("DocSets.utility_bill.files.map(x=>x.file.name).join(',')",ctx)==='p1.jpg,p2.jpg,p3.jpg');
  // DocSets renders into its OWN element, separate from the legacy single-file
  // chip, so the two cannot overwrite one another.
  const listed=vm.runInContext("document.getElementById('bill-fileset').innerHTML",ctx);
  check('every file is listed with a position',
    (listed.match(/class="docset-n"/g)||[]).length===vm.runInContext('DocSets.utility_bill.files.length',ctx));
  check('every file has a remove control',
    (listed.match(/class="docset-remove"/g)||[]).length===vm.runInContext('DocSets.utility_bill.files.length',ctx));
  check('names are shown', listed.includes('p1.jpg')&&listed.includes('p3.jpg'));

  console.log('\n--- adding more files later ---');
  vm.runInContext("docSetAddFiles('utility_bill',[{name:'p4.jpg',size:13,lastModified:4}]);",ctx);
  check('appended, not replaced',
    vm.runInContext("DocSets.utility_bill.files.length",ctx)===4);
  check('appended at the end',
    vm.runInContext("DocSets.utility_bill.files[3].file.name",ctx)==='p4.jpg');

  console.log('\n--- duplicate selection is ignored ---');
  vm.runInContext("docSetAddFiles('utility_bill',[{name:'p1.jpg',size:10,lastModified:1}]);",ctx);
  check('the same file picked twice is not double-added',
    vm.runInContext("DocSets.utility_bill.files.length",ctx)===4);
  console.log('\n--- but same NAME with different content IS kept ---');
  vm.runInContext("docSetAddFiles('utility_bill',[{name:'p1.jpg',size:999,lastModified:9}]);",ctx);
  check('two different files sharing a filename both kept',
    vm.runInContext("DocSets.utility_bill.files.length",ctx)===5);

  console.log('\n--- removing an individual file ---');
  const key=vm.runInContext("DocSets.utility_bill.files[1].key",ctx);
  vm.runInContext(`docSetRemove('utility_bill', ${JSON.stringify(key)});`,ctx);
  check('one file removed',
    vm.runInContext("DocSets.utility_bill.files.length",ctx)===4);
  check('the right one was removed',
    vm.runInContext("DocSets.utility_bill.files.map(x=>x.file.name).indexOf('p2.jpg')",ctx)===-1);
  check('the rest keep their order',
    vm.runInContext("DocSets.utility_bill.files.map(x=>x.file.name).slice(0,2).join(',')",ctx)==='p1.jpg,p3.jpg');

  console.log('\n--- upload sends the whole set ---');
  sent.length=0;
  await vm.runInContext("uploadDocumentSet('utility_bill');",ctx);
  await new Promise(r=>setImmediate(r));
  const req=sent.find(x=>x.url.includes('/document-sets'));
  check('posts to the document-set endpoint', !!req);
  const parts=req&&req.body&&req.body.parts||[];
  check('every file is included',
    parts.filter(p=>p[0]==='files').length===4);
  check('category is sent', parts.some(p=>p[0]==='category'&&p[1]==='utility_bill'));
  check('set id is remembered for later additions',
    vm.runInContext("DocSets.utility_bill.id",ctx)===77);

  console.log('\n--- a second upload APPENDS to the same set ---');
  sent.length=0;
  await vm.runInContext("uploadDocumentSet('utility_bill');",ctx);
  await new Promise(r=>setImmediate(r));
  const req2=sent.find(x=>x.url.includes('/document-sets'));
  check('the existing set id is sent',
    (req2.body.parts||[]).some(p=>p[0]==='document_set_id'&&String(p[1])==='77'));

  console.log('\n--- LMI uses the SAME mechanism, independently ---');
  vm.runInContext("docSetReset('lmi_document');",ctx);
  vm.runInContext("docSetAddFiles('lmi_document',[{name:'front.jpg',size:1,lastModified:1},{name:'back.jpg',size:2,lastModified:2}]);",ctx);
  check('LMI tracks its own two files',
    vm.runInContext("DocSets.lmi_document.files.length",ctx)===2);
  check('the bill set is unaffected',
    vm.runInContext("DocSets.utility_bill.files.length",ctx)===4);
  check('the two categories have separate ids',
    vm.runInContext("DocSets.lmi_document.id",ctx)===null);

  console.log('\n--- prefill scope is stated honestly ---');
  vm.runInContext("docSetReset('utility_bill');",ctx);
  vm.runInContext("docSetAddFiles('utility_bill',[{name:'p1.jpg',size:1,lastModified:1}]);",ctx);
  vm.runInContext("renderPrefillScope('utility_bill');",ctx);
  const one=vm.runInContext("document.getElementById('bill-prefill-scope')",ctx);
  check('single file: no scope warning shown', !one || one.style.display==='none');
  vm.runInContext("docSetAddFiles('utility_bill',[{name:'p2.jpg',size:2,lastModified:2},{name:'p3.jpg',size:3,lastModified:3}]);",ctx);
  vm.runInContext("renderPrefillScope('utility_bill');",ctx);
  const many=vm.runInContext("document.getElementById('bill-prefill-scope')",ctx);
  check('multi-file: the rep IS warned', many.style.display==='block');
  check('  ...it names the file that was read', many.textContent.includes('p1.jpg'));
  check('  ...it states how many are attached', many.textContent.includes('All 3 files'));
  check('  ...it tells the rep to check every field',
    /check every field/i.test(many.textContent));
  check('  ...it does not claim all pages were read',
    !/read all|all pages were read/i.test(many.textContent));

  console.log('\n--- the file list does not collide with the legacy chip ---');
  check('DocSets renders into its own element',
    /bill-fileset/.test(src) && /lmi-fileset/.test(src));
  check('legacy single-file chip element is separate',
    src.includes("getElementById('bill-file-chip')"));
  check('both elements exist in the markup',
    html.includes('id="bill-fileset"') && html.includes('id="bill-file-chip"'));

  console.log('\n--- upload handlers track every selected file ---');
  check('handleBillUpload adds ALL files to the set',
    /handleBillUpload[\s\S]{0,400}docSetAddFiles\('utility_bill', files\)/.test(src));
  check('handleLmiUpload adds ALL files to the set',
    /handleLmiUpload[\s\S]{0,400}docSetAddFiles\('lmi_document', files\)/.test(src));

  console.log('\n--- NO ZIP lookup code present ---');
  for(const gone of ['attachZipUtilityLookup','runZipUtilityLookup','zipLookupState',
                     'utilities/lookup','DALTON_ZIP_UTILITY_LOOKUP']){
    check(`'${gone}' is absent from app.js`, !src.includes(gone));
  }

  console.log('\n--- filenames are clickable links ---');
  vm.runInContext("docSetReset('utility_bill');",ctx);
  vm.runInContext("currentDraft={enrollment_id:5};",ctx);
  vm.runInContext("docSetAddFiles('utility_bill',[{name:'a.png',size:1,lastModified:1},{name:'b.pdf',size:2,lastModified:2}]);",ctx);
  let listHtml=vm.runInContext("document.getElementById('bill-fileset').innerHTML",ctx);
  check('pending files render as links', (listHtml.match(/docset-link/g)||[]).length===2);
  check('  ...pending files use the local-open path', listHtml.includes('openPendingDocument'));
  check('  ...names are still shown', listHtml.includes('a.png')&&listHtml.includes('b.pdf'));

  // Simulate a completed upload assigning server document ids.
  vm.runInContext("DocSets.utility_bill.files[0].documentId=11;DocSets.utility_bill.files[1].documentId=12;renderDocSetList('utility_bill');",ctx);
  listHtml=vm.runInContext("document.getElementById('bill-fileset').innerHTML",ctx);
  check('stored files link to the authorized view route',
    listHtml.includes('/api/enrollments/5/documents/11/view')
    && listHtml.includes('/api/enrollments/5/documents/12/view'));
  check('  ...each filename maps to its OWN document id',
    listHtml.indexOf('documents/11/view') < listHtml.indexOf('documents/12/view'));
  check('  ...opens in a NEW TAB', (listHtml.match(/target="_blank"/g)||[]).length===2);
  check('  ...with rel=noopener', listHtml.includes('rel="noopener"'));
  check('  ...no custom preview modal is used',
    !listHtml.includes('agr-overlay') && !/preview-modal/.test(listHtml));
  check('  ...remove control is still separate',
    (listHtml.match(/docset-remove/g)||[]).length===2);
  check('  ...the link is not the remove control',
    !/docset-link[^>]*docSetRemove/.test(listHtml));

  console.log('\n--- viewing is read-only ---');
  const beforeLen=vm.runInContext("DocSets.utility_bill.files.length",ctx);
  const beforeOrder=vm.runInContext("DocSets.utility_bill.files.map(x=>x.file.name).join(',')",ctx);
  await vm.runInContext("openStoredDocument(null,'/api/enrollments/5/documents/11/view');",ctx);
  await new Promise(r=>setImmediate(r));
  check('opening does not change the file count',
    vm.runInContext("DocSets.utility_bill.files.length",ctx)===beforeLen);
  check('opening does not change page order',
    vm.runInContext("DocSets.utility_bill.files.map(x=>x.file.name).join(',')",ctx)===beforeOrder);
  check('the view request is authenticated',
    sent.some(x=>x.url.includes('/documents/11/view')));

  console.log('\n--- reset clears a set ---');
  vm.runInContext("docSetReset('utility_bill');",ctx);
  check('files cleared', vm.runInContext("DocSets.utility_bill.files.length",ctx)===0);
  check('set id cleared', vm.runInContext("DocSets.utility_bill.id",ctx)===null);
  check('the list is emptied', vm.runInContext("document.getElementById('bill-fileset').innerHTML",ctx)==='');

  const f=R.filter(r=>!r.ok);
  console.log('\n'+'='.repeat(72));
  console.log(`${R.length-f.length} passed, ${f.length} failed`);
  console.log('='.repeat(72));
  if(f.length){f.forEach(x=>console.log('  FAILED: '+x.l));process.exit(1);}
  process.exit(0);
})();

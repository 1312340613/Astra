// Opt in: ASTRA_BROWSER_UPLOAD_E2E=1 ASTRA_BROWSER_EXECUTABLE=<Chromium> NODE_PATH=<playwright modules> node --test ...
const {test}=require('node:test');
const assert=require('node:assert/strict');
const path=require('node:path');
const crypto=require('node:crypto');

test('real Chromium file transactions preserve bytes, identity, events and no-submit boundary',
 {skip:process.env.ASTRA_BROWSER_UPLOAD_E2E!=='1'}, async()=>{
  const {chromium}=require('playwright');
  const browser=await chromium.launch({headless:true,executablePath:process.env.ASTRA_BROWSER_EXECUTABLE});
  try {
    const page=await browser.newPage();
    await page.clock.install();
    await page.route('http://upload.test/**',r=>r.fulfill({contentType:'text/html',body:`<form>
      <label>Files<input id=single type=file accept=".bin"></label>
      <div hidden><input id=hidden type=file multiple></div>
      <input id=disabled type=file disabled><input id=dir type=file webkitdirectory>
      <iframe srcdoc="<input id=child type=file>"></iframe><button>Submit</button></form>`}));
    await page.goto('http://upload.test/');
    await page.addScriptTag({path:path.join(__dirname,'../browser-control-extension/file-upload.js')});
    await page.addScriptTag({path:path.join(__dirname,'../browser-control-extension/page.js')});
    await page.evaluate(()=>{
      window.events=[];window.submits=0;
      document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submits++};
      document.addEventListener('input',e=>window.events.push([e.type,e.target.id]));
      document.addEventListener('change',e=>window.events.push([e.type,e.target.id]));
      HTMLElement.prototype.focus=()=>{throw Error('Upload must not call focus')};
      HTMLElement.prototype.click=()=>{throw Error('Upload must not call click')};
      HTMLInputElement.prototype.showPicker=()=>{throw Error('No native file picker')};
    });
    const call=(op,args={})=>page.evaluate(([op,args])=>window.__astraBrowserPage(op,args),[op,args]);
    const snapshot=()=>call('snapshot',{scope:'form',role_filter:'file',include_text:false});
    async function prepare(id, files) {
      const s=await snapshot(), el=s.elements.find(e=>e.id===id);
      assert.ok(el,`file ref ${id} must be discoverable: ${JSON.stringify(s)}`);
      return call('upload_prepare',{ref:el.ref,frame_ref:el.frameRef,files});
    }
    const data=crypto.randomBytes(1300017), meta={name:'test.bin',size:data.length,type:'application/octet-stream'};
    let receipt=await prepare('hidden',[meta]);
    assert.equal(receipt.status,'prepared');
    for(let offset=0;offset<data.length;offset+=256*1024) {
      const r=await call('upload_chunk',{transferId:receipt.transferId,index:0,offset,data:data.subarray(offset,offset+256*1024).toString('base64')});
      assert.equal(r.status,'buffered');
    }
    let result=await call('upload_commit',{transferId:receipt.transferId});
    assert.equal(result.status,'verified');assert.deepEqual(result.files,[meta]);
    const actual=await page.evaluate(async()=>Array.from(new Uint8Array(await document.querySelector('#hidden').files[0].arrayBuffer())));
    assert.equal(crypto.createHash('sha256').update(Buffer.from(actual)).digest('hex'),crypto.createHash('sha256').update(data).digest('hex'));
    assert.equal((await call('upload_commit',{transferId:receipt.transferId})).dispatch_state,'not_dispatched');
    assert.deepEqual(await page.evaluate(()=>window.events),[['input','hidden'],['change','hidden']]);
    assert.equal(await page.evaluate(()=>window.submits),0);
    // Canvas-style reparenting retires the action ref, not the exact input's
    // identity. Readback of that retained object can still prove selection.
    await page.evaluate(()=>document.querySelector('#hidden').addEventListener('change',e=>{
      const row=document.createElement('div');document.querySelector('form').append(row);row.append(e.target);
    },{once:true}));
    receipt=await prepare('hidden',[{name:'moved.bin',size:0,type:''}]);
    assert.equal((await call('upload_commit',{transferId:receipt.transferId})).status,'verified');
    assert.equal((await prepare('single',[meta,meta])).dispatch_state,'not_dispatched');
    assert.equal((await prepare('disabled',[meta])).dispatch_state,'not_dispatched');
    assert.equal((await prepare('dir',[meta])).status,'unsupported_operation');
    assert.equal((await prepare('single',[{...meta,name:'wrong.txt'}])).dispatch_state,'not_dispatched');
    for(const id of ['single','hidden','child']) {
      const files=id==='hidden'?[{name:'a',size:0,type:''},{name:'b',size:0,type:''}]:[{name:'empty.bin',size:0,type:''}];
      receipt=await prepare(id,files);
      result=await call('upload_commit',{transferId:receipt.transferId});
      assert.equal(result.status,'verified');assert.deepEqual(result.files,files);
      receipt=await prepare(id,[]);
      assert.deepEqual((await call('upload_commit',{transferId:receipt.transferId})).files,[]);
    }
    // A replacement, explicit revocation, resnapshot, or navigation cannot reuse buffered authority.
    for(const mutation of ['replace','invalidate','snapshot','navigation','detach-frame']) {
      const id=mutation==='detach-frame'?'child':'hidden';
      receipt=await prepare(id,[]);
      if(mutation==='replace') await page.evaluate(()=>{document.querySelector('#hidden').outerHTML='<input id=hidden type=file multiple hidden>'});
      if(mutation==='invalidate') await call('invalidate');
      if(mutation==='snapshot') await snapshot();
      if(mutation==='navigation') await page.evaluate(()=>history.pushState({},'','/changed'));
      if(mutation==='detach-frame') await page.evaluate(()=>document.querySelector('iframe').remove());
      assert.equal((await call('upload_commit',{transferId:receipt.transferId})).dispatch_state,'not_dispatched');
    }
    receipt=await prepare('hidden',[{name:'partial.bin',size:2,type:''}]);
    assert.equal((await call('upload_chunk',{transferId:receipt.transferId,index:0,offset:1,data:'YQ=='})).dispatch_state,'not_dispatched');
    assert.equal((await call('upload_commit',{transferId:receipt.transferId})).dispatch_state,'not_dispatched');
    // Browser timers release idle buffers and impose a total lifetime even
    // when chunks keep arriving before the idle deadline.
    receipt=await prepare('hidden',[]);
    await page.clock.fastForward(60001);
    assert.equal((await call('upload_commit',{transferId:receipt.transferId})).dispatch_state,'not_dispatched');
    receipt=await prepare('hidden',[{name:'timed.bin',size:6,type:''}]);
    for(let offset=0;offset<5;offset++) {
      await page.clock.fastForward(50000);
      assert.equal((await call('upload_chunk',{transferId:receipt.transferId,index:0,offset,data:'YQ=='})).status,'buffered');
    }
    await page.clock.fastForward(50001);
    assert.equal((await call('upload_chunk',{transferId:receipt.transferId,index:0,offset:5,data:'YQ=='})).dispatch_state,'not_dispatched');
    const verifiedWithoutSnapshot=await page.evaluate(async()=>{
      const el=document.querySelector('#hidden');
      const engine=window.__astraCreateFileUpload({resolve:()=>({el,frame:{doc:document}}),
        retained:t=>t.el===el && el.isConnected,describe:()=>({id:el.id}),
        nextTask:()=>Promise.resolve(),after:()=>{throw Error('private page diagnostics')}});
      const receipt=await engine.run('upload_prepare',{ref:'local-fixture',files:[]});
      return engine.run('upload_commit',{transferId:receipt.transferId});
    });
    assert.equal(verifiedWithoutSnapshot.status,'verified');
    assert.equal(verifiedWithoutSnapshot.observation_status,'unavailable');
    assert.ok(!JSON.stringify(verifiedWithoutSnapshot).includes('private page diagnostics'));
    // Reparenting readback must never keep authority after actual revocation.
    await page.evaluate(()=>document.querySelector('#hidden').addEventListener('change',()=>window.__astraBrowserPage('invalidate'),{once:true}));
    receipt=await prepare('hidden',[]);
    result=await call('upload_commit',{transferId:receipt.transferId});
    assert.equal(result.status,'unknown_outcome');assert.equal(result.phase,'target_readback');
    // Page event replacement happens after selection, so reporting uncertainty is mandatory.
    await page.evaluate(()=>document.querySelector('#hidden').addEventListener('change',e=>e.target.remove()));
    receipt=await prepare('hidden',[]);
    assert.equal((await call('upload_commit',{transferId:receipt.transferId})).status,'unknown_outcome');
    assert.equal(await page.evaluate(()=>window.submits),0);
  } finally {await browser.close();}
});

test('clear readback verifies a rebuilt logical field without granting another write',
 {skip:process.env.ASTRA_BROWSER_UPLOAD_E2E!=='1'}, async t=>{
  const {chromium}=require('playwright');
  const browser=await chromium.launch({headless:true,executablePath:process.env.ASTRA_BROWSER_EXECUTABLE});
  try {
    const page=await browser.newPage();
    await page.route('http://clear.test/**',r=>r.fulfill({contentType:'text/html',body:`
      <form id=upload><input id=slot name=attachment type=file accept=".bin"><button>Submit</button></form>
      <form id=other></form>`}));
    const call=(op,args={})=>page.evaluate(([op,args])=>window.__astraBrowserPage(op,args),[op,args]);
    const cases={replace:'verified',microtask:'verified',moved:'verified',
      duplicate:'unknown_outcome',name:'unknown_outcome',form:'unknown_outcome',
      accept:'unknown_outcome',multiple:'unknown_outcome',type:'unknown_outcome',
      noId:'unknown_outcome',ambiguousBefore:'unknown_outcome',
      revoked:'unknown_outcome',navigation:'unknown_outcome',wholeForm:'unknown_outcome',
      removed:'unknown_outcome',selection:'unknown_outcome',nonempty:'verification_failed'};
    for(const [kind,status] of Object.entries(cases)) await t.test(kind,async()=>{
      await page.goto('http://clear.test/');
      await page.addScriptTag({path:path.join(__dirname,'../browser-control-extension/file-upload.js')});
      await page.addScriptTag({path:path.join(__dirname,'../browser-control-extension/page.js')});
      await page.evaluate(kind=>{
        const input=document.querySelector('#slot'), dt=new DataTransfer();
        dt.items.add(new File(['initial'],'before.bin',{type:'application/octet-stream'}));input.files=dt.files;
        if(kind==='noId') input.removeAttribute('id');
        if(kind==='ambiguousBefore') input.after(input.cloneNode());
        window.events=[];window.submits=0;
        document.addEventListener('input',e=>window.events.push(e.type));
        document.addEventListener('change',e=>window.events.push(e.type));
        document.querySelector('#upload').onsubmit=e=>{e.preventDefault();window.submits++};
        input.addEventListener('change',()=>{
          const replace=()=>{
            const replacement=input.cloneNode();
            if(kind==='moved') {const row=document.createElement('div');input.after(row);row.append(input);return;}
            if(kind==='removed') {input.remove();return;}
            input.replaceWith(replacement);
            if(kind==='duplicate') replacement.after(replacement.cloneNode());
            if(kind==='name') replacement.name='another_field';
            if(kind==='form') document.querySelector('#other').append(replacement);
            if(kind==='accept') replacement.accept='.txt';
            if(kind==='multiple') replacement.multiple=true;
            if(kind==='type') replacement.type='text';
            if(kind==='wholeForm') {const form=replacement.form;form.replaceWith(form.cloneNode(true));}
            if(kind==='revoked') window.__astraBrowserPage('invalidate');
            if(kind==='navigation') history.pushState({},'','/elsewhere');
            if(kind==='nonempty') {const unexpected=new DataTransfer();unexpected.items.add(new File([],'other.bin'));replacement.files=unexpected.files;}
          };
          if(kind==='microtask') queueMicrotask(replace);else replace();
        },{once:true});
        HTMLElement.prototype.focus=()=>{throw Error('Clear must not focus')};
        HTMLElement.prototype.click=()=>{throw Error('Clear must not click')};
        HTMLInputElement.prototype.showPicker=()=>{throw Error('No native picker')};
      },kind);
      const snapshot=await call('snapshot',{scope:'form',role_filter:'file',include_text:false});
      const input=snapshot.elements[0], files=kind==='selection'?[{name:'new.bin',size:0,type:''}]:[];
      const prepared=await call('upload_prepare',{ref:input.ref,frame_ref:input.frameRef,files});
      assert.equal(prepared.status,'prepared');
      const result=await call('upload_commit',{transferId:prepared.transferId});
      assert.equal(result.status,status,JSON.stringify(result));
      if(status==='verified') {
        assert.equal(result.verified,true);assert.deepEqual(result.files,[]);
        assert.equal(result.verification_source,kind==='moved'?'retained_input':'replacement_input');
      } else assert.equal(result.verified,false);
      assert.deepEqual(await page.evaluate(()=>window.events),['input','change']);
      assert.equal(await page.evaluate(()=>window.submits),0);
      assert.equal((await call('upload_commit',{transferId:prepared.transferId})).dispatch_state,'not_dispatched');
    });
  } finally {await browser.close();}
});

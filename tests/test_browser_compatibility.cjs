// NODE_PATH=/tmp/astra-cu-test-runtime/node_modules node --test tests/test_browser_compatibility.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');

function form(count=10) {
  return '<nav>'+Array.from({length:80},(_,i)=>`<button>Navigation ${i}</button>`).join('')+'</nav>'+
    Array.from({length:count},(_,i)=>`<fieldset><legend>Question ${i+1}</legend>
      <p>Choose the requested value for question ${i+1}; preserve this entire prompt ${'word '.repeat(35)}.</p>
      ${['First','Second','Third'].map((label,j)=>`<label><input type="radio" name="q${i}" id="q${i}-${j}">${label}</label>`).join('')}
      </fieldset>`).join('')+'<button id="submit">Submit</button>';
}
function fixture(html=form()) {
  const dom = new JSDOM(html, {url:'https://example.org/form',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window, calls=[];
  w.Element.prototype.getClientRects=function(){return [{width:100,height:20}];};
  const source=fs.readFileSync(path.join(__dirname,'../browser-control-extension/page.js'),'utf8');
  const api={tabs:{get:async()=>({id:1,url:w.location.href,title:'Form'})},
    permissions:{contains:async()=>true},scripting:{executeScript:async options=>{
      if(options.files){w.eval(source);return [];}
      calls.push(options.args[0]);
      return [{result:await w.eval(`(${options.func.toString()})(...${JSON.stringify(options.args)})`)}];
    }}};
  w.eval(source);
  return {w,api,calls};
}
test('compact form retains long prompts, repeated choices and usable group refs',async()=>{
  const f=fixture();try {
    const s=await f.w.__astraBrowserPage('snapshot',{scope:'form',include_text:false});
    assert.equal(s.elements.filter(e=>e.role==='radio').length,30);
    assert.equal(s.groups.length,10);assert.equal(s.nextOffset,null);
    assert.ok(s.groups.every(g=>g.name.length>200 && !g.nameTruncated));
    assert.ok(s.groups.every(g=>!g.name.includes('First')));
    assert.ok(!JSON.stringify(s).includes('Navigation'));
    assert.ok(JSON.stringify(s).length<=10000);
    for(const g of s.groups) {
      assert.equal(s.elements.filter(e=>e.group===g.id).length,3);
      assert.ok((await f.w.__astraBrowserPage('read',{ref:g.ref})).value.includes(g.name.split(' ').slice(0,2).join(' ')));
    }
    const goals=s.elements.filter(e=>e.role==='radio' && e.name==='Second').map(e=>({ref:e.ref,checked:true}));
    const result=await f.w.__astraBrowserPage('check',{checks:goals});
    assert.equal(result.verified,true);assert.equal(result.completed,10);
    assert.equal(f.w.document.querySelectorAll('input:checked').length,10);
    assert.equal(result.after.scope,'form');assert.ok(JSON.stringify(result).length<=10000);
  } finally {f.w.close();}
});
test('large forms paginate all controls without dangling group references',async()=>{
  const f=fixture(form(80));try {
    const ids=new Set();let offset=0,pages=0;
    do {
      const s=await f.w.__astraBrowserPage('snapshot',{scope:'form',include_text:false,offset});
      assert.ok(s.elements.length>0);assert.ok(JSON.stringify(s).length<=10000);
      for(const el of s.elements) {
        assert.ok(!ids.has(el.id));ids.add(el.id);
        if(el.group!==undefined) assert.ok(s.groups.some(g=>g.id===el.group));
      }
      assert.ok(s.nextOffset===null || s.nextOffset>offset);
      offset=s.nextOffset;pages++;
    } while(offset!==null && pages<100);
    assert.equal(offset,null);assert.equal(ids.size,241);
  } finally {f.w.close();}
});
test('short accessible group captions do not hide the visible question prompt',async()=>{
  const f=fixture('<fieldset aria-label="Question 1"><legend>Question 1</legend><p>Which direction should the robot move?</p><label><input type="radio">Left</label></fieldset>');
  try {
    const s=await f.w.__astraBrowserPage('snapshot',{scope:'form',include_text:false});
    assert.equal(s.groups[0].name,'Question 1 Which direction should the robot move?');
    assert.equal(s.elements[0].name,'Left');
  } finally {f.w.close();}
});
test('cached 0.3.0 controller rejects native check but supports verified click goals',async()=>{
  const {createControl}=await import('./fixtures/browser-control-0.3.0.mjs');
  const f=fixture();try {
    const c=createControl(f.api);c.enable();await c.grant(1);let seq=0;
    const request=(operation,args={})=>c.handle({id:String(++seq),tabId:1,operation,args:{expectedOrigin:'https://example.org',...args}});
    const s=(await request('snapshot',{scope:'form',include_text:false})).result;
    const goals=s.elements.filter(e=>e.name==='Third').map(e=>({ref:e.ref,checked:true}));
    const unsupported=await request('check',{checks:goals});
    assert.equal(unsupported.error,'Unsupported operation or arguments');
    assert.equal(f.w.document.querySelectorAll('input:checked').length,0);
    f.calls.length=0;
    const result=await request('click',{choiceGoals:goals});
    assert.equal(result.result.verified,true);assert.equal(result.result.completed,10);
    assert.deepEqual(f.calls,['click']);assert.equal(f.w.document.querySelectorAll('input:checked').length,10);
    await request('handoff');
    const blocked=await request('click',{choiceGoals:[{selector:'#q0-0',checked:true}]});
    assert.equal(blocked.ok,false);assert.equal(f.w.document.querySelector('#q0-0').checked,false);
  } finally {f.w.close();}
});
test('compatibility goals skip satisfied checkboxes and support false without toggling twice',async()=>{
  const f=fixture('<label><input id="yes" type="checkbox" checked>Yes</label><label><input id="no" type="checkbox" checked>No</label>');
  try {
    let clicks=0;f.w.document.addEventListener('click',()=>clicks++);
    const args={choiceGoals:[{selector:'#yes',checked:true},{selector:'#no',checked:false}]};
    const first=await f.w.__astraBrowserPage('click',args);
    const second=await f.w.__astraBrowserPage('click',args);
    assert.equal(first.verified,true);assert.equal(second.verified,true);
    assert.equal(first.clickCount,1);assert.equal(second.clickCount,0);assert.equal(clicks,1);
    assert.equal(f.w.document.querySelector('#no').checked,false);
  } finally {f.w.close();}
});
test('compatibility batch preflights conflicting radios and never repeats unknown writes',async()=>{
  const f=fixture();try {
    let clicks=0;f.w.document.addEventListener('click',()=>clicks++);
    const conflict=await f.w.__astraBrowserPage('click',{choiceGoals:[{selector:'#q0-0',checked:true},{selector:'#q0-1',checked:true}]});
    assert.equal(conflict.dispatch_state,'not_dispatched');assert.equal(clicks,0);
    f.w.document.querySelector('#q0-0').addEventListener('click',e=>e.target.remove());
    const changed=await f.w.__astraBrowserPage('click',{choiceGoals:[{selector:'#q0-0',checked:true},{selector:'#q1-0',checked:true}]});
    assert.equal(changed.status,'unknown_outcome');assert.equal(changed.clickCount,1);
    assert.equal(f.w.document.querySelector('#q1-0').checked,false);
  } finally {f.w.close();}
});
test('form refs distinguish identical choices in separate frames and omit hidden controls',async()=>{
  const f=fixture('<iframe id="left"></iframe><iframe id="right"></iframe>');try {
    for(const id of ['left','right']) {
      const doc=f.w.document.querySelector('#'+id).contentDocument;
      doc.body.innerHTML=`<fieldset><legend>${id} question</legend><label><input id="yes" type="checkbox">True</label>
        <input type="password" value="do-not-expose"><input type="hidden" value="hidden-secret">
        <label style="display:none"><input type="checkbox">Invisible choice</label></fieldset>`;
      doc.defaultView.Element.prototype.getClientRects=function(){return [{width:100,height:20}];};
    }
    const s=await f.w.__astraBrowserPage('snapshot',{scope:'form',include_text:false});
    assert.equal(s.elements.length,2);assert.equal(s.groups.length,2);
    assert.equal(new Set(s.elements.map(e=>e.frameRef)).size,2);
    assert.ok(!JSON.stringify(s).includes('do-not-expose'));assert.ok(!JSON.stringify(s).includes('Invisible choice'));
    const right=s.groups.find(g=>g.name==='right question');
    const target=s.elements.find(e=>e.group===right.id);
    const result=await f.w.__astraBrowserPage('click',{choiceGoals:[{ref:target.ref,checked:true}]});
    assert.equal(result.verified,true);
    assert.equal(f.w.document.querySelector('#right').contentDocument.querySelector('#yes').checked,true);
    assert.equal(f.w.document.querySelector('#left').contentDocument.querySelector('#yes').checked,false);
  } finally {f.w.close();}
});

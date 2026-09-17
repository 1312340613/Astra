/* Private isolated-world buffers; never exposed in snapshots or receipts. */
(() => {
  'use strict';
  globalThis.__astraCreateFileUpload = ({resolve, retained, clearedReplacement, describe, after, nextTask}) => {
    const MAX_FILE=32*1024*1024, MAX_TOTAL=64*1024*1024, CHUNK=256*1024;
    let pending=null, timer=null;
    const fail=(message,status='error')=>Object.assign(new Error(message),{status});
    const metadata=el=>Array.from(el.files || [],file=>({name:file.name,size:file.size,type:file.type}));
    function abort() {
      clearTimeout(timer);timer=null;
      if(pending) pending.buffers.length=0;
      pending=null;
    }
    function arm() {
      clearTimeout(timer);
      timer=setTimeout(abort,Math.max(0,Math.min(60000, pending.started+300000-performance.now())));
    }
    function target(args) {
      if(typeof args.ref!=='string' || !args.ref) throw fail('A fresh file input ref is required','invalid_arguments');
      const t=resolve(args), el=t.el;
      if(el.tagName!=='INPUT' || el.type!=='file') throw fail('Target is not a file input');
      if(el.matches(':disabled') || el.closest('[inert],[aria-disabled="true"]')) throw fail('File input is disabled');
      if(el.webkitdirectory || el.hasAttribute('webkitdirectory')) throw fail('Directory upload is unsupported','unsupported_operation');
      return t;
    }
    function accepts(el, file) {
      const tokens=el.accept.split(',').map(x=>x.trim().toLowerCase()).filter(Boolean);
      return !tokens.length || tokens.some(token=>token.startsWith('.') ? file.name.toLowerCase().endsWith(token) :
        token.endsWith('/*') ? file.type.startsWith(token.slice(0,-1)) : file.type===token);
    }
    function checkFiles(el, files) {
      if(!Array.isArray(files) || files.length>10) throw fail('Provide at most 10 files','invalid_arguments');
      if(!el.multiple && files.length>1) throw fail('File input does not allow multiple files');
      let total=0;
      for(const file of files) {
        if(!file || typeof file.name!=='string' || !file.name || file.name.length>255 || /[/\\\x00-\x1f]/.test(file.name) ||
           typeof file.type!=='string' || file.type.length>127 || (file.type && !/^[\w!#$&^.+-]+\/[\w!#$&^.+-]+$/.test(file.type)) ||
           !Number.isSafeInteger(file.size) || file.size<0 || file.size>MAX_FILE || (total+=file.size)>MAX_TOTAL)
          throw fail('Invalid file metadata or upload size limit exceeded','invalid_arguments');
        if(!accepts(el,file)) throw fail('File does not match the input accept hint; inspect allowed formats');
      }
    }
    function live(args) {
      if(!pending || args.transferId!==pending.id) throw fail('Upload transfer expired or already consumed','stale_snapshot');
      if(performance.now()-pending.touched>=60000 || performance.now()-pending.started>=300000)
        throw fail('Upload transfer expired','stale_snapshot');
      const t=target(pending.args);
      if(t.el!==pending.target.el || t.frame.doc!==pending.target.frame.doc) throw fail('Upload target changed','stale_snapshot');
      checkFiles(t.el,pending.files);
      pending.touched=performance.now();arm();return pending;
    }
    async function run(operation,args) {
      let dispatched=false, phase='prepare';
      try {
        if(operation==='upload_abort') {
          if(pending && args.transferId===pending.id) abort();
          return {status:'aborted',dispatch_state:'not_dispatched'};
        }
        if(operation==='upload_prepare') {
          abort();
          const t=target(args), view=t.el.ownerDocument.defaultView;
          if(typeof view.DataTransfer!=='function' || typeof view.File!=='function')
            throw fail('File upload is unavailable in this document','unsupported_operation');
          checkFiles(t.el,args.files);
          const id=Array.from(crypto.getRandomValues(new Uint8Array(16)),n=>n.toString(16).padStart(2,'0')).join('');
          pending={id,args:{ref:args.ref,frame_ref:args.frame_ref},target:t,files:args.files.map(f=>({...f})),
            buffers:args.files.map(()=>[]),offsets:args.files.map(()=>0),started:performance.now(),touched:performance.now()};
          arm();return {status:'prepared',dispatch_state:'not_dispatched',transferId:id};
        }
        const transfer=live(args);
        if(operation==='upload_chunk') {
          const {index,offset,data}=args;
          const expected=transfer.offsets.findIndex((n,i)=>n<transfer.files[i].size);
          if(!Number.isInteger(index) || index!==expected || !Number.isSafeInteger(offset) || offset!==transfer.offsets[index] ||
             typeof data!=='string' || !data || data.length>4*Math.ceil(CHUNK/3) || data.length%4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(data))
            throw fail('Invalid, duplicate or out-of-order upload chunk','invalid_arguments');
          const raw=atob(data);
          if(!raw.length || raw.length>CHUNK || offset+raw.length>transfer.files[index].size)
            throw fail('Upload chunk exceeds declared file size','invalid_arguments');
          transfer.buffers[index].push(Uint8Array.from(raw,c=>c.charCodeAt(0)));
          transfer.offsets[index]+=raw.length;
          return {status:'buffered',dispatch_state:'not_dispatched'};
        }
        if(operation!=='upload_commit') throw fail('Unsupported upload operation','unsupported_operation');
        if(transfer.offsets.some((size,i)=>size!==transfer.files[i].size)) throw fail('Upload is incomplete');
        const {el}=transfer.target, view=el.ownerDocument.defaultView, dt=new view.DataTransfer();
        for(const [i,file] of transfer.files.entries()) dt.items.add(new view.File(transfer.buffers[i],file.name,{type:file.type}));
        const expected=transfer.files, identity=describe(transfer.target);
        // Consume before dispatch, including event handlers which replace/navigate the target.
        abort();dispatched=true;phase='assign_files';
        Object.getOwnPropertyDescriptor(view.HTMLInputElement.prototype,'files').set.call(el,dt.files);
        phase='input_event';
        el.dispatchEvent(new view.Event('input',{bubbles:true,composed:true}));
        phase='change_event';
        el.dispatchEvent(new view.Event('change',{bubbles:true,composed:true}));
        phase='settle';
        await nextTask();
        // Sites can move the exact input into a selected-file row during change.
        // Its old action ref correctly expires, but this readback sends no more
        // input and can verify the retained element/document/grant identity.
        phase='target_readback';
        let readback=el, verificationSource='retained_input';
        if(!retained(transfer.target) || el.type!=='file') {
          readback=expected.length===0 ? clearedReplacement?.(transfer.target) : null;
          if(!readback) throw fail('File input changed after selection','unknown_outcome');
          verificationSource='replacement_input';
        }
        phase='file_readback';
        const selected=metadata(readback), verified=JSON.stringify(selected)===JSON.stringify(expected);
        const observation={};
        try {observation.after=after();} catch {observation.observation_status='unavailable';}
        return {status:verified?'verified':'verification_failed',dispatch_state:'dispatched',verified,
          files:selected,target:identity,verification_source:verificationSource,repeat_input:false,
          message:'File input read back; server receipt and form submission are separate',...observation};
      } catch(error) {
        abort();
        return {status:dispatched?'unknown_outcome':error.status || 'error',dispatch_state:dispatched?'unknown':'not_dispatched',
          verified:false,repeat_input:false,phase,message:dispatched?'Selection may have executed; observe files before continuing':
            (error.status ? error.message : 'File transfer rejected; inspect target and declared metadata')};
      }
    }
    return {run,abort};
  };
})();

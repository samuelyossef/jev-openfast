const ownerKey = id => `jev.manual.${id}`;
export const manualHeaders = id => ({'X-Manual-Control':sessionStorage.getItem(ownerKey(id)) || ''});
export const ownsManual = (id, manual) => Boolean(id && manual?.owned!==false && sessionStorage.getItem(ownerKey(id)));
export async function manualCommand(name, state, body={}) {
  const id=state?.session_id;
  const response=await fetch(`/api/manual/${name}`,{method:'POST',headers:{'Content-Type':'application/json',
    'X-Demo-Token':document.querySelector('meta[name="demo-token"]').content},
    body:JSON.stringify({session_id:id,owner_token:sessionStorage.getItem(ownerKey(id)),...body})});
  const result=await response.json();
  if(!response.ok)throw Error(result.error || 'Não foi possível atualizar o controle manual.');
  if(result.manual_owner_token)sessionStorage.setItem(ownerKey(id),result.manual_owner_token);
  if(name==='end')sessionStorage.removeItem(ownerKey(id));
  delete result.manual_owner_token;
  return result;
}

export function bindManualInput(host,getState,getFrame,onState,onError,canStart=()=>false) {
  const keyboard=document.createElement('textarea');
  keyboard.className='manual-keyboard';keyboard.setAttribute('aria-label','Teclado da página');
  keyboard.tabIndex=-1;
  keyboard.autocomplete='off';keyboard.spellcheck=false;keyboard.setAttribute('autocapitalize','off');
  host.append(keyboard);
  let queue=Promise.resolve(),blocked=false,pointer=null,move=null,timer=null,composing=false,clickCount=1,lastClick=null;
  let acceptedSequence=0,acceptedOwner=null;
  const listeners=[];
  const listen=(target,name,handler,options)=>{target.addEventListener(name,handler,options);listeners.push(()=>target.removeEventListener(name,handler,options));};
  const active=()=>getState()?.manual?.status==='active' && ownsManual(getState().session_id,getState().manual) && getFrame()?.manual;
  listen(host,'focus',()=>{if(active())keyboard.focus({preventScroll:true});});
  const modifiers=e=>(e.altKey?1:0)|(e.ctrlKey?2:0)|(e.metaKey?4:0)|(e.shiftKey?8:0);
  const send=event=>{
    if(!active()||blocked)return;
    const state=getState(),frame=getFrame(),id=state.session_id;
    const owner=sessionStorage.getItem(ownerKey(id));
    const context=frame?.context;
    if(!context)return;
    queue=queue.then(async()=>{
      if(blocked || !active() || getState().session_id!==id || sessionStorage.getItem(ownerKey(id))!==owner)return;
      if(acceptedOwner!==owner){acceptedOwner=owner;acceptedSequence=0;}
      try {
        const request_id=Math.max(acceptedSequence,getState().manual.sequence)+1;
        const next=await manualCommand('input',getState(),{...event,context,request_id});
        acceptedSequence=next.manual.sequence;
        onState(next);
      }catch(error){blocked=true;onError(error.message);}
    });
  };
  // Between tasks the first click or scroll starts browsing control; its events wait for the first live frame.
  let pending=null;
  const begin=async()=>{
    pending=[];
    try{onState(await manualCommand('start',getState()));}
    catch(error){pending=null;onError(error.message);return;}
    const until=Date.now()+5000;
    while(!(active()&&getFrame()?.context)){
      if(Date.now()>until){pending=null;return;}  // Dropped, never replayed later.
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    const events=pending;pending=null;events.forEach(send);
  };
  const deliver=(event,startNew)=>{
    if(active())send(event);
    else if(pending)pending.push(event);
    else if(startNew&&canStart()){void begin();pending.push(event);}
  };
  const position=e=>{
    const img=host.querySelector('img'),frame=getFrame();
    if(!img||!frame||!(active()||pending||canStart()))return null;
    const r=img.getBoundingClientRect();
    if(!r.width||!r.height)return null;
    if(!pointer&&(e.clientX<r.left||e.clientX>=r.right||e.clientY<r.top||e.clientY>=r.bottom))return null;
    return {x:Math.max(0,Math.min(frame.page.w-0.01,(e.clientX-r.left)*frame.page.w/r.width)),
      y:Math.max(0,Math.min(frame.page.h-0.01,(e.clientY-r.top)*frame.page.h/r.height))};
  };
  const flushMove=()=>{if(timer)clearTimeout(timer);timer=null;if(move){send(move);move=null;}};
  listen(host,'pointerdown',e=>{
    if(e.button!==0||e.target===keyboard)return;
    const p=position(e);if(!p)return;
    e.preventDefault();blocked=false;pointer=e.pointerId;host.setPointerCapture(pointer);keyboard.focus({preventScroll:true});
    clickCount=lastClick && Date.now()-lastClick.time<400 && Math.hypot(p.x-lastClick.x,p.y-lastClick.y)<6 ? 2 : 1;
    lastClick={...p,time:Date.now()};
    deliver({type:'pointer',action:'down',...p,buttons:1,clickCount,modifiers:modifiers(e)},true);
  });
  listen(host,'pointermove',e=>{
    if(pointer!==e.pointerId)return;
    const p=position(e);if(!p)return;
    move={type:'pointer',action:'move',...p,buttons:1,clickCount:0,modifiers:modifiers(e)};
    if(!timer)timer=setTimeout(flushMove,50);
  });
  const release=e=>{
    if(pointer!==e.pointerId)return;
    flushMove();const p=position(e);
    if(p)deliver({type:'pointer',action:'up',...p,buttons:0,clickCount,modifiers:modifiers(e)},false);
    pointer=null;
  };
  listen(host,'pointerup',release);listen(host,'pointercancel',release);
  listen(host,'wheel',e=>{
    const p=position(e);if(!p)return;
    e.preventDefault();deliver({type:'wheel',action:'wheel',...p,
      deltaX:Math.max(-2000,Math.min(2000,e.deltaX)),deltaY:Math.max(-2000,Math.min(2000,e.deltaY)),modifiers:modifiers(e)},true);
  },{passive:false});
  listen(keyboard,'compositionstart',()=>{composing=true;});
  listen(keyboard,'compositionend',e=>{composing=false;if(e.data)send({type:'text',text:e.data});keyboard.value='';});
  listen(keyboard,'input',e=>{
    if(composing||e.inputType==='insertCompositionText')return;
    if(keyboard.value)send({type:'text',text:keyboard.value});keyboard.value='';
  });
  listen(keyboard,'paste',e=>{
    e.preventDefault();const text=e.clipboardData?.getData('text/plain');if(text)send({type:'text',text});keyboard.value='';
  });
  const keys=new Set(['Enter','Tab','Backspace','Delete','Escape','ArrowLeft','ArrowRight','ArrowUp','ArrowDown',
    'Home','End','PageUp','PageDown','Shift','Control','Alt','Meta']);
  for(const action of ['down','up'])listen(keyboard,`key${action}`,e=>{
    if(composing||!active())return;
    if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='v')return;
    if(keys.has(e.key)||((e.ctrlKey||e.metaKey)&&['a','c','x','z','y'].includes(e.key.toLowerCase()))){
      e.preventDefault();send({type:'key',action,key:e.key,modifiers:modifiers(e)});
    }else if((e.ctrlKey||e.metaKey)||e.key.startsWith('F'))e.preventDefault();
  });
  return ()=>{blocked=true;listeners.forEach(remove=>remove());clearTimeout(timer);keyboard.value='';keyboard.remove();};
}

import { useEffect, useRef, useState } from 'react';
import { bindManualInput, manualCommand, ownsManual } from '../../jev_ultrafast/static/manual.js';
import { useI18n } from './i18n';

// The preview accepts clicks and scrolling whenever Jev is not working (or is waiting on a handoff card).
export const canBrowse=(state:any)=>Boolean(state?.manual?.available && state.manual.status==='off' && (
  ['idle','answered','completed','error'].includes(state.chat_status) ||
  (state.chat_status==='paused' && state.messages?.at(-1)?.kind==='handoff')));

export default function ManualPanel({ state, frame, host, onState, onStart, inline }: {
  state:any; frame:any; host:HTMLElement|null; onState:(state:any)=>void; onStart?:()=>void; inline?:boolean;
}) {
  const { t }=useI18n();
  const latest=useRef({state,frame,onState});latest.current={state,frame,onState};
  const [error,setError]=useState(''),[busy,setBusy]=useState(false);
  useEffect(()=>{
    if(!host)return;
    return bindManualInput(host,()=>latest.current.state,()=>latest.current.frame,
      (next:any)=>latest.current.onState(next),setError,()=>canBrowse(latest.current.state));
  },[host,state?.session_id]);
  const manual=state?.manual, owned=ownsManual(state?.session_id,manual);
  const command=async(name:string,body={})=>{
    if(busy)return;setBusy(true);setError('');
    try{onState(await manualCommand(name,state,body));if(name==='start')onStart?.();}
    catch(e){setError((e as Error).message);}finally{setBusy(false);}
  };
  if(!inline||!manual?.available)return null;
  return <div className="manual-panel" role="region" aria-label={t('manualControl')}>
    {manual.status==='off' ? <button disabled={busy} onClick={()=>void command('start')}>{t('takeControl')}</button> :
      manual.status==='requested' ? <span role="status">{t('preparingManual')}</span> : <>
        <span role="status">{owned ? manual.status==='uncertain' ? manual.reason : t('youControl') : t('controlElsewhere')}</span>
        {owned && <>{manual.status==='uncertain' ? <button disabled={busy} onClick={()=>void command('start')}>{t('recoverControl')}</button> : <>
          {manual.can_resume && <button disabled={busy||!manual.input_ready} onClick={()=>void command('end',{resume:true})}>{t('continueJev')}</button>}
          <button disabled={busy} onClick={()=>void command('end',{resume:false})}>{t('exitManual')}</button>
        </>}</>}
      </>}
    {manual.status==='active' && owned && <small>{t('manualHint')}</small>}
    {error && <span role="alert">{error}</span>}
  </div>;
}

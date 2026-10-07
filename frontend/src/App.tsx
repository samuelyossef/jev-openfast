import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { currentLocale, LanguageSegment, translate, useI18n } from './i18n';
import ManualPanel, { canBrowse } from './ManualPanel';
import { manualHeaders, ownsManual } from '../../jev_ultrafast/static/manual.js';

type Message = {
  role: 'user' | 'assistant';
  content: string;
  turn_id?: string;
  kind?: string;
  handoff?: { code: string };
  verification?: { satisfied: boolean; stale?:boolean; evidence: string[]; reason?: string; checked_at?: number; url?: string;
    checks?: { requirement: string; status: 'confirmed' | 'not_met' | 'unknown'; evidence: string[]; reason: string }[] };
};
type Action = { id: string; node: number; label: string; rect?: { x: number; y: number; w: number; h: number } };
type Element = { index: string; label: string; role: string; operations: string[]; value?: string };
type Viewport = { width: number; height: number };
type Decision = { choice?: string; operation?: string; target?: string | null; target_confidence?: number | null };
type Attempt = { step?: number; action: string; execution: string; page_changed?: boolean | null };
type Snapshot = {
  session_id: string | null;
  turn_id?: string | null;
  messages: Message[];
  chat_status: string;
  manual?: { status:string; available:boolean; reason:string; sequence:number; can_resume:boolean; input_ready:boolean };
  user_actions?: { type:string; execution:string; request_id?:number }[];
  pause_requested?: boolean;
  can_recheck?: boolean;
  progress: string;
  storage_error?: string | null;
  viewport?: Viewport | null;
  viewport_error?: string | null;
  openrouter_key_source?: string;
  text_model?: string;
  last_url?: string | null;
  page?: { url: string; title: string; screenshot?: string; w: number; h: number; actions: Action[] } | null;
  approval?: { id: string; description: string; label: string; url: string; operation: string } | null;
  decision?: Decision | null;
  decisions?: Decision[];
  elements?: Element[];
  history?: Attempt[];
  goal?: string;
  started_at?: number | null;
  elapsed_ms?: number;
  preview_revision?: number;
  action_count?: number;
  decision_count?: number;
  timing?: { end_to_end_ms: number; execution_ms: number; approval_wait_ms: number; manual_wait_ms?:number; first_action_ms: number | null;
    stages?: Record<string, { count: number; duration_ms: number }> };
};
type PreviewCapture = { session_id: string; revision: number; manual?:boolean; context?:unknown; page: NonNullable<Snapshot['page']>; elements: Element[] };
type PreviewResponse = { session_id: string; revision: number; capture: Omit<PreviewCapture, 'session_id'> | null; error: string | null };
type Conversation = { id: string; title: string; updated_at: number; phase: string; last_url: string | null; archived: boolean };

const token = document.querySelector<HTMLMetaElement>('meta[name="demo-token"]')?.content || '';
const activePhases = new Set(['thinking', 'running', 'verifying']);

class HttpError extends Error {
  status: number;
  constructor(message: string, status: number) { super(message); this.status = status; }
}

async function readJson<T>(url: string, headers?:HeadersInit): Promise<T> {
  const response = await fetch(url, { cache: 'no-store', headers });
  if (!response.ok) throw new HttpError(translate(currentLocale(), 'queryError'), response.status);
  return response.json();
}

async function post(name: string, body: Record<string, unknown>): Promise<Snapshot> {
  const response = await fetch(`/api/${name}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Demo-Token': token, ...manualHeaders(body.session_id) },
    body: JSON.stringify({ locale: currentLocale(), ...body }),
  });
  if (response.status === 403) {
    // The server was restarted (new page token) or the page was opened from another address.
    // Reload once to pick up the current token; never loop.
    let last = 0;
    try { last = Number(sessionStorage.getItem('jev.reloadedFor403') || 0); } catch { /* storage unavailable */ }
    if (Date.now() - last > 10000) {
      try { sessionStorage.setItem('jev.reloadedFor403', String(Date.now())); } catch { /* storage unavailable */ }
      window.location.reload();
    }
    throw new Error(translate(currentLocale(), 'staleServer'));
  }
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || translate(currentLocale(), 'operationError'));
  return result;
}

function Icon({ name }: { name: 'panel' | 'plus' | 'search' | 'clock' | 'settings' | 'send' | 'trash' | 'sun' | 'moon' | 'more' | 'copy' | 'target' }) {
  const paths = {
    panel: <><rect x="2" y="3" width="12" height="10" rx="1.5" /><path d="M6 3v10" /></>,
    plus: <path d="M8 3v10M3 8h10" />,
    search: <><circle cx="7" cy="7" r="4.5" /><path d="M13.5 13.5L10.5 10.5" /></>,
    clock: <><circle cx="8" cy="8" r="6" /><path d="M8 4.5V8l2.5 1.5" /></>,
    settings: <><circle cx="8" cy="8" r="2" /><path d="M13.4 9.5l-.9-.5v-2l.9-.5-1-1.7-1 .4-1.7-1L9.5 3.1h-3L6.3 4.2l-1.7 1-1-.4-1 1.7.9.5v2l-.9.5 1 1.7 1-.4 1.7 1 .2 1.1h3l.2-1.1 1.7-1 1 .4z" /></>,
    send: <path d="M14 2L7.5 8.5M14 2l-5 12-2-5.5L2 6.5 14 2z" />,
    trash: <><path d="M3 4h10M6 4V2.5h4V4M5 4l.5 9.5h5L11 4" /></>,
    sun: <><circle cx="8" cy="8" r="3" /><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.5 1.5M11.5 11.5L13 13M13 3l-1.5 1.5M4.5 11.5L3 13" /></>,
    moon: <path d="M13 12.7A6 6 0 0 1 3.3 3 6 6 0 1 0 13 12.7z" />,
    more: <><circle cx="3" cy="8" r=".8" /><circle cx="8" cy="8" r=".8" /><circle cx="13" cy="8" r=".8" /></>,
    copy: <><rect x="8" y="8" width="8" height="8" rx="1.5" /><path d="M4 4h8v8H4z" /><path d="M8 8v8" /></>,
    target: <><circle cx="8" cy="8" r="5" /><circle cx="8" cy="8" r="2" /><path d="M8 1v2M8 13v2M1 8h2M13 8h2" /></>,
  };
  return <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

function fitTextarea(element: HTMLTextAreaElement) {
  element.style.height = 'auto';
  element.style.height = `${Math.min(240, Math.max(64, element.scrollHeight))}px`;
  element.style.overflowY = element.scrollHeight > 240 ? 'auto' : 'hidden';
}

function Composer({ value, onChange, onSend, disabled, overlays, onOverlaysChange, pauseVisible, paused, pausing, controlDisabled, onPause, onResume }: {
  value: string; onChange: (value: string) => void; onSend: () => void; disabled: boolean;
  overlays: boolean; onOverlaysChange: (value: boolean) => void;
  pauseVisible: boolean; paused: boolean; pausing: boolean; controlDisabled: boolean;
  onPause: () => void; onResume: () => void;
}) {
  const { t } = useI18n();
  const textarea = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { if (textarea.current) fitTextarea(textarea.current); }, [value]);
  useEffect(() => {
    const element = textarea.current;
    if (!element) return;
    let width = element.clientWidth;
    const observer = new ResizeObserver(() => {
      if (element.clientWidth !== width) { width = element.clientWidth; fitTextarea(element); }
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return <div className="composer-wrap">
    <div className="composer-block">
      <form className="composer" onSubmit={(event) => { event.preventDefault(); if (!disabled && value.trim()) onSend(); }}>
        <textarea ref={textarea} value={value} maxLength={2000} rows={2} onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && !disabled) {
              event.preventDefault(); if (value.trim()) onSend();
            }
          }} placeholder={t('askPlaceholder')} aria-label={t('yourMessage')} aria-describedby="composer-hint" />
        <div className="comp-foot"><button className="overlay-toggle" type="button" aria-label={t('targets')} title={t('targets')} aria-pressed={overlays} onClick={() => onOverlaysChange(!overlays)}><Icon name="target" /></button>
          <span className="comp-spacer" />
          {value.length >= 1800 && <span className="comp-count" aria-live="polite">{value.length}/2000</span>}
          {pauseVisible && <button className="pause-button" type="button" onClick={onPause} disabled={pausing || controlDisabled}>{pausing ? t('pausing') : t('pause')}</button>}
          {paused && <button className="pause-button" type="button" onClick={onResume} disabled={controlDisabled}>{t('resume')}</button>}
          <button className="send" type="submit" disabled={disabled || !value.trim()} aria-label={t('send')} title={t('send')}><Icon name="send" /></button></div>
      </form>
      <span className="comp-hint-below" id="composer-hint">{t('enterHint')}</span>
    </div>
  </div>;
}

const handoffKeys = { LOGIN: 'handoffLOGIN', CAPTCHA: 'handoffCAPTCHA', VERIFICATION_CODE: 'handoffVERIFICATION_CODE',
  PERSONAL_DATA: 'handoffPERSONAL_DATA', STUCK: 'handoffSTUCK', OTHER: 'handoffOTHER' } as const;

function MessageCard({ message, progress, onRecheck, rechecking, actions }: { message: Message; progress?: string; onRecheck?: () => void; rechecking?: boolean; actions?: ReactNode }) {
  const { locale, t } = useI18n();
  const [copyStatus, setCopyStatus] = useState('');
  useEffect(() => { setCopyStatus(''); }, [message.content]);
  const handleCopy = async () => {
    if (!message.content) return;
    try {
      await navigator.clipboard.writeText(message.content);
      setCopyStatus(t('copyOk'));
    } catch {
      setCopyStatus(t('copyFail'));
    }
  };

  if (message.role === 'user') return <div className="msg-user">{message.content}</div>;
  return <article className="msg-assist">
    <div className="msg-byline">
      Jev 
      {message.content && progress && <span className="msg-progress">{progress}</span>}
      {message.content && <button onClick={handleCopy} className="copy-icon-button" aria-label={t('copy')}><Icon name="copy" /></button>}
      {copyStatus && <span className="copy-status" role="status">{copyStatus}</span>}
    </div>
    {message.kind === 'handoff' ? <div className="approval-card handoff-card" role="status">
      <strong>{t('handoffTitle')}</strong>
      <p>{t(handoffKeys[message.handoff?.code as keyof typeof handoffKeys] ?? 'handoffOTHER')}</p>
      {actions}
    </div> : <div className="msg-text">{message.content || <span className="msg-pending">{t('stateThinking')}… {progress && <span className="msg-progress" role="status">{progress}</span>}</span>}</div>}
    {message.verification && <div className={`verification ${message.verification.satisfied ? 'verified' : 'unverified'}`}>
      {message.verification.stale ? 'Página alterada — verificação desatualizada' : message.verification.satisfied ? t('confirmed') : t('unconfirmed')}
    </div>}
    {message.verification?.evidence?.length ? <details className="evidence"><summary>{t('evidence')}</summary><p>{message.verification.evidence.join('\n')}</p></details> : null}
    {!!message.verification?.checks?.length && <details className="verification-checks" open={!message.verification.satisfied}>
      <summary>{t('requestCheck')} ({message.verification.checks.length})</summary>
      <ul>{message.verification.checks.map((check, index) => <li key={index} data-status={check.status}>
        <strong>{check.requirement}</strong><span>{check.status === 'confirmed' ? t('statusConfirmed') : check.status === 'not_met' ? t('statusNotMet') : t('statusUnknown')}</span>
        <p>{check.reason}</p>{!!check.evidence.length && <blockquote>{check.evidence.join('\n')}</blockquote>}
      </li>)}</ul>
    </details>}
    {message.verification?.checked_at && <small className="verification-time">{t('checkedAt', { date: new Date(message.verification.checked_at * 1000).toLocaleString(locale) })}</small>}
    {onRecheck && <button className="recheck-button" type="button" disabled={rechecking} onClick={onRecheck}>{t('recheck')}</button>}
  </article>;
}

function Preview({ state, frame, onState, overlays, detailsOpen, onDetailsToggle, width, maxWidth, onResize, onViewportChange, syncError }: {
  state: Snapshot; overlays: boolean; detailsOpen: boolean; onDetailsToggle: () => void;
  frame:PreviewCapture|null; onState:(state:Snapshot)=>void;
  width: number; maxWidth: number; onResize: (width: number) => void;
  onViewportChange: (viewport: Viewport | null) => void; syncError: string;
}) {
  const { t } = useI18n();
  const drag = useRef<{ pointerId: number; x: number; width: number } | null>(null);
  const content = useRef<HTMLDivElement>(null);
  const [contentSize, setContentSize] = useState<Viewport | null>(null);
  const [manualHost,setManualHost] = useState<HTMLDivElement|null>(null);
  const manualActive = Boolean(state.manual && state.manual.status !== 'off');
  const humanInput = state.manual?.status === 'active' && ownsManual(state.session_id,state.manual);
  const browsable = canBrowse(state) || (humanInput && !state.manual?.can_resume);
  useEffect(() => {
    const element = content.current;
    if (!element) return;
    const measure = () => {
      const width = Math.floor(element.clientWidth), height = Math.floor(element.clientHeight);
      const visible = width > 0 && height > 0 && element.getClientRects().length > 0;
      setContentSize(visible ? { width, height } : null);
      onViewportChange(visible ? { width: Math.min(width, 4096), height: Math.min(height, 4096) } : null);
    };
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    measure();
    return () => { observer.disconnect(); onViewportChange(null); };
  }, [onViewportChange]);
  const page = state.page;
  const imageWidth = page && contentSize ? Math.min(contentSize.width, contentSize.height * page.w / page.h) : undefined;
  const decision = state.decision;
  const actions = (page?.actions || []).filter((action, index, items) =>
    action.rect && items.findIndex((other) => other.rect && other.node === action.node) === index);
  const elapsed = state.timing?.execution_ms ?? state.elapsed_ms;
  const duration = elapsed == null ? '' : `${(elapsed / 1000).toFixed(1)} s`;
  return <section className="preview" aria-label={t('browserPreview')}>
    <div className="preview-resize" role="separator" tabIndex={0} aria-label={t('resizePreview')} aria-orientation="vertical"
      aria-valuemin={300} aria-valuemax={maxWidth} aria-valuenow={Math.round(width)}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        drag.current = { pointerId: event.pointerId, x: event.clientX, width };
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        if (drag.current?.pointerId === event.pointerId) onResize(drag.current.width - (event.clientX - drag.current.x));
      }}
      onPointerUp={(event) => { if (drag.current?.pointerId === event.pointerId) drag.current = null; }}
      onPointerCancel={(event) => { if (drag.current?.pointerId === event.pointerId) drag.current = null; }}
      onKeyDown={(event) => {
        if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
          event.preventDefault(); onResize(width + (event.key === 'ArrowLeft' ? 24 : -24));
        }
      }} />
    <div className="preview-head"><span className="preview-dot" /><span className="preview-url" title={page?.url || state.last_url || ''}>{page?.url || t('noSite')}</span>
      <span className="preview-label">{t('preview').toLocaleUpperCase()}</span>
      <button className="icon-btn details-toggle" type="button" aria-label={detailsOpen ? t('closeDetails') : t('openDetails')} title={t('executionDetails')} aria-expanded={detailsOpen} aria-controls="execution-details" onClick={onDetailsToggle}><Icon name="panel" /></button></div>
    <div className="preview-content" ref={content}>
      {page?.screenshot ? <div ref={setManualHost} tabIndex={humanInput ? 0 : -1} aria-label={t('pageImage')} className={`page-image ${humanInput ? 'manual-active' : ''} ${browsable ? 'browsable' : ''}`} style={{ aspectRatio: `${page.w} / ${page.h}`, width: imageWidth, height: imageWidth === undefined ? undefined : imageWidth * page.h / page.w }}>
        <img draggable={false} src={`data:image/jpeg;base64,${page.screenshot}`} alt={t('pageImage')} />
        {overlays && !manualActive && <div className="targets" aria-label={t('observedTargets')}>{actions.map((action, index) => <div key={action.id} className={`target ${decision?.target?.split(':')[0] === String(index + 1) ? 'selected' : ''}`}
          style={{ left: `${100 * action.rect!.x / page.w}%`, top: `${100 * action.rect!.y / page.h}%`, width: `${100 * action.rect!.w / page.w}%`, height: `${100 * action.rect!.h / page.h}%` }}><span>{index + 1}</span></div>)}</div>}
      </div> : <div className="preview-empty"><span className="preview-symbol">[ ↗ ]</span><h2>{page ? t('pageOpen') : t('manyPossibilities')}</h2>
        <p>{page ? t('imageUnavailable') : state.last_url ? t('previousTabClosed') : t('pageWillAppear')}</p></div>}
    </div>
    <ManualPanel state={state} frame={frame} host={manualHost} onState={onState} />
    {(syncError || state.viewport_error) && <div className="preview-error" role="alert">{syncError || state.viewport_error}</div>}
    <div className="preview-foot"><span>{page?.title || t('waitingSite')}</span><span>{duration}</span></div>
  </section>;
}

function ExecutionSidebar({ state }: { state: Snapshot }) {
  const { messages: labels, t } = useI18n();
  const page = state.page;
  const decision = state.decision || state.decisions?.at(-1);
  const targetIndex = decision?.target?.split(':')[0];
  const selectedElement = state.elements?.find((element) => element.index === targetIndex);
  const targetLabel = selectedElement?.label || decision?.target || decision?.choice || '—';
  const targetOption = decision?.target?.includes(':') ? decision.target.split(':').slice(1).join(':') : '';
  const statusLabels: Record<string, string> = { thinking: t('stateThinking'), running: t('stateThinking'), verifying: t('stateVerifying'), paused: t('statePaused'), awaiting_confirmation: t('stateConfirmation'), answered: t('stateAnswered'), completed: t('stateCompleted'), error: t('stateError'), idle: t('stateIdle') };
  const statusClass = activePhases.has(state.chat_status) ? 'is-active' : state.chat_status === 'paused' ? 'is-paused' : state.chat_status === 'awaiting_confirmation' ? 'is-waiting' : state.chat_status === 'error' ? 'is-error' : ['answered', 'completed'].includes(state.chat_status) ? 'is-complete' : '';
  return <aside className="execution-sidebar" id="execution-details" aria-label={t('executionDetails')}>
      <div className="execution-sidebar-head sb-top"><div className="execution-heading"><strong>{t('executionDetails')}</strong></div></div>
      <div className="inspector-content">
        <section className={`execution-summary ${statusClass}`} aria-label={t('currentExecution')}>
          <span className="execution-state"><i />{statusLabels[state.chat_status] || state.chat_status}</span>
          <div className="execution-current"><span>{decision?.operation ? t('operation') : t('activity')}</span><strong>{decision?.operation || (!state.session_id ? t('describeAction') : state.progress) || t('waitingTask')}</strong></div>
          {state.goal && <div className="execution-goal"><span>{t('currentRequest')}</span><p className="inspector-goal">{state.goal}</p></div>}
        </section>
        {!page && state.last_url && <p className="execution-note">{t('previousDetailsUnavailable')}</p>}
        {decision && <div className="execution-facts"><div className="execution-target"><span>{t('selectedTarget')}</span><strong>{targetLabel}</strong>{selectedElement && <small>#{selectedElement.index} · {selectedElement.role}{selectedElement.operations.length ? ` · ${selectedElement.operations.join(' / ')}` : ''}</small>}{targetOption && <small>{targetOption}</small>}</div><div className="execution-confidence"><span>{t('confidence')}</span><strong>{decision.target_confidence == null ? '—' : `${Math.round(decision.target_confidence * 100)}%`}</strong></div></div>}
        {state.timing && <section className="execution-timing" aria-label={t('requestTime')}>
          <div className="execution-timing-head"><span>{t('requestTime')}</span><strong>{(state.timing.end_to_end_ms / 1000).toFixed(2)} s</strong></div>
          <div className="execution-metrics">
            <div><span>{t('time')}</span><strong>{((state.timing.execution_ms ?? state.elapsed_ms ?? 0) / 1000).toFixed(2)} s</strong></div>
            {state.timing.first_action_ms != null && <div><span>{t('firstAction')}</span><strong>{(state.timing.first_action_ms / 1000).toFixed(2)} s</strong></div>}
            <div><span>{t('approvalWait')}</span><strong>{(state.timing.approval_wait_ms / 1000).toFixed(2)} s</strong></div>
            <div><span>{t('manualControl')}</span><strong>{((state.timing.manual_wait_ms || 0) / 1000).toFixed(2)} s</strong></div>
          </div>
          {!!Object.keys(state.timing.stages || {}).length && <details className="execution-stages" open><summary>{t('stageTimes')}</summary>{Object.entries(state.timing.stages || {}).map(([name, stage]) => <p key={name}><span>{labels.stages[name] || name}</span><strong>{(stage.duration_ms / 1000).toFixed(2)} s <small>({stage.count})</small></strong></p>)}<small>{t('overlap')}</small></details>}
        </section>}
        <div className="sb-section">{t('observedData')}</div>
        {!!state.user_actions?.length && <details className="execution-group"><summary>{t('manualControl')} ({state.user_actions.length})</summary>{state.user_actions.map((item,index)=><p key={index}>{item.type} · {item.execution}</p>)}</details>}
        <details className="execution-group" open><summary><span className="execution-group-title">{t('observedElements')}</span><span className="execution-count">{state.elements?.length || 0}</span></summary><div className="element-list">{(state.elements || []).map((element) => <div className={`execution-item ${selectedElement?.index === element.index ? 'is-selected' : ''}`} key={element.index}><span className="execution-index">{element.index}</span><span className="execution-item-body"><strong>{element.label}</strong><small>{element.role}{element.operations.length ? ` · ${element.operations.join(' / ')}` : ''}</small>{element.value && <small>{t('value')}: {element.value}</small>}</span></div>)}</div></details>
        <details className="execution-group" open><summary><span className="execution-group-title">{t('actionsHistory')}</span><span className="execution-count">{state.history?.length || 0}</span></summary><div className="element-list">{(state.history || []).map((item, index) => <div className="execution-item" key={index}><span className="execution-index">{String(item.step ?? index + 1).padStart(2, '0')}</span><span className="execution-item-body"><strong>{item.action}</strong><small>{item.execution || '—'}</small><small className={`execution-result ${item.page_changed === true ? 'is-changed' : item.page_changed === false ? 'is-unchanged' : ''}`}>{item.page_changed === true ? t('pageChanged') : item.page_changed === false ? t('noChange') : t('resultUnobserved')}</small></span></div>)}</div></details>
      </div>
    </aside>;
}

function App() {
  const { locale, t } = useI18n();
  const [state, setState] = useState<Snapshot | null>(null);
  const [capture, setCapture] = useState<PreviewCapture | null>(null);
  const captureRef = useRef<PreviewCapture | null>(null);
  const [previewError, setPreviewError] = useState('');
  const [detailState, setDetailState] = useState<Snapshot | null>(null);
  const stateRef = useRef<Snapshot | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [draft, setDraft] = useState('');
  const [search, setSearch] = useState('');
  const [historyOpen, setHistoryOpen] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth > 900 && window.location.pathname !== '/settings');
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const accountMenu = useRef<HTMLDivElement>(null);
  const [previewWidth, setPreviewWidth] = useState(() => {
    const saved = Number(localStorage.getItem('jev.preview.width'));
    return saved > 0 && Number.isFinite(saved) ? saved : window.innerWidth * 0.46;
  });
  const [viewportWidth, setViewportWidth] = useState(window.innerWidth);
  const [mobileView, setMobileView] = useState<'chat' | 'preview'>('chat');
  const [overlays, setOverlays] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pauseSubmitting, setPauseSubmitting] = useState(false);
  const busyRef = useRef(false);
  const viewportRef = useRef<Viewport | null>(null);
  const sentViewport = useRef<{ sessionId: string; width: number; height: number } | null>(null);
  const [measuredViewport, setMeasuredViewport] = useState<Viewport | null>(null);
  const [viewportSyncError, setViewportSyncError] = useState('');
  const onViewportChange = useCallback((viewport: Viewport | null) => {
    viewportRef.current = viewport;
    setMeasuredViewport((current) => current?.width === viewport?.width && current?.height === viewport?.height ? current : viewport);
  }, []);
  const mutationEpoch = useRef(0);
  const [error, setError] = useState('');
  const [theme, setTheme] = useState(() => localStorage.getItem('jev.theme') || 'light');
  const [keyInput, setKeyInput] = useState('');
  const [settingsTab, setSettingsTab] = useState<'general' | 'model'>('general');
  const thread = useRef<HTMLDivElement>(null);
  const settingsPage = window.location.pathname === '/settings';
  const viewportSupported = state !== null && 'viewport' in state;

  useEffect(() => { document.documentElement.dataset.theme = theme; localStorage.setItem('jev.theme', theme); }, [theme]);
  useEffect(() => { localStorage.setItem('jev.preview.width', String(Math.round(previewWidth))); }, [previewWidth]);
  useEffect(() => {
    let previousWidth = window.innerWidth;
    const onResize = () => {
      const nextWidth = window.innerWidth;
      if (previousWidth > 900 && nextWidth <= 900) setSidebarOpen(false);
      previousWidth = nextWidth;
      setViewportWidth(nextWidth);
    };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  useEffect(() => {
    if (!detailsOpen) return;
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setDetailsOpen(false); };
    document.addEventListener('keydown', escape);
    return () => document.removeEventListener('keydown', escape);
  }, [detailsOpen]);
  useEffect(() => { if (historyOpen) setDetailsOpen(false); }, [historyOpen]);
  useEffect(() => {
    if (!accountMenuOpen) return;
    const dismiss = (event: PointerEvent) => { if (!accountMenu.current?.contains(event.target as Node)) setAccountMenuOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setAccountMenuOpen(false); };
    document.addEventListener('pointerdown', dismiss);
    document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('pointerdown', dismiss); document.removeEventListener('keydown', escape); };
  }, [accountMenuOpen]);
  useEffect(() => { if (thread.current) thread.current.scrollTop = thread.current.scrollHeight; }, [state?.messages?.length, state?.approval, state?.manual?.status]);
  useEffect(() => {
    if (!['thinking', 'running'].includes(state?.chat_status || '')) setPauseSubmitting(false);
  }, [state?.chat_status]);

  const refreshHistory = useCallback(async () => {
    const epoch = mutationEpoch.current;
    try {
      const history = await readJson<{ conversations: Conversation[] }>('/api/conversations');
      if (epoch !== mutationEpoch.current) return;
      setConversations(history.conversations);
      setError((current) => current === translate(currentLocale(), 'updateHistoryError') ? '' : current);
    } catch { if (epoch === mutationEpoch.current) setError(translate(currentLocale(), 'updateHistoryError')); }
  }, []);

  const refresh = useCallback(async () => {
    const epoch = mutationEpoch.current;
    const next = await readJson<Snapshot>('/api/state?compact=1',manualHeaders(stateRef.current?.session_id));
    if (epoch !== mutationEpoch.current) return;
    stateRef.current = next;
    setState(next);
  }, []);

  useEffect(() => { void refreshHistory(); }, [historyOpen, state?.session_id, state?.chat_status, refreshHistory]);

  useEffect(() => {
    if (!detailsOpen) return;
    let cancelled = false;
    const epoch = mutationEpoch.current;
    void readJson<Snapshot>('/api/state',manualHeaders(stateRef.current?.session_id)).then((full) => {
      if (!cancelled && epoch === mutationEpoch.current && full.session_id === stateRef.current?.session_id && full.turn_id === stateRef.current?.turn_id) {
        setDetailState(full);
        setError((current) => current === translate(currentLocale(), 'updateDetailsError') ? '' : current);
      }
    }).catch(() => { if (!cancelled) setError(translate(currentLocale(), 'updateDetailsError')); });
    return () => { cancelled = true; };
  }, [detailsOpen, state?.session_id, state?.turn_id, state?.chat_status, state?.goal, state?.progress, state?.action_count, state?.decision_count, state?.preview_revision]);

  useEffect(() => {
    captureRef.current = null;
    setCapture(null);
    setPreviewError('');
    let cancelled = false;
    let timer: number;
    const pollPreview = async () => {
      const sessionId = stateRef.current?.session_id;
      if (sessionId) {
        const epoch = mutationEpoch.current;
        const previous = captureRef.current;
        const after = previous?.session_id === sessionId ? `&after=${previous.revision}` : '';
        try {
          const result = await readJson<PreviewResponse>(`/api/preview?session_id=${encodeURIComponent(sessionId)}${after}`, manualHeaders(sessionId));
          if (!cancelled && epoch === mutationEpoch.current && stateRef.current?.session_id === sessionId) {
            if (result.capture) {
              const next = { ...result.capture, session_id: sessionId };
              captureRef.current = next;
              setCapture(next);
            }
            setPreviewError(result.error || '');
          }
        } catch (cause) {
          if (cause instanceof HttpError && cause.status === 404) {
            // This tab still points at a conversation the server replaced (restart, another tab, or a new chat).
            // Follow the server's current conversation instead of polling a dead preview.
            try { await refresh(); } catch { /* the regular state poll reports connection errors */ }
            if (!cancelled && stateRef.current?.session_id !== sessionId) return;
          }
          if (!cancelled && stateRef.current?.session_id === sessionId) setPreviewError(translate(currentLocale(), 'updatePreviewError'));
        }
      }
      if (!cancelled) timer = window.setTimeout(pollPreview, stateRef.current?.manual?.status === 'active' ? 200 : 500);
    };
    void pollPreview();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [state?.session_id]);

  useEffect(() => {
    let cancelled = false;
    let timer: number;
    const poll = async () => {
      if (!busyRef.current) {
        try { await refresh(); if (!cancelled) setError((value) => value === translate(currentLocale(), 'connectionError') ? '' : value); }
        catch { if (!cancelled) setError(translate(currentLocale(), 'connectionError')); }
      }
      if (!cancelled) timer = window.setTimeout(poll, activePhases.has(stateRef.current?.chat_status || '') || ['paused', 'manual'].includes(stateRef.current?.chat_status || '') ? 650 : 2500);
    };
    void poll();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [refresh]);

  async function perform(name: string, body: Record<string, unknown> = {}) {
    if (busyRef.current) return false;
    busyRef.current = true;
    mutationEpoch.current += 1;
    setBusy(true);
    setError('');
    try {
      const next = await post(name, { session_id: stateRef.current?.session_id, ...body });
      stateRef.current = next;
      setState(next);
      void refreshHistory();
      return true;
    } catch (cause) {
      // Reconcile once. Never retry a POST that may have reached the browser.
      try { await refresh(); } catch { /* Keep the original error visible. */ }
      setError(cause instanceof Error ? cause.message : translate(currentLocale(), 'operationError'));
      return false;
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  useEffect(() => {
    setViewportSyncError('');
  }, [state?.session_id, viewportSupported]);

  useEffect(() => {
    const sessionId = state?.session_id;
    if (sessionId && !viewportSupported) {
      setViewportSyncError('Reinicie o servidor local do Jev para ativar a prévia responsiva.');
      return;
    }
    if (!sessionId || !measuredViewport || busy || (state.manual?.status !== 'off' && !ownsManual(state.session_id,state.manual))) return;
    const { width, height } = measuredViewport;
    if (state.viewport?.width === width && state.viewport?.height === height) return;
    const previous = sentViewport.current;
    if (previous?.sessionId === sessionId && previous.width === width && previous.height === height) return;
    const timer = window.setTimeout(async () => {
      if (busyRef.current || stateRef.current?.session_id !== sessionId) return;
      sentViewport.current = { sessionId, width, height };
      busyRef.current = true;
      mutationEpoch.current += 1;
      setBusy(true);
      setViewportSyncError('');
      try {
        const next = await post('viewport', { session_id: sessionId, width, height });
        stateRef.current = next;
        setState(next);
      } catch (cause) {
        // An uncertain browser resize is reconciled, never automatically repeated.
        try { await refresh(); } catch { /* Report the original resize failure. */ }
        setViewportSyncError(cause instanceof Error ? cause.message : translate(currentLocale(), 'updatePreviewError'));
      } finally {
        busyRef.current = false;
        setBusy(false);
      }
    }, 200);
    return () => window.clearTimeout(timer);
  }, [state?.session_id, state?.viewport?.width, state?.viewport?.height, state?.manual, viewportSupported, measuredViewport, busy, refresh]);

  async function send() {
    const submittedDraft = draft;
    const message = draft.trim();
    if (!message || disabled) return;
    const messageId = crypto.randomUUID();
    const done = await perform('message', { message, message_id: messageId, ...(!stateRef.current?.session_id ? viewportRef.current : {}) });
    if (done || stateRef.current?.messages?.some((item) => item.turn_id === messageId))
      setDraft((current) => current === submittedDraft ? '' : current);
  }

  async function pause() {
    if (busyRef.current || pauseSubmitting || stateRef.current?.pause_requested) return;
    setPauseSubmitting(true);
    if (!await perform('pause')) setPauseSubmitting(false);
  }

  function confirmDiscard() {
    if (state?.chat_status === 'awaiting_confirmation' || state?.chat_status === 'paused')
      return window.confirm(t('discardPending'));
    return true;
  }

  async function newChat() {
    if (disabledSession || !confirmDiscard()) return;
    if (state?.session_id && !await perform('reset')) return;
    if (settingsPage) { window.location.assign('/'); return; }
    setDraft(''); setHistoryOpen(false); setMobileView('chat');
    if (window.innerWidth <= 900) setSidebarOpen(false);
  }

  async function selectChat(id: string) {
    if (disabledSession || !confirmDiscard()) return;
    if (state?.session_id !== id && !await perform('select', { conversation_id: id })) return;
    if (settingsPage) { window.location.assign('/'); return; }
    setDraft(''); setHistoryOpen(false); setMobileView('chat');
    if (window.innerWidth <= 900) setSidebarOpen(false);
  }

  async function deleteChat(id: string) {
    if (disabledSession || (id === state?.session_id && !confirmDiscard()) || !window.confirm(t('deleteChat'))) return;
    if (await perform('delete', { conversation_id: id })) { setDraft(''); setHistoryOpen(false); }
  }

  async function renameChat(item: Conversation) {
    if (disabledSession) return;
    const title = window.prompt(t('conversationName'), item.title);
    if (title == null || title.trim() === item.title) return;
    await perform('rename', { conversation_id: item.id, title });
  }

  async function archiveChat(item: Conversation) {
    if (disabledSession || (item.id === state?.session_id && !confirmDiscard())) return;
    if (await perform('archive', { conversation_id: item.id }) && item.id === state?.session_id) {
      setDraft(''); setHistoryOpen(true);
    }
  }

  async function unarchiveChat(item: Conversation) {
    if (disabledSession) return;
    await perform('unarchive', { conversation_id: item.id });
  }

  const manualState = useCallback((next:Snapshot) => {
    mutationEpoch.current += 1;
    stateRef.current = next;
    setState(next);
  }, []);
  const manualActive = Boolean(state?.manual && state.manual.status !== 'off');
  // Browsing between tasks never blocks the chat; the server ends it when the next command arrives.
  const browsing = state?.manual?.status === 'active' && !state.manual.can_resume;
  const disabledSession = busy || !state || activePhases.has(state.chat_status) || (manualActive && !browsing);
  const disabled = disabledSession || state?.chat_status === 'awaiting_confirmation';
  const pausing = pauseSubmitting || Boolean(state?.pause_requested);
  const activeConversations = conversations.filter((item) => !item.archived);
  const filtered = conversations.filter((item) => item.archived === showArchived && item.title.toLocaleLowerCase(locale).includes(search.toLocaleLowerCase(locale)));
  const currentTitle = conversations.find((item) => item.id === state?.session_id)?.title || t('newChat');
  const maxPreviewWidth = Math.max(300, viewportWidth - (sidebarOpen ? 256 : 56) - 320 - (detailsOpen && viewportWidth >= 1132 ? 256 : 0));
  const effectivePreviewWidth = Math.min(maxPreviewWidth, Math.max(300, previewWidth));
  const resizePreview = (width: number) => setPreviewWidth(Math.min(maxPreviewWidth, Math.max(300, width)));

  function conversationActions(item: Conversation) {
    return <details className="conversation-actions"><summary className="icon-btn" aria-label={t('actionsFor', { title: item.title })}><Icon name="more" /></summary><div className="conversation-menu">
      <button type="button" disabled={disabledSession} onClick={(event) => { event.currentTarget.closest('details')?.removeAttribute('open'); void selectChat(item.id); }}>{t('openConversation')}</button>
      <button type="button" disabled={disabledSession} onClick={(event) => { event.currentTarget.closest('details')?.removeAttribute('open'); void renameChat(item); }}>{t('rename')}</button>
      <button type="button" disabled={disabledSession} onClick={(event) => { event.currentTarget.closest('details')?.removeAttribute('open'); void (item.archived ? unarchiveChat(item) : archiveChat(item)); }}>{item.archived ? t('restoreConversation') : t('archiveConversation')}</button>
      <button type="button" className="danger" disabled={disabledSession} onClick={(event) => { event.currentTarget.closest('details')?.removeAttribute('open'); void deleteChat(item.id); }}>{t('deleteConversation')}</button>
    </div></details>;
  }

  return <div className="app jev-app" data-sidebar={sidebarOpen ? 'open' : 'closed'} data-details={detailsOpen && !historyOpen ? 'open' : 'closed'} data-mobile-view={mobileView} data-page={settingsPage ? 'settings' : 'chat'}
    style={{ '--preview-width': `${effectivePreviewWidth}px` } as CSSProperties}>
    <aside className={`sidebar ${sidebarOpen ? '' : 'is-collapsed'} ${accountMenuOpen ? 'menu-open' : ''}`} aria-label={t('localMenu')}>
      <div className="sb-top"><span className="brand"><span className="brand-mark" aria-hidden="true" /><span className="brand-name" aria-hidden="true">JEV OpenFast Browser</span></span>
        <button className="icon-btn sb-toggle" onClick={() => { setSidebarOpen((value) => !value); if (window.innerWidth <= 900) setDetailsOpen(false); }} aria-label={sidebarOpen ? 'Recolher menu' : 'Expandir menu'}><Icon name="panel" /></button></div>
      <div className="sb-actions">
        <button className="sb-act primary" aria-label={t('newChat')} title={t('newChat')} onClick={newChat} disabled={disabledSession}><Icon name="plus" /><span className="sb-label">{t('newChat')}</span></button>
        <button className={`sb-act ${historyOpen ? 'is-active' : ''}`} aria-label={t('search')} title={t('search')} onClick={() => { setHistoryOpen(true); if (window.innerWidth <= 900) setSidebarOpen(false); }}><Icon name="search" /><span className="sb-label">{t('search')}</span></button>
      </div>
      <div className="sb-list"><div className="sb-section">{t('recents')} <span className="count">{activeConversations.length}</span></div>
        {activeConversations.slice(0, 30).map((item) => <div className="sidebar-conversation" key={item.id}><button className={`convo ${item.id === state?.session_id ? 'active' : ''}`} onClick={() => selectChat(item.id)} disabled={disabledSession} title={item.title}>
          <span className="convo-title">{item.title}</span></button>{conversationActions(item)}</div>)}
      </div>
      <div className="sb-foot" ref={accountMenu}>
        <button className="account-trigger" type="button" aria-haspopup="menu" aria-expanded={accountMenuOpen} aria-label={t('localMenu')}
          onClick={() => setAccountMenuOpen((value) => !value)}><span className="avatar">J</span><span className="sb-who"><span className="who">{t('localJev')}</span><span className="plan">{state?.openrouter_key_source === 'missing' ? t('keyMissingShort') : 'OpenRouter'}</span></span><span className="account-chevron" aria-hidden="true">⌄</span></button>
        {accountMenuOpen && <div className="account-menu" role="menu" aria-label={t('localMenu')}>
          <a href="/settings" role="menuitem"><Icon name="settings" />{t('settings')}</a>
          <button type="button" role="menuitem" onClick={() => { setTheme(theme === 'dark' ? 'light' : 'dark'); setAccountMenuOpen(false); }}><Icon name={theme === 'dark' ? 'sun' : 'moon'} />{theme === 'dark' ? t('lightTheme') : t('darkTheme')}</button>
        </div>}
      </div>
    </aside>
    {sidebarOpen && <button className="mobile-backdrop" aria-label={t('closeMenu')} onClick={() => setSidebarOpen(false)} />}

    {settingsPage ? <main className="main settings-main">
      <div className="topbar"><button className="icon-btn topbar-menu" onClick={() => setSidebarOpen(true)} aria-label={t('openMenu')}><Icon name="panel" /></button><a href="/" className="back-link">{t('backAssistant')}</a></div>
      {(error || state?.storage_error) && <div className="error-banner" role="alert">{error || state?.storage_error}</div>}
      <div className="settings-page"><header className="settings-head"><h1>{t('settings')}</h1><p>{t('settingsSubtitle')}</p></header>
      <div className="settings-layout"><nav className="settings-menu"><p className="group">{t('preferences')}</p>
        <button className={settingsTab === 'general' ? 'active' : ''} onClick={() => setSettingsTab('general')}>{t('general')}</button>
        <button className={settingsTab === 'model' ? 'active' : ''} onClick={() => setSettingsTab('model')}>{t('modelTab')}</button>
        <div className="settings-sign"><span>v0.1.0</span>
          <a href="https://github.com/samuelyossef/jev-openfast-browser" target="_blank" rel="noopener noreferrer" aria-label="GitHub" title="GitHub"><svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38v-1.33c-2.23.48-2.7-1.07-2.7-1.07-.36-.92-.89-1.17-.89-1.17-.73-.5.06-.49.06-.49.8.06 1.23.83 1.23.83.72 1.22 1.87.87 2.33.66.07-.52.28-.87.5-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.6 7.6 0 0 1 4 0c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.28.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48v2.2c0 .21.15.46.55.38A8 8 0 0 0 16 8c0-4.42-3.58-8-8-8z" /></svg></a>
          <a href="https://www.linkedin.com/in/samuelyossef" target="_blank" rel="noopener noreferrer" aria-label="LinkedIn" title="LinkedIn"><svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor" aria-hidden="true"><path d="M0 1.15C0 .52.52 0 1.17 0h13.66C15.48 0 16 .52 16 1.15v13.7c0 .63-.52 1.15-1.17 1.15H1.17C.52 16 0 15.48 0 14.85zM4.94 13.39V6.17H2.54v7.22zM3.74 5.18c.84 0 1.36-.55 1.36-1.25-.02-.71-.52-1.25-1.34-1.25s-1.36.54-1.36 1.25c0 .7.52 1.25 1.33 1.25zm4.9 8.21V9.36c0-.22.02-.43.08-.58.17-.43.57-.88 1.23-.88.87 0 1.21.66 1.21 1.63v3.86h2.4V9.25c0-2.22-1.18-3.25-2.76-3.25-1.27 0-1.84.7-2.16 1.19v.02h-.02l.02-.02V6.17H6.24c.03.68 0 7.22 0 7.22z" /></svg></a></div></nav>
      <section className="settings-panel">{settingsTab === 'general' ? <>
        <div className="srow"><span>{t('interfaceLanguage')}</span><LanguageSegment /></div>
        <p className="group">{t('appearance')}</p>
        <div className="srow"><span>{t('darkTheme')}</span><input type="checkbox" className="switch" checked={theme === 'dark'} onChange={(event) => setTheme(event.target.checked ? 'dark' : 'light')} /></div>
      </> : <div className="settings-card"><p className="eyebrow">{t('provider')}</p><h1>OpenRouter</h1>
        <p>{t('configureKey')}</p>
        <form onSubmit={async (event) => { event.preventDefault(); if (!keyInput.trim()) return; if (await perform('settings', { openrouter_api_key: keyInput.trim() })) setKeyInput(''); }}>
          <label htmlFor="openrouter-key">{t('openRouterKey')}</label><div className="provider-entry"><input id="openrouter-key" type="password" value={keyInput} maxLength={1024} autoComplete="new-password" onChange={(event) => setKeyInput(event.target.value)} placeholder={t('pasteKey')} required />
            <button className="primary-button" disabled={busy || !state || !keyInput.trim()}>{t('saveKey')}</button></div>
        </form><p role="status">{state?.openrouter_key_source === 'encrypted' ? t('keyEncrypted') : state?.openrouter_key_source === 'environment' ? t('keyEnvironment') : t('keyMissing')}</p>
        <small>{t('keyPrivacy')}</small></div>}</section></div></div>
    </main> : <>
      <main className={`main ${historyOpen ? 'history-main' : 'chat-main'}`}>
        <div className="topbar"><button className="icon-btn topbar-menu" onClick={() => { setSidebarOpen(true); setDetailsOpen(false); }} aria-label={t('openMenu')}><Icon name="panel" /></button>
          <span className="crumbs"><b>{historyOpen ? t('history') : currentTitle}</b></span><span className="top-right"><span className="top-pill"><span className="dot" /> JEV OpenFast Browser</span></span></div>
        {(error || state?.storage_error) && <div className="error-banner" role="alert">{error || state?.storage_error}</div>}
        {historyOpen ? <div className="history-view"><div className="history-heading"><h1>{t('history')}</h1><button onClick={() => setHistoryOpen(false)}>{t('backToChat')}</button></div>
          <label className="search-field"><Icon name="search" /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={t('searchConversations')} aria-label={t('searchConversations')} autoFocus /></label>
          <div className="history-tabs" role="tablist"><button type="button" role="tab" aria-selected={!showArchived} className={!showArchived ? 'active' : ''} onClick={() => setShowArchived(false)}>{t('conversations')} <span>{activeConversations.length}</span></button><button type="button" role="tab" aria-selected={showArchived} className={showArchived ? 'active' : ''} onClick={() => setShowArchived(true)}>{t('archived')} <span>{conversations.length - activeConversations.length}</span></button></div>
          <div className="history-list">{filtered.map((item) => <div key={item.id} className="history-item"><button className="history-item-open" onClick={() => selectChat(item.id)} disabled={disabledSession}><strong>{item.title}</strong><small>{new Date(item.updated_at).toLocaleString(locale)} {item.last_url ? `· ${item.last_url}` : ''}</small></button>
            {conversationActions(item)}</div>)}
            {!filtered.length && <p className="muted">{t('noConversations')}</p>}</div></div> : <>
          <div className="mobile-tabs"><button className={mobileView === 'chat' ? 'active' : ''} onClick={() => setMobileView('chat')}>{t('chat')}</button><button className={mobileView === 'preview' ? 'active' : ''} onClick={() => setMobileView('preview')}>{t('preview')}</button></div>
          <div className="thread" ref={thread} role="log" aria-label={t('messages')} aria-live="polite"><div className="thread-inner">
            {!state?.messages?.length && <div className="empty-home"><span className="greet-icon">✳</span><h1 className="greet-text">{t('greeting')}</h1><p>{t('greetingBody')}</p></div>}
            {state?.messages?.map((message, index) => <MessageCard key={`${message.turn_id || index}-${index}`} message={message}
              onRecheck={index === state.messages.length - 1 && state.can_recheck ? () => { void perform('verify'); } : undefined} rechecking={busy}
              progress={index === state.messages.length - 1 && (activePhases.has(state.chat_status) || state.chat_status === 'answered') ? state.progress : undefined}
              actions={index === state.messages.length - 1 && message.kind === 'handoff'
                ? <ManualPanel state={state} frame={capture} host={null} onState={manualState} inline onStart={() => setMobileView('preview')} />
                : undefined} />)}
            {state?.approval && <div className="approval-card"><strong>{t('confirmAction')}</strong><p>{state.approval.description}</p><small>{state.approval.url}</small><div className="approval-actions"><button disabled={busy} onClick={() => perform('reject', { approval_id: state.approval!.id })}>{t('reject')}</button><button className="primary-button" disabled={busy} onClick={() => perform('approve', { approval_id: state.approval!.id })}>{t('confirmAction')}</button></div></div>}
          </div></div>
          <Composer value={draft} onChange={setDraft} onSend={send} disabled={disabled} overlays={overlays} onOverlaysChange={setOverlays}
            pauseVisible={['thinking', 'running'].includes(state?.chat_status || '')} paused={state?.chat_status === 'paused'}
            pausing={pausing} controlDisabled={busy} onPause={() => { void pause(); }} onResume={() => { void perform('resume'); }} />
        </>}
      </main>
      {!historyOpen && <><Preview state={state ? { ...state,
        page: capture?.session_id === state.session_id && (!state.viewport || (capture.page.w === state.viewport.width && capture.page.h === state.viewport.height)) ? capture.page : state.page,
        decision: capture?.revision === state.preview_revision ? state.decision : null,
      } : { session_id: null, messages: [], chat_status: 'idle', progress: '' }} frame={capture} onState={manualState} overlays={overlays} detailsOpen={detailsOpen}
        onDetailsToggle={() => { if (window.innerWidth <= 900) setSidebarOpen(false); setDetailsOpen((value) => !value); }} width={effectivePreviewWidth} maxWidth={maxPreviewWidth} onResize={resizePreview}
        onViewportChange={onViewportChange} syncError={viewportSyncError || previewError} />
        {detailsOpen && <ExecutionSidebar state={(detailState?.session_id === state?.session_id && detailState?.turn_id === state?.turn_id ? detailState : state) || { session_id: null, messages: [], chat_status: 'idle', progress: '' }} />}</>}
    </>}
  </div>;
}

export default App;

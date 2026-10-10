import { bindManualInput, manualCommand, manualHeaders, ownsManual } from '/manual.js';
const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
const activePhases = new Set(["thinking", "running", "verifying"]);
let state = null, busy = false, pollTimer, renderedMessages = "";
let preview = null, previewBusy = false;
let stateEpoch = 0;
const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const percent = (value) => `${(value * 100).toFixed(value < 0.01 ? 1 : 0)}%`;

async function refresh() {
  const epoch = stateEpoch;
  const response = await fetch(document.querySelector('.inspector').open ? "/api/state" : "/api/state?compact=1", { cache: "no-store",headers:manualHeaders(state?.session_id) });
  if (!response.ok) throw Error("Não foi possível consultar a conversa.");
  const next = await response.json();
  if (epoch !== stateEpoch) return;
  state = next;
  if ($("error").textContent === 'Não foi possível atualizar os detalhes.') $("error").hidden = true;
  render();
}
async function refreshPreview() {
  if (previewBusy || !state?.session_id) return;
  const sessionId = state.session_id;
  const after = preview?.session_id === sessionId ? `&after=${preview.capture?.revision ?? ''}` : '';
  previewBusy = true;
  try {
    const response = await fetch(`/api/preview?session_id=${encodeURIComponent(sessionId)}${after}`, { cache: "no-store", headers:manualHeaders(sessionId) });
    if (!response.ok) throw Error("Não foi possível atualizar a prévia.");
    const result = await response.json();
    if (state?.session_id !== sessionId) return;
    preview = { ...result, capture: result.capture || (preview?.session_id === sessionId ? preview.capture : null) };
    $("preview-error").textContent = result.error || '';
    $("preview-error").hidden = !result.error;
    render();
  } catch (error) {
    if (state?.session_id === sessionId) {
      $("preview-error").textContent = error.message;
      $("preview-error").hidden = false;
    }
  } finally { previewBusy = false; }
}
async function call(name, body = {}) {
  stateEpoch += 1;
  const response = await fetch(`/api/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Demo-Token": token },
    body: JSON.stringify({ session_id: state?.session_id, ...body }),
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "Não foi possível realizar a operação.");
  state = data;
  render();
}
function controls() {
  const active = activePhases.has(state?.chat_status);
  const awaiting = state?.chat_status === "awaiting_confirmation";
  const manual = Boolean(state?.manual && state.manual.status !== 'off');
  $("new-session").hidden = !state?.session_id;
  $("new-session").disabled = busy || active || manual;
  $("message").disabled = busy || active || awaiting || manual;
  $("send").disabled = $("message").disabled || !$("message").value.trim();
  $("composer-hint").textContent = state?.pending_input ? "Responda à pergunta no chat para continuar a mesma tarefa." : "Enter envia · Shift + Enter quebra a linha";
  $("stop").hidden = !["thinking", "running"].includes(state?.chat_status);
  $("stop").disabled = busy;
  $("resume").hidden = state?.chat_status !== "paused";
  $("resume").disabled = busy || manual;
  document.querySelectorAll(".approval-actions button, .recheck-result").forEach((button) => { button.disabled = busy; });
}
async function perform(fn, label) {
  if (busy) return;
  busy = true;
  $("error").hidden = true;
  controls();
  if (label) $("status").textContent = label;
  try { await fn(); }
  catch (error) {
    // Recover state once. POSTs are never automatically repeated.
    try { await refresh(); } catch { /* Keep the original error visible. */ }
    $("error").textContent = error.message || "Conexão interrompida. Consulte o estado antes de reenviar.";
    $("error").hidden = false;
  } finally {
    busy = false;
    controls();
    schedulePoll();
  }
}
function renderChat() {
  const key = JSON.stringify([state.session_id, state.messages, state.approval, state.can_recheck, busy]);
  if (key === renderedMessages) return;
  renderedMessages = key;
  const container = $("messages");
  const follow = container.scrollHeight - container.scrollTop - container.clientHeight < 80;
  container.replaceChildren();
  if (!state.messages?.length) {
    const welcome = document.createElement("div");
    welcome.className = "chat-welcome";
    welcome.innerHTML = `<span class="welcome-mark" aria-hidden="true">↗</span><h3>O que vamos fazer hoje?</h3><p>${state.session_id
      ? "Peça uma tarefa ou pergunte sobre a página. A conversa continua na mesma aba."
      : "Peça uma tarefa ou faça uma pergunta. Jev encontra o destino quando necessário."}</p>`;
    container.append(welcome);
  }
  for (const message of state.messages || []) {
    const article = document.createElement("article");
    article.className = `chat-message ${message.role === "user" ? "user" : "assistant"}`;
    const name = document.createElement("span");
    name.className = "message-name";
    name.textContent = message.role === "user" ? "Você" : "Jev";
    const content = document.createElement("p");
    content.textContent = message.content || "Pensando…";
    article.append(name, content);
    if (message.kind === 'input' && state.pending_input && message === state.messages.at(-1)) {
      const hint=document.createElement('p'); hint.textContent='Responda no chat. Não envie senhas ou códigos.';
      const cancel=document.createElement('button'); cancel.type='button'; cancel.textContent='Cancelar pergunta';
      const requestId=state.pending_input.id;
      cancel.disabled=busy;
      cancel.addEventListener('click',()=>perform(()=>call('input',{request_id:requestId,cancel:true})));
      article.append(hint,cancel);
    }
    if (message.verification) {
      const badge = document.createElement("span");
      badge.className = `verification ${message.verification.satisfied && !message.verification.stale ? "verified" : "unverified"}`;
      badge.textContent = message.verification.stale ? "Página alterada — verificação desatualizada" : message.verification.satisfied ? "Confirmado na página" : "Conclusão não confirmada";
      article.append(badge);
      if (message.verification.checks?.length) {
        const checklist = document.createElement("details");
        checklist.open = !message.verification.satisfied || message.verification.stale;
        const summary = document.createElement("summary");
        summary.textContent = "Conferência do pedido";
        checklist.append(summary);
        for (const check of message.verification.checks) {
          const item = document.createElement("p");
          const labels = { confirmed: message.verification.stale ? "Confirmado anteriormente" : "Confirmado", not_met: "Não atendido", unknown: "Não foi possível verificar" };
          item.textContent = `${check.requirement} — ${labels[check.status]}\n${check.reason}\n${check.evidence.join("\n")}`;
          checklist.append(item);
        }
        article.append(checklist);
      }
      if (message.verification.evidence?.length) {
        const evidence = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = "Ver evidências";
        const quotes = document.createElement("p");
        quotes.textContent = message.verification.evidence.join("\n");
        evidence.append(summary, quotes);
        article.append(evidence);
      }
    }
    if (message === state.messages.at(-1) && state.can_recheck) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "Verificar resultado novamente";
      button.className = "recheck-result";
      button.disabled = busy;
      button.addEventListener("click", () => perform(() => call("verify")));
      article.append(button);
    }
    container.append(article);
  }
  if (state.approval) {
    const card = document.createElement("div");
    card.className = "approval-card";
    const label = document.createElement("p");
    label.textContent = `Ação: ${state.approval.label}`;
    const url = document.createElement("small");
    url.textContent = state.approval.url;
    const buttons = document.createElement("div");
    buttons.className = "approval-actions";
    const approvalId = state.approval.id;
    for (const [action, text] of [["reject", "Recusar"], ["approve", "Confirmar ação"]]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = text;
      if (action === "approve") button.className = "primary";
      button.addEventListener("click", () => perform(() => call(action, { approval_id: approvalId })));
      buttons.append(button);
    }
    card.append(label, url, buttons);
    container.append(card);
  }
  if (follow) container.scrollTop = container.scrollHeight;
}
function render() {
  if (!state) return;
  renderChat();
  $("status").textContent = state.progress || "Descreva o que deseja fazer.";
  $("status-dot").classList.toggle("working", activePhases.has(state.chat_status));
  $("helper").textContent = `Modelo de texto · ${state.text_model}`;
  const page = state.page;
  const capture = preview?.session_id === state.session_id ? preview.capture : null;
  const imagePage = capture && (!state.viewport || (capture.page.w === state.viewport.width && capture.page.h === state.viewport.height)) ? capture.page : null;
  const d = state.decision || state.decisions?.at(-1);
  const elapsed = Math.max(0, Number(state.timing?.execution_ms ?? state.elapsed_ms) || 0) / 1000;
  const duration = elapsed < 60 ? `${elapsed.toFixed(2)} s` : `${Math.floor(elapsed / 60)} min ${(elapsed % 60).toFixed(2)} s`;
  $("task-time").hidden = state.started_at == null;
  $("task-time").textContent = `Tempo da execução: ${duration}`;
  $("empty").hidden = Boolean(page);
  $("screenshot").hidden = !imagePage?.screenshot;
  if (imagePage?.screenshot) {
    const source = `data:image/jpeg;base64,${imagePage.screenshot}`;
    if ($("screenshot").getAttribute("src") !== source) $("screenshot").src = source;
  }
  $("url").textContent = page?.url || "Nenhum site aberto";
  $("page-title").textContent = page?.title || "Aguardando um site";
  $("plan").textContent = state.goal || "";
  $("action-count").textContent = `${state.elements?.length || 0} elementos`;
  const chosen = page?.actions?.find((a) => a.id === d?.choice);
  $("choice-title").textContent = d ? chosen?.label || d.choice : "Aguardando uma tarefa";
  $("latency").textContent = d ? `${d.latency_ms} ms` : "—";
  $("confidence").textContent = d?.target_confidence != null ? percent(d.target_confidence) : "—";
  $("completion").textContent = d?.operation || "—";
  $("ranking-note").textContent = d ? "Classificados por Jev" : "Sem decisão";
  $("operation-choices").innerHTML = Object.entries(d?.operation_probabilities || {}).sort((a, b) => b[1] - a[1]).map(([name, p]) =>
    `<span class="operation-choice ${name === d.operation ? "best" : ""}">${escape(name)} <b>${percent(p)}</b></span>`).join("");
  const probability = (element) => d?.target_probabilities?.[element.index] ??
    Math.max(-1, ...(element.options || []).map((option) => d?.target_probabilities?.[option.index] ?? -1));
  const selectedIndex = d?.target?.split(":")[0];
  const elements = [...(state.elements || [])];
  if (d) elements.sort((a, b) => probability(b) - probability(a));
  $("choices").innerHTML = elements.map((element) => {
    const p = probability(element);
    return `<div class="choice ${selectedIndex === element.index ? "best" : ""}" data-action="${escape(element.index)}"><span class="choice-id">[${escape(element.index)}]</span><div class="choice-label">${escape(element.label)}<small>${escape(element.role)} · ${escape(element.operations.join(" / "))}${element.value ? " · " + escape(element.value) : ""}</small></div><span class="probability">${p >= 0 ? percent(p) : "—"}</span></div>`;
  }).join("") || '<p class="muted">Nenhum elemento observado.</p>';
  const targets = new Map();
  for (const action of imagePage?.actions || []) if (action.rect && !targets.has(action.node)) targets.set(action.node, action);
  $("targets").innerHTML = [...targets.values()].map((action, i) =>
    `<div class="target ${capture.revision === state.preview_revision && String(i + 1) === selectedIndex ? "selected" : ""}" data-action="${i + 1}" style="left:${100 * action.rect.x / imagePage.w}%;top:${100 * action.rect.y / imagePage.h}%;width:${100 * action.rect.w / imagePage.w}%;height:${100 * action.rect.h / imagePage.h}%"><span>${i + 1}</span></div>`).join("");
  $("targets").hidden = !$("overlays").checked;
  $("history").innerHTML = (state.history || []).map((h) => {
    const effect = h.execution === "uncertain" ? "Execução incerta" : h.execution === "not_executed" ? "Não executada" :
      h.execution === "requested" ? "Em execução" : h.page_changed == null ? "Resultado não observado" :
      h.page_changed ? "Página alterada" : "Sem alteração";
    return `<div class="trace-row"><span class="number">${String(h.step).padStart(2, "0")}</span><div>${escape(h.action)}${h.text ? ` <b>“${escape(h.text)}”</b>` : ""}</div><span class="time">${h.latency_ms} ms</span><span class="effect">${effect}</span></div>`;
  }).join("") || '<p class="muted">Nenhuma ação solicitada nesta mensagem.</p>';
  const attempts = state.action_count ?? state.history?.length ?? 0;
  $("step-count").textContent = `${attempts} ${attempts === 1 ? "tentativa" : "tentativas"} · ${duration}`;
  if(state.user_actions?.length)$("history").innerHTML += '<h3>Ações do usuário</h3>' + state.user_actions.map(h=>
    `<div class="trace-row"><span class="number">${h.request_id ?? '—'}</span><div>${escape(h.type)}</div><span class="effect">${escape(h.execution)}</span></div>`).join('');
  $("model-state").textContent = JSON.stringify(d?.request || { goal: state.goal, url: page?.url, text: page?.text }, null, 2);
  controls();
  renderManual();
}
function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (!busy) {
      try { await refresh(); }
      catch { $("status").textContent = "Conexão com o servidor interrompida."; }
    }
    schedulePoll();
  }, activePhases.has(state?.chat_status) ? 650 : 2500);
}
$("new-session").addEventListener("click", () => perform(async () => {
  await call("reset");
  $("message").value = "";
  $("message").focus();
}));
$("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const message = $("message").value.trim();
  if (!message || $("send").disabled) return;
  const messageId = crypto.randomUUID();
  const input = state?.pending_input;
  perform(async () => {
    if (input) await call('input', {value:message,request_id:input.id});
    else await call("message", { message, message_id: messageId });
  }, "Entendendo seu pedido…").finally(() => {
    if (state?.messages?.some((entry) => input ? entry.input_request_id === input.id : entry.turn_id === messageId)) $("message").value = "";
    controls();
  });
});
$("message").addEventListener("input", controls);
$("message").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("chat-form").requestSubmit(); }
});
$("stop").addEventListener("click", () => perform(() => call("pause")));
$("resume").addEventListener("click", () => perform(() => call("resume")));
$("overlays").addEventListener("change", render);
$("choices").addEventListener("pointerover", (event) => {
  if (preview?.capture?.revision !== state?.preview_revision) return;
  const id = event.target.closest("[data-action]")?.dataset.action;
  document.querySelectorAll(".target").forEach((target) => target.classList.toggle("selected", target.dataset.action === id || target.dataset.action === state?.decision?.target?.split(":")[0]));
});
document.querySelector('.inspector').addEventListener('toggle', () => {
  void refresh().catch(() => { $("error").textContent = 'Não foi possível atualizar os detalhes.'; $("error").hidden = false; });
});
async function pollPreview() {
  await refreshPreview();
  setTimeout(pollPreview, state?.manual?.status === 'active' ? 80 : 500);
}
let manualBusy=false;
function manualState(next) { stateEpoch+=1;state=next;render(); }
function renderManual() {
  const manual=state?.manual,owned=ownsManual(state?.session_id,manual),active=manual && manual.status!=='off';
  $("manual-panel").hidden=!manual?.available;
  $("manual-start").hidden=!!active;
  $("manual-start").disabled=manualBusy;
  $("manual-recover").hidden=!(owned&&manual?.status==='uncertain');
  $("manual-continue").hidden=!(owned&&manual?.status==='active'&&manual.can_resume);
  $("manual-continue").disabled=manualBusy||!manual?.input_ready;
  $("manual-exit").hidden=!(owned&&manual?.status==='active');
  $("manual-exit").disabled=manualBusy;
  $("manual-help").hidden=!(owned&&manual?.status==='active');
  $("manual-status").textContent=manual?.status==='requested'?'Preparando controle manual…':
    active ? owned ? manual.reason : 'Controle em outra interface' : manual?.suggested ? manual.reason : '';
  $("viewport").classList.toggle('manual-active',!!active&&owned);
  $("viewport").tabIndex=manual?.status==='active'&&owned?0:-1;
  if(active)$("targets").hidden=true;
}
for(const [id,name,body] of [['manual-start','start',{}],['manual-recover','start',{}],
  ['manual-continue','end',{resume:true}],['manual-exit','end',{resume:false}]]) {
  $(id).addEventListener('click',async()=>{
    if(manualBusy)return;manualBusy=true;renderManual();$("manual-error").hidden=true;
    try{manualState(await manualCommand(name,state,body));}
    catch(error){$("manual-error").textContent=error.message;$("manual-error").hidden=false;}
    finally{manualBusy=false;renderManual();}
  });
}
bindManualInput($("viewport"),()=>state,()=>preview?.session_id===state?.session_id?preview.capture:null,manualState,
  error=>{$("manual-error").textContent=error;$("manual-error").hidden=false;});
void pollPreview();
refresh().catch(() => { $("status").textContent = "Não foi possível conectar ao servidor local."; }).finally(schedulePoll);

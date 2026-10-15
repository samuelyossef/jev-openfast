# Changelog

## 0.2.0 (2026-10-14)

- Agent: finish price-range searches when matching priced results are visible, and stop alternating clicks sooner.
- Agent: preserve task requirements across decisions and verification, keep completion terminal, and report unmet requirements without restarting the browser task.
- Models: add per-task usage and cost tracking, a preventive task budget, and a bounded specialist review for complex tasks.
- UI: added an About page in Settings with app version, license and source link; removed the LinkedIn footer icon.
- UI: keep model usage below request timing and collapse it, task requirements, and observed elements by default.
- Viewer: keep the Internet preview available and temporarily disable Documents.
- Docs: rewritten README (quick start, usage, configuration, troubleshooting, scripts, credits) with a
  screenshot; design notes now cover the chat, handoff, manual control and preview layers.
- UI: the "page changed" verification badge and the default conversation title follow the interface language.

## 0.1.0

First public release.

- Chat interface (pt-BR, English, Español, Français) with local history, rename/archive/delete.
- Decision loop: page → indexed elements → operation + target → execution, with guards against stale pages.
- Independent verification of results, approval for external commitments, pause/resume/recheck.
- Live preview, and free navigation in the preview while no task is running.
- Manual control with a "take over" card when login, CAPTCHA, a code or personal data is needed;
  continuing resumes the task. Typed secrets are hidden from the assistant.
- Rate-limit-aware model requests (HTTP 429 with Retry-After), clearer stop reasons.
- `jev --doctor` setup diagnostics; Windows launcher with an isolated headless Chrome.

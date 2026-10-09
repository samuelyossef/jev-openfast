# Design: dynamic operation + target

This page describes the decision loop first, then the chat layer built on top of it.

The input is a natural-language goal. Every page observation builds an indexed table of accessible elements and their current values. One node receives one index, even when it supports both clicking and typing.

One TypeSafe request asks which operation to perform and which target would be appropriate for each available operation. The executor consumes only the target head corresponding to the selected operation. This avoids serial operation-then-target calls and rejects targets incompatible with the operation. Dropdown targets include a code-owned option index.
An observed single-line input may also offer `PRESS_ENTER`; it has its own target head and uses the same freshness, safety and execution logging path as other browser mutations. A failed independent check after `DONE` can feed visibly unmet requirements into one further decision round. Unknown outcomes, uncertain mutations and approved external commitments end without automatic corrective input. Manual recheck remains read-only.

Operation and target questions receive the same next-step rules. Target criteria include current values and checked/selected state. The questions run independently: a target cannot read the operation answer, so its premise explicitly names the operation it assumes.

TYPE_TEXT sends the goal, selected field, visible page context, and recent actions to a small LLM. Its JSON must contain exactly one valid `text` value. The code does not extract quoted literals. A value can be reused after a stale decision only while the entire helper input is identical, and is discarded after a successful mutation.

## Runtime

One browser-side DOM snapshot supplies common HTML/ARIA roles, names, values, visible text, and executable targets. A WeakMap gives each actual node a code-owned identity; a Map keeps the live references used for execution. Replaced elements receive new identities, disconnected references are pruned, and navigation starts a new cache. These IDs are not CDP backend node IDs. Geometry is always read again immediately before input.

The model sees visible text. Background focus emulation keeps animation frames running in the owned tab. Screenshots are optional and disabled in library calls by default; `screenshots=True` or `record_dir=...` enables them. The chat preview enables them explicitly. A continuous screencast can record a run separately.

Freshness compares semantic state instead of counting DOM mutations. Before clicks, text generation, typing and selection, guards compare the document, full URL, viewport, safe form values/states, selected target, and nearby form/dialog/row context. Scrolling, waiting, and completion use a full semantic comparison. The executor rechecks target visibility, enabled state, geometry, and click occlusion. Scoped guards intentionally permit unrelated visible content to change; this is a practical heuristic, not proof that arbitrary page changes are irrelevant to the goal. Chat checks the target before and after safety assessment and pauses after three consecutive stale choices without execution. Approval still checks the full observation and must be renewed when it changes.

Browser mutations are not retried by transport recovery. Each attempt is logged before input and marked executed, not executed, or uncertain; completed execution remains logged even when the next observation encounters a navigation. An interrupted native-select evaluation stops because its change event may already have fired. Typing uses a browser select-all command followed by CDP text insertion, so existing input contents are replaced.

The next observation waits for up to two animation frames or 50 ms after an interaction. Editable ARIA comboboxes instead wait for visible options, capped at 200 ms. This avoids paying for a prediction before autocomplete suggestions arrive. An explicit WAIT remains 100 ms; network loading is never fast-forwarded in the recording.

## The chat layer

The chat session (`chat.py`, `assistant.py`) wraps the decision loop above and owns one conversation and one browser tab.

1. **Routing.** One text-model request classifies the message as a *task* or a *question about the page*, picks the destination (an explicit URL, a named site's homepage, or a public search page) and restates the message as one self-contained instruction. The restatement resolves references from the conversation and fixes obvious misspellings but never adds requirements. It drives the agent and the checks; the chat keeps the user's own words. A question the open page cannot answer becomes a lookup task.
2. **Safety.** Before each click, text entry, selection or Enter, a read-only assessment classifies the action as safe, requiring confirmation (sending, submitting, publishing, buying, paying, booking, deleting, account changes) or uncertain. Anything not safe pauses for the user. Text generation for a field overlaps this assessment; neither response authorizes input on its own.
3. **Verification.** After `DONE` (a request, never proof), a fresh observation is checked against every requirement. Each check is `confirmed`, `not_met` or `unknown` and must cite exact text from the page; quotes are matched ignoring whitespace between text nodes, and a quote that is not on the page downgrades the check. The verdict stands while its evidence is still shown on the same URL, so unrelated page churn (ads, counters) does not invalidate it. If at least one requirement is visibly unmet, up to two corrective decision rounds run, never after an approved external commitment, an uncertain mutation or a human action.
4. **Model requests** never touch the browser, so they are retried (up to five attempts, honoring `Retry-After` on HTTP 429) before a task is stopped.

## Handoff and manual control

When the operation is `BLOCKED`, a speculative extra head of the same request names why (`LOGIN`, `CAPTCHA`, `VERIFICATION_CODE`, `PERSONAL_DATA`, `OTHER`). The reason only words the request for help; it never causes an action. A `BLOCKED` without a human-only reason (`OTHER` or none) is chosen again once per turn, with feedback to use a visible control that continues, dismisses or retries; only a second block asks the user. A task that makes no progress, or a text field whose value only the user has, opens the same handoff.

The chat then pauses and shows a card. **Take control** requests an exclusive owner token (kept in `sessionStorage`); once no worker is active, input events (pointer, wheel, keys, text) go through `manual.py`, which validates the frame the user saw, the sequence number and the owner, and never repeats an event that may have committed. Text typed by the human is registered with an in-page privacy script (`privacy.js`) so it is redacted from every later observation. **Continue with JEV** releases input, re-observes the page, records that a human acted and resumes the same run; **Exit manual control** leaves the task paused. Control expires after 60 seconds without input.

Between tasks the same mechanism provides free browsing: the first click or scroll on the preview starts manual control without a task to resume, and the next chat command ends it and starts from the page the user left.

## Preview

`preview.py` captures screenshots in a separate worker so neither the decision loop nor HTTP reads wait for an image. Captures are tied to the page revision, retried on transient timeouts and dropped if the page changes underneath them. In manual control the worker streams frames about every 200 ms. Screenshots are never sent to a model.

## Persistence and versions

Conversations live in a local SQLite file under `artifacts/` (title, messages, phase, last URL). An OpenRouter key saved from Settings is encrypted with Windows DPAPI under `artifacts/`; elsewhere it comes from `.env`. The app version is read from the installed package metadata (`pyproject.toml`) and shown in Settings, `jev --version` and `jev --doctor`.

## What changed after the first demo

The initial prototype used five manually prepared steps and copied quoted strings. That proved finite-choice browser execution but did not demonstrate task decomposition or text generation. The current policy removes that shortcut and uses the original goal throughout. Operation/target distributions replace the earlier flat-choice and lookahead arrangement.

The audit also found that treating every INPUT as editable misclassified checkboxes. Editable roles now control TYPE_TEXT availability. Tests cover checkbox/radio/button distinction, invalid operation/target outputs, stale decisions, text-cache invalidation, missing credentials, waits, and final-route verification.

## Boundaries

Sixty browser actions and 120 decision requests bound a run. Up to 250 action candidates are retained; truncated candidates cannot be selected. The service stays loopback-only, serializes browser-changing requests, and checks Host, Origin, and a local request token. Credentials remain server-side. Tabs share the existing Chrome profile.

The policy is generic, but two websites do not establish broad reliability. Name resolution covers common labels, ARIA references, and text; it is not the browser's full accessibility algorithm. Shadow roots, frames, canvas, uploads, nested scrolling, and complex keyboard interactions can block progress. Pages opened by an owned tab (`target=_blank`, `window.open`, pop-ups) are followed by polling `Target.getTargets` for tabs whose opener is owned, because browser_harness has no per-session event subscription. A valid action can still be wrong. Independent checks, rather than the model's DONE choice, determine whether the demonstrated task succeeded.

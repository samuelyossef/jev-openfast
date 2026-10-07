# JEV OpenFast Browser

[![CI](https://github.com/samuelyossef/jev-openfast-browser/actions/workflows/ci.yml/badge.svg)](https://github.com/samuelyossef/jev-openfast-browser/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A local browser assistant. You describe a task in chat; Jev opens a real Chrome tab, picks each
next action from the elements it actually sees, and checks the result before reporting success.

The model never writes selectors or code. Each page becomes a numbered table of elements; the
model returns an operation (`CLICK`, `TYPE_TEXT`, `SELECT`, `PRESS_ENTER`, `SCROLL_UP`,
`SCROLL_DOWN`, `WAIT`, `DONE`, `BLOCKED`) and a target from that table, and the code executes it.
A small text model writes words only when the operation is `TYPE_TEXT`.

> **Status: beta.** It drives a real browser and calls paid model APIs (OpenRouter), so use a key with a
> spending limit and watch what it does. Windows is the primary tested platform; macOS and Linux work
> through Chrome remote debugging and are less tested.

## Features

- **Chat** in Português (Brasil), English, Español and Français. Enter sends, Shift+Enter adds a new line.
- **One owned tab.** The assistant picks the site from your message (a URL, a site name, or a web
  search), runs automatically, and keeps using the same tab for follow-ups.
- **Page questions.** Asking about the open page reads a fresh observation. It never clicks or types.
- **Approval for external commitments.** Sending, submitting, publishing, buying, paying, booking,
  deleting or changing account settings pauses and asks you first. Approval is renewed if the page changes.
- **Independent verification.** After `DONE`, the page is checked again and the result is shown as
  confirmed, not met or unknown. A `DONE` choice is never treated as proof.
- **Pause, resume, recheck.** Rechecking is read-only.
- **Live preview** of the browser with execution details: operation, target, confidence, action
  history and per-stage timings.
- **Manual control.** Take over the tab with mouse, keyboard and text (logins, one-time codes,
  CAPTCHAs, canvas). No model calls happen while you drive, and passwords and codes you type are
  hidden from the assistant.
- **History.** Recent conversations in the sidebar; **Search** opens the full list with rename,
  archive and delete. Stored locally in SQLite. After a restart, conversations come back as
  interrupted and nothing is replayed.
- **Settings** (`/settings`): General (interface language, dark theme) and Model (OpenRouter API key).
  The footer shows the app version (read from the package, also available as `uv run jev --version`) with GitHub and LinkedIn links.

## Run it

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Google Chrome, and an
[OpenRouter](https://openrouter.ai/) API key. Node.js is only needed to rebuild the UI (the built UI is included).

```bash
git clone https://github.com/samuelyossef/jev-openfast-browser.git
cd jev-openfast-browser
uv sync
cp .env.example .env
```

Add `OPENROUTER_API_KEY` to `.env`, or paste it later in **Settings → Model**. Use a standard
OpenRouter inference key, not a Management key.

**Windows** (starts Jev with an isolated Chrome profile and Browser Harness):

```powershell
pwsh -File scripts/start_windows.ps1
```

**macOS / Linux:** connect Browser Harness to
Chrome (enable remote debugging; `uv run jev --doctor` checks the setup), then:

```bash
uv run --env-file .env jev
```

Open **http://127.0.0.1:8766** (use exactly this address; `localhost` is rejected). Change the port
with `TYPESAFE_DEMO_PORT`. Run `uv run jev --doctor` to check Python, the key, the port and Chrome.

### Troubleshooting

| Symptom | Fix |
| --- | --- |
| `OPENROUTER_API_KEY is required` | Add the key to `.env` or paste it in **Settings → Model**. |
| HTTP 401 from the provider | The key is wrong or revoked; use a standard inference key. |
| HTTP 429 | The provider is rate limiting; Jev retries, then asks you to resend. |
| Chrome debugging unreachable | Windows: start with `scripts/start_windows.ps1`. Others: enable remote debugging at `chrome://inspect/#remote-debugging`, then `uv run jev --doctor`. |
| Port 8766 already in use | Another Jev is running, or set `TYPESAFE_DEMO_PORT`. |
| 403 in the browser console | The page is from an older server run or another address. Reload `http://127.0.0.1:8766`. |

### Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | empty | Provider key. Can also be saved from Settings. |
| `TYPESAFE_MODEL` | `typesafe/jev-1.13` | Model that chooses operation and target. |
| `TEXT_MODEL` | `inception/mercury-2.5` | Small model for `TYPE_TEXT` and chat helpers. |
| `TEXT_MODEL_REASONING` | `none` | Reasoning setting for the text model. |
| `TYPESAFE_DEMO_PORT` | `8766` | Local server port. |

A key saved in the interface is encrypted with Windows DPAPI under `artifacts/` and is never
displayed again. Keys stay on the server; the browser UI never receives them. `.env` is git-ignored.

### Local only

The server listens on `127.0.0.1` and rejects any other `Host` header. It controls a real browser
profile, so do not expose it to a network or deploy it to a hosting service as is.

## Use it as a library

```python
from jev_ultrafast import Agent

with Agent(
    "https://en.wikipedia.org/wiki/Main_Page",
    "Find and open the Wikipedia article about Gödel's incompleteness theorems.",
) as agent:
    for state in agent.run():
        print(state["elapsed_ms"], state["status"])
```

Run it with `uv run --env-file .env python your_script.py`. Ready-made examples: `examples/run.py`
(any URL and goal) and `examples/flights.py` (a Google Flights search that stops at the results and
never books).

## How it works

```text
page → element table → one model request → operation + target → code executes → observe again
```

- One DOM snapshot supplies roles, names, values and visible text. Every node gets a code-owned identity.
- One request returns the operation and a target for each supported operation; only the selected
  operation's target is used.
- Before every mutation, guards compare the page, URL, form values and target context with what the
  model saw. A stale choice is discarded, never executed.
- Browser mutations are never retried. Each one is logged before it happens and marked executed,
  not executed or uncertain.
- Page text and URLs are treated as untrusted evidence, never as instructions.

More detail in [docs/design.md](docs/design.md).

## Project layout

| Path | What it is |
| --- | --- |
| `jev_ultrafast/agent.py`, `model.py`, `browser.py`, `snapshot.js` | The decision loop, model client, browser control and DOM reader. |
| `jev_ultrafast/chat.py`, `assistant.py` | Chat session, safety and verification helpers. |
| `jev_ultrafast/manual.py`, `privacy.js` | Manual control and secret scrubbing. |
| `jev_ultrafast/preview.py`, `timing.py` | Live preview capture and timing evidence. |
| `jev_ultrafast/conversations.py`, `secrets_store.py` | Local history and encrypted key storage. |
| `jev_ultrafast/demo.py` | The local HTTP server (`jev` command). |
| `frontend/` | React + Vite UI. Builds into `jev_ultrafast/web/`. |

## Development

```bash
cd frontend && npm ci && npm run build && npm run lint && cd ..
uv run ruff check .
uv run pytest
node --check jev_ultrafast/snapshot.js
node --check jev_ultrafast/static/app.js
node --check jev_ultrafast/static/manual.js
node --check jev_ultrafast/privacy.js
uv build
```

Rebuild the frontend after any UI change; the server serves `/` and `/settings` from
`jev_ultrafast/web/`. Tests are offline and never call paid APIs. `scripts/check_guards.py` and
`scripts/check_manual.py` exercise real controls in a local browser with mocked models.

## Limits

The DOM reader handles common HTML and ARIA controls, not the full accessible-name specification.
Shadow roots, frames, canvas, uploads, pop-up tabs and nested scrolling are outside automatic
mode; manual control can operate frames, canvas and keyboard widgets. Owned tabs share the existing
Chrome profile. Timing and benchmark numbers are in [docs/performance.md](docs/performance.md) and
[docs/chat-performance.md](docs/chat-performance.md); they come from a few runs of specific tasks
and are not a general reliability benchmark.

## Contributing and security

Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md). Report vulnerabilities privately as described in
[SECURITY.md](SECURITY.md). Release notes are in [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE) © 2026 Samuel Yossef / Copyxyz.

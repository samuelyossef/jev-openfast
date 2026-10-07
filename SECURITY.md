# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them privately through GitHub:
[Report a vulnerability](https://github.com/samuelyossef/jev-openfast-browser/security/advisories/new).
Include what you found, how to reproduce it, and the impact. You will get a response as soon as
possible, and credit in the fix if you want it.

## What this software does

JEV OpenFast Browser drives a **real browser** on your machine and sends page content to model
providers (OpenRouter). Treat it accordingly:

- **Local only.** The server binds to `127.0.0.1`, rejects other `Host` headers, requires a
  per-run token on every request and refuses cross-origin POSTs. Do not expose it to a network or
  deploy it as a public service.
- **Keys.** Your OpenRouter key stays on the server and is never sent to the browser UI. On Windows a
  key saved in Settings is encrypted with DPAPI under `artifacts/`; on other systems use `.env`.
  Both are git-ignored. Use a standard inference key with a spending limit.
- **Page content is untrusted.** Pages, titles, URLs and labels are treated as data, never as
  instructions, and the model cannot emit selectors or code. External commitments (sending,
  publishing, buying, deleting) require your confirmation.
- **Manual control.** Text you type while you drive is redacted from what the assistant sees, and no
  model calls happen while you control the page.
- **Your Chrome profile.** On Windows the launcher uses an isolated profile in `artifacts/`. Other
  setups attach to a Chrome with remote debugging enabled, which exposes that browser to local
  processes; use a dedicated profile.

## Supported versions

Only the latest release on `main` receives fixes.

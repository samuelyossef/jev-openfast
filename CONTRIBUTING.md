# Contributing to JEV OpenFast Browser

Thanks for helping. Please read the [README](README.md) and [docs/design.md](docs/design.md) first;
the design is deliberately small: **page → indexed elements → operation + target → execution**.

## Ground rules

These keep the agent safe and predictable, and pull requests are reviewed against them:

- The input is one natural-language goal. No site-specific plans and no hard-coded field values.
- Targets map to observed elements and supported operations. The model never emits selectors or code.
- Never retry a browser mutation. Log it before it happens, observe afterwards.
- A `DONE` choice is not proof of success; outcomes are verified independently.
- Keys stay server-side. Never commit `.env`, keys or anything under `artifacts/`.
- **Tests must not call paid APIs.** Mock the model; use the offline fixtures.

## Setup

```bash
git clone https://github.com/samuelyossef/jev-openfast-browser.git
cd jev-openfast-browser
uv sync
cp .env.example .env   # add OPENROUTER_API_KEY only if you run the app against real models
```

## Before you open a pull request

```bash
uv run ruff check .
uv run pytest
node --check jev_ultrafast/snapshot.js
node --check jev_ultrafast/static/manual.js
cd frontend && npm ci && npm run lint && npm run build
```

- If you change `frontend/`, rebuild and commit the output in `jev_ultrafast/web/`; users run the
  built UI and do not need Node.
- If you change browser control (`browser.py`, `snapshot.js`, `manual.py`), also run
  `uv run python scripts/check_guards.py` and `scripts/check_manual.py` against a local Chrome.
- Keep changes focused, add or update tests, and match the surrounding style (ruff, 120 columns).
- Update the README or `docs/` when behavior or configuration changes.

## Versions

The version lives in one place, `version` in `pyproject.toml` ([semantic versioning](https://semver.org/)).
The app reads it from the installed package metadata: it shows in **Settings**, in `uv run jev --version`,
in `uv run jev --doctor` and in the server banner. To release: bump `version`, add a section to
[CHANGELOG.md](CHANGELOG.md), run `uv sync` so the installed metadata updates, then tag `vX.Y.Z`.

## Reporting bugs and ideas

Open an issue and include the output of `uv run jev --doctor`. For vulnerabilities, follow
[SECURITY.md](SECURITY.md) instead of opening a public issue.

## Conduct and license

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md). By contributing you agree that
your contribution is licensed under the project's [MIT License](LICENSE).

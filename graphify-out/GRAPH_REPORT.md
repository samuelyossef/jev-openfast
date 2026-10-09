# Graph Report - graphify-0ae37a  (2026-10-06)

## Corpus Check
- 35 files · ~92,665 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 151 nodes · 272 edges · 13 communities (10 shown, 3 thin omitted)
- Extraction: 92% EXTRACTED · 8% INFERRED · 0% AMBIGUOUS · INFERRED: 23 edges (avg confidence: 0.86)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Tests & Stale Pages
- Agent Core Class
- Agent Loop & CLI
- Browser Execution
- Project Rules & Demo
- Performance Evidence
- Flights Demo & Verify
- Design Rationale
- Web UI (app.js)
- Demo Rendering
- Fixture Rendering
- Package

## God Nodes (most connected - your core abstractions)
1. `StalePage` - 19 edges
2. `Agent` - 15 edges
3. `Browser` - 14 edges
4. `browser_operation()` - 11 edges
5. `page()` - 11 edges
6. `choose()` - 9 edges
7. `field_text()` - 8 edges
8. `verify()` - 7 edges
9. `command()` - 7 edges
10. `Jev Ultrafast` - 7 edges

## Surprising Connections (you probably didn't know these)
- `Jev Ultrafast` --references--> `Banner: Jev Ultrafast, Choose. Click. Gone.`  [EXTRACTED]
  README.md → docs/banner.svg
- `Run boundaries and limitations` --semantically_similar_to--> `Evidence and limits (25% faster, 3/3 verified)`  [INFERRED] [semantically similar]
  docs/design.md → README.md
- `main()` --uses--> `StalePage`  [INFERRED]
  scripts/check_guards.py → jev_ultrafast/browser.py
- `test_stale_decision_is_consumed_before_any_mutation()` --uses--> `StalePage`  [INFERRED]
  tests/test_agent.py → jev_ultrafast/browser.py
- `test_missing_text_credential_stops_before_guessing()` --calls--> `field_text()`  [EXTRACTED]
  tests/test_agent.py → jev_ultrafast/model.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Flights demo evidence set** — readme_flights_demo, docs_demo_gif, docs_flights_result, docs_performance_report [INFERRED 0.75]
- **Jev decision loop: snapshot, operation+target, execution** — readme_snapshot_js, readme_model_py, readme_browser_py, readme_agent_py [INFERRED 0.85]

## Communities (13 total, 3 thin omitted)

### Community 0 - "Tests & Stale Pages"
Cohesion: 0.13
Nodes (27): fixture, fingerprint(), A decision no longer refers to the observed page., StalePage, parametrize, choice(), decision(), page() (+19 more)

### Community 1 - "Agent Core Class"
Cohesion: 0.15
Nodes (11): BaseHTTPRequestHandler, Agent, close_browser(), command(), Handler, load_environment(), main(), Loopback-only inspector for the Jev browser agent. (+3 more)

### Community 2 - "Agent Loop & CLI"
Cohesion: 0.18
Nodes (14): uv run --env-file .env python examples/run.py --url URL --goal 'A narrow goal, The complete agent loop. Typed choices, observable state, bounded execution., action_space(), choose(), field_context(), field_text(), post_json(), TypeSafe makes choices; an optional small OpenAI-compatible model writes field… (+6 more)

### Community 3 - "Browser Execution"
Cohesion: 0.23
Nodes (6): Browser, browser_operation(), Observed actions through Browser Harness; one CDP session, no per-step…, Jev chooses an observed action. Code owns execution., main(), Local-browser freshness/execution regressions. No model calls or external…

### Community 4 - "Project Rules & Demo"
Cohesion: 0.15
Nodes (15): Independent outcome verification (DONE is not proof), Model never emits selectors or code, Never retry a browser mutation, Project rules (AGENTS.md), Banner: Jev Ultrafast, Choose. Click. Gone., Demo GIF of Google Flights run at 1x, Text helper cache on identical input, Flights result screenshot (Zurich to London, 7.07 s, checklist) (+7 more)

### Community 5 - "Performance Evidence"
Cohesion: 0.14
Nodes (14): Run boundaries and limitations, Changes after first prototype (no prepared steps), Inspector UI screenshot (indexed elements, CLICK 76%), Matched runtime comparison (9.450s to 7.092s), Other checks (Wikipedia, hotel fixture), Development attempts and freshness regression, Prepared-step recording report (12.884 s), Faster on the real web (performance report) (+6 more)

### Community 6 - "Flights Demo & Verify"
Cohesion: 0.24
Nodes (6): main(), Live Google Flights search. Calls TypeSafe; never selects or books a flight., Independent checks on the resulting page, not the model's DONE answer., verify(), One live measured flight search; freeze source externally to compare revisions., A measured live run with continuous CDP screencast; original timestamps…

### Community 7 - "Design Rationale"
Cohesion: 0.25
Nodes (9): Dynamic operation + target design, Semantic freshness guards, WeakMap/Map node identity cache, agent.py loop, browser.py execution, model.py heads and text generation, snapshot.js atomic DOM snapshot, Speculative target heads, one round trip (+1 more)

### Community 8 - "Web UI (app.js)"
Cohesion: 0.46
Nodes (7): call(), controls(), escape(), goals, percent(), perform(), render()

## Knowledge Gaps
- **14 isolated node(s):** `goals`, `jev-ultrafast`, `Speculative target heads, one round trip`, `Browser Harness (Chrome CDP)`, `browser.py execution` (+9 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **3 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `StalePage` connect `Tests & Stale Pages` to `Agent Core Class`, `Agent Loop & CLI`, `Browser Execution`?**
  _High betweenness centrality (0.067) - this node is a cross-community bridge._
- **Why does `Agent` connect `Agent Core Class` to `Tests & Stale Pages`, `Agent Loop & CLI`, `Browser Execution`, `Flights Demo & Verify`?**
  _High betweenness centrality (0.059) - this node is a cross-community bridge._
- **Why does `Browser` connect `Browser Execution` to `Agent Core Class`, `Agent Loop & CLI`?**
  _High betweenness centrality (0.037) - this node is a cross-community bridge._
- **Are the 4 inferred relationships involving `StalePage` (e.g. with `Agent` and `main()`) actually correct?**
  _`StalePage` has 4 INFERRED edges - model-reasoned connections that need verification._
- **Are the 2 inferred relationships involving `Agent` (e.g. with `Browser` and `StalePage`) actually correct?**
  _`Agent` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `goals`, `jev-ultrafast`, `Speculative target heads, one round trip` to the rest of the system?**
  _14 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Tests & Stale Pages` be split into smaller, more focused modules?**
  _Cohesion score 0.1349206349206349 - nodes in this community are weakly interconnected._
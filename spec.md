# spec.md

## Project: AML Detection Agent ("vigil")

> **Built and running.** This began as a pre-implementation spec and is now a description of a
> system that exists end to end: agent loop, five ML tools, FastAPI, React UI, SQLite audit trail,
> 430 backend and 10 frontend tests. Every `[PLANNED, NOT BUILT]` tag is gone, because none was true any
> more. Where this file and the code disagree, the code wins and this file should be corrected.
> See `README.md` to run it and `backend/docs/ml_spec.md` for the ML workstream.

---

## Decisions Log

**Read this section first.** Kept as a log rather than deleted, so a future session can see *why*
each call was made. Several of these interlock (2 and 3 especially), so don't re-litigate one
without checking the others.

1. **Team: 2-person.** Confirms the React+FastAPI split was the right call: two humans genuinely
   dividing frontend/backend work, not a solo dev who'd have been better off with Streamlit.
   Commit/branch traceability is required by the rules for 2-person teams.
2. **Route list: `/analyze` + `/escalate`, cut `/customer/{id}/risk-profile`.**
   - `POST /api/v1/analyze`: core, unchanged.
   - `POST /api/v1/escalate`: **built.** A judge clicks "escalate" on a flagged item in the UI and
     it persists the chosen action to the `flags` table. A genuine interactive demo moment rather
     than a stub.
   - `GET /api/v1/customer/{id}/risk-profile`: **cut.** The problem statement's entity-lookup case
     ("Is customer 4521 suspicious?") is already served by `/analyze` routing to a single-entity
     execution plan. A dedicated profile page/route would be nice drill-down UX but isn't required
     by any hard requirement. Per-row drill-down inside the flagged-items table covers the case
     without a second page. Don't revisit.
3. **SQLite: yes, minimal.** Two tables: `queries` (every `/analyze` call: query text, timestamp,
   intent detected, tools invoked/skipped) and `flags` (every flagged item). Written by `/analyze`
   after each run, updated by `/escalate`. Cheap (stdlib `sqlite3`, one file, no server) and buys
   two real things: an actual audit trail, which fits a compliance product, and a working
   `/escalate` endpoint instead of a stub. See Database Schema below for the full shape.
4. **LLM: Gemini, single provider.** The agent runs on Google's Gemini API, model alias
   `gemini-flash-lite-latest`, read from `GOOGLE_API_KEY`. Chosen for a free tier large enough to
   demo on, and for low latency across the several sequential calls one run makes. The
   multi-provider abstraction that briefly existed here was removed: one provider, one key, one
   code path, and nothing in the setup steps a judge has to choose between.
   - The alias rather than a pinned model id is load-bearing. Pinned 2.0/2.5 ids return 404 or a
     zero-quota 429 on new keys, and `gemini-flash-latest` allows only 20 requests/day, which one
     query can spend a quarter of.
   - Practical gotcha carried into the README's setup steps: the Gemini key must come from
     **aistudio.google.com/apikey**, not the Google Cloud Console. Cloud-Console-issued keys land
     in a project with a zero free-tier quota grant and fail with `429 RESOURCE_EXHAUSTED`
     regardless of enabling the API.
   - Known cost of a small model, accepted: `gemini-flash-lite` follows routing instructions
     imperfectly. A pattern search may report one account where the prompt asks for three. A larger
     model fixes it and shrinks the free-tier quota to unusable.
4a. **Orchestration: hand-rolled tool-calling loop, not LangGraph.** FastAPI makes the API call
   itself, and all SDK translation lives in `agent/providers.py`, so the loop speaks one internal
   message shape and never imports the SDK. That is what keeps the whole agent testable against a
   fake client with no key and no network. See "Agent Orchestration" below for the shipped shape.
5. **Feedback-driven threshold refinement stretch feature: cut, not attempted.** It was flagged as
   absent from the actual problem statement text, so cutting it entirely removed the risk of it
   quietly eating the last hours. What did ship instead is `scripts/tune_blend.py`, an offline
   search over the risk constants against an explicit objective, which is the defensible half of
   the same idea without a feedback loop nobody would have had time to validate.
6. **Kaggle dataset: manual browser download, documented in README.** Raw CSVs are not committed
   (large, and `.gitignore`-worthy alongside the derived Parquet files). Manual download needs zero
   extra dependency and no Kaggle API credentials; you fetch it once rather than scripting a
   repeatable pipeline run.
7. **Repo: public, no client references.** Confirmed before the first commit that the repo name
   carries zero SG/Societe Generale/SocGen/SGGSC references and visibility is public.

---

## Stack (as built)

```
Frontend  : React 19 + Vite 8, TypeScript
Styling   : Tailwind CSS v4, Radix UI primitives, lucide-react icons, light + dark theme
State     : React built-ins (useState/useContext), no state library; one page, tab state only
Charts    : Plotly.js via react-plotly.js
Backend   : FastAPI (Python) + Uvicorn
Agent     : Hand-rolled LLM tool-calling loop, NOT LangGraph
LLM       : Gemini, `gemini-flash-lite-latest`, key in `GOOGLE_API_KEY` (Decisions Log item 4)
Dev tool  : An AI coding assistant was used to help *write* this code; a dev-time tool, disclosed
            in README per competition rule 3, and it does not run at query time
Database  : SQLite (`queries` + `flags`, audit trail + working `/escalate`). Bulk data stays in
            flat files (Parquet); SQLite is only the small persisted-history layer.
Query     : DuckDB directly over Parquet (aggregation/threshold queries)
Data proc : Pandas + NumPy + PyArrow (one-time enrichment over 5.08M rows)
Detection : scikit-learn (Isolation Forest, LOF) + NumPy z-score + plain-Python rules engine
Explain   : Template-based NL generation tied to the firing rule/feature/threshold, no SHAP/LIME
Validation: Pydantic (all structured agent output)
Caching   : functools.lru_cache (repeat-query caching) + precomputed baselines at enrichment time
Async     : None. Streaming is a threaded generator behind SSE; no Celery/Airflow
Testing   : Pytest (backend, 16 modules) + Vitest/Testing Library (frontend)
Auth      : None, no login, local single-user demo tool
Deploy    : None, explicitly out of scope, runs locally only
```

---

## Folder Structure (as built)

```
vigil/
├── README.md
├── .gitignore
├── spec.md
├── assets/                            # README screenshots, committed
├── backend/
│   ├── .env                           # gitignored
│   ├── .env.example
│   ├── requirements.txt
│   ├── main.py                        # FastAPI entrypoint, startup key-check + db init
│   ├── db.py                          # sqlite3: schema, insert, escalate, stats, retention
│   ├── schemas.py                     # Pydantic: AgentResult + flag/evidence shapes
│   ├── aml_agent.db                   # gitignored, created at runtime (SQLITE_PATH)
│   ├── api/
│   │   └── routes/
│   │       └── agent.py               # /analyze, /analyze/stream, /escalate(+undo),
│   │                                  #   /escalations, /stats
│   ├── agent/
│   │   ├── providers.py               # Gemini client, get_client() -> chat_with_tools
│   │   └── loop.py                    # tool_calling_loop, system prompt, flag recovery
│   ├── tools/                         # public tool surface; re-exports from ml/
│   │   ├── eda.py feature_eng.py anomaly.py risk.py explain.py
│   ├── scripts/                       # one-time build pipeline, run in this order
│   │   ├── enrich.py                  # raw CSV -> enriched Parquet
│   │   ├── train_models.py            # -> data/models/*.joblib + metadata.json
│   │   ├── build_rule_hits.py         # -> data/HI-Small_Rule_Hits.parquet
│   │   ├── validate_risk_levels.py    # risk-level precision/recall against ground truth
│   │   ├── tune_blend.py              # searches the risk blend constants
│   │   └── live_check.py              # one end-to-end run against the live API
│   ├── notebooks/
│   │   └── 01_exploration.ipynb       # Phase 1 EDA notebook
│   ├── data/                          # everything under DATA_DIR, all gitignored
│   │   ├── raw/                       # HI-Small_Trans.csv, _accounts.csv, _Patterns.txt
│   │   ├── HI-Small_Enriched.parquet  # generated by enrich.py; Agent_Ready.parquet cut
│   │   ├── HI-Small_Rule_Hits.parquet # generated by build_rule_hits.py
│   │   └── models/                    # fitted detectors + metadata + validation artifacts
│   ├── ml/                            # ML implementation (12 modules)
│   │   ├── data.py                    # DATA_DIR paths + shared lru_cache'd DuckDB connection
│   │   ├── tools.py                   # TOOL_SCHEMAS + dispatch table; loop.py registers these
│   │   ├── eda.py feature_eng.py anomaly.py risk.py explain.py   # the five tools
│   │   └── rules.py features.py dates.py cache.py validation.py  # support modules
│   ├── docs/                          # ml_spec.md, phase1-7.md, ml_audit.md
│   └── tests/                         # 16 pytest modules
└── frontend/
    ├── .env                           # gitignored
    ├── .env.example
    ├── package.json
    ├── vite.config.ts                 # port 5174, strictPort
    ├── index.html
    └── src/
        ├── App.tsx                    # one page, three tabs, streaming state
        ├── api.ts                     # fetch + SSE client
        ├── theme.tsx                  # light/dark provider
        ├── types.ts                   # mirrors schemas.py
        ├── lib/csv.ts                 # flagged-items CSV export
        ├── mocks/mock_agent_result.json
        └── components/
            ├── ExecutionSummaryPanel.tsx  FlaggedItemsTable.tsx  RiskCharts.tsx
            ├── DashboardView.tsx          EscalationsView.tsx
            ├── chat/                      # composer, message, scroll area, thinking indicator
            ├── layout/                    # sidebar, theme toggle
            └── ui/                        # badge, button, collapsible, dialog, textarea
```

`DATA_DIR` / `SQLITE_PATH` default to `./data` and `./aml_agent.db`, which resolve relative to
`backend/`'s working directory when `uvicorn` runs from there. The README setup steps call this out
so a fresh clone doesn't hit a path-not-found from running `uvicorn` out of the repo root.

---

## Agent Orchestration (as built)

The runtime agent is a **hand-rolled tool-calling loop against the Gemini SDK**, not LangGraph.
FastAPI makes the API call itself, so a judge's setup is `pip install -r requirements.txt`, drop a
Gemini key in `.env`, run.

```
POST /api/v1/analyze  →  read GOOGLE_API_KEY from .env  →  get_client()  →
                          tool_calling_loop(client, query, on_event=None)  →  validate against
                          the AgentResult Pydantic schema  →  persist  →  return to frontend
```

- **Tool functions**: the five ML tools (EDA, feature engineering, anomaly detection, risk
  classification, explanation) are plain Python functions dispatched through `ml/tools.py`, not
  shelled-out scripts. Escalation is app-owned and happens through `/escalate` rather than as a
  model-callable tool, since it records a *human* decision.
- **SDK boundary**: `get_client()` returns a thin wrapper exposing `chat_with_tools(...)`. All
  translation between the loop's own message shape and Gemini's `Content`/`Tool` types lives in
  `agent/providers.py`, so `agent/loop.py` never imports the SDK and can be tested against a fake
  client with no key and no network.
- **Transparency feed**: each dispatch is logged as it happens (name, args, result). That log is
  the source for the "execution summary: tools invoked / skipped / why" panel, and the same data is
  persisted to SQLite. More code than a framework's built-in trace, but fully under our control.
- **Bounded**: `MAX_ITERATIONS = 14` turns, and each tool result is capped at
  `MAX_TOOL_RESULT_CHARS = 20,000` before it enters the context. Truncation is reported to the
  model rather than silently applied, so it can narrow scope and retry instead of reasoning on a
  fragment.
- **Result sanitising**: tool output is coerced to strictly JSON-serialisable primitives before it
  goes back to the model. NaN/Infinity become null, numpy scalars become native ints and floats.
  Without this, `np.int64(37)` reached the model as the string `"37"`.

### Three failure modes the loop closes

Each of these cost a whole run before it was fixed, and each is covered by a test:

1. **Findings described in prose, `flagged_items: []` returned.** The table renders empty and there
   is nothing to escalate, so a successful analysis reads as a failed run. Flags are rebuilt from
   the `risk` and `explain` results the loop already captured. `LOW` is excluded deliberately:
   "we looked and it is fine" is a real answer, and flagging it would fabricate a finding.
2. **Iteration budget exhausted mid-chain.** `reply.text` is empty, so intent, filters, summary and
   flags all fall back to defaults. The loop now spends one final turn with tools closed off.
3. **JSON wrapped in prose or a code fence.** A strict whole-string parse discarded everything.
   Parsing falls back to the first balanced top-level object, brace-counted so explanations
   containing braces don't truncate it.

### Evidence

After flags are settled, the loop builds two chart series (daily activity for the flagged accounts,
and their rule-hit mix) through the same `eda` tool, deterministically via DuckDB. No extra model
calls, so it costs nothing in tokens or latency beyond two indexed queries. It returns `None`
rather than empty series when there is nothing to plot, so the UI omits the charts instead of
rendering empty axes.

### Time anchoring

The dataset's first and last timestamps are read at import and injected into the system prompt.
Relative expressions ("the last 30 days") resolve against the end of the data (2022-09-18) rather
than wall-clock now, which otherwise makes every relative window an empty result years in the past.

---

## Database Schema (as built)

Two layers.

### Bulk data: flat files, not a database
- Input: raw Kaggle CSVs (`HI-Small_Trans.csv`, `HI-Small_accounts.csv`, `HI-Small_Patterns.txt`),
  gitignored, not committed
- Working data: `HI-Small_Enriched.parquet` (5,078,345 × 32, 352.6 MB) plus
  `HI-Small_Rule_Hits.parquet`. `HI-Small_Agent_Ready.parquet`, originally planned as a second
  file, was **cut** during ML implementation: it was a byte-identical copy, and `feature_eng`
  queries the enriched file directly.
- Queried via DuckDB over the Parquet files, through one `lru_cache`d connection in `ml/data.py`.
  **Not** preloaded into memory at FastAPI startup: the ML tools own the connection, so a startup
  load would be a second copy of the same data. Revisit only if per-query open time measurably
  hurts.

Enrichment column list (matches `phase2.md`):

| Field | Description |
|---|---|
| `is_laundering` | Boolean, raw `Is Laundering==1` label, authoritative |
| `is_suspicious` | Boolean, pattern-file match, a **subset** of `is_laundering` (3,209 of 5,177 rows), kept as a separate column rather than a synonym |
| `aml_pattern` | One of 8 motifs (FAN-OUT, FAN-IN, CYCLE, GATHER-SCATTER, SCATTER-GATHER, BIPARTITE, STACK, RANDOM), else NORMAL |
| `hour`, `day_of_week` | Extracted from timestamp |
| `amount_category` | micro / small / medium / large / xlarge / xxlarge, quantile buckets (`pd.qcut`, 6 equal-frequency buckets on `log1p(Amount Received)`, ~846k rows/bucket) |
| `is_self_loop` | Boolean, `From Account == To Account`, 591,212 rows (11.64%). Kept and flagged rather than dropped, so `txn_count`/`avg_amount` don't silently shrink |
| `currency_mismatch` | Boolean, Payment Currency ≠ Receiving Currency |
| `payment_format_risk` | `P(is_laundering \| format)`, the strongest single class-separator found (86.6% of laundering rows are ACH vs 11.8% normal) |
| `txn_count`, `total_volume`, `avg_amount`, `std_amount` | Per-sender baseline stats |
| `unique_receivers` | Distinct receiver count for this sender |
| `deviation_from_avg` | z-score of a transaction against its sender's own baseline, div-by-zero guarded |
| 6 account-join columns | sender/receiver bank name, entity id, entity name |

### Persisted history: SQLite

A single `aml_agent.db` file, stdlib `sqlite3`, no server. Two tables.

**`queries`**: one row per `/analyze` call, the audit trail of what the agent decided to do.
- `id`: integer primary key autoincrement (single local writer, no need for UUIDs)
- `query_text`, `timestamp`
- `intent_detected`, `filters_applied` (JSON text), `tools_invoked` (JSON text),
  `tools_skipped` (JSON text): mirrors `execution_summary` in `AgentResult`

**`flags`**: one row per flagged item across all queries, updated when a human escalates one.
- `id`: integer primary key autoincrement
- `query_id`: foreign key → `queries.id`
- `customer_id`, `transaction_id` (TEXT: the Kaggle data has no transaction key, so this is a
  deterministic `tx_`-prefixed hash of the identifying tuple)
- `amount` (REAL), `timestamp` (TEXT)
- `risk_level` (TEXT, one of four), `pattern_detected`, `anomaly_score`, `explanation`
- `escalation_action`: the agent's *recommended* action (MONITOR/REVIEW/REPORT), set at insert time
- `escalated_at`: nullable, set by `POST /api/v1/escalate` when a human acts on it, which is what
  distinguishes "the agent recommended REPORT" from "someone actually clicked escalate"
- `escalation_note`: nullable, the analyst's own reasoning. Added by migration in `init_db()` rather
  than to `SCHEMA`, since `CREATE TABLE IF NOT EXISTS` is a no-op on a database that already exists
  and every running install would otherwise break.

Indexes on `flags.query_id` (FK lookup), `flags.transaction_id` and `flags.customer_id`.

**Retention:** `purge_old_queries(conn, keep=500)` trims to the most recent 500 runs and their
flags. Not a compliance retention policy, just a bound on a demo database that would otherwise grow
for the life of the file.

> **Contract note.** `amount` and `timestamp` were not in this spec's original `flags` list. Both
> were added during implementation and are genuinely needed rather than speculative: the flagged-items
> table shows amount, and the temporal chart needs a timestamp per flagged item. They are persisted
> as columns, not merely returned in the API response.
>
> **Known limitation, accepted:** `POST /api/v1/escalate` overwrites `escalation_action` with the
> human's chosen action, so the agent's *original* recommendation is not preserved after someone
> escalates. Fine for a demo audit log. Surfacing "agent said REPORT, human downgraded to REVIEW"
> would need a third column; deliberately not built.

---

## RBAC Model

**N/A, omitted.** No authentication, no user accounts, no roles. A single-user local tool; anyone
running it locally has full access to every function.

---

## API Endpoints (as built)

### `agent` router, prefix `/api/v1`
File: `backend/api/routes/agent.py`

- `POST /api/v1/analyze` → Body `{query: str}`. Runs the full agent loop (intent parse → dynamic
  tool selection → execution → structured result), writes a `queries` row and one `flags` row per
  flagged item, and returns an `AgentResult` with `flag_id` populated per item so the UI's Escalate
  button has a row to reference.
- `POST /api/v1/analyze/stream` → same body and same run, streamed as Server-Sent Events: one
  `tool_start`/`tool_end` event per dispatch, then the finished `AgentResult`. Added because a run
  is 5-15s of sequential tool calls and the UI could otherwise only show a spinner. `/analyze` is
  unchanged and remains the fallback.
- `POST /api/v1/escalate` → Body `{flag_id: int, action: str, note?: str}`. Sets
  `flags.escalated_at` and returns confirmation. The optional note is the analyst's own reasoning:
  an audit trail that records the decision but not the why is half a record.
- `POST /api/v1/escalate/undo` → Body `{flag_id: int}`. Withdraws an escalation. Escalating is one
  click, so a mis-click writes a decision nobody meant to take; the flag survives, only the human
  action is cleared.
- `GET /api/v1/escalations` → the audit trail: every flag a human escalated, newest first, each
  joined back to the query that surfaced it. Read from SQLite rather than session state, so it
  survives a reload and spans conversations.
- `GET /api/v1/stats` → dashboard aggregates over every run recorded: totals, escalation rate,
  flags by risk level, motif frequency, tool usage, most-flagged accounts, recent queries.

**Error mapping.** `ProviderError` carries the upstream status. A 429 passes through as 429 so the
UI can distinguish "slow down" from "broken"; anything else upstream becomes a 502, since the
failure isn't the caller's fault.

**Divergence from the original two-route plan.** The three read routes and the stream were added
during implementation. None of them changes the agent: `/stats` and `/escalations` only read what
`/analyze` already persisted, and `/analyze/stream` runs the identical loop. They exist because the
data was being recorded and never shown.

**Cut:** `GET /api/v1/customer/{customer_id}/risk-profile` (see Decisions Log item 2). Entity-lookup
queries route through `/analyze` instead.

No auth guards (no auth exists). No rate limiting (single local user). CORS accepts any localhost
port by regex rather than pinning 5174, since a mismatch surfaces as an opaque browser error that
points nowhere near the port. Safe precisely because nothing is deployed and there is no auth.

---

## Frontend (as built)

One page, no router. Tab state only, so `react-router` was never needed.

```
/  → App.tsx   tabs: Investigate | Escalations | Dashboard
```

**Investigate** is a chat surface. Each turn renders:
- the query, then a decision-flow panel: intent detected, filters applied, tools invoked in order,
  and tools skipped with the reason each was skipped. This is the "transparent decision flow"
  differentiator and is the first thing a judge reads.
- a flagged-items table: customer, transaction, amount, risk level, pattern, explanation,
  recommended action. Sortable and filterable, per-row drill-down, CSV export, and an Escalate
  button per row that opens a dialog for the optional note.
- evidence charts (Plotly): daily activity for the flagged accounts, and their rule-hit mix.
- while a run is in flight: live tool events over SSE, and a cancel control. Cancelling aborts the
  in-flight request rather than leaving it to finish invisibly.

**Escalations** renders `GET /escalations`: escalated flags newest first, each joined to the query
that raised it, with undo.

**Dashboard** renders `GET /stats`: queries run, items flagged, escalation rate, flags by risk
level, motifs detected, tool usage, most-flagged accounts.

**Chrome**: a sidebar with session counters (queries, flagged, high risk) and recent queries, plus
a light/dark/system theme toggle. Example-query chips on the empty state, one per routing path the
agent supports, each verified against the live dataset to return something worth looking at.

---

## Auth & Onboarding Flow

**N/A, omitted.** No accounts, no login, no onboarding. Running the app locally *is* the entire
access flow: clone, install, run backend + frontend, use it.

---

## Non-Functional Notes (actual, not aspirational)

| Area | Current state |
|---|---|
| Rate limiting | None, not needed, single local user, no public exposure |
| Security headers | None, not needed, no deployment, no public network exposure |
| Error handling | Built. Tools never raise across the boundary and return a structured error dict; the loop has a backstop for when they do anyway. `ProviderError` maps upstream failures to 429/502. Malformed model output is dropped per-item rather than failing the run |
| DB query performance | Bulk data has no DB. Parquet + DuckDB keeps aggregation fast without one. SQLite is small and append-mostly, no perf concern at this scale |
| Concurrency safety | Parquet: read-only, no concern. SQLite: single local process, its own file locking is sufficient, no pooling needed |
| Deployment status | **Not deployed, not planned to be.** Local `uvicorn` + local Vite dev server only |
| Testing coverage | Built. 16 pytest modules (tool contract, all 8 motif rules, risk invariants, date normalisation, cache isolation, agent recovery paths, routes, db) + 10 Vitest tests. Live-API tests are skipped unless `VIGIL_LIVE_TESTS=1` |
| Structured output validation | Built. `AgentResult` / `FlaggedItem` / `ExecutionSummary` / `Evidence` in `schemas.py`, validated on every run |
| Caching | Built. `ml/cache.py` memoizes `eda`, `feature_eng` and `anomaly` on JSON-canonicalised args, results deep-copied out so a caller mutating one can't poison the next. A repeat `anomaly` call goes 1.96s → ~0s. `risk` and `explain` are deliberately uncached: their arguments are whole result dicts, so building the key costs more than recomputing |

---

## Environment Variables

### Backend (`backend/.env`, gitignored; `.env.example` committed)
```
# Gemini key, from https://aistudio.google.com/apikey (NOT the Google Cloud Console)
GOOGLE_API_KEY=

# Data paths
DATA_DIR=./data              # optional, defaults to ./data
SQLITE_PATH=./aml_agent.db   # optional, defaults to ./aml_agent.db

# Kaggle: not needed at runtime. The dataset fetch is a documented manual download step in the
# README (see Decisions Log item 6), not a scripted/credentialed part of the app.
```

**Startup behaviour if the key is missing:** fails fast at FastAPI startup with an error naming the
variable and where to get a key, rather than failing on the first query.

### Frontend (`frontend/.env`)
```
VITE_API_BASE_URL=http://127.0.0.1:8000
```

`127.0.0.1`, not `localhost`: uvicorn binds IPv4, browsers resolve `localhost` to IPv6 `::1` first,
and the request is refused before it reaches the server.

---

## Divergence From Original Plan

**Built as originally scoped:** the agent loop and its five ML tools, the `/analyze` and
`/escalate` routes, SQLite persistence, DuckDB-over-Parquet bulk querying, the detector +
rules-engine split, template explanations, Pydantic validation, and the single-page React UI.

**Built beyond original scope**, all of it because the data was already being recorded and never
shown, or because a demo failure mode made it necessary:

- `/analyze/stream` (SSE) plus live tool events and cancel in the UI. A run is 5-15s and the page
  otherwise showed a spinner.
- `GET /escalations` and `GET /stats`, and the two tabs that render them.
- Escalation notes and undo.
- Evidence charts derived deterministically from DuckDB.
- Flagged-items sort, filter, drill-down and CSV export.
- `scripts/validate_risk_levels.py` and `scripts/tune_blend.py`: measured risk-level precision and
  recall against ground truth, and a constant search against an explicit objective. The headline
  result is HIGH+ recall 7.2% → 66% with CRITICAL's precision unchanged.
- Light/dark theming and the sidebar session counters.
- 430 backend and 10 frontend tests, against a spec that asked for three areas of minimum
  coverage.

**Cut:** `GET /customer/{id}/risk-profile` (item 2), the feedback-driven threshold stretch feature
(item 5), `HI-Small_Agent_Ready.parquet` (byte-identical duplicate), and the multi-provider LLM
abstraction (item 4).

**Reversals worth remembering**, so neither flips again without a new reason:

1. Interface layer: started as "Streamlit only, skip FastAPI" → reversed to React + FastAPI once
   the team confirmed a 2-person split where a real frontend/backend boundary earns its keep.
2. Agent orchestration: started as a hand-rolled SDK loop → briefly changed to a framework-hosted
   loop for the dev-speed win → reversed back, since the loop is the part the brief actually grades
   and hiding it behind a framework would have cost the transparency feed (item 4a).
3. LLM provider: planned multi-provider with `LLM_PROVIDER` → collapsed to Gemini only. The
   abstraction did its job once, absorbing a provider switch with zero changes to `agent/loop.py`,
   and was then removed because a second provider nobody could test was a setup choice a judge
   should not have to make.

---

*Where this document and the code disagree, the code wins. Correct this file rather than the code.*

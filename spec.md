# spec.md

## Project: AML Detection Agent ("vigil")

> **Built and running as of 2026-07-26.** This began as a pre-implementation spec; the system now
> exists end to end — agent loop, five ML tools, FastAPI, React UI, SQLite audit trail. Sections
> still marked **[PLANNED, NOT BUILT]** are the ones nobody has revisited, not statements about
> the current system; where this file and the code disagree, the code wins and this file should
> be corrected. See `README.md` to run it and `backend/docs/ml_spec.md` for the ML workstream.

---

## Decisions Log (previously open, now resolved)

**Read this section first.** Everything below was an open gap as of the last spec revision and got
drilled and closed in a follow-up session. Kept as a log, not deleted, so a future session can see
*why* each call was made. Several of these interlock (2 and 3 especially), so don't re-litigate one
without checking the others.

1. **Team: 2-person** (you + one friend). Confirms the React+FastAPI split was the right call: it's
   two humans genuinely dividing frontend/backend work, not a solo dev who'd have been better off
   with Streamlit. Commit/branch traceability is required by the rules for 2-person teams, so set up
   branches or a clear commit-authorship convention from the first commit, not retroactively.
2. **Route list: `/analyze` + `/escalate`, cut `/customer/{id}/risk-profile`.**
   - `POST /api/v1/analyze`: core, unchanged.
   - `POST /api/v1/escalate`: **build it.** Now that SQLite is in (item 3), this becomes a real,
     small feature: judge clicks "escalate" on a flagged item in the UI, it persists the chosen
     action to the `flags` table. That's a genuine interactive demo moment, not a stub.
   - `GET /api/v1/customer/{id}/risk-profile`: **cut.** The problem statement's entity-lookup case
     ("Is customer 4521 suspicious?") is already served by `/analyze` routing to a single-entity
     execution plan. A dedicated profile page/route would be a nice drill-down UX but isn't required
     by any hard requirement, and 48h doesn't have room for a route plus a second frontend page that
     only serves a "would be nice" case. Not stretch-listed either, just cut, don't revisit unless
     both required scope and the SQLite work finish with real time left over.
3. **SQLite: yes, minimal.** Two tables: `queries` (every `/analyze` call: query text, timestamp,
   intent detected, tools invoked/skipped) and `flags` (every flagged item: customer/transaction id,
   risk level, pattern, explanation, escalation_action, escalated_at). Written by `/analyze` after
   each run, updated by `/escalate`. This is cheap (stdlib `sqlite3`, one file, no server) and buys
   two real things: an actual audit trail, which fits a compliance product, and a working `/escalate`
   endpoint instead of a stub. See Database Schema below for the full shape.
4. **LLM: Gemini, single provider.** The agent runs on Google's Gemini API, model alias
   `gemini-flash-lite-latest`, read from `GOOGLE_API_KEY`. Chosen for a free tier large enough to
   demo on, and for low latency across the several sequential calls one run makes. The
   multi-provider abstraction that briefly existed here was removed: one provider, one key, one
   code path, and nothing in the setup steps a judge has to choose between.
   - The alias rather than a pinned model id is load-bearing. Pinned 2.0/2.5 ids return 404 or a
     zero-quota 429 on new keys, and `gemini-flash-latest` allows only 20 requests/day, which one
     query can spend a quarter of.
   - Practical gotcha worth carrying into the README's setup steps: the Gemini key must come from
     **aistudio.google.com/apikey**, not the Google Cloud Console. Cloud-Console-issued keys land
     in a project with a zero free-tier quota grant and fail with `429 RESOURCE_EXHAUSTED`
     regardless of enabling the API.
4a. **Orchestration: hand-rolled tool-calling loop, not LangGraph.** FastAPI makes the API call
   itself, and all SDK translation lives in `agent/providers.py`, so the loop speaks one internal
   message shape and never imports the SDK. That is what keeps the whole agent testable against a
   fake client with no key and no network. See Section "Agent Orchestration" below for the final
   shape.
5. **Feedback-driven threshold refinement stretch feature: cut, not attempted.** No time is budgeted
   for it. It was already flagged as absent from the actual problem statement text, so cutting it
   entirely (rather than leaving it as a maybe) removes the risk of it quietly eating hours 40-48.
   If it's worth a mention at all, one line in the README's "future work" section is enough; don't
   build toward it.
6. **Kaggle dataset: already downloaded for dev.** Raw `HI-Small_Trans.csv` / `HI-Small_Patterns.txt`
   are already on the dev machine, so this stops blocking implementation. Still document the fetch
   step in `README.md` for a fresh clone: raw CSVs shouldn't be committed to the repo (large, and
   `.gitignore`-worthy alongside the derived Parquet files). Default to **manual browser download**
   as the documented method: zero extra dependency, no Kaggle API credentials to manage, and nothing
   here needs the automation a CLI/`kagglehub` download would buy. You're fetching it once, not
   scripting a repeatable pipeline run.
7. **Repo: about to be created and connected.** No further decision needed here beyond what the
   rules already require: confirm before the first commit that the repo name has zero SG/Societe
   Generale/SocGen/SGGSC references, visibility is public, and (per item 1, 2-person team) a branch
   or commit-authorship convention is agreed before either of you starts pushing.

---

## Stack (planned)

```
Frontend  : React 18 + Vite, TypeScript (--template react-ts)
State     : React built-ins (useState/useContext), no state library planned; SPA is one page
Backend   : FastAPI (Python, async)
Agent     : Hand-rolled LLM tool-calling loop, NOT LangGraph
LLM       : Gemini, `gemini-flash-lite-latest`, key in `GOOGLE_API_KEY` (Decisions Log item 4)
Dev tool  : An AI coding assistant may be used to help *write* this code; that's a dev-time tool,
            disclosed in README per competition rule 3, and does not run at query time
Database  : SQLite (`queries` + `flags` tables, audit trail + working `/escalate`). Bulk data still
            flat files (Parquet); SQLite is only for the small persisted-history layer.
Query     : DuckDB directly over Parquet (aggregation/threshold queries)
Data proc : Pandas + NumPy (row-wise feature engineering, feeds scikit-learn)
Detection : scikit-learn (Isolation Forest, LOF, Z-score) + plain-Python rules engine
Explain   : Template-based NL generation tied to the firing rule/feature/threshold, no SHAP/LIME
Validation: Pydantic (all structured agent output)
Caching   : functools.lru_cache (repeat-query caching) + precomputed baselines at enrichment time, no Redis
Async     : None planned. FastAPI run_in_threadpool/BackgroundTasks only if a specific call proves slow, no Celery/Airflow
Testing   : Pytest (intent parsing, rule engine, risk classification)
Auth      : None, no login, local single-user demo tool
Deploy    : None, explicitly out of scope, runs locally only
```

Nothing in this stack has been installed or scaffolded yet. No `requirements.txt`, no `package.json`,
no repo exists at time of writing.

---

## Folder Structure (planned)

**[PLANNED, NOT BUILT]**. Not previously written down anywhere as a single tree — file paths were
scattered across this doc, `IMPLEMENTATION_PLAN.md`, and `ml_spec.md`, and two of those disagreed
(this doc said `api/routes/agent.py`, the implementation plan says `backend/api/routes/agent.py`).
Reconciled here to the `backend/`-prefixed layout, since that's what `IMPLEMENTATION_PLAN.md`'s
phases actually build against. Treat this section as authoritative going forward; if a path changes
during implementation, update it here too.

```
vigil/
├── README.md
├── .gitignore
├── spec.md
├── backend/
│   ├── .env                       # gitignored
│   ├── .env.example
│   ├── requirements.txt
│   ├── main.py                    # FastAPI app entrypoint, startup key-check
│   ├── db.py                      # sqlite3: queries + flags table creation
│   ├── schemas.py                 # Pydantic: AgentResult + flag shape (frozen contract, Phase 0)
│   ├── aml_agent.db                # gitignored, created at runtime (SQLITE_PATH)
│   ├── api/
│   │   └── routes/
│   │       └── agent.py           # /analyze, /analyze/stream, /escalate(+undo),
│   │                                #   /escalations, /stats
│   ├── agent/
│   │   ├── providers.py           # get_client(provider) -> chat_with_tools wrapper
│   │   └── loop.py                # tool_calling_loop
│   ├── tools/                     # public tool surface; re-exports from ml/ (escalate is app-owned)
│   │   ├── eda.py
│   │   ├── feature_eng.py
│   │   ├── anomaly.py
│   │   ├── risk.py
│   │   └── explain.py
│   ├── scripts/                   # one-time build pipeline, run in this order
│   │   ├── enrich.py              # raw CSV -> enriched Parquet
│   │   ├── train_models.py        # -> data/models/*.joblib + metadata.json
│   │   ├── build_rule_hits.py     # -> data/HI-Small_Rule_Hits.parquet
│   │   ├── validate_risk_levels.py  # risk-level precision/recall against ground truth
│   │   └── tune_blend.py          # searches the risk blend constants
│   ├── notebooks/
│   │   └── 01_exploration.ipynb   # ML owner's Phase 1 EDA notebook
│   ├── data/                      # everything under DATA_DIR, all gitignored
│   │   ├── raw/                   # HI-Small_Trans.csv, HI-Small_Patterns.txt, accounts
│   │   ├── HI-Small_Enriched.parquet    # generated by enrich.py; Agent_Ready.parquet cut
│   │   ├── HI-Small_Rule_Hits.parquet   # generated by build_rule_hits.py
│   │   └── models/                # fitted detectors + metadata + validation artifacts
│   ├── ml/                        # ML implementation (12 modules)
│   │   ├── data.py                # DATA_DIR paths + shared lru_cache'd DuckDB connection
│   │   ├── tools.py               # TOOL_SCHEMAS + dispatch table; agent/loop.py registers these
│   │   ├── eda.py feature_eng.py anomaly.py risk.py explain.py   # the five tools
│   │   └── rules.py features.py dates.py cache.py validation.py  # support modules
│   ├── docs/                      # ml_spec.md, phase1-7.md, ml_audit.md
│   └── tests/                     # pytest: tools, rule engine, risk classification, agent, routes
└── frontend/
    ├── .env                        # gitignored
    ├── .env.example
    ├── package.json
    ├── index.html
    └── src/
        ├── App.tsx                 # single page, no router (per Frontend Pages & Routes below)
        ├── mocks/
        │   └── mock_agent_result.json   # hand-written fixture matching AgentResult exactly
        └── components/              # execution summary panel, flagged items table, risk charts
```

DATA_DIR/SQLITE_PATH env defaults (`./data`, `./aml_agent.db`) resolve relative to `backend/`'s
working directory when `uvicorn` runs from there — call this out in README setup steps so a fresh
clone doesn't hit a path-not-found from running `uvicorn` out of the repo root instead.

---

## Agent Orchestration (as implemented)

**[PLANNED, NOT BUILT]**

Decided (final, after one reversal, see Q4a): the runtime agent is a **hand-rolled tool-calling
loop against the Gemini SDK**, not LangGraph. FastAPI makes the API call itself, so a judge's
setup is `pip install -r requirements.txt`, drop a Gemini key in `.env`, run. Nothing external
needs installing beyond your own `requirements.txt`.

```
POST /api/v1/analyze  →  read GOOGLE_API_KEY from .env  →  get_client()  →
                          tool_calling_loop(client, query, tools=[eda, feature_eng, anomaly,
                          risk, explain, escalate])  →  validate against AgentResult Pydantic
                          schema  →  return to frontend
```

- **Tool functions**: the six agent tools (EDA, feature engineering, anomaly detection, risk
  classification, explanation, escalation) are plain Python functions registered with the loop as
  tool-call targets (native `tool_use`/function-calling schemas per provider), not shelled-out
  scripts. Not written yet.
- **SDK boundary**: `get_client()` returns a thin wrapper exposing `chat_with_tools(...)`. All
  translation between the loop's own message shape and Gemini's `Content`/`Tool` types lives in
  `agent/providers.py`, so `agent/loop.py` never imports the SDK and can be tested against a fake
  client with no key and no network.
- **Transparency feed**: since you're writing the loop, log each tool call (name, args, result) as
  you dispatch it. This becomes the source for the "execution summary: tools invoked / skipped /
  why" panel. It is more code than a framework's built-in trace would have been, but it is fully
  under your control.
- **No CLI dependency, no separate install step**: a judge's setup is
  `pip install -r requirements.txt`, drop a key in `.env`, run.
- **Deck/README framing note**: this version is the one where "here's our intent parser, here's our
  planner, here's how we dispatch tools" is genuinely your own code, a stronger technical-details
  slide than delegating it to a framework would have been, at the cost of the SDK-boundary layer
  above being work you have to actually write.

---

## Database Schema (as implemented)

**[PLANNED, NOT BUILT]**, two layers, neither exists yet.

### Bulk data: flat files, not a database
- Input: raw Kaggle CSV (`HI-Small_Trans.csv`, `HI-Small_Patterns.txt`), already downloaded for dev,
  gitignored, not committed
- Working data: `HI-Small_Enriched.parquet` only, produced by a one-time enrichment script,
  columnar, loaded into memory at FastAPI startup. `HI-Small_Agent_Ready.parquet` (originally
  planned as a second file) was **cut** during ML implementation: `feature_eng` queries the enriched
  file directly via DuckDB through a shared `lru_cache`d connection (`ml/data.py`) instead.
- Queried via DuckDB directly over the Parquet file (no separate DB process)

Target enrichment column list (updated to match what the ML owner actually built — `phase2.md`):

| Field | Description |
|---|---|
| `is_laundering` | Boolean, raw `Is Laundering==1` label, authoritative |
| `is_suspicious` | Boolean, pattern-file match — a **subset** of `is_laundering` (3,209 of 5,177 rows), kept as a separate column, not a synonym |
| `aml_pattern` | One of 8 motifs found in Phase 1 (FAN-OUT, FAN-IN, CYCLE, GATHER-SCATTER, SCATTER-GATHER, BIPARTITE, STACK, RANDOM), else NORMAL |
| `hour`, `day_of_week` | Extracted from timestamp |
| `amount_category` | micro / small / medium / large / xlarge / xxlarge, via quantile buckets (`pd.qcut`, 6 equal-frequency buckets on `log1p(Amount Received)`, ~846k rows/bucket) |
| `is_self_loop` | Boolean, `From Account == To Account`, 591,212 rows (11.64%). Kept + flagged, not dropped — stays in per-sender baseline stats so `txn_count`/`avg_amount` don't silently shrink |
| `currency_mismatch` | Boolean, Payment Currency ≠ Receiving Currency |
| `payment_format_risk` | Encoded Payment Format signal — strongest single class-separator found (86.6% of laundering rows are ACH vs 11.8% normal) |
| `txn_count`, `total_volume`, `avg_amount`, `std_amount` | Per-sender baseline stats |
| `unique_receivers` | Distinct receiver count for this sender |
| `deviation_from_avg` | How far a given transaction deviates from sender baseline |

### Persisted history: SQLite (decided, see Decisions Log item 3)

A single `aml_agent.db` file, stdlib `sqlite3`, no server. Two tables:

**`queries`**: one row per `/analyze` call, the audit trail of what the agent decided to do.
- `id`: primary key, integer autoincrement (no need for UUIDs, single local writer, no distributed
  generation)
- `query_text`: the raw natural-language query
- `timestamp`: when it ran
- `intent_detected`, `filters_applied` (JSON text column), `tools_invoked` (JSON text column),
  `tools_skipped` (JSON text column): mirrors the `execution_summary` in `AgentResult`

**`flags`**: one row per flagged item across all queries, updated when a judge escalates one.
- `id`: primary key, integer autoincrement
- `query_id`: foreign key → `queries.id`
- `customer_id`, `transaction_id`
- `amount`, `timestamp`: added during Phase 0/4 implementation — without them a persisted flag can't
  be re-rendered (the UI table shows amount, the temporal chart plots the transaction timestamp)
- `risk_level`, `pattern_detected`, `anomaly_score`, `explanation`
- `escalation_action`: the agent's *recommended* action (MONITOR/REVIEW/REPORT), set at insert time
- `escalated_at`: nullable; set by `POST /api/v1/escalate` when a judge actually acts on it,
  distinguishing "the agent recommended REPORT" from "someone actually clicked escalate"

No soft-delete, no retention/purge logic. This is a single-demo-run audit log, not a production
compliance system; rows just accumulate for the life of the local `aml_agent.db` file.

> **Contract note (Phase 0/4):** `amount` and `timestamp` above were not in this spec's original
> `flags` list. Both were added during implementation and are genuinely needed, not speculative: the
> "Frontend Pages & Routes" section below puts **amount** in the flagged-items table, and the temporal
> risk chart it also asks for needs a **timestamp** per flagged item. They are persisted as `flags`
> columns (`amount REAL`, `timestamp TEXT`), not merely returned in the API response.
>
> **Known limitation, accepted:** `POST /api/v1/escalate` overwrites `escalation_action` with the
> human's chosen action, so the agent's *original* recommendation is not preserved after someone
> escalates. Fine for a single-run demo audit log. Surfacing "agent said REPORT, human downgraded to
> REVIEW" would need a third column; deliberately not built.

> Indexes: `flags.query_id` (FK lookup), `flags.transaction_id` and `flags.customer_id` (the
> lookups `/escalate` and any future customer-history view would use).

---

## RBAC Model (as implemented)

**N/A, omitted.** No authentication, no user accounts, no roles. This is a single-user local tool
for a hackathon demo; anyone running it locally has full access to every function.

---

## API Endpoints (as implemented)

**[PLANNED, NOT BUILT]**, no FastAPI app exists yet. Two routes, final (see Decisions Log item 2):

### `agent` router, prefix `/api/v1`
File: `backend/api/routes/agent.py`

- `POST /api/v1/analyze` → Body: `{query: str}`. Runs the full agent loop (intent parse → dynamic
  tool selection → execution → structured result), writes a `queries` row and one `flags` row per
  flagged item to SQLite, and returns an `AgentResult` (see Non-Functional Notes for the shape).
- `POST /api/v1/analyze/stream` → same body and same run, streamed as Server-Sent Events: one
  `tool_start`/`tool_end` event per dispatch, then the finished `AgentResult`. Added because a
  run is 5-15s of sequential tool calls and the UI could otherwise only show a spinner.
  `/analyze` is unchanged and remains the fallback.
- `POST /api/v1/escalate` → Body: `{flag_id: int, action: str, note?: str}`. Sets
  `flags.escalated_at` for that row in SQLite and returns confirmation. This is what makes the
  "judge clicks escalate" interaction actually persist. The optional note is the analyst's own
  reasoning: an audit trail that records the decision but not the why is half a record.
- `POST /api/v1/escalate/undo` → Body: `{flag_id: int}`. Withdraws an escalation. Escalating is
  one click, so a mis-click writes a decision nobody meant to take; the flag itself survives,
  only the human action is cleared.
- `GET /api/v1/escalations` → the audit trail: every flag a human escalated, newest first, each
  joined back to the query that surfaced it. Read from SQLite rather than session state, so it
  survives a reload and spans conversations.
- `GET /api/v1/stats` → dashboard aggregates over every run recorded: totals, escalation rate,
  flags by risk level, motif frequency, tool usage, most-flagged accounts, recent queries.

**Divergence from the original two-route plan.** The three read routes and the stream were added
during implementation. None of them changes the agent: `/stats` and `/escalations` only read
what `/analyze` already persisted, and `/analyze/stream` runs the identical loop. They exist
because the data was being recorded and never shown.

**Cut:** `GET /api/v1/customer/{customer_id}/risk-profile`, not building (see Decisions Log item 2).
Entity-lookup queries route through `/analyze` instead.

No auth guards planned (no auth exists). No rate limiting planned (single local user, not needed).

---

## Frontend Pages & Routes (as implemented)

**[PLANNED, NOT BUILT]**, no React project scaffolded yet (`npm create vite@latest` not yet run).

Planned as a single page, no router library needed initially:

```
/  → App.tsx (single page, no auth guard, none exists)
```

**Page contents (all planned, nothing rendered yet):**
- Query input box: free-text natural language query, submits to `POST /api/v1/analyze`
- Execution summary panel: shows intent detected, filters applied, tools invoked vs. skipped, and
  why (this is the "transparent decision flow" differentiator; must be visually prominent, it's
  what a judge reads first)
- Flagged items table: customer/transaction ID, amount, risk level, pattern detected, explanation,
  escalation action, with an "Escalate" button per row that calls `POST /api/v1/escalate`
- Risk charts: Plotly, embedded (risk distribution, temporal pattern chart; exact chart set not
  finalized)

No second page/route planned. The customer risk-profile page was cut (Decisions Log item 2), so
`react-router` isn't needed; this stays a true single-page app.

---

## Auth & Onboarding Flow (as implemented)

**N/A, omitted.** No accounts, no login, no onboarding. Running the app locally *is* the entire
access flow: clone repo, install dependencies, run backend + frontend, use it.

---

## Non-Functional Notes (actual, not aspirational)

| Area | Current state |
|---|---|
| Rate limiting | None, not needed, single local user, no public exposure |
| Security headers | None, not needed, no deployment, no public network exposure |
| Error handling | **[PLANNED, NOT BUILT]**, no error handling exists yet; plan is FastAPI's standard exception handlers + Pydantic validation errors surfaced to the frontend as-is |
| DB query performance | Bulk data has no DB. Parquet + DuckDB chosen so aggregation queries stay fast without one. SQLite (`queries`/`flags`) is small, append-mostly, no perf concern at hackathon scale |
| Concurrency safety | Parquet data: read-only after startup, no concern. SQLite: single local process, SQLite's own file locking is sufficient for a single-user demo, no pooling/queueing needed |
| Deployment status | **Not deployed, not planned to be.** Explicit hackathon-scope decision, runs via local `uvicorn` + local Vite dev server only |
| Testing coverage | **[PLANNED, NOT BUILT]**, zero tests exist. Planned minimum: intent parsing, rule engine, risk classification (Pytest) |
| Structured output validation | **[PLANNED, NOT BUILT]**, Pydantic models for `AgentResult` not yet written |
| Caching | **[PLANNED, NOT BUILT]**, `functools.lru_cache` on tool functions, precomputed baselines at enrichment time; nothing implemented |

---

## Environment Variables (as required by the actual code)

**[PLANNED, NOT BUILT]**, no `.env` files or config loading exist yet. Anticipated variables based
on decided stack:

### Backend (`.env`, not yet created, gitignored, `.env.example` with placeholders committed instead)
```
# Which provider the agent loop uses, judges set this to match whichever key they have
# Gemini key, from https://aistudio.google.com/apikey (NOT the Google Cloud Console)
GOOGLE_API_KEY=

# Data paths
DATA_DIR=./data          # optional, defaults to ./data if unset; path to enriched Parquet files
SQLITE_PATH=./aml_agent.db   # optional, defaults to ./aml_agent.db if unset

# Kaggle: not needed at runtime, dataset fetch is a documented manual download step in README
# (see Decisions Log item 6), not a scripted/credentialed part of the app itself.
```
Startup behavior if a required LLM key is missing: **[PLANNED, NOT BUILT]**, needs to be decided.
Recommend failing fast at FastAPI startup with a clear error rather than failing on first query.

### Frontend (`.env`, not yet created)
```
VITE_API_BASE_URL=http://localhost:8000   # FastAPI backend URL, no default decided yet
```

---

## Divergence From Original Plan (for historical context)

- **Built and exceeds original scope**: None yet, nothing is built.
- **Built as originally scoped**: None yet, nothing is built.
- **Not built**: Everything. This spec is entirely pre-implementation. Two decisions have already
  reversed once during planning, worth remembering so neither flips a third time without a new
  reason showing up:
  1. Interface layer: started as "Streamlit only, skip FastAPI" → reversed to "React + FastAPI" once
     the team confirmed a 2-person split where a real frontend/backend boundary earns its keep (see
     Open Question 1, if it turns out to be solo, revisit whether Streamlit-only was the better call).
  2. Agent orchestration: started as a hand-rolled provider SDK loop → briefly changed to headless
     a framework-hosted loop for the dev-speed win → reversed back to the hand-rolled loop, since
     the loop is the part the brief actually grades and hiding it behind a framework would have
     cost the transparency feed (see Decisions Log item 4a).

  As of this revision, all seven previously-open items in the Decisions Log are resolved. Nothing
  is drifting silently; if a future session wants to change one of these, treat it as a deliberate
  reversal worth logging here, the same way the two above were, not a silent edit.

---

*Everything in this document is derived from planning-session decisions, not from any existing
code. Once implementation starts, update the marked sections in place and remove
`[PLANNED, NOT BUILT]` tags as each piece actually ships; don't let this drift into aspirational
documentation.*

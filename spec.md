# spec.md

## Project: AML Detection Agent ("aml-agent")

> Living spec reflecting the **planned** system as of 2026-07-25. Nothing has been implemented yet
> — every section below describes intended scope, so almost everything is marked
> **[PLANNED — NOT BUILT]**. This spec is the standalone reference for future sessions; open
> decisions are called out explicitly in the section right after this header, not silently assumed.

---

## Decisions Log (previously open, now resolved)

**Read this section first.** Everything below was an open gap as of the last spec revision and got
drilled and closed in a follow-up session. Kept as a log, not deleted, so a future session can see
*why* each call was made — several of these interlock (2 and 3 especially), so don't re-litigate one
without checking the others.

1. **Team: 2-person** (you + one friend). Confirms the React+FastAPI split was the right call — it's
   two humans genuinely dividing frontend/backend work, not a solo dev who'd have been better off
   with Streamlit. Commit/branch traceability is required by the rules for 2-person teams — set up
   branches or a clear commit-authorship convention from the first commit, not retroactively.
2. **Route list: `/analyze` + `/escalate`, cut `/customer/{id}/risk-profile`.**
   - `POST /api/v1/analyze` — core, unchanged.
   - `POST /api/v1/escalate` — **build it.** Now that SQLite is in (item 3), this becomes a real,
     small feature: judge clicks "escalate" on a flagged item in the UI, it persists the chosen
     action to the `flags` table. That's a genuine interactive demo moment, not a stub.
   - `GET /api/v1/customer/{id}/risk-profile` — **cut.** The problem statement's entity-lookup case
     ("Is customer 4521 suspicious?") is already served by `/analyze` routing to a single-entity
     execution plan. A dedicated profile page/route would be a nice drill-down UX but isn't required
     by any hard requirement, and 48h doesn't have room for a route + a second frontend page that
     only serves a "would be nice" case. Not stretch-listed either — just cut, don't revisit unless
     both required scope and the SQLite work finish with real time left over.
3. **SQLite: yes, minimal.** Two tables: `queries` (every `/analyze` call — query text, timestamp,
   intent detected, tools invoked/skipped) and `flags` (every flagged item — customer/transaction id,
   risk level, pattern, explanation, escalation_action, escalated_at). Written by `/analyze` after
   each run, updated by `/escalate`. This is cheap (stdlib `sqlite3`, one file, no server) and buys
   two real things: an actual audit trail — which is thematically right for a compliance product —
   and a working `/escalate` endpoint instead of a stub. See Database Schema below for the full shape.
4. **LLM: Claude for dev, provider-agnostic for judges.** Development and internal testing run on
   Claude (`LLM_PROVIDER=anthropic` as the default in `.env.example`). The hand-rolled provider-agnostic
   loop (decided in item 4a below, unchanged) still supports Groq/Gemini via env swap for anyone
   running it themselves. README disclosure: state plainly that the team built and tested against
   Claude (Anthropic API), and that Groq/Gemini are supported alternates via `LLM_PROVIDER` for
   judges using their own keys — that's an accurate, complete disclosure either way.
4a. **Orchestration: hand-rolled tool-calling loop, not headless Claude Code CLI, not LangGraph.**
   (Unchanged from the previous revision — kept here for continuity.) This was briefly changed to
   headless Claude Code CLI mid-planning, then reverted because it can't satisfy "judges drop
   whichever provider's key they have into `.env`" — Claude Code is Anthropic-only and needs its own
   CLI installed, not just a key. See Section "Agent Orchestration" below for the final shape. Claude
   Code remains fine as a dev-time coding assistant (disclose in README like any AI tool used), just
   not the query-time engine.
5. **Feedback-driven threshold refinement stretch feature: cut, not attempted.** No time is budgeted
   for it. It was already flagged as absent from the actual problem statement text — cutting it
   entirely (rather than leaving it as a maybe) removes the risk of it quietly eating hours 40-48.
   If it's worth a mention at all, one line in the README's "future work" section is enough — don't
   build toward it.
6. **Kaggle dataset: already downloaded for dev.** Raw `HI-Small_Trans.csv` / `HI-Small_Patterns.txt`
   are already on the dev machine, so this stops blocking implementation. Still document the fetch
   step in `README.md` for a fresh clone — raw CSVs shouldn't be committed to the repo (large, and
   `.gitignore`-worthy alongside the derived Parquet files). Default to **manual browser download**
   as the documented method: zero extra dependency, no Kaggle API credentials to manage, and nothing
   here needs the automation a CLI/`kagglehub` download would buy — you're fetching it once, not
   scripting a repeatable pipeline run.
7. **Repo: about to be created and connected.** No further decision needed here beyond what the
   rules already require — confirm before the first commit: repo name has zero SG/Societe
   Generale/SocGen/SGGSC references, visibility is public, and (per item 1, 2-person team) a branch
   or commit-authorship convention is agreed before either of you starts pushing.

---

## Stack (planned)

```
Frontend  : React 18 + Vite, TypeScript (--template react-ts)
State     : React built-ins (useState/useContext) — no state library planned; SPA is one page
Backend   : FastAPI (Python, async)
Agent     : Hand-rolled LLM tool-calling loop, provider selected via `LLM_PROVIDER` env var —
            NOT headless Claude Code CLI (tried mid-session, reverted — see Q4a), NOT LangGraph
LLM       : Claude (default, `LLM_PROVIDER=anthropic`, what dev/testing runs on) — Groq/Gemini
            supported alternates via `.env` swap for judges using their own keys
Dev tool  : Claude Code (interactive or headless) may be used to help *write* this code — that's a
            dev-time tool, disclosed in README like any AI assistant, and does not run at query time
Database  : SQLite (`queries` + `flags` tables, audit trail + working `/escalate`). Bulk data still
            flat files (Parquet) — SQLite is only for the small persisted-history layer.
Query     : DuckDB directly over Parquet (aggregation/threshold queries)
Data proc : Pandas + NumPy (row-wise feature engineering, feeds scikit-learn)
Detection : scikit-learn (Isolation Forest, LOF, Z-score) + plain-Python rules engine
Explain   : Template-based NL generation tied to the firing rule/feature/threshold — no SHAP/LIME
Validation: Pydantic (all structured agent output)
Caching   : functools.lru_cache (repeat-query caching) + precomputed baselines at enrichment time — no Redis
Async     : None planned. FastAPI run_in_threadpool/BackgroundTasks only if a specific call proves slow — no Celery/Airflow
Testing   : Pytest (intent parsing, rule engine, risk classification)
Auth      : None — no login, local single-user demo tool
Deploy    : None — explicitly out of scope, runs locally only
```

Nothing in this stack has been installed or scaffolded yet. No `requirements.txt`, no `package.json`,
no repo exists at time of writing.

---

## Agent Orchestration (as implemented)

**[PLANNED — NOT BUILT]**

Decided (final, after one reversal — see Q4a): the runtime agent is a **hand-rolled tool-calling
loop against the active provider's native SDK**, not headless Claude Code CLI and not LangGraph.
This is the setting where "judges paste any provider's key into `.env`" is actually achievable —
FastAPI makes the API call itself, so any Anthropic/OpenAI/Groq/Gemini-compatible key just works
once `LLM_PROVIDER` points at it. Nothing external needs installing beyond your own `requirements.txt`.

```
POST /api/v1/analyze  →  parse LLM_PROVIDER + matching key from .env  →  get_client(provider)  →
                          tool_calling_loop(client, query, tools=[eda, feature_eng, anomaly,
                          risk, explain, escalate])  →  validate against AgentResult Pydantic
                          schema  →  return to frontend
```

- **Tool functions**: the six agent tools (EDA, feature engineering, anomaly detection, risk
  classification, explanation, escalation) are plain Python functions registered with the loop as
  tool-call targets (native `tool_use`/function-calling schemas per provider) — not shelled-out
  scripts. Not written yet.
- **Provider abstraction**: `get_client(provider: str)` returns a thin wrapper exposing a common
  `chat_with_tools(...)` interface over Anthropic's Messages API, OpenAI-style function-calling
  (used by both OpenAI and Groq's compatible endpoint), and Gemini's function-calling — so the loop
  itself doesn't care which provider is active. Not written yet; this is the one piece of extra
  code the reversal from Claude Code CLI actually costs you.
- **Transparency feed**: since you're writing the loop, log each tool call (name, args, result) as
  you dispatch it — this becomes the source for the "execution summary: tools invoked / skipped /
  why" panel. More code than Claude Code's free `stream-json` trace would have given you, but fully
  under your control and provider-independent.
- **No CLI dependency, no lock-in, no separate install step** — this is the direct payoff of the
  reversal: a judge's setup is `pip install -r requirements.txt`, drop a key in `.env`, run.
- **Deck/README framing note**: this version is the one where "here's our intent parser, here's our
  planner, here's how we dispatch tools" is genuinely your own code — a stronger technical-details
  slide than the Claude-Code-CLI version would have been, at the cost of the provider-abstraction
  layer above being work you have to actually write.

---

## Database Schema (as implemented)

**[PLANNED — NOT BUILT]** — two layers, neither exists yet.

### Bulk data: flat files, not a database
- Input: raw Kaggle CSV (`HI-Small_Trans.csv`, `HI-Small_Patterns.txt`) — already downloaded for dev,
  gitignored, not committed
- Working data: `HI-Small_Enriched.parquet`, `HI-Small_Agent_Ready.parquet` — produced by a one-time
  enrichment script, columnar, loaded into memory at FastAPI startup
- Queried via DuckDB directly over the Parquet file (no separate DB process)

Target enrichment column list:

| Field | Description |
|---|---|
| `is_suspicious` | Boolean, derived from `Patterns.txt` |
| `aml_pattern` | STRUCTURING / SMURFING / LAYERING / etc., or NORMAL |
| `hour`, `day_of_week` | Extracted from timestamp |
| `amount_category` | micro / small / medium / large / xlarge / xxlarge |
| `txn_count`, `total_volume`, `avg_amount`, `std_amount` | Per-sender baseline stats |
| `unique_receivers` | Distinct receiver count for this sender |
| `deviation_from_avg` | How far a given transaction deviates from sender baseline |

### Persisted history: SQLite (decided — see Decisions Log item 3)

A single `aml_agent.db` file, stdlib `sqlite3`, no server. Two tables:

**`queries`** — one row per `/analyze` call, the audit trail of what the agent decided to do.
- `id` — primary key, integer autoincrement (no need for UUIDs, single local writer, no distributed
  generation)
- `query_text` — the raw natural-language query
- `timestamp` — when it ran
- `intent_detected`, `filters_applied` (JSON text column), `tools_invoked` (JSON text column),
  `tools_skipped` (JSON text column) — mirrors the `execution_summary` in `AgentResult`

**`flags`** — one row per flagged item across all queries, updated when a judge escalates one.
- `id` — primary key, integer autoincrement
- `query_id` — foreign key → `queries.id`
- `customer_id`, `transaction_id`
- `risk_level`, `pattern_detected`, `anomaly_score`, `explanation`
- `escalation_action` — the agent's *recommended* action (MONITOR/REVIEW/REPORT), set at insert time
- `escalated_at` — nullable; set by `POST /api/v1/escalate` when a judge actually acts on it,
  distinguishing "the agent recommended REPORT" from "someone actually clicked escalate"

No soft-delete, no retention/purge logic — this is a single-demo-run audit log, not a production
compliance system; rows just accumulate for the life of the local `aml_agent.db` file.

> Indexes: `flags.query_id` (FK lookup), `flags.transaction_id` and `flags.customer_id` (the
> lookups `/escalate` and any future customer-history view would use).

---

## RBAC Model (as implemented)

**N/A — omitted.** No authentication, no user accounts, no roles. This is a single-user local tool
for a hackathon demo; anyone running it locally has full access to every function.

---

## API Endpoints (as implemented)

**[PLANNED — NOT BUILT]** — no FastAPI app exists yet. Two routes, final (see Decisions Log item 2):

### `agent` router — prefix `/api/v1`
File: `api/routes/agent.py` (not yet created)

- `POST /api/v1/analyze` → Body: `{query: str}`. Runs the full agent loop (intent parse → dynamic
  tool selection → execution → structured result), writes a `queries` row and one `flags` row per
  flagged item to SQLite, and returns an `AgentResult` (see Non-Functional Notes for the shape).
- `POST /api/v1/escalate` → Body: `{flag_id: int, action: str}`. Sets `flags.escalated_at` for that
  row in SQLite and returns confirmation. This is a real, small endpoint now that SQLite exists —
  not a stub — it's what makes the "judge clicks escalate" interaction in the UI actually persist.

**Cut:** `GET /api/v1/customer/{customer_id}/risk-profile` — not building (see Decisions Log item 2).
Entity-lookup queries route through `/analyze` instead.

No auth guards planned (no auth exists). No rate limiting planned (single local user, not needed).

---

## Frontend Pages & Routes (as implemented)

**[PLANNED — NOT BUILT]** — no React project scaffolded yet (`npm create vite@latest` not yet run).

Planned as a single page, no router library needed initially:

```
/  → App.tsx (single page, no auth guard — none exists)
```

**Page contents (all planned, nothing rendered yet):**
- Query input box — free-text natural language query, submits to `POST /api/v1/analyze`
- Execution summary panel — shows intent detected, filters applied, tools invoked vs. skipped, and
  why (this is the "transparent decision flow" differentiator — must be visually prominent, it's
  what a judge reads first)
- Flagged items table — customer/transaction ID, amount, risk level, pattern detected, explanation,
  escalation action, with an "Escalate" button per row that calls `POST /api/v1/escalate`
- Risk charts — Plotly, embedded (risk distribution, temporal pattern chart; exact chart set not
  finalized)

No second page/route planned — the customer risk-profile page was cut (Decisions Log item 2), so
`react-router` isn't needed; this stays a true single-page app.

---

## Auth & Onboarding Flow (as implemented)

**N/A — omitted.** No accounts, no login, no onboarding. Running the app locally *is* the entire
access flow: clone repo, install dependencies, run backend + frontend, use it.

---

## Non-Functional Notes (actual, not aspirational)

| Area | Current state |
|---|---|
| Rate limiting | None — not needed, single local user, no public exposure |
| Security headers | None — not needed, no deployment, no public network exposure |
| Error handling | **[PLANNED — NOT BUILT]** — no error handling exists yet; plan is FastAPI's standard exception handlers + Pydantic validation errors surfaced to the frontend as-is |
| DB query performance | Bulk data has no DB — Parquet + DuckDB chosen so aggregation queries stay fast without one. SQLite (`queries`/`flags`) is small, append-mostly, no perf concern at hackathon scale |
| Concurrency safety | Parquet data: read-only after startup, no concern. SQLite: single local process, SQLite's own file locking is sufficient for a single-user demo — no pooling/queueing needed |
| Deployment status | **Not deployed, not planned to be.** Explicit hackathon-scope decision — runs via local `uvicorn` + local Vite dev server only |
| Testing coverage | **[PLANNED — NOT BUILT]** — zero tests exist. Planned minimum: intent parsing, rule engine, risk classification (Pytest) |
| Structured output validation | **[PLANNED — NOT BUILT]** — Pydantic models for `AgentResult` not yet written |
| Caching | **[PLANNED — NOT BUILT]** — `functools.lru_cache` on tool functions, precomputed baselines at enrichment time; nothing implemented |

---

## Environment Variables (as required by the actual code)

**[PLANNED — NOT BUILT]** — no `.env` files or config loading exist yet. Anticipated variables based
on decided stack:

### Backend (`.env`, not yet created — gitignored, `.env.example` with placeholders committed instead)
```
# Which provider the agent loop uses — judges set this to match whichever key they have
LLM_PROVIDER=anthropic     # anthropic | groq | gemini

# Only the key matching LLM_PROVIDER is required; the other two are unused if unset
ANTHROPIC_API_KEY=
GROQ_API_KEY=
GOOGLE_API_KEY=

# Data paths
DATA_DIR=./data          # optional, defaults to ./data if unset — path to enriched Parquet files
SQLITE_PATH=./aml_agent.db   # optional, defaults to ./aml_agent.db if unset

# Kaggle: not needed at runtime — dataset fetch is a documented manual download step in README
# (see Decisions Log item 6), not a scripted/credentialed part of the app itself.
```
Startup behavior if a required LLM key is missing: **[PLANNED — NOT BUILT]**, needs to be decided —
recommend failing fast at FastAPI startup with a clear error rather than failing on first query.

### Frontend (`.env`, not yet created)
```
VITE_API_BASE_URL=http://localhost:8000   # FastAPI backend URL, no default decided yet
```

---

## Divergence From Original Plan (for historical context)

- **Built and exceeds original scope**: None yet — nothing is built.
- **Built as originally scoped**: None yet — nothing is built.
- **Not built**: Everything. This spec is entirely pre-implementation. Two decisions have already
  reversed once during planning — worth remembering so neither flips a third time without a new
  reason showing up:
  1. Interface layer: started as "Streamlit only, skip FastAPI" → reversed to "React + FastAPI" once
     the team confirmed a 2-person split where a real frontend/backend boundary earns its keep (see
     Open Question 1 — if it turns out to be solo, revisit whether Streamlit-only was the better call).
  2. Agent orchestration: started as a hand-rolled provider SDK loop → briefly changed to headless
     Claude Code CLI for the dev-speed win → reversed back to the hand-rolled loop once "judges can
     drop in any provider's key" turned out to be incompatible with Claude Code being Anthropic-only
     and requiring a separate CLI install (see Decisions Log item 4a). Claude Code remains usable as
     a dev-time coding assistant, just not as the query-time engine.

  As of this revision, all seven previously-open items in the Decisions Log are resolved. Nothing
  is drifting silently — if a future session wants to change one of these, treat it as a deliberate
  reversal worth logging here, the same way the two above were, not a silent edit.

---

*Everything in this document is derived from planning-session decisions, not from any existing
code. Once implementation starts, update the marked sections in place and remove
`[PLANNED — NOT BUILT]` tags as each piece actually ships — don't let this drift into aspirational
documentation.*

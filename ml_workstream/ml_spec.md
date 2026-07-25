# ml_spec.md

## ML Workstream Spec — AML Detection Agent

> Subordinate to `spec.md` (the project bible). Nothing here overrides it — this document exists to
> break the ML-owner's slice of `spec.md` into an actionable, sequenced plan. Where this doc and
> `spec.md` disagree, `spec.md` wins; fix this file, not the other way round.

**Owner scope**: one person (solo), covering everything under `spec.md`'s "Data proc", "Detection",
and "Explain" stack rows — i.e. everything from raw CSV to a scored, explained, tool-callable output.
Not owned: React frontend, FastAPI routes/app wiring, SQLite persistence, LLM provider abstraction,
orchestration loop itself. Those are the teammate's side of the 2-person split (`spec.md` Decisions
Log item 1).

Status: Phase 1 (dataset understanding, `phase1.md`) done. This doc covers Phase 2 onward.

---

## Interface contract with teammate (read this first)

The agent loop (teammate's code) calls tool functions by name with JSON-serializable args and expects
JSON-serializable results back, validated against `AgentResult` (Pydantic, teammate-owned). Five of
the six tools listed in `spec.md`'s Agent Orchestration section are this workstream's responsibility:

| Tool | Owner | Notes |
|---|---|---|
| `eda` | **ML** | ad-hoc query over enriched Parquet via DuckDB |
| `feature_eng` | **ML** | on-demand feature computation for a query scope |
| `anomaly` | **ML** | run detector(s), return scores |
| `risk` | **ML** | classify/score → risk level |
| `explain` | **ML** | template NL generation tied to firing rule |
| `escalate` | teammate | writes `flags.escalated_at` in SQLite — not ML's |

Each ML tool function must:
- Take plain args (dicts/primitives — no custom classes crossing the boundary)
- Return a plain dict matching an agreed shape (defined per-tool below)
- Never raise on bad input — return a structured error dict instead (teammate's loop needs a
  predictable shape to feed back to the LLM as a tool result, not a stack trace)
- Be pure / side-effect-free except reading the Parquet files — no writes to SQLite, no network calls

Lock these signatures early and hand them to teammate before wiring the orchestration loop, so
integration isn't blocked on your model work finishing.

---

## Phase 2 — Enrichment pipeline

**Deliverable**: `data/HI-Small_Enriched.parquet`, produced by a one-time script
(`scripts/enrich.py`, not yet written), gitignored input → gitignored output, loaded into memory by
FastAPI at startup (teammate's side) and queried via DuckDB.

Column list is fixed by `spec.md` (line 153-163) — don't add/rename without updating `spec.md` too:

| Column | Source | Computation |
|---|---|---|
| `is_suspicious` | `HI-Small_Patterns.txt` | boolean; row matched to a pattern-file entry, **not** just `Is Laundering==1` — see Phase 1 finding: pattern file (3,209 rows) is a subset of `Is Laundering==1` rows (5,177). Decide and document which one `is_suspicious` actually tracks — recommend keeping both: `is_laundering` (raw label, authoritative) and `is_suspicious`/`aml_pattern` (pattern-file match, richer but partial) |
| `aml_pattern` | `HI-Small_Patterns.txt` block header | one of the 8 motif types found in Phase 1 (FAN-OUT, FAN-IN, CYCLE, GATHER-SCATTER, SCATTER-GATHER, BIPARTITE, STACK, RANDOM), else `NORMAL` |
| `hour`, `day_of_week` | `Timestamp` | `pd.to_datetime` then `.dt.hour` / `.dt.day_name()` — flag/exclude 2022-09-01 per Phase 1 volume-spike finding when this feeds baselines |
| `amount_category` | `Amount Received` | bucket into micro/small/medium/large/xlarge/xxlarge — define bucket edges off the log-amount distribution from `01_exploration.ipynb` §5, not arbitrary round numbers |
| `txn_count`, `total_volume`, `avg_amount`, `std_amount` | groupby `From Account` | per-sender baseline stats, computed once at enrichment time (this is the "precomputed baselines" caching strategy from `spec.md`) |
| `unique_receivers` | groupby `From Account` → `To Account` | reuses Phase 1's out-degree computation (`01_exploration.ipynb` §7) |
| `deviation_from_avg` | row `Amount Received` vs sender's `avg_amount`/`std_amount` | z-score style: `(amount - avg_amount) / std_amount`, guard div-by-zero for single-transaction senders |

Also carry forward from Phase 1 as extra columns (not in the original spec list, but justified by
findings — note the addition inline in the script, don't silently diverge from the spec table):
- `currency_mismatch` (Payment Currency ≠ Receiving Currency) — flagged in `phase1.md` §5/§6
- `is_self_loop` (From Account == To Account) — 11.6% of rows, needs an explicit decision either way
- `payment_format_risk` — Payment Format is the single strongest class-separating signal found
  (86.6% of laundering rows are ACH vs 11.8% normal) — encode this, don't leave it as raw categorical

**Join**: `From Bank`/`To Bank` → `accounts.Bank ID`, `From Account`/`To Account` →
`accounts.Account Number`. Both confirmed 100% overlap in Phase 1 — safe to inner-join without a
fallback path.

---

## Phase 3 — Feature engineering (agent-ready)

**Deliverable**: `data/HI-Small_Agent_Ready.parquet` + a `feature_eng` tool function that can compute
scoped features on demand (e.g. "features for customer 4521" rather than the whole table), since the
agent will call this per-query, not just once at enrichment time.

`feature_eng(scope: dict) -> dict`:
- `scope` examples: `{"account_id": "8000EBD30"}`, `{"date_range": [...], "min_amount": ...}`
- Returns the relevant enriched rows/aggregates as records, not a DataFrame (JSON boundary)

Feature set = enrichment columns above, plus anything genuinely query-time-only (e.g. rolling velocity
over a caller-specified window) that doesn't belong precomputed.

---

## Phase 4 — Anomaly detection

**Deliverable**: `anomaly(scope, method?) -> dict` tool function.

Per `spec.md`: scikit-learn Isolation Forest + LOF + Z-score, **plus** a plain-Python rules engine —
two separate mechanisms, not one blended model. Sequence:

1. Train Isolation Forest + LOF on enriched feature set (unsupervised — imbalance from Phase 1
   (0.102% positive) makes supervised classification unreliable as the sole method; treat
   `is_laundering`/`is_suspicious` as evaluation labels, not training targets, for the unsupervised
   pass).
2. Z-score on `deviation_from_avg` and other per-account baselines — cheap, explainable, catches the
   "way outside this sender's own history" case directly.
3. Rules engine (plain Python, not sklearn) encodes the pattern-motif knowledge from `phase1.md` §9 —
   e.g. out-degree > threshold in a short window → fan-out candidate; cycle detection over the account
   graph → cycle candidate. This is what makes `aml_pattern` predictions explainable by *name*, not
   just by anomaly score.
4. Combine: anomaly score (continuous) + rule hits (categorical/boolean) both feed Phase 5 risk
   scoring — don't collapse them into one number before risk classification, keep them as separate
   signals so `explain` can cite which one fired.

Return shape: `{"anomaly_score": float, "method_scores": {...}, "rule_hits": [...]}`.

---

## Phase 5 — Risk classification

**Deliverable**: `risk(anomaly_result, context?) -> dict`.

Maps combined signal (Phase 4 output) → discrete risk level + recommended action, matching the
`flags` table shape teammate already spec'd (`spec.md` line 177-184):
- `risk_level` — needs a defined scale (e.g. LOW/MEDIUM/HIGH/CRITICAL — pick one, document here once
  chosen, this is currently undecided and blocks the `flags` table format teammate is building against)
- `pattern_detected` — from rule engine hits / `aml_pattern`
- `anomaly_score` — passthrough from Phase 4
- `escalation_action` — recommended MONITOR/REVIEW/REPORT (per `spec.md` line 182), this is *your*
  output; the judge's actual click is teammate's `/escalate` endpoint, separate concern

This function's output is exactly what gets written into a `flags` row — the shape must match
teammate's SQLite schema field-for-field. Confirm field names with teammate before finalizing, don't
let two independently-guessed shapes diverge.

---

## Phase 6 — Explanation generation

**Deliverable**: `explain(risk_result) -> dict` → `{"explanation": str}`.

Template-based, not SHAP/LIME (per `spec.md`). One template per rule/pattern/threshold that can fire,
filled with the actual values that triggered it. E.g.:
> "Account {account_id} sent to {unique_receivers} distinct receivers within {window}, {std_dev}σ
> above its own historical average — consistent with FAN-OUT pattern."

Keep a template per `aml_pattern` type found in Phase 1 (8 motifs) plus one for pure-anomaly-score
(no rule fired, but Isolation Forest/LOF flagged it anyway) plus one for currency-mismatch/
payment-format-risk-only flags, so every possible `risk` output has a matching explanation path —
no silent "no explanation available" case.

---

## Phase 7 — EDA tool (ad-hoc, for the agent)

**Deliverable**: `eda(query_spec) -> dict`.

Distinct from Phase 1's exploration notebook — this is the *runtime* tool the agent calls for
open-ended questions ("how many high-risk transactions last week"), executed via DuckDB directly over
the enriched Parquet (per `spec.md`'s Query row). Thin wrapper: translate a constrained query-spec
dict into a DuckDB SQL string, execute, return records. Keep the query surface constrained (don't let
this become arbitrary-SQL-from-LLM — that's an injection risk even in a local single-user tool; build
it as a small set of parameterized query shapes, not raw SQL passthrough).

---

## Testing (Pytest, per `spec.md`)

Minimum, matching `spec.md`'s stated coverage target:
- Rule engine: each of the 8 motif rules, with a synthetic transaction sequence that should/shouldn't
  fire it
- Risk classification: known anomaly-score + rule-hit combinations → expected risk_level/action
- (Intent parsing is teammate's — not ML's test surface)

---

## Open decisions (blocking, need answers before Phase 5/6 finalize)

1. **Risk level scale** — not yet defined (LOW/MEDIUM/HIGH/CRITICAL vs numeric vs something else).
   Blocks `flags` table integration with teammate.
2. **`is_suspicious` definition** — pattern-file match vs raw `Is Laundering==1` vs both as separate
   columns (recommended: both, see Phase 2 table).
3. **Self-loop rows (11.6% of data)** — keep, drop, or flag-only. Affects baseline stats if kept
   unflagged.
4. **Amount-category bucket edges** — need concrete thresholds off the log-distribution, not chosen
   yet.

Resolve these before Phase 4/5 code is finalized — flag to teammate as soon as decided since #1
blocks their schema work too.

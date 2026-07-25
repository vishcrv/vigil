# ml_spec.md

> **Ported into the app 2026-07-25.** This workstream was developed standalone in
> `ml_workstream/` on the `model-work` branch and has been moved into `backend/`. Paths written
> below as `ml/…`, `scripts/…`, `data/…` and `tests/…` are all relative to `backend/` now, and
> every data path resolves from `DATA_DIR` (see `ml/data.py`) rather than the repo root. The
> five tools are registered into `agent/loop.py` from `ml/tools.py`; `backend/tools/*.py` are
> thin re-export shims providing the public surface `spec.md` documents.

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

## Open decisions — all resolved

1. **Risk level scale** — **RESOLVED: `LOW` / `MEDIUM` / `HIGH` / `CRITICAL`** (string enum, four
   tiers). Maps onto the three `escalation_action` values from `spec.md` line 182 as
   LOW→MONITOR, MEDIUM/HIGH→REVIEW, CRITICAL→REPORT. Four tiers rather than three so the
   flagged-items table stays usable for triage — collapsing "unusual" and "almost certainly
   laundering" into one HIGH bucket removes the distinction a judge most needs. **Tell teammate:
   `flags.risk_level` is a 4-value TEXT column.**
2. **`is_suspicious` definition** — RESOLVED in Phase 2: both columns kept (`is_laundering` raw
   label, `is_suspicious`/`aml_pattern` pattern-file match). See `phase2.md` §2.
3. **Self-loop rows (11.6%)** — RESOLVED in Phase 2: kept and flagged via `is_self_loop`, not
   dropped. Phase 4 excludes them from the *graph* pass only (a transfer to yourself has no
   counterparty and cannot form a motif); they remain in baselines and detector features.
4. **Amount-category bucket edges** — RESOLVED in Phase 2: 6 quantile buckets over
   `log1p(Amount Received)`. See `phase2.md` §2.

### Phase 4 decisions (recorded here, not in the original list)

5. **Where detectors are fit** — pre-fit once at build time (`scripts/train_models.py`), artifacts
   persisted to `data/models/`. Fitting per-call was rejected: a 70-row account scope produces a
   meaningless forest, and scores would shift between identical calls. LOF uses `novelty=True` so
   it can score unseen rows.
6. **Where graph rules run** — batch precompute (`scripts/build_rule_hits.py` →
   `data/HI-Small_Rule_Hits.parquet`), joined at query time. Multi-hop motifs over 1,015,736 edges
   are too slow per-call, and a cycle reaching outside the caller's scope would be invisible to a
   scope-local pass — exactly the multi-hop structure these rules exist to catch.

### Post-review decisions (audit of the five shipped tools)

7. **Caller dates are normalized, never passed through** (`ml/dates.py`). `Timestamp` is a
   VARCHAR in `YYYY/MM/DD HH:MM` form, compared lexicographically, and `/` sorts above `-` —
   so an ISO-8601 bound from the agent matched **zero rows and returned success**: `anomaly`
   scored nothing and `risk` reported LOW/MONITOR on an account it had never looked at. Every
   bound is now rewritten to the stored format, a bare date widens to cover its whole day
   (`00:00` lower, `23:59` upper), and anything unparseable is a structured error rather than
   an empty result. **Tell teammate: the tool schemas accept either separator; no date
   formatting is required on their side.**
8. **Truncation is reported, not silent.** `anomaly` scores at most `MAX_SCORE_ROWS` (5,000)
   newest rows and now returns `rows_matched` and `truncated` alongside `row_count_scored`;
   `risk` carries them into `contributing_signals` so a flag can say its score covers part of
   a larger scope.
9. **`risk` rejects non-anomaly input.** A dict without `row_count_scored` used to fall through
   to the "nothing matched" branch and come back LOW/MONITOR — an unexamined account reported
   as clean. Same failure class as 7, so it is closed the same way.
10. **Detectors are blended by measured lift, not equally.** `metadata.json` now carries
    `method_weights` (Isolation Forest 0.64, LOF 0.30, z-score 0.05), derived from each
    method's average-precision lift over the base rate. Under the previous unweighted mean,
    LOF at 1.6x lift moved the headline as much as the forest at 2.3x. z-score is now
    evaluated against labels too (ROC-AUC 0.536 — near chance, hence its weight).
11. **Reported detector metrics are leak-corrected.** `payment_format_risk` is a target
    encoding computed over the whole dataset, so the held-out split carried an encoding that
    had seen its own labels. `metadata.json` now records `metrics_leak_corrected`, re-deriving
    that encoding from the train split alone: AP 0.00214 → 0.00209. Small, but it is the
    number to quote.
12. **Rule weights are derived, not hand-banded** (`RULE_STATS` → `RULE_WEIGHTS` in
    `ml/risk.py`): Wilson lower bound on each rule's observed precision, log-scaled in lift,
    normalized so the best-evidenced rule is 1.0. The old band table let BIPARTITE (6.7%
    precision on **15** accounts) outrank FAN-OUT (5.0% on **1,736**). After shrinkage:
    SCATTER-GATHER 1.0, GATHER-SCATTER 0.81, FAN-OUT 0.35, STACK 0.24, CYCLE 0.20, RANDOM
    0.16, BIPARTITE 0.10, FAN-IN 0.01. RANDOM alone no longer reaches MEDIUM.
13. **One enriched Parquet, not two.** `HI-Small_Agent_Ready.parquet` was a byte-identical
    352 MB copy of `HI-Small_Enriched.parquet` — Phase 3 defines no extra precomputed columns,
    so its real deliverable is the `feature_eng` tool. `scripts/enrich.py` writes only
    `HI-Small_Enriched.parquet`.
    **Verified 2026-07-25**: the edited script was re-run from the raw CSVs and reproduces the
    on-disk parquet byte for byte — sha256 `cc5da2ae…3dec2d`, 352,607,697 bytes, 5,078,345 rows
    × 32 columns, 3,209 pattern-matched / 5,177 laundering rows. The rebuild is deterministic,
    and the models, rule hits and validation samples were all built from a parquet the current
    script still produces.
14. **Repeat-query caching** (`ml/cache.py`): `eda`, `feature_eng` and `anomaly` are memoized
    on JSON-canonicalized arguments, results deep-copied on the way out so a caller mutating
    one cannot poison the next. A repeated `anomaly` call goes 1.96s → ~0s. `risk` and
    `explain` are deliberately uncached — their arguments are whole result dicts, so building
    the key costs more than recomputing.

---

## Risk-level validation (measured end-to-end)

`scripts/validate_risk_levels.py` pushes a labelled account sample through the real tool
surface — `dispatch("anomaly")` → `dispatch("risk")`, no shortcut — and cross-tabulates the
emitted `risk_level` against ground truth. Artifacts: `data/models/risk_validation.json`
(summary) and `risk_validation_records.json` (raw scored sample, used by the constant sweep).
Deliberately *not* written into `metadata.json`, which `train_models.py` rewrites wholesale.

**Sample**: 600 laundering-involved + 1,400 clean accounts, seed 42, 2,000 scored with zero
errors. Ground truth is account-level — an account counts as laundering-involved if it appears
on either side of at least one `is_laundering` transaction, matching `risk`'s `role="both"`
scoping. Population: 6,357 laundering-involved / 508,723 clean = **1.234% base rate**.

**Weighting**: the laundering stratum is deliberately over-sampled (a random 2,000 would hold
~25 positives, far too few per level), so each observation is reweighted to its population
share — Horvitz-Thompson, weights 10.6 (laundering) and 363.4 (clean). Raw sampled proportions
would overstate precision by more than an order of magnitude and must not be quoted.

**Baseline, before the blend constants were tuned** (kept for comparison — the current numbers
are in "Blend tuning" below):

| Risk level | Sampled n | Sampled launderers | Est. population n | Precision | Worst case | Lift | Recall |
|---|---|---|---|---|---|---|---|
| LOW | 565 | 31 | 194,370 | 0.2% | 0.2% | 0.1x | 5.2% |
| MEDIUM | 1,386 | 526 | 318,074 | 1.8% | 1.8% | 1.4x | 87.7% |
| HIGH | 24 | 18 | 2,371 | 8.0% | 8.0% | 6.5x | 3.0% |
| CRITICAL | 25 | 25 | 265 | 100.0% | **19.5%** | 81.0x | 4.2% |

Recall at MEDIUM+ 94.8%, HIGH+ 7.2%, CRITICAL+ 4.2%.

**Read this table carefully — three things in it are easy to misreport.**

1. **CRITICAL precision is not 100%.** All 25 sampled CRITICAL accounts were launderers and
   *no* clean account reached CRITICAL, so the cell has no observed false positives and both
   the point estimate and the stratified bootstrap collapse to exactly 100%. That is an empty
   cell, not certainty. The rule-of-three bound (0 in 1,400 clean → rate up to 3/1,400 → up to
   ~1,090 estimated clean accounts) puts the honest range at **~20%–100%**. Quote the worst
   case, or quote the range; never the bare 100%.
2. **MEDIUM is a dumping ground.** An estimated 318,074 accounts — **~62% of the entire
   population** — at 1.8% precision. A triage tier holding two-thirds of all accounts sorts
   nothing, and this is the clearest miscalibration in the system.
3. **Coverage at the actionable tiers is small.** HIGH+ catches 7.2% of laundering-involved
   accounts, CRITICAL+ 4.2%. The ordering across levels is monotonic and directionally
   correct (0.2% → 1.8% → 8.0% → ~100%), but nearly everything lands in one bucket.

Cells with fewer than `MIN_CELL_FOR_STABLE_ESTIMATE` (10) sampled accounts are flagged
`noisy` in the JSON and marked in the rendered table — HIGH at n=24 is thin enough that its
3.7%–25.1% interval is the honest width.

---

## Blend tuning (`scripts/tune_blend.py`)

`DETECTOR_WEIGHT`, `RULE_BASE_CREDIT`, `BENIGN_DETECTOR_DAMPING` and the four `RISK_THRESHOLDS`
were hand-picked. They are now searched against an explicit objective over the validated
sample, replaying `risk()` on cached `anomaly()` output (no account is re-scored; scores depend
only on the weights, so the sweep computes 2,000 scores per weight triple and evaluates all 416
threshold sets against that vector).

**Objective, after two revisions — both forced by the search gaming the previous version:**

```
minimize   the largest actionable tier's share of population   (MEDIUM, HIGH or CRITICAL)
subject to precision strictly increasing LOW < MEDIUM < HIGH < CRITICAL
           recall at HIGH+   >= baseline (7.167%)
           recall at MEDIUM+ >= baseline (94.833%)
           precision at CRITICAL >= 25%
```

1. *Minimize MEDIUM's share* alone drove MEDIUM to 0.4% by squeezing it into a 0.05-wide band
   and moving **80% of the population into HIGH**. Every constraint held; the dumping ground
   was relabelled, not removed. Scoring the largest tier instead makes relabelling worthless.
2. *Minimize the largest actionable tier* then inflated CRITICAL to 22% of the population at
   3% precision — spreading the load by destroying the one tier that worked. Hence the
   CRITICAL precision floor.
3. Recall floors are read off the baseline at runtime, not hardcoded: the rounded 7.2% / 94.8%
   in the table above made the *current* constants fail their own constraint (true HIGH+
   recall is 7.167%).

**Result** — `DETECTOR_WEIGHT` 0.40→0.30, `RULE_BASE_CREDIT` 0.60→0.40,
`BENIGN_DETECTOR_DAMPING` 0.50→0.25, thresholds CRITICAL 0.75→0.55, HIGH 0.50→0.25,
MEDIUM 0.25→0.20:

| Level | Share before | Share after | Precision before | after | Recall before | after |
|---|---|---|---|---|---|---|
| LOW | 37.7% | 37.8% | 0.17% | 0.10% | 5.2% | 3.0% |
| MEDIUM | **61.8%** | **34.0%** | 1.75% | 1.13% | 87.7% | 31.0% |
| HIGH | **0.5%** | **28.2%** | 8.04% | 2.69% | 3.0% | 61.5% |
| CRITICAL | 0.1% | 0.1% | 100% | 100% | 4.2% | 4.5% |

Recall at MEDIUM+ 94.8% → **97.0%**, HIGH+ 7.2% → **66.0%**, CRITICAL+ 4.2% → 4.5%. Accounts
moved *up*, which is the check that matters: shrinking MEDIUM by pushing true positives down
into LOW would have improved the objective while making the product worse.

**Held-out check.** The constants were selected on the seed-42 sample, so they were re-measured
end-to-end on seed 7 — 2,000 accounts the search never saw:

| Level | Precision (seed 42) | Precision (seed 7) | Recall (seed 42) | Recall (seed 7) |
|---|---|---|---|---|
| LOW | 0.1% | 0.2% | 3.0% | 4.3% |
| MEDIUM | 1.1% | 1.0% | 31.0% | 30.8% |
| HIGH | 2.7% | 2.7% | 61.5% | 60.5% |
| CRITICAL | 100% (worst 20.8%) | 100% (worst 20.2%) | 4.5% | 4.3% |

MEDIUM+ recall 97.0% / 95.7%, HIGH+ 66.0% / 64.8%. The gain reproduces on unseen accounts, so
this is tuning rather than fitting to 2,000 sampled accounts.

**What this does not do.** Total actionable share is unchanged: 62.3% → 62.2% of accounts still
land at REVIEW-or-above, and `ESCALATION_ACTIONS` maps both MEDIUM and HIGH to `REVIEW`, so the
recommended *action* for those accounts is identical. The gain is ordering inside that bucket —
HIGH now carries 61.5% of all launderers at 2.4x MEDIUM's precision, so a reviewer working HIGH
first sees far better yield. It is not a reduction in review workload, and should not be
described as one.

Two tests changed because they asserted levels derived from the old constants, not because
behaviour regressed: `test_detector_alone_cannot_reach_critical` expected the literal `MEDIUM`
(DETECTOR_WEIGHT now lands in HIGH — rewritten to assert the invariant that it is never
CRITICAL), and `test_self_loop_zero_risk_format_rows_damp_the_detector` expected the literal
`LOW` (now asserts damping costs a tier relative to the same score undamped).

---

## Hand-off to teammate — `ml/tools.py`

The signatures ml_spec said to lock early are now a module, so the loop does not need to know
anything about this workstream's internals:

```python
from ml.tools import TOOL_SCHEMAS, dispatch, FLAGS_COLUMN_MAP

# TOOL_SCHEMAS: list of {name, description, input_schema} in plain JSON Schema — what
#   Anthropic tool_use, OpenAI/Groq function-calling and Gemini all consume, modulo a
#   wrapper key each. No SDK is imported anywhere in this workstream.
result = dispatch(tool_name, tool_args)   # always a dict, never raises
```

`dispatch` returns a structured error for an unknown tool name, a non-object argument payload,
or an argument the schema does not declare (a hallucinated argument must not be silently
dropped — the query that runs would not be the one the model asked for).

**`flags` row assembly.** `FLAGS_COLUMN_MAP` maps each `flags` column to the `risk()` key that
fills it. Three things worth stating outright:
- `explanation` is **not** in the risk result — call `explain(risk_result)` and read
  `["explanation"]`.
- `transaction_id` is **TEXT**, not an integer: the Kaggle data has no transaction key
  (`phase1.md` §9), so it is a deterministic `tx_`-prefixed hash of the identifying tuple.
- `risk_level` is TEXT, one of four values (`LOW`/`MEDIUM`/`HIGH`/`CRITICAL`).
- `risk()` also returns `risk_score` (the blended 0-1 figure) and `contributing_signals`, and
  the current `flags` schema has no column for either. `risk_score` is the right sort key for
  the flagged-items table — either add a column or sort by `risk_level` then `anomaly_score`.

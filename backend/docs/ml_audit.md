# ml_audit.md

## ML workstream audit — 2026-07-25

Session context for whoever picks this up next. Scope was **ML only** (`ml_workstream/`) — the
five ML-owned tools from raw data to a scored, explained, tool-callable output. FastAPI, React,
SQLite and the orchestration loop are the teammate's side and were deliberately not touched.

Branch: `model-work`. Three commits added on top of `895e682`:

| Commit | What |
|---|---|
| `1b5fb72` | date normalization, truncation reporting, lift-weighted detectors, tool registry |
| `89972c3` | derived rule weights, duplicate parquet dropped, tool-call caching |
| `d54868f` | decisions + hand-off recorded in `ml_spec.md` |

Test count went 189 → **305, all passing**.

---

## What the audit found

The ML code was in good shape structurally — five tools, clean contracts, real measurement
behind the thresholds. The problems were concentrated in two places: **input handling that
failed silently**, and **weights that looked measured but weren't**.

### P0 — silent wrong answers (fixed)

**1. ISO dates matched zero rows and reported success.**
`Timestamp` is stored as a VARCHAR in `YYYY/MM/DD HH:MM` form, so DuckDB compares it
lexicographically. `/` is 0x2F, `-` is 0x2D, so `'2022/09/01 00:00' <= '2022-09-05'` is **false**
— an ISO-8601 upper bound excluded every row in the dataset.

```
eda between ['2022-09-01','2022-09-05']  ->  n = 0          (before)
eda between ['2022/09/01','2022/09/05']  ->  n = 2,284,182
```

An LLM emits ISO for "last week" every time. No error was raised: `anomaly` returned
`row_count_scored: 0`, and `risk` then returned **LOW / MONITOR** — the tool confidently
reporting an account clean when it had looked at nothing.

Fixed by `ml/dates.py`: every caller bound is rewritten to the stored format before it reaches
SQL, a bare date widens to cover its whole day (`00:00` lower, `23:59` upper), and anything
unparseable returns a structured error instead of an empty result. Wired into
`feature_eng.validate_scope` (so `anomaly` inherits it) and `eda`'s timestamp filter path.

**2. `risk()` scored non-anomaly input as LOW/MONITOR.**
Same failure class from a different direction — `risk({"bogus": 1})` fell through to the
"nothing matched" branch. It now requires `row_count_scored` to be present. `dispatch()`
likewise rejects arguments the schema does not declare rather than silently dropping them (a
hallucinated argument means the query that runs is not the one the model asked for).

**3. `anomaly` truncated silently.**
It scores at most 5,000 rows (`MAX_SCORE_ROWS`, newest first) and reported
`row_count_scored: 5000`, which reads as "we scored everything". Now also returns
`rows_matched` and `truncated`; `risk` carries both into `contributing_signals`.

### P1 — measurement (fixed)

**4. Detectors were blended equally despite unequal quality.**
Isolation Forest 2.3x lift, LOF 1.6x (near chance, and 53s to score vs 2.7s). An unweighted
mean let the weak one move the headline as hard as the strong one. `metadata.json` now carries
`method_weights`, derived from each method's average-precision lift: **IF 0.644 / LOF 0.304 /
z-score 0.052**. z-score is now evaluated against labels too (ROC-AUC 0.536).

**5. Reported metrics leaked.**
`payment_format_risk` is `P(is_laundering | Payment Format)` computed over the whole dataset, so
the held-out split carried an encoding that had seen its own labels. `metadata.json` now records
`metrics_leak_corrected`, re-deriving that encoding from the train split alone:
**AP 0.00214 → 0.00209**. Small, but it is the number to quote.

**6. Rule weights were hand-banded, not derived.**
The old table let BIPARTITE (6.7% precision on **15** accounts) outrank FAN-OUT (5.0% on
**1,736**). Weights now come from `RULE_STATS` via Wilson lower bound → log-scaled lift →
normalized so the best-evidenced rule is 1.0:

| Rule | n | precision | old weight | new weight |
|---|---|---|---|---|
| SCATTER-GATHER | 306 | 100% | 1.00 | 1.000 |
| GATHER-SCATTER | 46 | 52.2% | 0.85 | 0.805 |
| FAN-OUT | 1,736 | 5.0% | 0.25 | **0.347** |
| STACK | 8,858 | 2.7% | 0.15 | 0.237 |
| CYCLE | 1,249 | 2.8% | 0.15 | 0.203 |
| RANDOM | 12 | 8.7% | 0.35 | **0.155** |
| BIPARTITE | 15 | 6.7% | 0.30 | **0.096** |
| FAN-IN | 3,050 | 1.1% | 0.05 | 0.010 |

RANDOM alone no longer reaches MEDIUM. Two test expectations were updated to match the
derivation — they had encoded the old bands, not behaviour anyone had reasoned about.

**7. `anomaly` and `feature_eng` had no tests at all.**
Both P0 bugs lived in exactly those two untested modules. Added `test_anomaly.py` (22),
`test_feature_eng.py` (31), `test_dates.py` (29), plus `test_tools.py` and `test_cache.py`.

### P2 — hygiene (fixed)

**8. 352 MB duplicate parquet.** `HI-Small_Agent_Ready.parquet` was byte-identical to
`HI-Small_Enriched.parquet` (verified by full-file hash before deleting). Phase 3 defines no
extra precomputed columns, so its real deliverable is the `feature_eng` tool.
`scripts/enrich.py` now writes one file; `ml/data.py` falls back to the old path if that is all
a data directory has.

**9. No hand-off surface.** `ml/tools.py` added: `TOOL_SCHEMAS` (plain JSON Schema, no SDK
imported anywhere in this workstream), `dispatch()`, and `FLAGS_COLUMN_MAP`.

**10. No caching.** `ml/cache.py` memoizes `eda`/`feature_eng`/`anomaly` on JSON-canonicalized
arguments, deep-copying results out so a caller mutating one cannot poison the next. Repeat
`anomaly` call: 1.96s → ~0s. `risk`/`explain` deliberately uncached — their arguments are whole
result dicts, so building the key costs more than recomputing.

---

## Current state

**Working.** All five tools run end-to-end on the real 5.08M-row dataset, return
JSON-serializable dicts, and return structured errors rather than raising on malformed input.
305 tests pass.

**Not issue-free.** What follows is known-weak, not broken.

### Open issue 1 — no end-to-end validation of the risk levels *(highest value, ~1h)*

Per-rule precision exists. Per-detector AP exists. The number for **the thing the product
actually outputs** does not: *of the accounts we label CRITICAL, what fraction are actually
laundering?* That is the first question a judge asks about the flagged-items table, and right
now there is no answer. Everything needed to compute it is on disk — score a labelled sample
through `anomaly` → `risk` and cross-tabulate `risk_level` against `is_laundering`.

### Open issue 2 — blend constants are still hand-picked

`DETECTOR_WEIGHT=0.40`, `RULE_BASE_CREDIT=0.60`, `BENIGN_DETECTOR_DAMPING=0.50` and the four
`RISK_THRESHOLDS` are reasoned but not measured. This is the same criticism that was just fixed
one level down for `RULE_WEIGHTS`, left standing at the combining step. Issue 1 would give the
evidence needed to tune them.

### Open issue 3 — detection quality is intrinsically limited

Not a defect, but it should be said out loud before someone else says it:

- Detectors top out around 2.3x lift. That is the ceiling of unsupervised detection at 0.09%
  positives, which is why rules carry the real signal.
- Only 2 of 8 rules are strong (SCATTER-GATHER 133x, GATHER-SCATTER 70x). The other six are
  hints.
- Recall is low across the board — the best single rule is STACK at 7.5%. Most laundering in
  this dataset goes uncaught.

### Open issue 4 — `enrich.py` edited but not re-run

The second parquet write was removed; the file on disk was produced by the previous version.
Because the two outputs were byte-identical the remaining file is correct, but **the edited
script path itself is unexercised**. A fresh `python scripts/enrich.py` (a few minutes) would
confirm it. `train_models.py` *was* re-run — all metrics above come from that run.

### Open issue 5 — cross-workstream, deliberately not fixed here

- **`spec.md` names two parquet deliverables**; only one exists now. `spec.md` wins on conflicts,
  so it needs a one-line correction there rather than reinstating the copy. Recorded as decision
  13 in `ml_spec.md`.
- **`phase1-7.md` are untracked** (deliberately, in `6f7c807`) but `ml_spec.md` cites them
  normatively ("see `phase2.md` §2", "phase4.md §7"). The contract references files that are not
  in the repo.
- **Tests skip rather than fail** when the parquet or models are absent, which is correct — but
  it means a fresh clone with no data runs a much smaller suite than it appears to.

---

## Reproducing from a clean checkout

Order matters; each step depends on the previous one's output.

```bash
# 1. raw CSVs at repo root (gitignored, manual Kaggle download)
#    HI-Small_Trans.csv, HI-Small_accounts.csv, HI-Small_Patterns.txt

cd ml_workstream
pip install -r requirements.txt

python scripts/enrich.py           # -> data/HI-Small_Enriched.parquet   (~352 MB)
python scripts/train_models.py     # -> data/models/*.joblib + metadata.json
python scripts/build_rule_hits.py  # -> data/HI-Small_Rule_Hits.parquet

python -m pytest -q                # 305 passed
```

## Interface for the teammate

```python
from ml.tools import TOOL_SCHEMAS, dispatch, FLAGS_COLUMN_MAP

# TOOL_SCHEMAS: [{name, description, input_schema}] in plain JSON Schema — what Anthropic
#   tool_use, OpenAI/Groq function-calling and Gemini all consume, modulo a wrapper key each.
result = dispatch(tool_name, tool_args)   # always a dict, never raises
```

Five tools: `eda`, `feature_eng`, `anomaly`, `risk`, `explain`. `escalate` is the teammate's —
it writes SQLite, which this workstream must not.

Assembling a `flags` row:

- `explanation` is **not** in the risk result — call `explain(risk_result)["explanation"]`.
- `transaction_id` is **TEXT**, not an integer: the Kaggle data has no transaction key, so it is
  a deterministic `tx_`-prefixed hash of the identifying tuple.
- `risk_level` is TEXT, one of `LOW` / `MEDIUM` / `HIGH` / `CRITICAL`.
- `risk()` also returns `risk_score` (blended 0-1) and `contributing_signals`, and the current
  schema has no column for either. `risk_score` is the right sort key for the flagged-items
  table — either add a column or sort by `risk_level` then `anomaly_score`.
- Dates: the schemas accept either separator and either a bare date or a `HH:MM` time. No date
  formatting is required on the caller's side.

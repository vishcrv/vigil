# Phase 4 — Anomaly Detection

Source: `ml/rules.py`, `ml/features.py`, `ml/anomaly.py`, `scripts/build_rule_hits.py`,
`scripts/train_models.py`, `tests/test_rules.py`.

Deliverable per `ml_spec.md` Phase 4: `anomaly(scope, method?) -> dict`, backed by two
independent mechanisms — sklearn detectors and a plain-Python/SQL rules engine — kept as
separate signals all the way out to the caller so Phase 5 can weigh them and Phase 6 can cite
which one fired.

---

## 1. Decisions taken (recorded in `ml_spec.md` items 5 and 6)

**Detectors are pre-fit at build time**, not per call. A per-call fit on a 70-row account scope
produces a meaningless forest and gives different answers on identical repeated calls. LOF uses
`novelty=True` so it can score rows it never trained on.

**Graph rules are batch-precomputed**, not scope-local. Multi-hop motifs span 4,487,133 edges;
running them per call is far too slow, and a cycle that reaches outside the caller's scope would
be invisible to a scope-local pass — which is exactly the structure these rules exist to catch.

**Risk scale resolved** (open decision #1, was blocking teammate): `LOW`/`MEDIUM`/`HIGH`/`CRITICAL`.
Applied in Phase 5; recorded here because it was answered as part of this phase's planning.

---

## 2. Artifacts produced

| Artifact | Contents |
|---|---|
| `data/HI-Small_Rule_Hits.parquet` | account-level motif hits: `account`, `rule`, `evidence` (JSON), `score` |
| `data/models/isolation_forest.joblib` | IF, 200 trees, fit on 300k scaled rows |
| `data/models/lof.joblib` | LOF, `novelty=True`, `n_neighbors=20`, fit on 50k rows |
| `data/models/scaler.joblib` | StandardScaler fit on the same 300k rows |
| `data/models/metadata.json` | feature order, sample sizes, held-out metrics, score quantile grids |

All gitignored (`data/` is ignored wholesale).

---

## 3. Rules engine — measured, not guessed

Eight motif rules matching the eight `aml_pattern` labels from `phase1.md` §9, encoded as DuckDB
SQL over a normalized `edges` view (self-loops excluded — a transfer to yourself has no
counterparty and cannot form a motif).

Every threshold was chosen by sweeping it against pattern-file ground truth on the full graph.
**Base rate: 3,170 implicated accounts out of 422,726 with at least one non-self-loop edge =
0.75%.** "Lift" below is precision ÷ base rate.

| Rule | Threshold chosen | Accounts hit | Precision | Recall | Lift |
|---|---|---|---|---|---|
| SCATTER-GATHER | ≥6 intermediaries | 306 | **100%** | 9.7% | **133x** |
| GATHER-SCATTER | ≥5 in and ≥5 out | 46 | 52.2% | 0.8% | 69.6x |
| RANDOM | ≥200 txns | 12* | 8.7% | 0.5% | 11.6x |
| BIPARTITE | ≥5 shared | 15 | 6.7% | 0.03% | 8.9x |
| FAN-OUT | ≥8 new receivers/24h | 1,736 | 5.0% | 2.7% | 6.7x |
| CYCLE | ≥90% retention | 1,249 | 2.8% | 1.1% | 3.7x |
| STACK | ±15% per hop | 8,858 | 2.7% | 7.5% | 3.6x |
| FAN-IN | ≥8 new senders/24h | 3,050 | 1.1% | 1.1% | 1.5x |

\* RANDOM's precision/recall/lift are measured *before* the structured-rule exclusion; the shipped
rule subtracts accounts already matched by the other seven, which drops it from 195 accounts to
12 — nearly every high-activity account turns out to have a detectable shape.

**Batch pass total: 15,272 hits across 14,540 distinct accounts**, in ~130s (SCATTER-GATHER's
2-hop path expansion is 109s of that; everything else is under 7s).

**Honest read of this table.** Only SCATTER-GATHER and GATHER-SCATTER are strong. FAN-IN at 1.5x
lift is barely better than picking accounts at random and is kept only because the motif is
spec-required and the pattern file contains 40 FAN-IN groups — it should not be given meaningful
weight in Phase 5 scoring. STACK has the best recall of any rule (7.5%) at low precision, so it
works as a net, not as evidence on its own. Recall is understated across the board: the pattern
file covers 3,209 of 5,177 laundering rows (`phase1.md` §9), so an account correctly flagged for
laundering that isn't in the pattern file counts as a false positive here.

### 3a. Three defects found by measuring, not by reading

The first version of this engine was substantially wrong and only the ground-truth evaluation
surfaced it:

1. **GATHER-SCATTER matched exactly 1 account.** It compared lifetime `min(ts)`/`max(ts)` per
   account, which is unsatisfiable on an 18-day dataset — any account active across the span
   fails it. Rewritten to pivot on each inflow event with real time windows: **46 accounts at
   52% precision.**
2. **The obvious fix for #1 hung the machine.** Joining inflow→inflow→outflow per account is
   correct but materializes I×I×O rows before grouping — billions on a hub account (max
   out-degree 14,230). Killed at 21 min CPU / 3.1 GB. Rewritten with `FILTER`ed window frames
   over a deduplicated event stream: **1.1s**.
3. **FAN-OUT cost 575s** via a quadratic per-sender self-join. Reformulated to dedupe to
   first-contact per `(src, dst)` and count rows in a `RANGE` window: **1.1s**, and a better
   signal — a burst of *new* counterparties, rather than an established account paying its usual
   eight suppliers every day.

Initial untuned thresholds also produced 126,508 hits across 124,452 accounts (RANDOM alone hit
90,157 at ≥20 txns, 1.1% precision). Retuning cut that by roughly an order of magnitude while
raising precision on every rule.

---

## 4. Detectors — held-out results

Trained on a 500k reservoir sample (seed 42), split 300k train / 200k eval, disjoint.
Positive rate in sample 0.0986%, matching `phase1.md` §6's 0.102%.

| Detector | ROC-AUC | Average precision | vs. baseline AP 0.00092 |
|---|---|---|---|
| Isolation Forest | 0.794 | 0.00214 | 2.3x |
| LOF | 0.638 | 0.00147 | 1.6x |

Unsupervised throughout — `is_laundering` is used only to evaluate, never as a training target
(`ml_spec.md` Phase 4 step 1, on the 0.102% imbalance).

**Honest read.** ROC-AUC 0.794 is a reasonable unsupervised result at this prevalence; average
precision stays tiny because 1-in-1000 base rates make precision hard regardless of ranking
quality. LOF is the weak link — 0.638 AUC and 73s to score 200k rows, the slowest component by
far. `spec.md` mandates it, so it ships, but it earns its place poorly and Phase 5 should weight
it below the forest. The rules engine's best motifs (133x and 70x lift) are considerably stronger
evidence than either detector.

### 4a. Score combination

Raw IF and LOF scores are unbounded and not comparable to each other or to a z-score. Training
persists a 1001-point quantile grid of each method's score distribution over the eval split;
`anomaly()` maps every raw score to its percentile against that reference. All three methods then
mean the same thing — "more anomalous than X% of transactions" — which makes both averaging and
the Phase 6 templates defensible.

- Per row: `anomaly_score` = mean of the method percentiles. Averaging calibrated percentiles
  keeps the result calibrated; taking the max would push scores toward 1.0 purely because three
  detectors were consulted instead of one.
- Per scope: headline `anomaly_score` = the **max** row score, with `mean_anomaly_score` also
  returned. One clearly anomalous transfer among 500 routine ones is the case this tool exists to
  surface, and a scope-level mean would bury it.

---

## 5. Tool contract

```python
anomaly(scope: dict, method: str = "all") -> dict
```

`scope` is the same shape `feature_eng` accepts — validation and WHERE-building are imported from
`ml/feature_eng.py` rather than reimplemented, so the two tools cannot drift apart.
`method` ∈ `isolation_forest` | `lof` | `zscore` | `all`.

```python
{
  "scope": {...normalized...},
  "method": "all",
  "row_count_scored": 71,
  "anomaly_score": 0.8977,          # max row score in scope
  "mean_anomaly_score": 0.3617,
  "method_scores": {"isolation_forest": 0.946, "lof": 0.943, "zscore": 0.997},
  "rule_hits": [{"account", "rule", "score", "evidence": {...}}],
  "rule_names": ["FAN-OUT", "RANDOM", "STACK"],
  "top_rows": [ {...10 highest-scoring rows, each with per-method scores...} ],
}
```

Never raises. Bad scope, unknown method, missing model artifacts, and empty scope-match all
return structured dicts (`{"error": ...}` or a `note`), per the `ml_spec.md` interface contract.

Rule hits and detector scores are returned side by side and never blended — `ml_spec.md` Phase 4
step 4 requires this so `explain` can name which signal fired.

**The two signals are at different granularities**, and Phase 5/6 must not conflate them:
`rule_hits` are **account-level** (this account participates in a motif), while `top_rows` and
every `anomaly_score` are **transaction-level**. A verified example: account `8001694F0` carries a
SCATTER-GATHER hit with 16 intermediaries, yet its highest-scoring individual transaction is
labelled `NORMAL` / not laundering. That is not a contradiction — the motif is a property of the
account's position in the graph, not of any one transfer. Explanations must attribute a rule to
the account and a score to the transaction.

Scoring is capped at `MAX_SCORE_ROWS = 5000` per call; LOF's neighbour search is the binding
constraint (~2s at that cap, extrapolating from 73s per 200k rows).

---

## 6. Testing

`tests/test_rules.py` — 33 tests, all passing. Per `ml_spec.md`'s stated test surface, each of the
8 motifs gets a synthetic transaction sequence that should fire it and one that shouldn't
(fan-out spread past its window, a cycle whose return leg carries 5% of the outbound amount, a
stack whose last hop drops the amount, a gather-scatter running in the wrong order, and so on),
plus two parametrized invariants across all seven structured rules: every rule emits the agreed
`(account, rule, evidence, score)` shape, and none of them fire on a single isolated transaction.

Tests execute the exact SQL the batch pass runs, against a tiny in-memory `edges` table — there is
one implementation, so a threshold or join-condition change cannot pass tests while breaking the
5M-row output.

Not yet covered: `anomaly()` itself has been verified by hand (real account, empty scope, bad
scope, unknown method, single-method calls) but has no automated tests. Risk classification tests
are Phase 5.

---

## 7. Known limitations

- **Self-loop/Reinvestment false positives.** The highest-scoring row for the sample account is a
  367,764 self-transfer with Payment Format `Reinvestment` — a format `phase2.md` §4 measured at a
  0.0% laundering rate. Large routine internal transfers score high on amount-driven features.
  Phase 5 should down-weight `is_self_loop` + zero-risk payment formats rather than letting the
  raw detector score through.
- **FAN-IN is near-useless** (1.5x lift) and **BIPARTITE has negligible recall** (15 accounts).
- **Recall is understated** by the pattern file's partial coverage (see §3).
- **LOF is slow and weak** (§4).
- Thresholds are tuned against this specific dataset's ground truth; they are not portable
  defaults.

---

## 8. Deliverable checklist

- [x] `anomaly(scope, method?) -> dict` implemented, matches interface contract (plain dict I/O,
      never raises, read-only)
- [x] Isolation Forest + LOF + Z-score, all three available and individually selectable
- [x] Rules engine encoding all 8 `phase1.md` §9 motifs, evaluated against ground truth
- [x] Anomaly score and rule hits returned as separate signals (not collapsed)
- [x] Thresholds and model artifacts persisted and reproducible (fixed seed)
- [x] 33 rule-engine tests passing, fire/no-fire per motif
- [x] `ml_spec.md` open decisions #1-#4 closed, Phase 4 decisions #5-#6 recorded

## Next: Phase 5 (risk classification) — `risk(anomaly_result, context?) -> dict`, mapping the
anomaly score plus rule hits onto `LOW`/`MEDIUM`/`HIGH`/`CRITICAL` and a MONITOR/REVIEW/REPORT
action, with per-rule weighting informed by the lift table in §3 (SCATTER-GATHER and
GATHER-SCATTER weighted heavily, FAN-IN barely at all) and the self-loop correction from §7.
Field names must be confirmed against teammate's `flags` schema before finalizing.

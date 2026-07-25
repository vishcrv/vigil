# Phase 5 — Risk Classification

Source: `ml/risk.py`, `tests/test_risk.py`. Deliverable per `ml_spec.md` Phase 5:
`risk(anomaly_result, context?) -> dict`, mapping Phase 4's two signals onto a discrete risk
level and recommended action in exactly the shape a `flags` row takes.

---

## 1. Risk scale (open decision #1, now closed)

`LOW` / `MEDIUM` / `HIGH` / `CRITICAL` — string enum, four tiers.

**Teammate action required: `flags.risk_level` is a 4-value TEXT column.** This was the item
blocking their schema work.

Mapped onto the three `escalation_action` values `spec.md` line 182 defines:

| risk_level | escalation_action |
|---|---|
| LOW | MONITOR |
| MEDIUM | REVIEW |
| HIGH | REVIEW |
| CRITICAL | REPORT |

MEDIUM and HIGH share REVIEW deliberately — the tiers still differ for queue ordering in the
flagged-items table, which is the distinction a judge triaging the list actually needs.

---

## 2. Scoring model

Two inputs, combined so that rules lead and detectors modulate:

```
R = max over fired rules of  weight(rule) x (0.60 + 0.40 x rule_confidence)
D = detector percentile, damped for benign-profile rows
risk_score = R + (1 - R) x D x 0.40
```

**Why rules lead.** Phase 4 measured Isolation Forest at 2.3x lift and LOF at 1.6x, against
rules reaching 133x and 70x. Treating those as comparable evidence would be wrong. The
`(1 - R)` term keeps the result in [0,1] and stops a strong rule plus a strong detector from
running off the top of the scale.

**Why the detector is capped at 0.40.** With no rule firing, a perfect detector score lands at
exactly 0.40 — MEDIUM. The detectors alone can flag something for review but can never
recommend REPORT. That ceiling is a direct consequence of their measured weakness.

**Why `max` and not a sum over rules.** Two low-precision rules firing on one account is not
stronger evidence than one 100%-precision rule. Summing would let a pile of weak hits
manufacture a CRITICAL; the test suite pins this.

**Why the 0.60 base credit.** Firing at all is most of a rule's evidentiary value. Without a
floor, a 100%-precision rule that clears its threshold by a small margin would contribute
almost nothing.

### Rule weights (from the measured lift table, `phase4.md` §3)

| Rule | Precision | Lift | Weight |
|---|---|---|---|
| SCATTER-GATHER | 100% | 133x | 1.00 |
| GATHER-SCATTER | 52% | 70x | 0.85 |
| RANDOM | 8.7% | 11.6x | 0.35 |
| BIPARTITE | 6.7% | 8.9x | 0.30 |
| FAN-OUT | 5.0% | 6.7x | 0.25 |
| CYCLE | 2.8% | 3.7x | 0.15 |
| STACK | 2.7% | 3.6x | 0.15 |
| FAN-IN | 1.1% | 1.5x | 0.05 |

Assigned by band rather than by a formula on lift: lift is noisy at low hit counts (BIPARTITE's
8.9x rests on 15 accounts), and what matters downstream is the coarse question "is this rule
evidence, a hint, or noise". FAN-IN at 0.05 cannot escalate anything on its own — deliberate,
given it measured barely better than chance.

### Level thresholds

`CRITICAL` ≥ 0.75 · `HIGH` ≥ 0.50 · `MEDIUM` ≥ 0.25 · `LOW` below that.

### Benign-profile correction

`phase4.md` §7 recorded a false positive: the top-scoring row for a sample account was a
367,764 self-transfer in `Reinvestment`, a format measured at a 0.0% laundering rate. Amount-
driven features score those highly. `risk()` computes the fraction of scored rows that are
self-loops in a zero-risk payment format and damps the detector component by up to 50% on that
fraction. It damps **only** the detector — a fired rule is independent evidence and survives
the correction (pinned by test).

---

## 3. Output shape

```python
{
  # flags-table fields (spec.md line 177-184)
  "risk_level": "CRITICAL",
  "pattern_detected": "SCATTER-GATHER",   # from the rule engine only
  "anomaly_score": 0.9967,                # passthrough from Phase 4
  "escalation_action": "REPORT",
  "customer_id": "8001694F0",
  "transaction_id": "tx_4edee48e25647387",

  # supporting detail for Phase 6 and the transparency panel
  "risk_score": 0.8931,
  "contributing_signals": {
    "rule_component": 0.8222,
    "detector_component_raw": 0.9967,
    "detector_component_adjusted": 0.9967,
    "benign_profile_fraction": 0.0,
    "rules_fired": [ {rule, account, weight, rule_confidence, contribution, evidence}, ... ],
    "counterparty_rules": [ ...same shape, hits on other accounts in scope... ],
    "rows_scored": 80,
    "row_flags": {"currency_mismatch": 0.0, "elevated_payment_format": 0.7},
  },
}
```

> **Amended during Phase 6.** Two fields above were added after this document was first
> written: `counterparty_rules` and `row_flags`. See `phase6.md` §2 — `anomaly()` returns rule
> hits for *every* account in the scoped rows, and the original `risk()` attributed
> counterparty motifs to the subject account, which would have written factually wrong `flags`
> rows. Only the scoped account's hits now drive `rule_component` and `pattern_detected`.
> `row_flags` is descriptive context for the Phase 6 templates and does not affect the score.

Never raises. Non-dict input, bad context, and malformed rule hits all return structured
errors or degrade safely.

**Upstream errors propagate rather than scoring LOW.** If `anomaly()` returned an error, `risk()`
returns an error with `risk_level: None` — reporting LOW would read as "we checked and it is
fine" when nothing was checked.

**`pattern_detected` never echoes `aml_pattern`.** That column is pattern-file ground truth;
using it would leak the label into the agent's own output. `pattern_detected` comes from the
rule engine only, and is `None` when no rule fired. Pinned by test.

### `transaction_id`

The Kaggle data has **no transaction ID** — `phase1.md` §9 established that pattern rows are
matched back by full field tuple, not a key. Teammate's `flags` table has a `transaction_id`
column, so `risk()` fills it with `tx_<sha1[:16]>` over
`(Timestamp, From Account, To Account, Amount Received)`. Deterministic and stable across
enrichment re-runs, unlike a synthetic sequential ID. **Flag to teammate**: this is a content
hash, not a dataset-native key.

---

## 4. Verified end-to-end against real data

| Account | Anomaly | Rules fired | Risk | Action |
|---|---|---|---|---|
| `8001694F0` | 0.9967 | SCATTER-GATHER, FAN-OUT, FAN-IN | **CRITICAL** | REPORT |
| `80C5F4510` | 0.8977 | FAN-OUT | **MEDIUM** | REVIEW |

The first is rule-driven (`rule_component` 0.8222) rather than detector-driven — the intended
behaviour. The second is the Phase 4 false-positive case: the benign correction fired
(`benign_profile_fraction` 0.1, detector damped 0.8977 → 0.8528), landing it at MEDIUM/REVIEW
rather than escalating a routine self-transfer.

---

## 5. Testing

`tests/test_risk.py` — 37 tests, all passing (70 across the suite with Phase 4's).
`ml_spec.md`'s stated surface is "known anomaly-score + rule-hit combinations → expected
risk_level/action", covered plus the invariants the weighting exists to enforce:

- Per-rule level boundaries (strongest rule → CRITICAL, FAN-IN alone → LOW)
- Detector alone cannot reach CRITICAL (asserts the exact 0.40 ceiling)
- Strongest rule wins; weak rules do not accumulate
- `pattern_detected` never echoes the ground-truth label
- Benign damping applies to detectors but not to rule hits
- Every `flags` column present; every level has an action
- `transaction_id` stable across calls, distinct across transactions
- Upstream error propagation; non-dict input; malformed rule hits; score bounded in [0,1]

---

## 6. Known limitations

- **Weights are tuned to this dataset's ground truth**, which covers 3,209 of 5,177 laundering
  rows. They are not portable defaults.
- **Thresholds (0.75/0.50/0.25) are judgement, not measurement.** Unlike the rule thresholds in
  Phase 4, there was no held-out quantity to optimise them against — they were placed so the
  detector ceiling lands at MEDIUM and a top-weight rule reaches CRITICAL. Worth revisiting if
  flagged-item volume turns out wrong in the UI.
- **`risk()` grades an entire scope, not one transaction.** `customer_id` and `transaction_id`
  describe the highest-scoring row; a scope containing several independently suspicious
  transactions produces one flag, not several. If teammate needs one `flags` row per flagged
  transaction, that is a shape change to agree on.
- **Rule hits are account-level, scores are transaction-level** (`phase4.md` §5). A CRITICAL
  driven by a rule is a statement about the account's position in the graph, not about the
  specific `transaction_id` attached to the flag.

---

## 7. Deliverable checklist

- [x] `risk(anomaly_result, context?) -> dict` implemented, matches interface contract
- [x] Risk scale defined and documented (open decision #1 closed)
- [x] Per-rule weighting derived from measured Phase 4 precision, not assigned by eye
- [x] Benign-profile correction from `phase4.md` §7 applied
- [x] Output carries every `flags` column from `spec.md` line 177-184
- [x] 37 risk tests passing; full suite 70
- [x] Verified end-to-end on real accounts through `anomaly()`

## Next: Phase 6 (explanation) — `explain(risk_result) -> dict`. `contributing_signals` was
shaped for it: `rules_fired` carries each rule's evidence JSON for template filling, and the
`rule_component` / `detector_component` split says whether to reach for a motif template or the
pure-anomaly-score one. Templates needed: 8 motifs + pure-anomaly + benign/flag-only, so every
`risk()` output has a matching path and there is no silent "no explanation available" case.

## Open for teammate
1. `flags.risk_level` is 4-value TEXT (`LOW`/`MEDIUM`/`HIGH`/`CRITICAL`) — unblocks their schema.
2. `transaction_id` is a content hash, not a dataset-native key (§3).
3. One `flags` row per scope or per flagged transaction? (§6)

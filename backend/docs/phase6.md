# Phase 6 — Explanation Generation

Source: `ml/explain.py`, `tests/test_explain.py`. Deliverable per `ml_spec.md` Phase 6:
`explain(risk_result) -> dict` → `{"explanation": str}`.

Template-based, tied to the firing rule, per `spec.md`'s "Explain" stack row — no SHAP/LIME.

---

## 1. Coverage guarantee

`ml_spec.md` requires that every possible `risk()` output has a matching explanation path, with
no silent "no explanation available" case. `select_template()` is total over the shapes `risk()`
can return:

| Template | Selected when |
|---|---|
| 8 motif templates | a rule fired on the subject account (`pattern_detected` set) |
| `PURE_ANOMALY` | no rule, detector ≥ 95th percentile |
| `ROW_FLAGS_ONLY` | no rule, not an outlier, but ≥20% of rows carry a currency mismatch or elevated-risk payment format |
| `NO_CONCERN` | nothing fired |
| `NO_DATA` | scope matched zero transactions |
| `ERROR` | upstream error, or `risk_level` is `None` |

A test asserts the reachable set equals the declared set — a template no input can select is a
silent gap waiting to happen, and a declared-but-dead template would pass a weaker test.

### Motif templates

One per `aml_pattern` value from `phase1.md` §9, each quoting the fields **its own rule** wrote
into the evidence JSON (`ml/rules.py`), so the sentence contains the numbers that actually
triggered the rule rather than a restatement of the score. Example, real output:

> Account 8001694F0 is part of a structure in which funds split across 16 distinct intermediary
> accounts and reconverged on a single destination, between 2022/09/10 02:46 and 2022/09/13
> 21:42 (10,046,630.09 in, 261,785,298.42 out). Parallel paths between one origin and one
> endpoint are consistent with a SCATTER-GATHER layering pattern.

Two templates carry a measured caveat into the prose, so a judge reading one weak signal is told
it is weak: FAN-IN says it "is weakly discriminating on this dataset and should not be relied on
alone" (1.5x lift), and SCATTER-GATHER says it "is the highest-precision rule in the detection
set" (133x).

---

## 2. Attribution bug found by running the pipeline end-to-end

Unit tests passed and the first real query looked fine until the account names were read
carefully. Querying `80C5F4510` produced:

> **Account 8000B0DB0** sent funds to 8 distinct new receivers…

The FAN-OUT hit belonged to a **counterparty**, not the queried account. `anomaly()` returns
rule hits for every account appearing in the scoped rows — both `From Account` and
`To Account` — and `risk()` was treating all of them as the subject's.

This was not cosmetic: it would have written a `flags` row asserting `80C5F4510` matched
FAN-OUT, which is false, and a compliance judge would have escalated the wrong account.

**Fix** (in `ml/risk.py`): when the scope names an account, only that account's hits drive
`rule_component` and `pattern_detected`. Counterparty hits go to
`contributing_signals.counterparty_rules` and are narrated separately:

> Separately, counterparties of this account matched laundering patterns: 80465E020 (FAN-IN);
> 2 accounts (FAN-OUT). These are properties of those accounts, not of the one assessed here,
> and do not contribute to the score above.

Counterparty hits deliberately **do not** affect the score. Transacting with a flagged account
is genuinely meaningful in AML, but no weight for it was measured in Phase 4 and inventing one
would be a guess. Dropping them entirely would lose a real lead, so they are reported and
disclaimed. Nine regression tests pin the split.

Same query after the fix:

> No laundering pattern matched 80C5F4510 and its transactions are not statistical outliers,
> but individual transaction attributes are worth noting…

---

## 3. Supporting sentences

Beyond the lead template, an explanation may carry:

- **Detector context** — percentile of the most unusual scored row, and, when the benign
  correction applied, why the score was reduced ("…reduced to 85.3% because 10.0% of the scored
  rows are self-transfers in a payment format with no observed laundering in this dataset").
- **Row flags** — currency mismatch / elevated payment-format share, when ≥20% of rows.
- **Counterparty patterns** — §2.
- **Assessment** — risk level, composite score, recommended action.

`risk()` gained a `row_flags` block for this: `ml_spec.md` asks for a currency-mismatch /
payment-format-only explanation path, and that path needs real values to quote. Those fractions
are **descriptive only and do not feed the score** — neither was measured as an independent
signal in Phase 4, so weighting them would be an unmeasured guess.

---

## 4. Failure-mode handling

The dangerous failure here is narrating a *failed analysis* as a clean result. `explain()` never
does:

> No assessment could be produced: models not found. **This is a failure to analyse, not a
> finding that the activity is clean** — the transactions in scope have not been evaluated.

`explain()` never raises. Non-dict input, `None`, malformed `contributing_signals`, a
`row_flags` that is not a dict, a non-numeric detector score, and missing evidence keys all
degrade to usable prose. Missing evidence renders as "an unrecorded amount" / "an unrecorded
time" rather than a literal `None` in text shown to a judge — a test asserts no explanation ever
contains `None`, `nan`, or an unfilled `{}` placeholder.

---

## 5. Testing

`tests/test_explain.py` — 43 tests. Full suite **132 passing**.

- Every motif: template selected, pattern named, evidence values present in the prose
- Every motif with **empty** evidence: still usable prose, no placeholder leak
- Every declared template reachable; reachable set == declared set
- Each non-motif path selected under its own conditions
- Counterparty attribution: named, disclaimed, summarised when numerous, absent when none
- Error path does not imply the activity is clean
- Garbage input (`None`, string, int, list, empty dict) still yields an explanation
- Level and action stated in every explanation

---

## 6. Known limitations

- **Templates are hand-written prose, not generated.** Wording is fixed; only the values vary.
  That is the point (`spec.md` rules out SHAP/LIME), but it means a genuinely novel combination
  of signals gets a generic sentence rather than a bespoke one.
- **The lead template is the single highest-weighted rule.** Secondary rules on the subject are
  mentioned by name only, without their evidence, to keep the explanation readable.
- **Counterparty hits carry no score weight** (§2) — deliberate, but it means an account
  surrounded by flagged counterparties can still read as LOW.
- **`ROW_FLAGS_ONLY` fires on descriptive attributes** that were never validated as predictive
  signals; the prose says "worth noting", not "suspicious", for that reason.

---

## 7. Deliverable checklist

- [x] `explain(risk_result) -> dict` returning `{"explanation": str}`, template-based, no SHAP/LIME
- [x] One template per `aml_pattern` motif (8), each filled from its own rule's evidence
- [x] Pure-anomaly-score template for detector-only flags
- [x] Currency-mismatch / payment-format-only template (`ROW_FLAGS_ONLY`)
- [x] No silent "no explanation available" — coverage asserted by test
- [x] Never raises; failed analysis never narrated as a clean result
- [x] 43 explanation tests; full suite 132 passing
- [x] Verified end-to-end on real accounts through `anomaly()` → `risk()` → `explain()`

## Next: Phase 7 (`eda`) — the last ML-owned tool. Constrained parameterized query shapes over
the enriched Parquet via DuckDB, explicitly **not** raw-SQL-from-LLM passthrough
(`ml_spec.md` Phase 7 calls that an injection risk even in a local single-user tool).
`ml/feature_eng.py` already establishes the parameter-binding pattern to follow.

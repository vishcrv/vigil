# Phase 2 — Enrichment Pipeline

Source: `scripts/enrich.py`, run against `HI-Small_Trans.csv` (5,078,345 rows), `HI-Small_accounts.csv`
(518,581 rows), `HI-Small_Patterns.txt`. Output: `data/HI-Small_Enriched.parquet` (gitignored,
352.6 MB, 5,078,345 rows × 32 columns).

---

## 1. Pipeline steps

1. Load transactions (explicit column rename — see phase1.md note on duplicate `Account` header)
   and accounts.
2. Parse `HI-Small_Patterns.txt` into a row-level DataFrame (`aml_pattern` + the 11 tx fields),
   using the same BEGIN/END block parse as Phase 1's notebook.
3. Join pattern rows back to `transactions` by exact field-tuple match (no shared ID — per
   phase1.md §9). All 3,209 pattern rows matched with **zero ambiguous duplicates** (row count
   before/after identical).
4. Enrich: temporal fields, amount buckets, per-sender baselines, deviation z-score, extra flags.
5. Inner-join `accounts` twice (sender side, receiver side) on `Bank ID`/`Account Number` — 100%
   overlap confirmed in Phase 1, row count unchanged after join (5,078,345 in, 5,078,345 out).
6. Write single Parquet file.

Runtime: a few minutes on the full 5M-row file, single pass, no chunking needed at this scale.

---

## 2. Open decisions from ml_spec.md — resolved

**#2 — `is_suspicious` definition:** kept **both** columns, as recommended.

- `is_laundering` (bool) — raw `Is Laundering==1`, authoritative row label. 5,177 True.
- `is_suspicious` (bool) + `aml_pattern` (str) — pattern-file match. 3,209 True, else `NORMAL`.
- Confirmed again at enrichment time: pattern file is a strict subset of `is_laundering==1`
  (3,209 vs 5,177), not an alternate labeling — matches Phase 1 finding exactly.

**#3 — Self-loop rows (11.6%):** **keep + flag**, not drop. `is_self_loop` bool column added
(591,212 rows, 11.64% — matches Phase 1 §8 exactly). Kept in per-sender baseline stats rather than
excluded, since dropping would silently shrink `txn_count`/`avg_amount` for accounts that use
self-transfers as part of normal behavior; the flag lets the rules/anomaly engine (Phase 4) decide
whether to treat them specially instead of losing the information here.

**#4 — Amount-category bucket edges:** **quantile-based**, not fixed round numbers. `pd.qcut` into
6 equal-frequency buckets over `log1p(Amount Received)` (~846k rows/bucket). Computed edges (on
log1p scale): `(-0.001, 4.411], (4.411, 5.908], (5.908, 7.253], (7.253, 8.569], (8.569, 10.455],
(10.455, 27.676]` → labeled `micro/small/medium/large/xlarge/xxlarge`. Data-driven per Phase 1's
heavy-right-tail finding, not arbitrary.

**#1 (risk level scale)** — still open, not a Phase 2 concern; carried into Phase 5.

---

## 3. Column list (32 total)

Raw passthrough (11): `Timestamp`, `From Bank`, `From Account`, `To Bank`, `To Account`,
`Amount Received`, `Receiving Currency`, `Amount Paid`, `Payment Currency`, `Payment Format`,
`Is Laundering`.

Pattern-derived (3): `aml_pattern`, `is_suspicious`, `is_laundering`.

Spec-required enrichment (9, `spec.md` line 153-163): `hour`, `day_of_week`, `amount_category`,
`txn_count`, `total_volume`, `avg_amount`, `std_amount`, `unique_receivers`, `deviation_from_avg`.

Extra, justified by Phase 1 findings (3, per ml_spec.md Phase 2 "also carry forward"):
`currency_mismatch`, `is_self_loop`, `payment_format_risk`.

Account join columns (6): `sender_acc_bank_name`, `sender_acc_entity_id`, `sender_acc_entity_name`,
`receiver_acc_bank_name`, `receiver_acc_entity_id`, `receiver_acc_entity_name` — entity/bank context
for entity-level rollups (Phase 1 candidate feature), account-number/bank-id join keys dropped
after merge (redundant with `From/To Account`, `From/To Bank`).

---

## 4. Verification against Phase 1 findings

All cross-checked post-run, all match:

| Check                       | Phase 1 finding                     | Phase 2 output                                                           |
| --------------------------- | ----------------------------------- | ------------------------------------------------------------------------ |
| `currency_mismatch` count   | 72,170 (1.4%)                       | 72,170                                                                   |
| `is_self_loop` rate         | 11.6%                               | 11.64% (591,212 rows)                                                    |
| Pattern-file → tx join      | 3,209 rows, no shared key           | 3,209 matched, 0 ambiguous dups                                          |
| Account join coverage       | 100% overlap                        | row count unchanged (5,078,345 in/out)                                   |
| Payment Format risk ranking | ACH ≫ others, Wire/Reinvestment ≈ 0 | ACH 0.75%, Bitcoin 0.038%, Cash/Cheque/CC ~0.02%, Wire/Reinvestment 0.0% |

`payment_format_risk` = empirical `P(is_laundering \| format)` over the full dataset — a fixed
aggregate encoding (WOE-style), not a per-row label leak, computed once at enrichment time.

`deviation_from_avg`: mean ≈ 0 (expected, z-score), std 0.94, range [-6.25, 199.6] (extreme upside
outliers = exactly the large-deviation transactions anomaly detection should catch). 152,750 senders
have `txn_count==1` → `std_amount==0` → deviation guarded to 0.0 (no NaN/inf in output, confirmed).

---

## 5. Deliverable checklist

- [x] `scripts/enrich.py` written, runs end-to-end on full dataset
- [x] `data/HI-Small_Enriched.parquet` produced (5,078,345 rows × 32 cols, gitignored)
- [x] All spec.md-mandated enrichment columns present (line 153-163)
- [x] Open decisions #2, #3, #4 resolved and documented above
- [x] No nulls in output (checked column-by-column)
- [x] Outputs cross-checked against Phase 1 notebook findings — all match
- [x] `.gitignore` added (raw CSVs, `data/`, `*.parquet`, `aml_agent.db`, env/venv/node_modules)

## Next: Phase 3 (feature engineering / `feature_eng` tool) — build the on-demand scoped-query

version of this same enrichment logic so the agent can compute features for an arbitrary
account/date-range scope at query time, not just the whole-table precomputed pass done here.

<!--
Took raw 5M-row transaction file plus accounts file plus laundering-pattern file, merged/enhanced into one big table, saved as single file (HI-Small_Enriched.parquet).

What got added to each transaction row:

1. Laundering tags — matched each transaction against known laundering-pattern file. Row now says: is it flagged as suspicious pattern (fan-out, cycle, etc), what pattern type, and separately whether raw ground-truth label says laundering (two different signals, kept both, don't merge — pattern file only covers subset).
2. Time fields — hour of day, day of week pulled from timestamp. Makes "laundering happens more 8am-6pm" queryable.
3. Amount bucket — each transaction sorted into micro/small/medium/large/xlarge/xxlarge based on real distribution of amounts in data (not made-up cutoffs — used actual quantiles).
4. Sender behavior baseline — for each sending account: how many transactions normally sends, total volume, average amount, how many different people sends to. Computed once per account, reused per row.
5. Deviation score — how far this specific transaction is from that sender's own normal behavior (z-score). Big number = way outside how this account usually behaves = red flag material.
6. Extra flags — currency mismatch (sent one currency, received another — FX layering signal), self-transaction flag (sender==receiver, 11.6% of data), payment-method risk score (ACH heavily overused by launderers per Phase 1, encoded as number).
7. Account context — joined in sender/receiver's bank name and business entity info. -->

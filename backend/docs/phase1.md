# Phase 1 — Dataset Understanding

Source: `nbs/01_exploration.ipynb` (executed against `HI-Small_Trans.csv`, `HI-Small_accounts.csv`, `HI-Small_Patterns.txt`).

Files: `transactions` = 5,078,345 rows / 11 cols. `accounts` = 518,581 rows / 5 cols.

---

## 1. Transaction columns

| Column | Meaning | Dtype | Cardinality | AML usefulness | Feature ideas |
|---|---|---|---|---|---|
| Timestamp | transaction time (`YYYY/MM/DD HH:MM`) | object → parse to datetime | 15,018 unique | temporal patterns, burst detection | hour, weekday, time-since-last-tx-for-account, velocity |
| From Bank | sender bank code | int64 | 30,470 | interbank flow, bank concentration | sender bank frequency, is-high-risk-bank flag |
| From Account | sender account id | object (hex-like string) | 496,995 | graph node (source) | out-degree, sent volume, account age proxy |
| To Bank | receiver bank code | int64 | 15,811 | receiver-side concentration | receiver bank frequency |
| To Account | receiver account id | object | 420,636 | graph node (sink) | in-degree, received volume |
| Amount Received | amount credited | float64 | 915,161 | size/anomaly signal | log-amount, z-score vs account history, round-number flag |
| Receiving Currency | currency credited | object | 15 | FX layering signal | currency one-hot, is-crypto flag |
| Amount Paid | amount debited | float64 | 923,873 | fee/FX-spread signal | Amount Paid − Amount Received (spread) |
| Payment Currency | currency debited | object | 15 | FX mismatch detection | currency-mismatch flag (Payment != Receiving) |
| Payment Format | payment channel (Cheque/Credit Card/ACH/Cash/Reinvestment/Wire/Bitcoin) | object | 7 | channel risk (ACH/Bitcoin over-represented in laundering, see §6) | one-hot, channel-risk-score |
| Is Laundering | ground-truth label | int64 (0/1) | 2 | **target** | — |

Note: raw CSV header has two columns both named `Account` (from/to) — pandas silently renames the second to `Account.1`. Renamed explicitly to `From Account` / `To Account` in the notebook to avoid ambiguity downstream.

---

## 2. accounts.csv structure

- 518,581 rows, 518,573 unique `Account Number` → **8 duplicate account-number rows** (investigate: likely same account re-registered under a different entity/bank pairing — needs row-level dedup check before using as a join dimension key).
- 30,470 unique `Bank ID`, 20,053 unique `Bank Name` (banks can share a display name across countries, e.g. "Bank #N" templates — don't dedup banks by name).
- 166,207 unique `Entity ID` == unique `Entity Name` (1:1, name is just a stringified entity id+type).
- No missing values in any column.
- 63,518 entities (of 166,207) own more than one account — mean 3.12 accounts/entity, but heavily skewed (max 7,820 accounts under a single entity — almost certainly a shell/institutional bucket, not a real customer).
- 8 accounts appear under more than one Bank ID (same as the duplicate-row count above — same root cause).
- Entity types (parsed from `Entity Name` prefix): Partnership (189,683), Corporation (172,351), Sole Proprietorship (149,048), Country (6,692), Individual (740), Direct (67). Retail "Individual" accounts are a tiny minority — dataset is dominated by business-entity accounts.

**Answers to the posed questions:**
- One row per account? Essentially yes (518,573 of 518,581 — 8 exceptions to dig into).
- Can accounts belong to the same entity? Yes, common (63,518 entities, up to 7,820 accounts each).
- Do entities own multiple accounts? Yes — this is the basis for entity-level (not just account-level) graph features later.
- One bank per account? Yes, except the same 8 duplicate rows.

---

## 3. Join verification (critical finding)

Initial concern from `.head()`: transactions showed `From Bank = 10`, accounts showed `Bank ID = 331579` — looked like different ID spaces.

**Verified, not assumed:**
- `set(transactions["From Bank"]) | set(transactions["To Bank"])` → 30,470 unique codes.
- `set(accounts["Bank ID"])` → 30,470 unique codes.
- **Overlap = 30,470 (100%).** `Bank ID` *is* the correct join key — the `.head()` sample just happened to show a large Bank ID first. False alarm, but correctly worth checking rather than assuming.
- Account join: transactions reference 515,080 unique account ids (`From Account` ∪ `To Account`); accounts.csv has 518,573 unique `Account Number`. Overlap = 515,080 (**100% of transaction-referenced accounts resolve** in accounts.csv; accounts.csv has ~3,493 accounts that never transact in this slice — expected, it's a "Small" sample).

**Join strategy confirmed:** `transactions["From Bank"/"To Bank"] → accounts["Bank ID"]`, `transactions["From Account"/"To Account"] → accounts["Account Number"]`. Both keys are clean, full-coverage joins. No alternate key needed.

---

## 4. Temporal coverage

- Span: 2022-09-01 00:00 → 2022-09-18 16:18 → **18 days**.
- Avg ~282,130 tx/day.
- Busiest day: 2022-09-01 (1,114,921 tx — day one is a massive outlier, likely a backfill/seed batch, not organic volume; worth flagging before using "daily volume" as a feature without normalizing day 1).
- Busiest hour (all data): hour 0 (634,726 tx) — also skewed by the day-1 spike.
- Busiest weekday: Thursday (1,597,740 tx).

Feature ideas: hour-of-day, day-of-week, tx velocity per account (time since previous tx), rolling window counts — but exclude/flag 2022-09-01 as a possible ingestion artifact.

---

## 5. Distribution analysis

- `Amount Received` heavy right-tailed: mean $5.99M, median $1,411, max ~$1.046 trillion → use `log1p(amount)` for any modeling/EDA (plotted in notebook, roughly bimodal after log transform).
- Payment Format counts: Cheque 1,864,331 · Credit Card 1,323,324 · ACH 600,797 · Cash 490,891 · Reinvestment 481,056 · Wire 171,855 · Bitcoin 146,091.
- Currency counts: US Dollar (1.88M) and Euro (1.17M) dominate; Bitcoin appears as a currency (148,151) — crypto rails present.
- Top sender bank overall: Bank 70 (449,859 tx, ~8.9% of all volume) — a clear hub, worth an is-hub-bank feature.
- Currency-mismatch rows (Receiving Currency ≠ Payment Currency): 72,170 (1.4% of all tx) — real signal, see §6.

---

## 6. Class analysis — normal vs laundering

Class counts: 5,073,168 normal (0) vs **5,177 laundering (1) → 0.102% positive rate.** Severe imbalance, confirmed.

Differences that stood out:

- **Amount:** laundering mean $36.1M vs normal $5.96M; laundering median $8,667 vs normal $1,408. Laundering transactions skew larger on average but the median gap is smaller — a few extreme laundering amounts pull the mean, so log-amount + median-relative features will separate classes better than raw mean.
- **Payment Format — the strongest categorical signal found:** 86.6% of laundering transactions use **ACH**, vs only 11.8% of normal transactions. Laundering has **zero** Reinvestment or Wire transactions. Cheque/Credit Card, which dominate normal traffic (36.7%/26.1%), barely appear in laundering (6.3%/4.0%). This is a very strong discriminator — payment-format alone should be a top feature.
- **Currency:** less separating than format, but Saudi Riyal is over-represented in laundering (7.2% vs 1.8% normal) — worth flagging as a candidate FX-layering corridor.
- **Hour of day:** normal traffic is flat across hours except a spike at hour 0 (12.5%, an artifact of the day-1 volume spike, see §4). Laundering activity concentrates in daytime hours 8–18 (roughly 5–6.5% per hour vs ~3.8% baseline), and is comparatively quiet at hours 20–23 and 0. Suggests laundering attempts are scheduled/patterned rather than randomly timed like background traffic.
- **Sender bank:** Bank 70 is the top sender for both classes (8.9% normal, 12.2% laundering) — not a clean discriminator by itself, but its share is elevated in laundering.

---

## 7. Relationship / network stats

- Out-degree (unique receivers per sender): median 1, mean 2.04, max 14,230 (extreme fan-out hub). 4,027 accounts have out-degree > 10.
- In-degree (unique senders per receiver): median 2, mean 2.41, max 545 (fan-in hub). 2,872 accounts have in-degree > 10.
- Most accounts are simple 1-to-1 senders/receivers — high-degree accounts are rare but exactly the profile fan-out/fan-in/gather-scatter laundering patterns target (confirmed against the pattern file in §9).

---

## 8. Graph characteristics

- Unique senders: 496,995. Unique receivers: 420,636. Unique accounts overall: 515,080.
- Unique (sender, receiver) edges: 1,015,736.
- Self-loops (From Account == To Account, i.e. same-account transactions — seen in `.head()` row 0): 591,212 rows (11.6% of all transactions) — need a decision on whether to keep, drop, or feature-flag these as internal/reinvestment transfers before graph modeling.
- Repeat pairs (same sender→receiver used more than once): 561,575 edges repeat, max 89 repeats on a single pair — recurring counterparties are common, not itself suspicious without context.

---

## 9. Pattern file (`HI-Small_Patterns.txt`)

Format: plain text, grouped blocks — `BEGIN LAUNDERING ATTEMPT - <TYPE>: <description>` … CSV-formatted rows (same 11-column schema as `HI-Small_Trans.csv`, `Is Laundering` always 1) … `END LAUNDERING ATTEMPT - <TYPE>`.

- **370 laundering-attempt groups**, 8 pattern types:

| Type | # groups |
|---|---|
| CYCLE | 54 |
| GATHER-SCATTER | 51 |
| BIPARTITE | 49 |
| FAN-OUT | 48 |
| STACK | 43 |
| SCATTER-GATHER | 44 |
| RANDOM | 41 |
| FAN-IN | 40 |

- These are named **structural motifs** (fan-out = one sender → many receivers, fan-in = many senders → one receiver, cycle = closed loop of hops, gather-scatter/scatter-gather/bipartite/stack = compound multi-hop shapes) — this is a group-level/topological ground truth, not a flat list of independently-labeled transactions.
- The file contains **3,209 data rows total**, but `transactions.csv` has **5,177 rows with `Is Laundering == 1`**. The counts don't match — the pattern file is very likely a **subset/sample of illustrative laundering attempts**, not an exhaustive enumeration of every `Is Laundering == 1` row. Do not assume 1:1 coverage; treat `Is Laundering` in `transactions.csv` as the authoritative row-level label, and the pattern file as auxiliary structural/group context (useful for building graph-motif features, not as the primary label source).
- Does it map to transaction IDs? No explicit ID column — rows are matched back to `transactions.csv` by exact field values (timestamp, accounts, amount, currency, format), not a shared key. Any join back to `transactions.csv` must be done on the full tuple, and should be checked for duplicate/ambiguous matches.

---

## Deliverable checklist

- [x] Every column (transactions + accounts) profiled: meaning, dtype, cardinality, AML relevance, feature ideas
- [x] Every table understood (row counts, keys, dup/missing checks, entity↔account relationship)
- [x] Join strategy verified empirically (`Bank ID`/`Account Number` — 100% overlap, not assumed)
- [x] Temporal coverage: 18 days, Sep 1–18 2022, day-1 volume spike flagged as artifact
- [x] Transaction distributions: heavy-tailed amounts, format/currency frequencies, log-transform needed
- [x] Class imbalance: 0.102% positive, quantified per-feature differences (Payment Format is the standout signal)
- [x] Network properties: degree distributions, self-loops, repeat pairs, hub accounts identified
- [x] Pattern file structure: 370 attempt groups, 8 motif types, row-count mismatch vs `Is Laundering` flagged, join method identified

### Candidate engineered features (carried into Phase 2)
- `log1p(Amount Received)`, `Amount Paid − Amount Received` (spread), round-number flag
- `currency_mismatch` (Payment Currency ≠ Receiving Currency)
- Payment Format one-hot / risk-weighted encoding (ACH strongly over-represented in laundering)
- hour-of-day, day-of-week, tx velocity per account (exclude/normalize 2022-09-01)
- out-degree, in-degree per account; hub flags (degree > 10)
- self-loop flag (From Account == To Account)
- entity-level rollups (accounts-per-entity, entity type)
- graph-motif features derived from pattern-file shapes (fan-in/out, cycle length, gather-scatter span) for accounts implicated in known attempts

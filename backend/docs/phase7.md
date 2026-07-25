# Phase 7 — EDA Tool (ad-hoc, for the agent)

Source: `ml/eda.py`, `tests/test_eda.py`. Deliverable per `ml_spec.md` Phase 7:
`eda(query_spec) -> dict` — the *runtime* ad-hoc query tool, distinct from Phase 1's exploration
notebook. Executed via DuckDB directly over the enriched Parquet.

This is the last of the five ML-owned tools.

---

## 1. Security model

`ml_spec.md` is explicit that this must not become arbitrary-SQL-from-LLM — "that's an injection
risk even in a local single-user tool". It is right for a reason worth stating: the model
writing the query is steered by whatever text the *question* came from, so a hostile transaction
memo or account name is an injection vector even with no external attacker.

So there is no SQL passthrough. The tool exposes a fixed catalogue of query shapes, and two
rules cover the entire surface:

1. **Every literal is bound** as a DuckDB `?` parameter. Caller values never reach the SQL
   string.
2. **Every identifier is allow-listed.** Column names, aggregate functions, sort direction and
   time interval cannot be parameterized in SQL — so each is a *lookup key* into a fixed dict,
   and only the stored value is interpolated. Caller-supplied text is never interpolated,
   quoted or otherwise.

Anything failing either rule returns a structured error instead of executing.

Generated SQL and its bound parameters are both returned, so the execution-summary panel
(`spec.md`'s stated differentiator) can show exactly what ran — and the unfilled `?` in that
output is itself the evidence that values were bound rather than concatenated.

---

## 2. Query shapes

| Operation | Answers |
|---|---|
| `count` | "how many transactions match X" |
| `aggregate` | sum/avg/min/max/median/stddev/count/count_distinct of one measure |
| `group` | a measure aggregated by a dimension |
| `distribution` | value counts of a categorical column |
| `time_series` | counts or a measure bucketed by hour/day/month |
| `top_accounts` | highest sender/receiver accounts by count or volume |
| `sample` | example rows, allow-listed projection |

Two sources: `transactions` (the enriched Parquet) and `rule_hits` (Phase 4's motif output), so
questions like "how many accounts matched CYCLE" are answerable. `time_series` and
`top_accounts` are transactions-only and say so rather than failing obscurely.

Filters accept `=`, `!=`, `>`, `>=`, `<`, `<=`, `in`, `between`, `contains`, `is_null`,
`not_null`. `contains` supplies its own wildcards around a bound value, so `%' OR 1=1 --` is
matched as a literal substring.

`Timestamp` is bucketed by string prefix rather than parsed — it is fixed-width
`YYYY/MM/DD HH:MM` VARCHAR, so a prefix substring sorts and groups correctly (`phase3.md` §3a).

---

## 3. Verified against real data

```
count                      -> 5,078,345          (matches phase2.md row count)
distribution aml_pattern   -> NORMAL 5,075,136; GATHER-SCATTER 716;
                              SCATTER-GATHER 626; STACK 466   (matches the enrichment run)
time_series interval=day   -> 18 buckets, 2022/09/01 .. 2022/09/18
                              first bucket 1,114,921          (the phase1.md §4 day-1 spike)
```

Cross-checking against numbers established in earlier phases is the point — these are the same
counts `scripts/enrich.py` printed, arrived at through a completely different code path.

---

## 4. Testing

`tests/test_eda.py` — 57 tests. **Full suite: 189 passing.**

The suite splits deliberately: validation and injection-rejection tests run anywhere, because
those specs are rejected before a connection is opened. Execution tests are marked `needs_data`
and skip cleanly when the enriched Parquet is absent — it is gitignored, so a fresh clone would
otherwise show a wall of failures that look like broken code rather than absent data.

Security coverage:
- Five injection payloads against column names, filter columns, aggregation, order, interval,
  operation and source — all rejected before execution
- The same payloads as filter *values* — bound, match zero rows, no error
- `contains` wildcard escape attempt
- A post-hoc `count` proving the table survived every attempt
- Returned SQL contains `?` and not the value

Also covered: every operation against real data, malformed filters, invalid limits, the 100-item
cap on `in` lists, non-numeric measures rejected for numeric aggregations, and the normalized
spec echoed back.

---

## 5. Known limitations

- **No `risk_level` dimension.** `eda` queries stored data; risk levels are computed per query
  by Phase 5 and not persisted here. "How many high-risk transactions last week" has to route
  through `anomaly` → `risk`, or read teammate's `flags` table, which is their side. Worth
  agreeing on explicitly — it is the exact example question `ml_spec.md` uses.
- **No joins between sources.** `transactions` and `rule_hits` are queryable separately but
  cannot be joined in one call; the agent must issue two.
- **Single-dimension grouping only.** No cross-tabs.
- **`sample` has no ordering control** — it returns whatever DuckDB scans first, which is fine
  for "show me examples" and wrong for "show me the largest". Use `top_accounts` for that.

---

## 6. Deliverable checklist

- [x] `eda(query_spec) -> dict` over the enriched Parquet via DuckDB
- [x] Constrained parameterized query shapes — no raw SQL passthrough
- [x] All literals bound; all identifiers allow-listed
- [x] Generated SQL + bound parameters returned for the transparency panel
- [x] Never raises; structured errors throughout
- [x] 57 tests including explicit injection attempts; full suite 189 passing
- [x] Results cross-checked against Phase 1/2 findings

---

## 7. ML workstream status

All five ML-owned tools from `ml_spec.md`'s interface contract are built:

| Tool | Phase | Status |
|---|---|---|
| `feature_eng` | 3 | done |
| `anomaly` | 4 | done |
| `risk` | 5 | done |
| `explain` | 6 | done |
| `eda` | 7 | done |
| `escalate` | — | teammate's, not ML |

All take plain dicts, return plain dicts, never raise, and are read-only over Parquet.

### Outstanding for teammate

1. `flags.risk_level` is 4-value TEXT: `LOW`/`MEDIUM`/`HIGH`/`CRITICAL`.
2. `transaction_id` is a content hash (`tx_<sha1[:16]>`) — the dataset has no native key.
3. One `flags` row per scope, or per flagged transaction? `risk()` currently emits one per scope.
4. "High-risk transactions last week" cannot be answered by `eda` alone (§5).

### Not done in this workstream

- No automated tests for `feature_eng`, `anomaly`, or `explain`'s integration with real data —
  those were verified by hand. `ml_spec.md`'s required test surface (rule engine, risk
  classification) is covered automatically.
- Model quality is modest and honestly reported: Isolation Forest 0.794 ROC-AUC, LOF 0.638,
  FAN-IN barely better than chance (`phase4.md` §4, §7).

# Phase 3 — Feature Engineering (agent-ready)

Source: `scripts/enrich.py` (extended) + `ml/feature_eng.py` + `ml/data.py`. Deliverable: `data/
HI-Small_Agent_Ready.parquet` + the `feature_eng(scope: dict) -> dict` tool function, per
`ml_spec.md` Phase 3 (line 79) / `spec.md` (line 149) — on-demand scoped features for the agent
loop, distinct from Phase 2's whole-table precomputed pass.

---

## 0. `HI-Small_Agent_Ready.parquet`

Both `ml_spec.md` and `spec.md` name this file explicitly as a required deliverable, separate from
Phase 2's `HI-Small_Enriched.parquet`. It's produced by `scripts/enrich.py`, written right after
`HI-Small_Enriched.parquet` from the same DataFrame (same 5,078,345 rows × 32 columns) — Phase 3's
spec text defines no additional precomputed columns for it ("anything genuinely query-time-only...
doesn't belong precomputed," ml_spec.md line 87-88), so there's no schema delta to justify a
different pipeline. `ml/data.py` points at `HI-Small_Agent_Ready.parquet`, not `HI-Small_Enriched.
parquet`, so the agent-facing tool reads the deliverable the spec actually names.

---

## 1. What it does

`feature_eng(scope)` filters `data/HI-Small_Agent_Ready.parquet` down to a caller-specified scope
and returns aggregates + row records + (optionally) a rolling velocity figure — all via DuckDB
queries, no full-table load into pandas.

`ml/data.py` holds one shared, `lru_cache`d DuckDB connection with the Agent_Ready Parquet
registered as a view (`enriched`) — reused by every future ML tool (`eda`, `anomaly`, `risk`), not
just this one, per spec.md's caching strategy.

---

## 2. Scope shape (input)

```python
{
  "account_id": "8000EBD30",       # optional*
  "role": "sender"|"receiver"|"both",  # optional, default "both" — matches From/To Account
  "date_range": ["2022/09/01 00:00", "2022/09/03 00:00"],  # optional*
  "min_amount": 1000.0,            # optional
  "max_amount": 50000.0,           # optional
  "payment_format": "ACH",         # optional
  "velocity_window_days": 3,       # optional — needs account_id, adds rolling-window count
  "limit": 200,                    # optional, default 200, capped at 1000
}
```
\* at least one of `account_id` / `date_range` required — an unscoped full-table pull isn't a
supported query shape here.

## 3. Output shape

```python
{
  "scope": {...normalized scope actually applied...},
  "aggregate": {
    "txn_count", "total_volume", "avg_amount", "std_amount",
    "unique_receivers", "unique_senders", "earliest_ts", "latest_ts",
    "suspicious_count", "laundering_count",
  },
  "records": [ {...15 key fields per matched row...}, ... ],   # capped at `limit`
  "record_count_returned": int,
  "record_count_truncated": bool,   # True if aggregate.txn_count > len(records)
  "velocity": {"window_days", "txn_count_in_window", "rate_per_day"},  # only if requested
}
```

On bad input: `{"error": "<message(s)>", "scope": <original input, unmodified>}` — **never raises**,
per the interface contract in `ml_spec.md`. Multiple validation failures are joined into one message.

`records` returns a fixed 15-column projection (id fields, amount, format, the three label/pattern
columns, deviation, three risk flags) — not the full 32-column enriched row — since this crosses a
JSON tool-call boundary and the agent doesn't need account-join columns per-row (those live in
`aggregate` context instead).

---

## 3a. Timestamp handling (implementation note)

`Timestamp` is stored as `VARCHAR` (`"2022/09/10 18:21"`), not a DuckDB `TIMESTAMP` — Phase 2 never
cast it. Zero-padded fixed-width format means plain string comparison (`>=`, `<=`, `min`/`max`)
already sorts chronologically, so `date_range` filtering and `earliest_ts`/`latest_ts` work directly
on the raw string with no cast needed. The one place true date arithmetic is required —
`velocity_window_days`'s `INTERVAL` subtraction — explicitly parses via
`strptime("Timestamp", '%Y/%m/%d %H:%M')` on both sides of the comparison; an implicit
`::TIMESTAMP` cast was tried first and failed (`Binder Error: Cannot compare VARCHAR and TIMESTAMP`)
— fixed by making the parse explicit rather than adding a Phase-2 schema migration for one query path.

---

## 4. Safety / injection

All filter values are bound as DuckDB query parameters (`?` placeholders via `con.execute(sql,
params)`), never string-concatenated into SQL — matches the constrained-query-shape requirement
`ml_spec.md` Phase 7 calls out for `eda` (no raw-SQL-from-LLM passthrough), applied here too since
`feature_eng` takes the same kind of caller-controlled dict.

---

## 5. Testing performed (manual, against real data)

- Real account (`80C5F4510`, 71 txns): full record set + aggregate returned correctly, matches a
  spot-check against the enriched Parquet.
- `date_range` + `min_amount` combined filter, `limit`: truncation flag correctly set
  (`67,383` matched, `3` returned).
- `velocity_window_days=3` on the same account: `20` txns in trailing 3-day window, `6.67`/day.
- Error paths: empty scope, non-dict scope, wrong-typed `account_id`, `min_amount > max_amount` —
  all return structured `{"error": ...}`, no exceptions raised.
- Nonexistent account: `txn_count=0`, `records=[]`, no crash — empty-scope-match handled cleanly.

No formal Pytest suite yet — `ml_spec.md`'s stated test surface (rule engine, risk classification)
is Phase 4/5 work; `feature_eng` was verified by hand here since it has no "expected fire/no-fire"
semantics to unit-test yet, only shape/error-handling.

---

## 6. Deliverable checklist

- [x] `data/HI-Small_Agent_Ready.parquet` produced (5,078,345 rows × 32 cols, gitignored) — see §0
- [x] `feature_eng(scope: dict) -> dict` implemented, matches interface contract (plain dict I/O,
      never raises, pure/read-only)
- [x] Shared DuckDB connection (`ml/data.py`) — reusable by `eda`/`anomaly`/`risk` next phases
- [x] Scoped filtering: account (sender/receiver/both), date range, amount range, payment format
- [x] Query-time-only feature: rolling velocity over caller-specified window (doesn't belong
      precomputed, per ml_spec.md Phase 3)
- [x] Injection-safe: parameterized queries, no raw SQL from caller
- [x] Manually verified against real enriched data, including error paths

## Next: Phase 4 (anomaly detection) — `anomaly(scope, method?) -> dict`, consuming this tool's
output (or the same scoped DuckDB access pattern) to run Isolation Forest / LOF / Z-score plus the
rules engine.

"""
Phase 4 rules engine — the 8 AML motif rules from phase1.md §9, encoded as DuckDB SQL
over a normalized `edges` view.

Why SQL and not pandas/networkx: these are graph motifs over 1,015,736 unique edges
(phase1.md §8). Self-joins with degree pre-filters are what DuckDB is good at, and keeping
one implementation means the batch pass (scripts/build_rule_hits.py) and the unit tests
(tests/test_rules.py) exercise the exact same code — no reference-vs-production drift.

Each rule is a function `(void) -> str` returning SQL that selects implicated accounts from
`edges`, with columns: account, rule, evidence (JSON string), score (0-1 confidence).

`edges` schema expected by every rule:
    src VARCHAR, dst VARCHAR, amt DOUBLE, ts TIMESTAMP

Self-loops are excluded upstream (they are not transfers between two parties, so they cannot
participate in a structural motif) — see phase2.md open-decision #3, which kept them flagged
rather than dropped precisely so this layer could make that call itself.
"""
from typing import Callable

# --- Thresholds -------------------------------------------------------------------------
# Every value below was picked by sweeping it against pattern-file ground truth on the full
# graph and reading off precision/lift — not chosen by eye. Base rate is 0.75% of accounts
# (3,170 implicated of 422,726 with at least one non-self-loop edge), so "lift" is precision
# divided by that. The measured operating point for each rule is recorded in phase4.md §3;
# the first-guess values were substantially worse and are documented there too.

WINDOW_HOURS = 24
FAN_OUT_MIN_RECEIVERS = 8   # 1,736 accounts, 5.0% precision, 6.7x lift
FAN_IN_MIN_SENDERS = 8      # 3,050 accounts, 1.1% precision, 1.5x lift — weakest rule kept

CYCLE_WINDOW_HOURS = 72
CYCLE_MIN_AMOUNT_RETENTION = 0.9  # 1,249 accounts, 2.8% precision, 3.7x lift

STACK_AMOUNT_TOLERANCE = 0.15  # 8,858 accounts, 2.7% precision, 3.6x lift, best recall (7.5%)
STACK_WINDOW_HOURS = 72

GATHER_SCATTER_MIN_SENDERS = 5    # 46 accounts, 52.2% precision, 69.6x lift
GATHER_SCATTER_MIN_RECEIVERS = 5
GATHER_SCATTER_WINDOW_HOURS = 96

SCATTER_GATHER_MIN_PATHS = 6      # 306 accounts, 100% precision, 133x lift — strongest rule
SCATTER_GATHER_WINDOW_HOURS = 96

BIPARTITE_MIN_SENDERS = 5         # 15 accounts, 6.7% precision, 8.9x lift
BIPARTITE_MIN_RECEIVERS = 5

RANDOM_MIN_TXNS = 200  # 195 accounts, 8.7% precision, 11.6x lift (before structured exclusion)

ALL_RULES = [
    "FAN-OUT", "FAN-IN", "CYCLE", "STACK",
    "GATHER-SCATTER", "SCATTER-GATHER", "BIPARTITE", "RANDOM",
]

# Rule names deliberately match the 8 pattern-file motif labels in `aml_pattern`
# (phase1.md §9) so Phase 6 can key one explanation template per name with no mapping layer.


def _clamp_score(expr: str, floor_val: float, ceil_val: float) -> str:
    """Map a raw count/ratio onto a 0-1 confidence, saturating at ceil_val."""
    return (
        f"least(1.0, greatest(0.0, "
        f"({expr} - {floor_val}) / nullif({ceil_val} - {floor_val}, 0)))"
    )


def fan_out_sql() -> str:
    """One sender opening many *new* receiver relationships inside a short window.

    Counts first-contact edges, not raw transactions: the table is deduplicated to one row per
    (src, dst) at the pair's first timestamp, so a RANGE window over that stream counts newly
    contacted receivers directly. This is both the cheaper formulation — a sort plus a window
    frame, instead of the quadratic per-sender self-join the first version used, which cost
    575s on the full graph — and the better signal, since a burst of *new* counterparties is
    what distinguishes fan-out from an established account paying its usual 8 suppliers daily.
    """
    return f"""
    WITH first_seen AS (
        SELECT src, dst, min(ts) AS ts, sum(amt) AS amt
        FROM edges GROUP BY src, dst
    ),
    cand AS (
        SELECT src FROM first_seen GROUP BY src
        HAVING count(*) >= {FAN_OUT_MIN_RECEIVERS}
    ),
    windowed AS (
        SELECT src, ts,
               count(*) OVER w AS n_receivers,
               sum(amt) OVER w AS window_amount
        FROM first_seen
        WHERE src IN (SELECT src FROM cand)
        WINDOW w AS (PARTITION BY src ORDER BY ts
                     RANGE BETWEEN CURRENT ROW AND INTERVAL '{WINDOW_HOURS} hours' FOLLOWING)
    ),
    peak AS (
        SELECT src, max(n_receivers) AS n_receivers,
               max_by(ts, n_receivers) AS anchor_ts,
               max_by(window_amount, n_receivers) AS window_amount
        FROM windowed GROUP BY src
        HAVING max(n_receivers) >= {FAN_OUT_MIN_RECEIVERS}
    )
    SELECT src AS account, 'FAN-OUT' AS rule,
           json_object(
               'unique_receivers', n_receivers,
               'window_hours', {WINDOW_HOURS},
               'window_start', strftime(anchor_ts, '%Y/%m/%d %H:%M'),
               'window_amount', round(window_amount, 2)
           ) AS evidence,
           {_clamp_score('n_receivers', FAN_OUT_MIN_RECEIVERS, FAN_OUT_MIN_RECEIVERS * 4)} AS score
    FROM peak
    """


def fan_in_sql() -> str:
    """Mirror of fan-out: many new senders converging on one receiver inside a window."""
    return f"""
    WITH first_seen AS (
        SELECT dst, src, min(ts) AS ts, sum(amt) AS amt
        FROM edges GROUP BY dst, src
    ),
    cand AS (
        SELECT dst FROM first_seen GROUP BY dst
        HAVING count(*) >= {FAN_IN_MIN_SENDERS}
    ),
    windowed AS (
        SELECT dst, ts,
               count(*) OVER w AS n_senders,
               sum(amt) OVER w AS window_amount
        FROM first_seen
        WHERE dst IN (SELECT dst FROM cand)
        WINDOW w AS (PARTITION BY dst ORDER BY ts
                     RANGE BETWEEN CURRENT ROW AND INTERVAL '{WINDOW_HOURS} hours' FOLLOWING)
    ),
    peak AS (
        SELECT dst, max(n_senders) AS n_senders,
               max_by(ts, n_senders) AS anchor_ts,
               max_by(window_amount, n_senders) AS window_amount
        FROM windowed GROUP BY dst
        HAVING max(n_senders) >= {FAN_IN_MIN_SENDERS}
    )
    SELECT dst AS account, 'FAN-IN' AS rule,
           json_object(
               'unique_senders', n_senders,
               'window_hours', {WINDOW_HOURS},
               'window_start', strftime(anchor_ts, '%Y/%m/%d %H:%M'),
               'window_amount', round(window_amount, 2)
           ) AS evidence,
           {_clamp_score('n_senders', FAN_IN_MIN_SENDERS, FAN_IN_MIN_SENDERS * 4)} AS score
    FROM peak
    """


def cycle_sql() -> str:
    """Money leaving an account and coming back through 2 or 3 hops, time-ordered.

    Hops must strictly advance in time and the returning amount must retain at least
    CYCLE_MIN_AMOUNT_RETENTION of the outbound amount — without that constraint any pair of
    unrelated recurring counterparties (561,575 repeat edges exist per phase1.md §8) reads as
    a 2-cycle.
    """
    return f"""
    WITH two_hop AS (
        SELECT e1.src AS account, 2 AS hops, e1.amt AS out_amount, e2.amt AS back_amount,
               e1.ts AS started_at, e2.ts AS closed_at
        FROM edges e1
        JOIN edges e2
          ON e2.src = e1.dst AND e2.dst = e1.src
         AND e2.ts > e1.ts
         AND e2.ts < e1.ts + INTERVAL '{CYCLE_WINDOW_HOURS} hours'
         AND e2.amt >= e1.amt * {CYCLE_MIN_AMOUNT_RETENTION}
    ),
    three_hop AS (
        SELECT e1.src AS account, 3 AS hops, e1.amt AS out_amount, e3.amt AS back_amount,
               e1.ts AS started_at, e3.ts AS closed_at
        FROM edges e1
        JOIN edges e2
          ON e2.src = e1.dst AND e2.ts > e1.ts
         AND e2.ts < e1.ts + INTERVAL '{CYCLE_WINDOW_HOURS} hours'
        JOIN edges e3
          ON e3.src = e2.dst AND e3.dst = e1.src
         AND e3.ts > e2.ts
         AND e3.ts < e1.ts + INTERVAL '{CYCLE_WINDOW_HOURS} hours'
        WHERE e2.dst <> e1.src
          AND e3.amt >= e1.amt * {CYCLE_MIN_AMOUNT_RETENTION}
    ),
    combined AS (SELECT * FROM two_hop UNION ALL SELECT * FROM three_hop),
    best AS (
        SELECT account, min(hops) AS hops, count(*) AS n_cycles,
               max_by(out_amount, out_amount) AS out_amount,
               max_by(back_amount, out_amount) AS back_amount,
               max_by(started_at, out_amount) AS started_at,
               max_by(closed_at, out_amount) AS closed_at
        FROM combined GROUP BY account
    )
    SELECT account, 'CYCLE' AS rule,
           json_object(
               'hops', hops,
               'cycles_found', n_cycles,
               'out_amount', round(out_amount, 2),
               'returned_amount', round(back_amount, 2),
               'retention_pct', round(100.0 * back_amount / nullif(out_amount, 0), 1),
               'started_at', strftime(started_at, '%Y/%m/%d %H:%M'),
               'closed_at', strftime(closed_at, '%Y/%m/%d %H:%M')
           ) AS evidence,
           {_clamp_score('n_cycles', 0, 5)} AS score
    FROM best
    """


def stack_sql() -> str:
    """Layering: a 3-hop chain A->B->C->D where each hop passes on most of the amount.

    The amount-retention band is what separates a deliberate pass-through chain from three
    coincidental transactions that happen to share endpoints. All four accounts are
    implicated, since the intermediaries are the point of the structure.
    """
    band_lo = 1.0 - STACK_AMOUNT_TOLERANCE
    return f"""
    WITH chains AS (
        SELECT e1.src AS a, e1.dst AS b, e2.dst AS c, e3.dst AS d,
               e1.amt AS amt_in, e3.amt AS amt_out,
               e1.ts AS started_at, e3.ts AS ended_at
        FROM edges e1
        JOIN edges e2
          ON e2.src = e1.dst AND e2.ts > e1.ts
         AND e2.ts < e1.ts + INTERVAL '{STACK_WINDOW_HOURS} hours'
         AND e2.amt BETWEEN e1.amt * {band_lo} AND e1.amt
        JOIN edges e3
          ON e3.src = e2.dst AND e3.ts > e2.ts
         AND e3.ts < e1.ts + INTERVAL '{STACK_WINDOW_HOURS} hours'
         AND e3.amt BETWEEN e2.amt * {band_lo} AND e2.amt
        WHERE e1.src <> e2.dst AND e1.src <> e3.dst AND e1.dst <> e3.dst
    ),
    implicated AS (
        SELECT a AS account, amt_in, amt_out, started_at, ended_at FROM chains
        UNION ALL SELECT b, amt_in, amt_out, started_at, ended_at FROM chains
        UNION ALL SELECT c, amt_in, amt_out, started_at, ended_at FROM chains
        UNION ALL SELECT d, amt_in, amt_out, started_at, ended_at FROM chains
    ),
    best AS (
        SELECT account, count(*) AS n_chains,
               max_by(amt_in, amt_in) AS amt_in,
               max_by(amt_out, amt_in) AS amt_out,
               max_by(started_at, amt_in) AS started_at,
               max_by(ended_at, amt_in) AS ended_at
        FROM implicated GROUP BY account
    )
    SELECT account, 'STACK' AS rule,
           json_object(
               'hops', 3,
               'chains_found', n_chains,
               'amount_in', round(amt_in, 2),
               'amount_out', round(amt_out, 2),
               'retention_pct', round(100.0 * amt_out / nullif(amt_in, 0), 1),
               'started_at', strftime(started_at, '%Y/%m/%d %H:%M'),
               'ended_at', strftime(ended_at, '%Y/%m/%d %H:%M')
           ) AS evidence,
           {_clamp_score('n_chains', 0, 5)} AS score
    FROM best
    """


def gather_scatter_sql() -> str:
    """Collect from many, then redistribute to many — the classic mule-account shape.

    Ordering matters: the gather phase must *begin* before the scatter phase begins,
    otherwise this is just a busy hub account rather than a staged consolidation.
    """
    return f"""
    -- One deduplicated event per (account, direction, counterparty) at first contact, so a
    -- plain row count over a window frame *is* a distinct-counterparty count.
    WITH events AS (
        SELECT account, direction, counterparty, min(ts) AS ts, sum(amt) AS amt
        FROM (
            SELECT dst AS account, 'in' AS direction, src AS counterparty, ts, amt FROM edges
            UNION ALL
            SELECT src, 'out', dst, ts, amt FROM edges
        ) GROUP BY account, direction, counterparty
    ),
    cand AS (
        SELECT account FROM events GROUP BY account
        HAVING count(*) FILTER (WHERE direction = 'in') >= {GATHER_SCATTER_MIN_SENDERS}
           AND count(*) FILTER (WHERE direction = 'out') >= {GATHER_SCATTER_MIN_RECEIVERS}
    ),
    -- Pivot on each inflow event: senders arriving in the preceding window, receivers paid in
    -- the following one. Two earlier formulations were wrong here. Comparing lifetime min/max
    -- is unsatisfiable on an 18-day span (it matched exactly one account); joining inflow to
    -- inflow to outflow is correct but materializes I*I*O rows per account, which on a hub
    -- account is billions of rows. Window frames give the same answer in one sorted pass.
    windowed AS (
        SELECT account, ts, direction,
               count(*) FILTER (WHERE direction = 'in')  OVER prev AS n_senders,
               sum(amt) FILTER (WHERE direction = 'in')  OVER prev AS amount_in,
               min(ts)  FILTER (WHERE direction = 'in')  OVER prev AS first_in,
               count(*) FILTER (WHERE direction = 'out') OVER nxt  AS n_receivers,
               sum(amt) FILTER (WHERE direction = 'out') OVER nxt  AS amount_out,
               max(ts)  FILTER (WHERE direction = 'out') OVER nxt  AS last_out
        FROM events
        WHERE account IN (SELECT account FROM cand)
        WINDOW prev AS (PARTITION BY account ORDER BY ts
                        RANGE BETWEEN INTERVAL '{GATHER_SCATTER_WINDOW_HOURS} hours' PRECEDING
                                  AND CURRENT ROW),
               nxt  AS (PARTITION BY account ORDER BY ts
                        RANGE BETWEEN CURRENT ROW
                                  AND INTERVAL '{GATHER_SCATTER_WINDOW_HOURS} hours' FOLLOWING)
    ),
    pivots AS (
        SELECT * FROM windowed
        WHERE direction = 'in'
          AND n_senders >= {GATHER_SCATTER_MIN_SENDERS}
          AND n_receivers >= {GATHER_SCATTER_MIN_RECEIVERS}
    ),
    joined AS (
        SELECT account,
               max(n_senders) AS n_senders,
               max_by(n_receivers, n_senders) AS n_receivers,
               max_by(amount_in, n_senders) AS amount_in,
               max_by(amount_out, n_senders) AS amount_out,
               max_by(first_in, n_senders) AS first_in,
               max_by(last_out, n_senders) AS last_out
        FROM pivots GROUP BY account
    )
    SELECT account, 'GATHER-SCATTER' AS rule,
           json_object(
               'unique_senders', n_senders,
               'unique_receivers', n_receivers,
               'amount_in', round(amount_in, 2),
               'amount_out', round(amount_out, 2),
               'passthrough_pct', round(100.0 * amount_out / nullif(amount_in, 0), 1),
               'first_inflow', strftime(first_in, '%Y/%m/%d %H:%M'),
               'last_outflow', strftime(last_out, '%Y/%m/%d %H:%M')
           ) AS evidence,
           {_clamp_score('least(n_senders, n_receivers)', GATHER_SCATTER_MIN_SENDERS, GATHER_SCATTER_MIN_SENDERS * 4)} AS score
    FROM joined
    """


def scatter_gather_sql() -> str:
    """One origin splits across several intermediaries that all converge on one sink.

    Detected as >=N distinct 2-hop paths sharing the same (origin, sink) pair. Both endpoints
    and every intermediary are implicated.
    """
    return f"""
    WITH paths AS (
        SELECT e1.src AS origin, e1.dst AS mid, e2.dst AS sink,
               e1.amt AS amt_in, e2.amt AS amt_out, e1.ts AS started_at, e2.ts AS ended_at
        FROM edges e1
        JOIN edges e2
          ON e2.src = e1.dst AND e2.ts > e1.ts
         AND e2.ts < e1.ts + INTERVAL '{SCATTER_GATHER_WINDOW_HOURS} hours'
        WHERE e1.src <> e2.dst
    ),
    grouped AS (
        SELECT origin, sink, count(DISTINCT mid) AS n_paths,
               sum(amt_in) AS amount_in, sum(amt_out) AS amount_out,
               min(started_at) AS started_at, max(ended_at) AS ended_at
        FROM paths GROUP BY origin, sink
        HAVING count(DISTINCT mid) >= {SCATTER_GATHER_MIN_PATHS}
    ),
    implicated AS (
        SELECT origin AS account, * EXCLUDE (origin, sink) FROM grouped
        UNION ALL SELECT sink, * EXCLUDE (origin, sink) FROM grouped
        UNION ALL
        SELECT DISTINCT p.mid, g.* EXCLUDE (origin, sink)
        FROM paths p JOIN grouped g ON p.origin = g.origin AND p.sink = g.sink
    ),
    best AS (
        SELECT account, max(n_paths) AS n_paths,
               max_by(amount_in, n_paths) AS amount_in,
               max_by(amount_out, n_paths) AS amount_out,
               max_by(started_at, n_paths) AS started_at,
               max_by(ended_at, n_paths) AS ended_at
        FROM implicated GROUP BY account
    )
    SELECT account, 'SCATTER-GATHER' AS rule,
           json_object(
               'intermediaries', n_paths,
               'amount_in', round(amount_in, 2),
               'amount_out', round(amount_out, 2),
               'started_at', strftime(started_at, '%Y/%m/%d %H:%M'),
               'ended_at', strftime(ended_at, '%Y/%m/%d %H:%M')
           ) AS evidence,
           {_clamp_score('n_paths', SCATTER_GATHER_MIN_PATHS, SCATTER_GATHER_MIN_PATHS * 4)} AS score
    FROM best
    """


def bipartite_sql() -> str:
    """A dense block: >=M senders each paying into the same set of >=N receivers.

    Found by pairing senders that share receivers, then requiring the shared-receiver count
    to clear the threshold. The degree pre-filter keeps the sender-pair join from exploding —
    only accounts that already reach BIPARTITE_MIN_RECEIVERS distinct receivers can qualify.
    """
    return f"""
    WITH cand AS (
        SELECT src FROM edges GROUP BY src
        HAVING count(DISTINCT dst) >= {BIPARTITE_MIN_RECEIVERS}
    ),
    ce AS (SELECT DISTINCT src, dst FROM edges WHERE src IN (SELECT src FROM cand)),
    shared AS (
        SELECT a.src AS src_a, b.src AS src_b, count(*) AS shared_receivers
        FROM ce a JOIN ce b ON a.dst = b.dst AND a.src < b.src
        GROUP BY a.src, b.src
        HAVING count(*) >= {BIPARTITE_MIN_RECEIVERS}
    ),
    members AS (
        SELECT src_a AS account, shared_receivers FROM shared
        UNION ALL SELECT src_b, shared_receivers FROM shared
    ),
    peers AS (
        SELECT account, max(shared_receivers) AS shared_receivers,
               count(*) + 1 AS block_senders
        FROM members GROUP BY account
        HAVING count(*) + 1 >= {BIPARTITE_MIN_SENDERS}
    )
    SELECT account, 'BIPARTITE' AS rule,
           json_object(
               'block_senders', block_senders,
               'shared_receivers', shared_receivers
           ) AS evidence,
           {_clamp_score('shared_receivers', BIPARTITE_MIN_RECEIVERS, BIPARTITE_MIN_RECEIVERS * 4)} AS score
    FROM peers
    """


def random_sql(structured_rules_table: str = "structured_hits") -> str:
    """Residual motif: a busy two-way account that matches none of the seven shaped rules.

    The pattern file's RANDOM blocks are laundering attempts with no clean topology
    (phase1.md §9), so defining this as a set difference against the structured rules is the
    honest encoding — there is no positive shape to test for. `structured_rules_table` must
    be a relation with an `account` column holding every account hit by rules 1-7.
    """
    return f"""
    WITH activity AS (
        SELECT account, sum(n) AS txn_count, count(DISTINCT direction) AS directions
        FROM (
            SELECT src AS account, 'out' AS direction, count(*) AS n FROM edges GROUP BY src
            UNION ALL
            SELECT dst, 'in', count(*) FROM edges GROUP BY dst
        ) GROUP BY account
        HAVING sum(n) >= {RANDOM_MIN_TXNS} AND count(DISTINCT direction) = 2
    )
    SELECT a.account, 'RANDOM' AS rule,
           json_object('txn_count', a.txn_count, 'structured_match', false) AS evidence,
           {_clamp_score('a.txn_count', RANDOM_MIN_TXNS, RANDOM_MIN_TXNS * 5)} AS score
    FROM activity a
    WHERE a.account NOT IN (SELECT account FROM {structured_rules_table})
    """


# Rules 1-7 are positive structural tests and can run in any order. RANDOM is deliberately
# excluded here because it is a set difference over the other seven — see build_rule_hits.py.
STRUCTURED_RULES: dict[str, Callable[[], str]] = {
    "FAN-OUT": fan_out_sql,
    "FAN-IN": fan_in_sql,
    "CYCLE": cycle_sql,
    "STACK": stack_sql,
    "GATHER-SCATTER": gather_scatter_sql,
    "SCATTER-GATHER": scatter_gather_sql,
    "BIPARTITE": bipartite_sql,
}


EDGES_DDL = """
CREATE OR REPLACE TABLE edges (
    src VARCHAR, dst VARCHAR, amt DOUBLE, ts TIMESTAMP
)
"""

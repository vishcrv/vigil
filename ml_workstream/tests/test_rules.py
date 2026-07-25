"""
Phase 4 rule-engine tests, per ml_spec.md "Testing": each of the 8 motif rules gets a
synthetic transaction sequence that should fire it and one that shouldn't.

These run the exact SQL the batch pass runs (ml/rules.py), against a tiny in-memory `edges`
table — so a threshold or join-condition change that breaks a motif fails here rather than
silently changing 5M rows of output.
"""
from datetime import datetime, timedelta

import duckdb
import pytest

from ml import rules

T0 = datetime(2022, 9, 1, 9, 0)


def make_con(edges: list[tuple]) -> duckdb.DuckDBPyConnection:
    """Fresh in-memory DB with `edges` populated. Rows are (src, dst, amt, ts)."""
    con = duckdb.connect(":memory:")
    con.execute(rules.EDGES_DDL)
    con.executemany("INSERT INTO edges VALUES (?, ?, ?, ?)", edges)
    return con


def accounts_hit(con: duckdb.DuckDBPyConnection, sql: str) -> set[str]:
    return {row[0] for row in con.execute(sql).fetchall()}


def hours(n: float) -> timedelta:
    return timedelta(hours=n)


# --- FAN-OUT ------------------------------------------------------------------------------

def test_fan_out_fires_when_many_receivers_in_window():
    edges = [("A", f"R{i}", 1000.0, T0 + hours(i)) for i in range(rules.FAN_OUT_MIN_RECEIVERS)]
    hits = accounts_hit(make_con(edges), rules.fan_out_sql())
    assert "A" in hits


def test_fan_out_silent_when_same_receivers_spread_past_window():
    # Same 8 receivers, but one every 3 days — never 8 inside a 24h window.
    edges = [("A", f"R{i}", 1000.0, T0 + timedelta(days=3 * i))
             for i in range(rules.FAN_OUT_MIN_RECEIVERS)]
    hits = accounts_hit(make_con(edges), rules.fan_out_sql())
    assert "A" not in hits


# --- FAN-IN -------------------------------------------------------------------------------

def test_fan_in_fires_when_many_senders_in_window():
    edges = [(f"S{i}", "B", 1000.0, T0 + hours(i)) for i in range(rules.FAN_IN_MIN_SENDERS)]
    hits = accounts_hit(make_con(edges), rules.fan_in_sql())
    assert "B" in hits


def test_fan_in_silent_below_sender_threshold():
    edges = [(f"S{i}", "B", 1000.0, T0 + hours(i))
             for i in range(rules.FAN_IN_MIN_SENDERS - 1)]
    hits = accounts_hit(make_con(edges), rules.fan_in_sql())
    assert "B" not in hits


# --- CYCLE --------------------------------------------------------------------------------

def test_cycle_fires_on_two_hop_round_trip():
    edges = [("A", "B", 1000.0, T0), ("B", "A", 900.0, T0 + hours(1))]
    hits = accounts_hit(make_con(edges), rules.cycle_sql())
    assert "A" in hits


def test_cycle_fires_on_three_hop_loop():
    edges = [("A", "B", 1000.0, T0),
             ("B", "C", 950.0, T0 + hours(1)),
             ("C", "A", 900.0, T0 + hours(2))]
    hits = accounts_hit(make_con(edges), rules.cycle_sql())
    assert "A" in hits


def test_cycle_silent_when_returned_amount_too_small():
    # Reciprocal edge exists, but only 5% comes back — ordinary two-way counterparties,
    # not value cycling back to origin.
    edges = [("A", "B", 1000.0, T0), ("B", "A", 50.0, T0 + hours(1))]
    hits = accounts_hit(make_con(edges), rules.cycle_sql())
    assert "A" not in hits


def test_cycle_silent_when_return_precedes_outflow():
    edges = [("A", "B", 1000.0, T0 + hours(5)), ("B", "A", 900.0, T0)]
    hits = accounts_hit(make_con(edges), rules.cycle_sql())
    assert "A" not in hits


# --- STACK --------------------------------------------------------------------------------

def test_stack_fires_on_passthrough_chain():
    edges = [("A", "B", 1000.0, T0),
             ("B", "C", 900.0, T0 + hours(1)),
             ("C", "D", 850.0, T0 + hours(2))]
    hits = accounts_hit(make_con(edges), rules.stack_sql())
    assert {"A", "B", "C", "D"} <= hits


def test_stack_silent_when_amount_not_carried_through():
    # Final hop drops to 5% of the previous one — C is not passing the money on, so this is
    # three unrelated transfers that happen to chain.
    edges = [("A", "B", 1000.0, T0),
             ("B", "C", 900.0, T0 + hours(1)),
             ("C", "D", 45.0, T0 + hours(2))]
    hits = accounts_hit(make_con(edges), rules.stack_sql())
    assert hits == set()


# --- GATHER-SCATTER -----------------------------------------------------------------------

def test_gather_scatter_fires_when_inflow_precedes_outflow():
    n = rules.GATHER_SCATTER_MIN_SENDERS
    edges = [(f"S{i}", "M", 1000.0, T0 + hours(i)) for i in range(n)]
    edges += [("M", f"R{i}", 900.0, T0 + hours(24 + i)) for i in range(n)]
    hits = accounts_hit(make_con(edges), rules.gather_scatter_sql())
    assert "M" in hits


def test_gather_scatter_silent_when_outflow_precedes_inflow():
    # Same degrees, reversed order: the account paid out before it collected anything, so
    # this is not a staged consolidation.
    n = rules.GATHER_SCATTER_MIN_SENDERS
    edges = [("M", f"R{i}", 900.0, T0 + hours(i)) for i in range(n)]
    edges += [(f"S{i}", "M", 1000.0, T0 + hours(24 + i)) for i in range(n)]
    hits = accounts_hit(make_con(edges), rules.gather_scatter_sql())
    assert "M" not in hits


# --- SCATTER-GATHER -----------------------------------------------------------------------

def test_scatter_gather_fires_on_parallel_two_hop_paths():
    n = rules.SCATTER_GATHER_MIN_PATHS
    edges = []
    for i in range(n):
        edges.append(("O", f"M{i}", 1000.0, T0 + hours(i)))
        edges.append((f"M{i}", "Z", 950.0, T0 + hours(i + 1)))
    hits = accounts_hit(make_con(edges), rules.scatter_gather_sql())
    assert {"O", "Z"} <= hits


def test_scatter_gather_silent_below_path_threshold():
    n = rules.SCATTER_GATHER_MIN_PATHS - 1
    edges = []
    for i in range(n):
        edges.append(("O", f"M{i}", 1000.0, T0 + hours(i)))
        edges.append((f"M{i}", "Z", 950.0, T0 + hours(i + 1)))
    hits = accounts_hit(make_con(edges), rules.scatter_gather_sql())
    assert hits == set()


# --- BIPARTITE ----------------------------------------------------------------------------

def test_bipartite_fires_on_dense_sender_receiver_block():
    senders = [f"S{i}" for i in range(rules.BIPARTITE_MIN_SENDERS)]
    receivers = [f"R{i}" for i in range(rules.BIPARTITE_MIN_RECEIVERS)]
    edges = [(s, r, 1000.0, T0 + hours(i))
             for i, (s, r) in enumerate((s, r) for s in senders for r in receivers)]
    hits = accounts_hit(make_con(edges), rules.bipartite_sql())
    assert set(senders) <= hits


def test_bipartite_silent_when_senders_share_no_receivers():
    # Same sender and receiver counts, but disjoint counterparties — no shared block.
    edges = []
    for i in range(rules.BIPARTITE_MIN_SENDERS):
        for j in range(rules.BIPARTITE_MIN_RECEIVERS):
            edges.append((f"S{i}", f"R{i}_{j}", 1000.0, T0 + hours(i * 10 + j)))
    hits = accounts_hit(make_con(edges), rules.bipartite_sql())
    assert hits == set()


# --- RANDOM -------------------------------------------------------------------------------

def _random_edges(account: str, n_pairs: int) -> list[tuple]:
    """Alternating in/out activity for `account`, n_pairs*2 transactions total."""
    edges = []
    for i in range(n_pairs):
        edges.append((f"IN{i}", account, 1000.0, T0 + hours(2 * i)))
        edges.append((account, f"OUT{i}", 900.0, T0 + hours(2 * i + 1)))
    return edges


def _with_structured(con: duckdb.DuckDBPyConnection, accounts: list[str]):
    con.execute("CREATE OR REPLACE TABLE structured_hits (account VARCHAR)")
    for acct in accounts:
        con.execute("INSERT INTO structured_hits VALUES (?)", [acct])
    return con


def test_random_fires_on_busy_account_with_no_structural_match():
    con = make_con(_random_edges("X", rules.RANDOM_MIN_TXNS))
    _with_structured(con, [])
    hits = accounts_hit(con, rules.random_sql())
    assert "X" in hits


def test_random_silent_when_account_already_matched_a_structured_rule():
    con = make_con(_random_edges("X", rules.RANDOM_MIN_TXNS))
    _with_structured(con, ["X"])
    hits = accounts_hit(con, rules.random_sql())
    assert "X" not in hits


def test_random_silent_below_activity_threshold():
    con = make_con(_random_edges("X", 2))
    _with_structured(con, [])
    hits = accounts_hit(con, rules.random_sql())
    assert "X" not in hits


# --- shared invariants --------------------------------------------------------------------

@pytest.mark.parametrize("name,builder", sorted(rules.STRUCTURED_RULES.items()))
def test_every_structured_rule_returns_the_agreed_columns(name, builder):
    """All rules must emit (account, rule, evidence, score) so the batch pass can UNION them
    and Phase 5/6 can consume one shape regardless of which motif fired."""
    con = make_con([("A", "B", 1000.0, T0)])
    con.execute(builder())
    assert [d[0] for d in con.description] == ["account", "rule", "evidence", "score"]


@pytest.mark.parametrize("name,builder", sorted(rules.STRUCTURED_RULES.items()))
def test_every_structured_rule_is_silent_on_a_single_transaction(name, builder):
    """One isolated transfer is not a motif — nothing should fire on it."""
    con = make_con([("A", "B", 1000.0, T0)])
    assert accounts_hit(con, builder()) == set()

"""
Phase 4 `anomaly` tests.

Covers the two properties the tool's output is only meaningful under — the score is a
calibrated [0,1] percentile, and the caller can tell whether the scope was scored in full —
plus the interface contract (structured errors, JSON-serializable output).

Runs against the real Parquet and the persisted detectors, and skips when either is missing.
"""
import json
from pathlib import Path

import pytest

from ml.anomaly import (
    MAX_RULE_HITS,
    MAX_SCORE_ROWS,
    METHODS,
    MODEL_DIR,
    _weights_for,
    anomaly,
)
from ml.data import ENRICHED_PATH

pytestmark = pytest.mark.skipif(
    not Path(ENRICHED_PATH).exists() or not (MODEL_DIR / "metadata.json").exists(),
    reason="enriched Parquet or trained models missing - run scripts/enrich.py then "
           "scripts/train_models.py",
)

ACCOUNT = "8000EBD30"
BUSY_RANGE = ["2022-09-01", "2022-09-05"]   # millions of rows, forces truncation


# --- contract ---------------------------------------------------------------------------------

@pytest.mark.parametrize("scope", [None, "8000EBD30", 42, {}, {"role": "both"}])
def test_malformed_scope_returns_error_not_exception(scope):
    assert "error" in anomaly(scope)


def test_unknown_method_is_rejected_and_lists_the_valid_ones():
    error = anomaly({"account_id": ACCOUNT}, method="magic")["error"]
    assert "method must be one of" in error
    for name in METHODS:
        assert name in error


def test_bad_date_is_reported_rather_than_scoring_nothing():
    """An unparseable bound must not fall through to 'no transactions matched' — that reads
    as 'we checked and it is clean'."""
    result = anomaly({"date_range": ["last week", "2022-09-05"]})
    assert "error" in result
    assert "row_count_scored" not in result


def test_result_is_json_serializable():
    json.dumps(anomaly({"account_id": ACCOUNT}))


# --- scoring -------------------------------------------------------------------------------------

def test_scores_an_account_within_the_calibrated_range():
    result = anomaly({"account_id": ACCOUNT})
    assert result["row_count_scored"] == 15
    assert 0.0 <= result["anomaly_score"] <= 1.0
    assert result["mean_anomaly_score"] <= result["anomaly_score"]


def test_headline_is_the_worst_row_not_the_average():
    result = anomaly({"account_id": ACCOUNT})
    assert result["anomaly_score"] == max(r["anomaly_score"] for r in result["top_rows"])


def test_top_rows_are_sorted_by_score():
    scores = [r["anomaly_score"] for r in anomaly({"date_range": BUSY_RANGE})["top_rows"]]
    assert scores == sorted(scores, reverse=True)


def test_unmatched_scope_reports_no_data_rather_than_a_low_score():
    result = anomaly({"account_id": "NO_SUCH_ACCOUNT"})
    assert result["row_count_scored"] == 0
    assert result["truncated"] is False
    assert "note" in result


@pytest.mark.parametrize("method", METHODS)
def test_single_method_runs_alone_and_takes_the_full_weight(method):
    result = anomaly({"account_id": ACCOUNT}, method=method)
    assert set(result["method_scores"]) == {method}
    assert result["method_weights"] == {method: 1.0}


def test_all_methods_are_blended_by_measured_weight():
    result = anomaly({"account_id": ACCOUNT}, method="all")
    assert set(result["method_scores"]) == set(METHODS)
    weights = result["method_weights"]
    assert pytest.approx(sum(weights.values()), abs=1e-3) == 1.0
    # The whole point of weighting: the detector with the higher measured lift leads, rather
    # than a near-chance method counting equally in an unweighted mean.
    assert weights["isolation_forest"] > weights["lof"] > weights["zscore"]


# --- truncation ------------------------------------------------------------------------------------

def test_small_scope_is_not_marked_truncated():
    result = anomaly({"account_id": ACCOUNT})
    assert result["truncated"] is False
    assert result["rows_matched"] == result["row_count_scored"] == 15


def test_large_scope_reports_what_it_left_out():
    result = anomaly({"date_range": BUSY_RANGE})
    assert result["row_count_scored"] == MAX_SCORE_ROWS
    assert result["rows_matched"] > MAX_SCORE_ROWS
    assert result["truncated"] is True


def test_caller_limit_below_the_cap_is_honoured():
    result = anomaly({"date_range": BUSY_RANGE, "limit": 100})
    assert result["row_count_scored"] == 100
    assert result["truncated"] is True


# --- rule hits ----------------------------------------------------------------------------------

def test_rule_hits_carry_account_rule_score_and_evidence():
    result = anomaly({"date_range": BUSY_RANGE})
    for hit in result["rule_hits"][:20]:
        assert set(hit) == {"account", "rule", "score", "evidence"}
        assert isinstance(hit["evidence"], dict)
    assert result["rule_names"] == sorted({h["rule"] for h in result["rule_hits"]})


# --- weighting helper ------------------------------------------------------------------------------

def test_weights_fall_back_to_equal_when_metadata_predates_weighting():
    assert _weights_for(("a", "b"), {}) == {"a": 0.5, "b": 0.5}


def test_weights_are_renormalized_over_the_requested_subset():
    weights = _weights_for(("a", "b"), {"a": 0.6, "b": 0.2, "c": 0.2})
    assert pytest.approx(weights["a"], abs=1e-9) == 0.75
    assert pytest.approx(sum(weights.values()), abs=1e-9) == 1.0


# --- rule-hit budget --------------------------------------------------------------------

def test_subject_rule_hits_survive_the_cap():
    """The scoped account's own hits must never be evicted by its counterparties'.

    Regression: hits were capped at MAX_RULE_HITS ordered by score alone. Account 1004286A8
    has a GATHER-SCATTER hit scoring 1.000 and so do dozens of accounts it transacts with, so
    the subject fell outside the top 25. `risk` then saw no hit for the account being asked
    about and reported pattern_detected NONE with an explanation reading "no named laundering
    pattern matched" — for a perfect motif match.
    """
    result = anomaly({"account_id": "1004286A8"})
    assert result["rule_hits_truncated"] is True, "fixture must exceed the cap to be meaningful"

    subject = [h for h in result["rule_hits"] if h["account"] == "1004286A8"]
    assert subject, "the subject's own rule hits were dropped"
    assert {h["rule"] for h in subject} >= {"GATHER-SCATTER"}
    # and they come first, so a truncating consumer still sees them
    assert result["rule_hits"][0]["account"] == "1004286A8"


def test_rule_hits_stay_within_the_loop_budget():
    """Payload must fit agent.loop.MAX_TOOL_RESULT_CHARS or the chain into risk() breaks."""
    for scope in ({"account_id": "1004286A8"}, {"date_range": ["2022-09-01", "2022-09-05"]}):
        assert len(json.dumps(anomaly(scope))) < 20_000, scope


def test_capped_hits_report_the_true_total():
    result = anomaly({"date_range": ["2022-09-01", "2022-09-05"]})
    assert result["rule_hits_returned"] == len(result["rule_hits"]) <= MAX_RULE_HITS
    assert result["rule_hits_total"] > result["rule_hits_returned"]
    assert result["rule_hits_truncated"] is True

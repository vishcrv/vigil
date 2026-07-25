"""
Phase 3 `feature_eng` tests.

Two things are being defended here. The interface contract — plain dict in, plain JSON out,
structured error instead of an exception on anything malformed — and scope filtering, which is
where the ISO-date bug lived: a filter that matches nothing looks identical to an account with
no activity unless a test pins an expected count.

Runs against the real enriched Parquet, like the Phase 7 EDA tests, and skips rather than
fails when it is absent (a fresh clone has to run scripts/enrich.py first).
"""
import json

import pytest

from ml.data import ENRICHED_PATH
from ml.feature_eng import DEFAULT_LIMIT, MAX_LIMIT, feature_eng, validate_scope

pytestmark = pytest.mark.skipif(
    not __import__("pathlib").Path(ENRICHED_PATH).exists(),
    reason="enriched Parquet not built - run scripts/enrich.py",
)

# 15 transactions between 2022/09/01 and 2022/09/10, none laundering.
ACCOUNT = "8000EBD30"


# --- validation: never raises, always a structured error ------------------------------------

@pytest.mark.parametrize("scope", [
    None, "8000EBD30", 42, [], {"role": "both"},
])
def test_malformed_scope_returns_error_not_exception(scope):
    result = feature_eng(scope)
    assert "error" in result


@pytest.mark.parametrize("scope,fragment", [
    ({}, "account_id or date_range"),
    ({"account_id": ""}, "account_id"),
    ({"account_id": ACCOUNT, "role": "payer"}, "role"),
    ({"account_id": ACCOUNT, "min_amount": "big"}, "min_amount"),
    ({"account_id": ACCOUNT, "min_amount": 100, "max_amount": 10}, "min_amount must be <="),
    ({"account_id": ACCOUNT, "limit": 0}, "limit"),
    ({"account_id": ACCOUNT, "limit": True}, "limit"),
    ({"account_id": ACCOUNT, "velocity_window_days": -3}, "velocity_window_days"),
    ({"date_range": "2022-09-01"}, "date_range"),
    ({"date_range": ["yesterday", "today"]}, "date_range"),
])
def test_each_invalid_field_is_named_in_the_error(scope, fragment):
    result = feature_eng(scope)
    assert fragment in result["error"]


def test_missing_scope_message_is_not_added_on_top_of_a_date_parse_failure():
    """A malformed date_range already says why; also claiming it is absent misleads."""
    error = feature_eng({"date_range": ["nope", "2022-09-02"]})["error"]
    assert "must include at least" not in error


def test_limit_is_capped_not_rejected():
    clean, errors = validate_scope({"account_id": ACCOUNT, "limit": MAX_LIMIT * 10})
    assert not errors
    assert clean["limit"] == MAX_LIMIT


# --- scope filtering -------------------------------------------------------------------------

def test_account_scope_returns_that_accounts_activity():
    result = feature_eng({"account_id": ACCOUNT})
    assert result["aggregate"]["txn_count"] == 15
    assert all(
        ACCOUNT in (r["From Account"], r["To Account"]) for r in result["records"]
    )


@pytest.mark.parametrize("role,field", [("sender", "From Account"), ("receiver", "To Account")])
def test_role_restricts_which_side_the_account_appears_on(role, field):
    result = feature_eng({"account_id": ACCOUNT, "role": role})
    assert all(r[field] == ACCOUNT for r in result["records"])


def test_both_is_the_union_of_the_two_roles():
    """Union, not sum: a self-loop (11.6% of the dataset, ml_spec.md decision 3) appears on
    both sides and must not be double-counted."""
    both = feature_eng({"account_id": ACCOUNT, "limit": MAX_LIMIT})
    sender = feature_eng({"account_id": ACCOUNT, "role": "sender"})["aggregate"]["txn_count"]
    receiver = feature_eng({"account_id": ACCOUNT, "role": "receiver"})["aggregate"]["txn_count"]
    self_loops = sum(1 for r in both["records"] if r["is_self_loop"])
    assert both["aggregate"]["txn_count"] == sender + receiver - self_loops


def test_iso_date_range_matches_the_same_rows_as_the_stored_format():
    """The regression that mattered: an ISO bound used to match zero rows silently."""
    iso = feature_eng({"date_range": ["2022-09-01", "2022-09-02"], "limit": 1})
    slash = feature_eng({"date_range": ["2022/09/01", "2022/09/02"], "limit": 1})
    assert iso["aggregate"]["txn_count"] == slash["aggregate"]["txn_count"] > 0


def test_date_range_upper_bound_includes_the_final_day():
    one_day = feature_eng({"date_range": ["2022-09-01", "2022-09-01"], "limit": 1})
    assert one_day["aggregate"]["txn_count"] > 0
    assert one_day["aggregate"]["latest_ts"].startswith("2022/09/01")


def test_date_range_excludes_rows_outside_it():
    result = feature_eng({"date_range": ["2022-09-02", "2022-09-03"], "limit": 50})
    assert result["records"]
    assert all("2022/09/02" <= r["Timestamp"][:10] <= "2022/09/03" for r in result["records"])


def test_amount_bounds_are_applied():
    result = feature_eng({"account_id": ACCOUNT, "min_amount": 100, "limit": 50})
    assert all(r["Amount Received"] >= 100 for r in result["records"])


def test_unmatched_scope_reports_zero_rather_than_erroring():
    result = feature_eng({"account_id": "NO_SUCH_ACCOUNT"})
    assert "error" not in result
    assert result["aggregate"]["txn_count"] == 0
    assert result["records"] == []


# --- output shape ------------------------------------------------------------------------------

def test_truncation_is_reported():
    result = feature_eng({"account_id": ACCOUNT, "limit": 3})
    assert result["record_count_returned"] == 3
    assert result["record_count_truncated"] is True


def test_untruncated_result_says_so():
    result = feature_eng({"account_id": ACCOUNT, "limit": 100})
    assert result["record_count_truncated"] is False


def test_velocity_is_only_computed_when_asked_for():
    assert "velocity" not in feature_eng({"account_id": ACCOUNT})
    with_velocity = feature_eng({"account_id": ACCOUNT, "velocity_window_days": 7})
    assert with_velocity["velocity"]["window_days"] == 7
    assert with_velocity["velocity"]["txn_count_in_window"] > 0


def test_result_is_json_serializable():
    """The loop hands this straight back to the LLM as a tool result — a numpy scalar or a
    pandas Timestamp surviving to here breaks the boundary, not this module."""
    result = feature_eng({"account_id": ACCOUNT, "limit": 5, "velocity_window_days": 3})
    json.dumps(result)


def test_default_limit_applies_when_unset():
    result = feature_eng({"date_range": ["2022-09-01", "2022-09-18"]})
    assert result["record_count_returned"] == DEFAULT_LIMIT

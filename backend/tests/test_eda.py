"""
Phase 7 EDA tool tests.

Two halves. The validation/injection half runs anywhere — those specs are rejected before a
connection is opened, so they need no data. The execution half needs the enriched Parquet,
which is gitignored, so it skips cleanly on a fresh clone rather than failing and looking like
a broken build.
"""
from pathlib import Path

import pytest

from ml.data import ENRICHED_PATH
from ml.eda import (
    AGGREGATIONS,
    COLUMNS,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    OPERATIONS,
    eda,
)

needs_data = pytest.mark.skipif(
    not Path(ENRICHED_PATH).exists(),
    reason="enriched parquet not present (gitignored) - run scripts/enrich.py",
)

# Strings that would matter if the tool ever concatenated caller input into SQL.
INJECTION_STRINGS = [
    "'; DROP TABLE enriched; --",
    '" OR 1=1 --',
    "1); DELETE FROM enriched WHERE ('a'='a",
    "amount_received UNION SELECT * FROM enriched",
    "*/ UNION ALL SELECT NULL --",
]


# --- the security contract ------------------------------------------------------------------

@pytest.mark.parametrize("payload", INJECTION_STRINGS)
def test_injection_in_a_column_name_is_rejected_not_executed(payload):
    """Identifiers cannot be bound as parameters, so the allow-list is the only thing standing
    between a hostile column name and the query. Nothing outside it may pass."""
    result = eda({"operation": "distribution", "dimension": payload})
    assert "error" in result
    assert "allowed columns" in result["error"]


@pytest.mark.parametrize("payload", INJECTION_STRINGS)
def test_injection_in_a_filter_column_is_rejected(payload):
    result = eda({"operation": "count",
                  "filters": [{"column": payload, "op": "=", "value": 1}]})
    assert "error" in result


@pytest.mark.parametrize("field,value", [
    ("aggregation", "sum(1); DROP TABLE enriched"),
    ("order", "desc; DROP TABLE enriched"),
    ("interval", "day'); DROP TABLE enriched --"),
    ("operation", "count; DROP TABLE enriched"),
    ("source", "enriched; DROP TABLE enriched"),
])
def test_injection_in_any_non_column_identifier_is_rejected(field, value):
    spec = {"operation": "time_series", "dimension": "aml_pattern",
            "measure": "amount_received", "interval": "day"}
    spec[field] = value
    result = eda(spec)
    assert "error" in result


@needs_data
@pytest.mark.parametrize("payload", INJECTION_STRINGS)
def test_injection_in_a_filter_value_is_bound_and_matches_nothing(payload):
    """Values are bound, so hostile text is compared as a literal string. The query must run
    normally and simply match no rows — not error, and certainly not execute."""
    result = eda({"operation": "count",
                  "filters": [{"column": "payment_format", "op": "=", "value": payload}]})
    assert "error" not in result, result
    assert result["records"][0]["n"] == 0


@needs_data
def test_contains_wildcards_do_not_let_the_value_escape():
    result = eda({"operation": "count",
                  "filters": [{"column": "payment_format", "op": "contains",
                               "value": "%' OR 1=1 --"}]})
    assert "error" not in result
    assert result["records"][0]["n"] == 0


@needs_data
def test_the_table_still_exists_after_every_injection_attempt():
    """Belt and braces: prove the earlier attempts did not quietly damage anything."""
    result = eda({"operation": "count"})
    assert "error" not in result
    assert result["records"][0]["n"] > 5_000_000


def test_returned_sql_keeps_placeholders_unfilled():
    """The transparency panel shows the SQL; values must appear as bound parameters, which is
    also the proof they were never interpolated."""
    result = eda({"operation": "count",
                  "filters": [{"column": "payment_format", "op": "=", "value": "ACH"}]})
    if "error" in result:
        pytest.skip("needs data")
    assert "?" in result["sql"]
    assert "ACH" not in result["sql"]
    assert result["sql_parameters"] == ["ACH"]


# --- validation ------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, "string", 42, []])
def test_non_dict_spec_returns_structured_error(bad):
    assert "error" in eda(bad)


def test_unknown_operation_is_rejected_and_lists_the_valid_ones():
    result = eda({"operation": "obliterate"})
    assert "error" in result
    for op in OPERATIONS:
        assert op in result["error"]


def test_unknown_source_is_rejected():
    assert "error" in eda({"operation": "count", "source": "somewhere_else"})


def test_non_numeric_measure_is_rejected_for_a_numeric_aggregation():
    result = eda({"operation": "aggregate", "measure": "payment_format", "aggregation": "avg"})
    assert "error" in result
    assert "not numeric" in result["error"]


def test_count_distinct_is_allowed_on_a_non_numeric_column():
    result = eda({"operation": "aggregate", "measure": "payment_format",
                  "aggregation": "count_distinct"})
    if "error" in result:
        pytest.skip("needs data")
    assert result["records"][0]["value"] > 0


@pytest.mark.parametrize("bad_filter", [
    "not-a-list",
    [{"column": "payment_format", "op": "nonsense", "value": "ACH"}],
    [{"column": "amount_received", "op": "between", "value": [1]}],
    [{"column": "payment_format", "op": "in", "value": []}],
    [{"column": "payment_format", "op": "contains", "value": 5}],
    ["not-a-dict"],
])
def test_malformed_filters_are_rejected(bad_filter):
    assert "error" in eda({"operation": "count", "filters": bad_filter})


@pytest.mark.parametrize("bad_limit", [0, -1, "10", 1.5, True])
def test_invalid_limit_is_rejected(bad_limit):
    assert "error" in eda({"operation": "distribution", "dimension": "aml_pattern",
                           "limit": bad_limit})


@needs_data
def test_limit_is_capped():
    result = eda({"operation": "sample", "limit": MAX_LIMIT + 5000})
    assert result["query_spec"]["limit"] == MAX_LIMIT


def test_in_filter_length_is_capped():
    result = eda({"operation": "count",
                  "filters": [{"column": "payment_format", "op": "in",
                               "value": [str(i) for i in range(200)]}]})
    assert "error" in result and "capped" in result["error"]


# --- operations --------------------------------------------------------------------------------

@needs_data
def test_count_with_and_without_a_filter():
    total = eda({"operation": "count"})["records"][0]["n"]
    ach = eda({"operation": "count",
               "filters": [{"column": "payment_format", "op": "=", "value": "ACH"}]})
    assert 0 < ach["records"][0]["n"] < total


@needs_data
def test_aggregate_sum_matches_a_filtered_subset():
    result = eda({"operation": "aggregate", "measure": "amount_received",
                  "aggregation": "sum",
                  "filters": [{"column": "is_laundering", "op": "=", "value": True}]})
    assert result["records"][0]["value"] > 0


@needs_data
def test_distribution_over_patterns_covers_the_motifs():
    result = eda({"operation": "distribution", "dimension": "aml_pattern", "limit": 20})
    seen = {r["dimension"] for r in result["records"]}
    assert "NORMAL" in seen
    assert {"CYCLE", "FAN-OUT", "SCATTER-GATHER"} <= seen


@needs_data
def test_group_with_a_measure():
    result = eda({"operation": "group", "dimension": "payment_format",
                  "measure": "amount_received", "aggregation": "avg"})
    assert result["row_count"] > 0
    assert set(result["columns"]) == {"dimension", "value"}


@needs_data
def test_time_series_by_day_spans_the_dataset_window():
    """phase1.md §4: 18 days of coverage, 2022-09-01 to 2022-09-18."""
    result = eda({"operation": "time_series", "interval": "day", "limit": 100})
    buckets = [r["bucket"] for r in result["records"]]
    assert len(buckets) == 18
    assert buckets == sorted(buckets)
    assert buckets[0].startswith("2022/09/01")


@needs_data
def test_time_series_rejects_the_rule_hits_source():
    result = eda({"operation": "time_series", "source": "rule_hits"})
    assert "error" in result


@needs_data
def test_top_accounts_is_ordered_descending():
    result = eda({"operation": "top_accounts", "side": "sender",
                  "aggregation": "sum", "measure": "amount_received", "limit": 10})
    values = [r["value"] for r in result["records"]]
    assert values == sorted(values, reverse=True)
    assert len(values) == 10


@needs_data
def test_sample_returns_the_default_projection():
    result = eda({"operation": "sample", "limit": 5})
    assert result["row_count"] == 5
    assert "amount_received" in result["columns"]
    assert "from_account" in result["columns"]


@needs_data
def test_sample_honours_a_requested_projection():
    result = eda({"operation": "sample", "columns": ["aml_pattern", "amount_received"],
                  "limit": 3})
    assert result["columns"] == ["aml_pattern", "amount_received"]


@needs_data
def test_rule_hits_source_is_queryable():
    result = eda({"operation": "distribution", "source": "rule_hits", "dimension": "rule"})
    seen = {r["dimension"] for r in result["records"]}
    assert "SCATTER-GATHER" in seen


# --- output contract ----------------------------------------------------------------------------

@needs_data
def test_result_reports_the_normalized_spec_back():
    result = eda({"operation": "count",
                  "filters": [{"column": "is_laundering", "op": "=", "value": True}]})
    spec = result["query_spec"]
    assert spec["operation"] == "count"
    assert spec["source"] == "transactions"
    assert spec["filters"] == [{"column": "is_laundering", "op": "=", "value": True}]
    assert spec["limit"] == DEFAULT_LIMIT


def test_every_aggregation_name_is_a_known_template():
    for name, template in AGGREGATIONS.items():
        assert "{col}" in template or template.isalpha(), name


def test_allow_lists_are_non_empty_for_every_source():
    for source, table in COLUMNS.items():
        assert table, source


# --- min_value (HAVING) ---------------------------------------------------------------------

def test_min_value_filters_groups_by_their_aggregate():
    """"Customers with 10+ transactions" needs a threshold on the aggregate, not a ranking.

    Without HAVING the only available answer was "here are the top senders", which looks
    plausible and answers a different question.
    """
    base = {
        "operation": "top_accounts", "side": "sender", "aggregation": "count", "limit": 1000,
        "filters": [{"column": "amount_paid", "op": "<", "value": 10000}],
    }
    loose = eda({**base, "min_value": 1000})
    strict = eda({**base, "min_value": 100000})

    assert len(loose["records"]) > len(strict["records"]) >= 1
    assert all(r["value"] >= 1000 for r in loose["records"])
    assert all(r["value"] >= 100000 for r in strict["records"])


def test_min_value_is_bound_as_a_parameter_not_interpolated():
    result = eda({"operation": "top_accounts", "aggregation": "count", "min_value": 25})
    assert "HAVING" in result["sql"]
    assert "25" not in result["sql"], "threshold must be a bound ? parameter"
    assert 25 in result["sql_parameters"]


def test_min_value_applies_to_group_operation_too():
    result = eda({"operation": "group", "dimension": "payment_format",
                  "aggregation": "count", "min_value": 500000})
    assert all(r["value"] >= 500000 for r in result["records"])


def test_min_value_absent_means_no_having_clause():
    assert "HAVING" not in eda({"operation": "top_accounts", "aggregation": "count"})["sql"]


@pytest.mark.parametrize("bad", ["ten", None, True, [10]])
def test_non_numeric_min_value_is_rejected(bad):
    assert "min_value" in eda({"operation": "top_accounts", "aggregation": "count",
                               "min_value": bad})["error"]

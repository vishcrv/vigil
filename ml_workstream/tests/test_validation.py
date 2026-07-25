"""
Risk-level validation tests.

Two separate concerns. The cross-tab arithmetic — weighting, precision, recall, error handling
— is tested on synthetic records with no data dependency, because that is where a silent
mistake would misreport how good the product is. Then one end-to-end test runs a small labelled
sample through the real `dispatch` surface, to lock in that the pipeline can be validated at
all; it asserts the shape and the invariants, not specific precision numbers, which move
whenever the constants are retuned.
"""
import json
from pathlib import Path

import pytest

from ml.data import ENRICHED_PATH
from ml.validation import (
    MIN_CELL_FOR_STABLE_ESTIMATE,
    RISK_ORDER,
    attach_bootstrap_ci,
    bootstrap_precision_ci,
    crosstab,
    format_markdown,
    recall_at_or_above,
    wilson_interval,
    wilson_lower_bound,
)


def record(level, positive, stratum="clean", **extra):
    return {"risk_level": level, "is_laundering": positive, "stratum": stratum, **extra}


# --- cross-tab arithmetic -------------------------------------------------------------------

def test_unweighted_crosstab_counts_each_observation_once():
    table = crosstab([
        record("CRITICAL", True), record("CRITICAL", True), record("CRITICAL", False),
        record("LOW", False), record("LOW", False),
    ])
    critical = table["levels"]["CRITICAL"]
    assert critical["sampled_n"] == 3
    assert critical["sampled_positive"] == 2
    assert critical["precision"] == pytest.approx(2 / 3, abs=1e-5)   # stored rounded to 5dp
    assert table["levels"]["LOW"]["precision"] == 0.0


def test_every_level_appears_even_when_empty():
    table = crosstab([record("LOW", False)])
    assert set(table["levels"]) == set(RISK_ORDER)
    assert table["levels"]["CRITICAL"]["sampled_n"] == 0
    assert table["levels"]["CRITICAL"]["precision"] is None


def test_stratum_weights_correct_for_oversampled_positives():
    """The reason weighting exists: the laundering stratum is deliberately over-sampled, so
    raw sampled proportions would overstate precision by an order of magnitude."""
    records = [record("CRITICAL", True, "laundering")] + [
        record("CRITICAL", False, "clean")
    ]
    weights = {"laundering": 10.0, "clean": 400.0}

    raw = crosstab(records)["levels"]["CRITICAL"]["precision"]
    weighted = crosstab(records, weights)["levels"]["CRITICAL"]["precision"]

    assert raw == pytest.approx(0.5)
    assert weighted == pytest.approx(10.0 / 410.0, abs=1e-5)
    assert weighted < raw


def test_estimated_population_counts_scale_by_weight():
    table = crosstab(
        [record("HIGH", True, "laundering"), record("HIGH", False, "clean")],
        {"laundering": 10.0, "clean": 400.0},
    )
    assert table["levels"]["HIGH"]["estimated_population_n"] == pytest.approx(410.0)
    assert table["levels"]["HIGH"]["estimated_positive"] == pytest.approx(10.0)


def test_errors_are_excluded_not_counted_as_clean():
    """An account that failed to score is not evidence of anything. Folding it into LOW would
    report unexamined accounts as clean - the failure mode this workstream has already had to
    fix twice."""
    table = crosstab([
        record("LOW", False),
        {"error": "models not found", "is_laundering": True, "stratum": "laundering"},
    ])
    assert table["errors"] == 1
    assert table["scored_accounts"] == 1
    assert table["levels"]["LOW"]["sampled_n"] == 1


def test_unexpected_level_is_reported_rather_than_silently_dropped():
    table = crosstab([record("SEVERE", True), record("LOW", False)])
    assert table["unknown_levels"] == {"SEVERE": 1}
    assert table["scored_accounts"] == 1


def test_thin_cells_are_flagged_as_noisy():
    thin = crosstab([record("CRITICAL", True)])
    assert thin["levels"]["CRITICAL"]["noisy"] is True

    thick = crosstab([record("CRITICAL", True)] * (MIN_CELL_FOR_STABLE_ESTIMATE + 1))
    assert thick["levels"]["CRITICAL"]["noisy"] is False


def test_base_rate_and_lift_are_consistent():
    records = [record("CRITICAL", True)] + [record("LOW", False)] * 9
    table = crosstab(records)
    assert table["base_rate"] == pytest.approx(0.1)
    assert table["levels"]["CRITICAL"]["lift"] == pytest.approx(10.0)
    assert table["levels"]["LOW"]["lift"] == pytest.approx(0.0)


def test_recall_sums_to_one_across_levels():
    records = [record("CRITICAL", True), record("HIGH", True), record("LOW", True),
               record("LOW", False)]
    table = crosstab(records)
    assert sum(table["levels"][lvl]["recall"] for lvl in RISK_ORDER) == pytest.approx(
        1.0, abs=1e-4
    )


def test_recall_at_or_above_accumulates_upward():
    records = [record("CRITICAL", True), record("HIGH", True), record("MEDIUM", True),
               record("LOW", True)]
    table = crosstab(records)
    assert recall_at_or_above(table, "CRITICAL") == pytest.approx(0.25, abs=1e-5)
    assert recall_at_or_above(table, "HIGH") == pytest.approx(0.50, abs=1e-5)
    assert recall_at_or_above(table, "LOW") == pytest.approx(1.0, abs=1e-4)


def test_recall_at_or_above_rejects_an_unknown_level():
    with pytest.raises(ValueError):
        recall_at_or_above(crosstab([record("LOW", False)]), "SEVERE")


def test_empty_input_does_not_divide_by_zero():
    table = crosstab([])
    assert table["base_rate"] is None
    assert all(table["levels"][lvl]["precision"] is None for lvl in RISK_ORDER)


# --- intervals --------------------------------------------------------------------------------

def test_wilson_interval_brackets_the_point_estimate():
    low, high = wilson_interval(100, 0.5)
    assert low < 0.5 < high
    assert 0.0 <= low and high <= 1.0


def test_wilson_interval_widens_as_the_sample_shrinks():
    narrow = wilson_interval(1000, 0.5)
    wide = wilson_interval(10, 0.5)
    assert (wide[1] - wide[0]) > (narrow[1] - narrow[0])


def test_wilson_lower_bound_matches_the_interval():
    assert wilson_lower_bound(50, 0.3) == pytest.approx(wilson_interval(50, 0.3)[0])


def test_bootstrap_ci_brackets_the_point_estimate():
    records = ([record("CRITICAL", True, "laundering")] * 30
               + [record("LOW", False, "clean")] * 70)
    weights = {"laundering": 2.0, "clean": 5.0}
    table = crosstab(records, weights)
    intervals = bootstrap_precision_ci(records, weights, iterations=100, seed=1)
    low, high = intervals["CRITICAL"]
    assert low <= table["levels"]["CRITICAL"]["precision"] <= high


def test_bootstrap_covers_every_level_in_one_pass():
    records = [record("CRITICAL", True, "laundering"), record("LOW", False, "clean")]
    intervals = bootstrap_precision_ci(records, {"laundering": 1.0, "clean": 1.0},
                                       iterations=20, seed=1)
    assert set(intervals) == set(RISK_ORDER)


def test_bootstrap_is_deterministic_for_a_given_seed():
    records = ([record("HIGH", True, "laundering")] * 10
               + [record("LOW", False, "clean")] * 10)
    weights = {"laundering": 3.0, "clean": 9.0}
    first = bootstrap_precision_ci(records, weights, iterations=50, seed=7)
    second = bootstrap_precision_ci(records, weights, iterations=50, seed=7)
    assert first == second


def test_attach_bootstrap_ci_fills_the_table():
    records = [record("CRITICAL", True, "laundering"), record("LOW", False, "clean")]
    weights = {"laundering": 1.0, "clean": 1.0}
    table = attach_bootstrap_ci(crosstab(records, weights), records, weights, iterations=20)
    assert table["levels"]["CRITICAL"]["precision_ci95"] is not None


def test_bootstrap_handles_an_all_error_sample():
    intervals = bootstrap_precision_ci([{"error": "boom", "stratum": "clean"}],
                                       {"clean": 1.0}, iterations=5)
    assert all(v is None for v in intervals.values())


# --- rendering ----------------------------------------------------------------------------------

def test_markdown_table_has_a_row_per_level_and_is_ascii():
    table = attach_bootstrap_ci(
        crosstab([record("CRITICAL", True), record("LOW", False)]),
        [record("CRITICAL", True), record("LOW", False)],
        {},
        iterations=10,
    )
    rendered = format_markdown(table)
    rendered.encode("ascii")   # printed to a cp1252 Windows console
    for level in RISK_ORDER:
        assert f"| {level} |" in rendered


def test_markdown_marks_noisy_cells():
    table = crosstab([record("CRITICAL", True)])
    assert "(noisy)" in format_markdown(table)


# --- end to end -----------------------------------------------------------------------------------

@pytest.mark.skipif(
    not Path(ENRICHED_PATH).exists()
    or not (Path(ENRICHED_PATH).parent / "models" / "metadata.json").exists(),
    reason="enriched Parquet or trained models missing",
)
def test_pipeline_can_be_validated_end_to_end():
    """Small labelled sample straight through dispatch(anomaly) -> dispatch(risk).

    Deliberately asserts no precision figure: those move whenever the blend constants are
    retuned. What must not break is that a labelled sample can be pushed through the real tool
    surface and cross-tabulated at all.
    """
    from scripts.validate_risk_levels import collect

    records, context = collect(n_launderers=6, n_clean=6, seed=3, quiet=True)
    assert len(records) == 12
    assert {r["stratum"] for r in records} == {"laundering", "clean"}

    table = crosstab(records, context["weights"])
    assert table["errors"] == 0
    assert table["scored_accounts"] == 12
    assert not table["unknown_levels"]
    assert sum(
        table["levels"][lvl]["recall"] or 0.0 for lvl in RISK_ORDER
    ) == pytest.approx(1.0, abs=1e-4)
    json.dumps(table)


# --- zero-cell handling -----------------------------------------------------------------

def test_empty_stratum_cell_gets_a_conservative_precision_bound():
    """The CRITICAL cell in the real run had 25 launderers and zero clean accounts, so both
    the point estimate and the bootstrap said exactly 100%. That is an empty cell, not
    certainty - the rule-of-three bound is the honest statement."""
    records = ([record("CRITICAL", True, "laundering")] * 25
               + [record("LOW", False, "clean")] * 1400)
    weights = {"laundering": 10.6, "clean": 363.4}
    table = crosstab(records, weights)

    critical = table["levels"]["CRITICAL"]
    assert critical["precision"] == 1.0
    assert critical["unobserved_strata"] == ["clean"]
    # 25*10.6 positives against up to 3*363.4 unobserved clean accounts.
    assert critical["precision_conservative"] == pytest.approx(265 / (265 + 1090.2), abs=1e-3)
    assert critical["precision_conservative"] < 0.25


def test_conservative_bound_equals_precision_when_every_stratum_is_represented():
    records = [record("HIGH", True, "laundering"), record("HIGH", False, "clean")]
    weights = {"laundering": 10.0, "clean": 400.0}
    high = crosstab(records, weights)["levels"]["HIGH"]
    assert high["unobserved_strata"] == []
    assert high["precision_conservative"] == pytest.approx(high["precision"])


def test_markdown_marks_a_degenerate_interval_as_an_empty_cell():
    records = ([record("CRITICAL", True, "laundering")] * 25
               + [record("LOW", False, "clean")] * 100)
    weights = {"laundering": 10.6, "clean": 363.4}
    table = attach_bootstrap_ci(crosstab(records, weights), records, weights, iterations=50)
    assert "(empty cell)" in format_markdown(table)


def test_rule_of_three_needs_a_nonempty_stratum():
    from ml.validation import rule_of_three_upper_count

    assert rule_of_three_upper_count(0) == 0.0
    assert rule_of_three_upper_count(1400) == 3.0

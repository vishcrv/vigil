"""
Phase 5 risk-classification tests, per ml_spec.md "Testing": known anomaly-score + rule-hit
combinations mapped to expected risk_level / escalation_action.

The cases that matter most are the boundaries the weighting exists to enforce — a weak rule
must not reach the same level as a strong one, and the detectors must not be able to reach
CRITICAL on their own.
"""
import pytest

from ml.risk import (
    DETECTOR_WEIGHT,
    NO_PATTERN,
    ESCALATION_ACTIONS,
    RULE_BASE_CREDIT,
    RISK_LEVELS,
    RISK_THRESHOLDS,
    RULE_STATS,
    RULE_WEIGHTS,
    risk,
)


def make_anomaly(anomaly_score=0.0, rules=(), rows=1, top_rows=None, scope=None) -> dict:
    """Minimal Phase 4-shaped result. `rules` is an iterable of (name, confidence)."""
    return {
        "scope": scope or {"account_id": "ACC1"},
        "method": "all",
        "row_count_scored": rows,
        "anomaly_score": anomaly_score,
        "mean_anomaly_score": anomaly_score,
        "method_scores": {},
        "rule_hits": [
            {"account": "ACC1", "rule": name, "score": conf, "evidence": {}}
            for name, conf in rules
        ],
        "rule_names": sorted({name for name, _ in rules}),
        "top_rows": top_rows if top_rows is not None else [_row()],
    }


def _row(**overrides) -> dict:
    row = {
        "Timestamp": "2022/09/10 18:21",
        "From Account": "ACC1",
        "To Account": "ACC2",
        "Amount Received": 1000.0,
        "Payment Format": "ACH",
        "aml_pattern": "NORMAL",
        "is_laundering": False,
        "is_suspicious": False,
        "is_self_loop": False,
        "payment_format_risk": 0.0075,
        "anomaly_score": 0.5,
        "method_scores": {},
    }
    row.update(overrides)
    return row


# --- level boundaries ---------------------------------------------------------------------

@pytest.mark.parametrize("rule,confidence,expected", [
    ("SCATTER-GATHER", 1.0, "CRITICAL"),   # 100%-precision rule at full confidence
    ("GATHER-SCATTER", 1.0, "CRITICAL"),   # ~0.8 weight clears the CRITICAL floor
    ("SCATTER-GATHER", 0.0, "HIGH"),       # same rule barely over threshold -> base credit only
    # RANDOM's 8.7% precision was measured on 12 accounts; once shrunk for that sample size
    # it is a hint, not grounds for escalating on its own.
    ("RANDOM", 1.0, "LOW"),
    ("FAN-IN", 1.0, "LOW"),                # near-chance rule must not escalate on its own
])
def test_rule_alone_maps_to_expected_level(rule, confidence, expected):
    result = risk(make_anomaly(anomaly_score=0.0, rules=[(rule, confidence)]))
    assert result["risk_level"] == expected
    assert result["pattern_detected"] == rule


def test_detector_alone_cannot_reach_critical():
    """A perfect detector score with no rule hit tops out at DETECTOR_WEIGHT by construction —
    the detectors measured 2.3x lift against rules reaching 133x, so they may raise a score
    but never carry one alone.

    Asserts the ceiling, not the level it lands on: which tier DETECTOR_WEIGHT falls into is a
    function of the tuned thresholds and moved from MEDIUM to HIGH when they were retuned. The
    invariant that matters is that it is never CRITICAL.
    """
    result = risk(make_anomaly(anomaly_score=1.0))
    assert result["risk_score"] == pytest.approx(DETECTOR_WEIGHT)
    assert result["risk_level"] != "CRITICAL"
    assert DETECTOR_WEIGHT < dict(RISK_THRESHOLDS)["CRITICAL"]
    assert result["pattern_detected"] == NO_PATTERN


def test_no_signal_is_low_and_monitor():
    result = risk(make_anomaly(anomaly_score=0.0))
    assert result["risk_level"] == "LOW"
    assert result["escalation_action"] == "MONITOR"


def test_detector_raises_level_when_combined_with_a_rule():
    without = risk(make_anomaly(anomaly_score=0.0, rules=[("FAN-OUT", 0.5)]))
    with_detector = risk(make_anomaly(anomaly_score=1.0, rules=[("FAN-OUT", 0.5)]))
    assert with_detector["risk_score"] > without["risk_score"]


# --- weighting invariants -----------------------------------------------------------------

def test_strongest_rule_wins_rather_than_hits_accumulating():
    """Two low-precision rules firing together must not out-score one high-precision rule;
    summing contributions would let a pile of weak hits manufacture a CRITICAL."""
    two_weak = risk(make_anomaly(rules=[("CYCLE", 1.0), ("STACK", 1.0)]))
    one_strong = risk(make_anomaly(rules=[("SCATTER-GATHER", 0.0)]))
    assert two_weak["risk_score"] < one_strong["risk_score"]
    assert two_weak["risk_score"] == pytest.approx(
        max(RULE_WEIGHTS["CYCLE"], RULE_WEIGHTS["STACK"])
    )


def test_pattern_detected_reports_the_highest_weighted_rule():
    result = risk(make_anomaly(rules=[("FAN-IN", 1.0), ("SCATTER-GATHER", 0.5), ("STACK", 1.0)]))
    assert result["pattern_detected"] == "SCATTER-GATHER"
    assert [r["rule"] for r in result["contributing_signals"]["rules_fired"]][0] == "SCATTER-GATHER"


def test_pattern_detected_never_echoes_the_ground_truth_label():
    """`aml_pattern` on a row is pattern-file ground truth. pattern_detected must come from
    the rule engine only — echoing the label would leak it into the agent's own output."""
    rows = [_row(aml_pattern="CYCLE", is_suspicious=True, is_laundering=True)]
    result = risk(make_anomaly(anomaly_score=0.9, rules=(), top_rows=rows))
    assert result["pattern_detected"] == NO_PATTERN


def test_unknown_rule_names_are_ignored_not_crashed_on():
    result = risk(make_anomaly(rules=[("NOT-A-REAL-MOTIF", 1.0)]))
    assert result["risk_level"] == "LOW"
    assert result["pattern_detected"] == NO_PATTERN


# --- benign-profile correction (phase4.md §7) ---------------------------------------------

def test_self_loop_zero_risk_format_rows_damp_the_detector():
    """Damping must pull the score down and, at full benign fraction, down a whole tier.

    The absolute tier is not asserted: it depends on the tuned thresholds (this case was
    LOW under the old floors and is MEDIUM under the current ones). What must hold is that
    damping costs the account a level relative to the same score undamped.
    """
    benign = [_row(is_self_loop=True, payment_format_risk=0.0) for _ in range(4)]
    damped = risk(make_anomaly(anomaly_score=1.0, top_rows=benign))
    normal = risk(make_anomaly(anomaly_score=1.0))
    assert damped["risk_score"] < normal["risk_score"]
    assert damped["contributing_signals"]["benign_profile_fraction"] == 1.0
    assert RISK_LEVELS.index(damped["risk_level"]) > RISK_LEVELS.index(normal["risk_level"])


def test_self_loop_in_a_risky_format_is_not_damped():
    rows = [_row(is_self_loop=True, payment_format_risk=0.0075)]
    result = risk(make_anomaly(anomaly_score=1.0, top_rows=rows))
    assert result["contributing_signals"]["benign_profile_fraction"] == 0.0


def test_damping_does_not_suppress_a_rule_hit():
    """The correction targets detector false positives on routine self-transfers; a fired
    rule is separate evidence and must survive it."""
    benign = [_row(is_self_loop=True, payment_format_risk=0.0)]
    result = risk(make_anomaly(anomaly_score=1.0, rules=[("SCATTER-GATHER", 1.0)],
                               top_rows=benign))
    assert result["risk_level"] == "CRITICAL"


# --- flags-row contract -------------------------------------------------------------------

FLAGS_FIELDS = [
    "risk_level", "pattern_detected", "anomaly_score",
    "escalation_action", "customer_id", "transaction_id",
]


def test_output_carries_every_flags_column():
    result = risk(make_anomaly(anomaly_score=0.5, rules=[("FAN-OUT", 0.5)]))
    for field in FLAGS_FIELDS:
        assert field in result, field


@pytest.mark.parametrize("level", RISK_LEVELS)
def test_every_risk_level_has_an_escalation_action(level):
    assert ESCALATION_ACTIONS[level] in ("MONITOR", "REVIEW", "REPORT")


def test_escalation_action_matches_the_assigned_level():
    result = risk(make_anomaly(rules=[("SCATTER-GATHER", 1.0)]))
    assert result["risk_level"] == "CRITICAL"
    assert result["escalation_action"] == "REPORT"


def test_transaction_id_is_stable_across_calls():
    a = risk(make_anomaly(anomaly_score=0.5))
    b = risk(make_anomaly(anomaly_score=0.5))
    assert a["transaction_id"] == b["transaction_id"]
    assert a["transaction_id"].startswith("tx_")


def test_transaction_id_changes_with_the_transaction():
    a = risk(make_anomaly(anomaly_score=0.5))
    b = risk(make_anomaly(anomaly_score=0.5, top_rows=[_row(**{"Amount Received": 2000.0})]))
    assert a["transaction_id"] != b["transaction_id"]


def test_customer_id_falls_back_from_context_to_scope():
    from_scope = risk(make_anomaly(scope={"account_id": "FROM_SCOPE"}))
    assert from_scope["customer_id"] == "FROM_SCOPE"
    from_context = risk(make_anomaly(scope={"account_id": "FROM_SCOPE"}),
                        {"customer_id": "FROM_CONTEXT"})
    assert from_context["customer_id"] == "FROM_CONTEXT"


# --- error handling -----------------------------------------------------------------------

def test_upstream_error_propagates_instead_of_scoring_low():
    """A failed detection must not be reported as LOW risk — that reads as 'checked, fine'
    when nothing was checked."""
    result = risk({"error": "models not found", "scope": {}})
    assert "error" in result
    assert result["risk_level"] is None
    assert result["escalation_action"] is None


@pytest.mark.parametrize("bad", [None, "string", 42, []])
def test_non_dict_input_returns_structured_error(bad):
    result = risk(bad)
    assert "error" in result


def test_bad_context_returns_structured_error():
    assert "error" in risk(make_anomaly(), context="not-a-dict")


def test_empty_scope_match_is_low_risk_with_a_note():
    result = risk(make_anomaly(rows=0, top_rows=[]))
    assert result["risk_level"] == "LOW"
    assert result["escalation_action"] == "MONITOR"
    assert "note" in result["contributing_signals"]


def test_malformed_rule_hits_do_not_raise():
    payload = make_anomaly()
    payload["rule_hits"] = [None, "junk", {"rule": "FAN-OUT"}, {"rule": "STACK", "score": "x"}]
    result = risk(payload)
    assert result["risk_level"] in RISK_LEVELS


@pytest.mark.parametrize("score", [-5.0, 0.0, 0.5, 1.0, 99.0])
def test_risk_score_stays_within_unit_range(score):
    result = risk(make_anomaly(anomaly_score=score, rules=[("SCATTER-GATHER", 1.0)]))
    assert 0.0 <= result["risk_score"] <= 1.0


# --- scope-aware rule attribution -----------------------------------------------------------
# anomaly() returns rule hits for every account in the scoped rows, counterparties included.
# Attributing one of those to the subject writes a factually wrong flags row.

def _mixed_hits(subject="ACC1", other="OTHER9"):
    payload = make_anomaly(anomaly_score=0.0, scope={"account_id": subject})
    payload["rule_hits"] = [
        {"account": other, "rule": "SCATTER-GATHER", "score": 1.0, "evidence": {}},
        {"account": subject, "rule": "FAN-IN", "score": 1.0, "evidence": {}},
    ]
    return payload


def test_counterparty_rule_is_not_attributed_to_the_subject():
    result = risk(_mixed_hits())
    assert result["pattern_detected"] == "FAN-IN"
    assert result["risk_level"] == "LOW"


def test_counterparty_rule_does_not_inflate_the_score():
    subject_only = make_anomaly(anomaly_score=0.0, rules=[("FAN-IN", 1.0)])
    assert risk(_mixed_hits())["risk_score"] == pytest.approx(risk(subject_only)["risk_score"])


def test_counterparty_rules_are_still_reported_as_context():
    signals = risk(_mixed_hits())["contributing_signals"]
    assert [h["rule"] for h in signals["counterparty_rules"]] == ["SCATTER-GATHER"]
    assert signals["counterparty_rules"][0]["account"] == "OTHER9"


def test_subject_with_no_hits_of_its_own_gets_no_pattern():
    payload = make_anomaly(anomaly_score=0.0, scope={"account_id": "ACC1"})
    payload["rule_hits"] = [
        {"account": "OTHER9", "rule": "SCATTER-GATHER", "score": 1.0, "evidence": {}},
    ]
    result = risk(payload)
    assert result["pattern_detected"] == NO_PATTERN   # no motif, not a null
    assert result["contributing_signals"]["rule_component"] == 0.0


def test_scopeless_query_still_uses_every_hit():
    """With no account in scope there is no subject to attribute against, so all hits count."""
    payload = make_anomaly(anomaly_score=0.0, scope={"date_range": ["a", "b"]})
    payload["rule_hits"] = [
        {"account": "ANY1", "rule": "SCATTER-GATHER", "score": 1.0, "evidence": {}},
    ]
    assert risk(payload)["pattern_detected"] == "SCATTER-GATHER"


# --- weight derivation --------------------------------------------------------------------

def test_every_rule_has_measured_stats_behind_its_weight():
    """No hand-picked weights: each one must trace to a (hits, precision) pair from
    phase4.md §3, so a judge asking "why 0.8?" gets a measurement, not a preference."""
    assert set(RULE_WEIGHTS) == set(RULE_STATS)
    for hits, precision in RULE_STATS.values():
        assert hits > 0
        assert 0.0 <= precision <= 1.0


def test_weights_are_bounded_and_topped_by_the_best_evidenced_rule():
    assert all(0.0 <= w <= 1.0 for w in RULE_WEIGHTS.values())
    assert RULE_WEIGHTS["SCATTER-GATHER"] == 1.0


def test_thin_evidence_is_shrunk_below_well_measured_evidence():
    """BIPARTITE's raw precision (6.7%) beats FAN-OUT's (5.0%), but rests on 15 accounts
    against 1,736 — after shrinkage the better-measured rule must rank higher."""
    assert RULE_STATS["BIPARTITE"][1] > RULE_STATS["FAN-OUT"][1]
    assert RULE_WEIGHTS["BIPARTITE"] < RULE_WEIGHTS["FAN-OUT"]


def test_near_chance_rule_is_effectively_zero():
    assert RULE_WEIGHTS["FAN-IN"] < 0.05


def test_wilson_bound_shrinks_harder_on_smaller_samples():
    from ml.risk import _wilson_lower_bound

    assert _wilson_lower_bound(10, 0.5) < _wilson_lower_bound(1000, 0.5)
    assert _wilson_lower_bound(0, 1.0) == 0.0
    assert _wilson_lower_bound(1000, 0.5) < 0.5


# --- blend parameterization ---------------------------------------------------------------

def test_default_blend_matches_the_module_constants():
    """DEFAULT_BLEND is what risk() uses when no blend is passed; if it drifts from the
    constants, the tuning sweep would be optimizing something the product does not run."""
    from ml.risk import BENIGN_DETECTOR_DAMPING, DEFAULT_BLEND

    assert DEFAULT_BLEND["detector_weight"] == DETECTOR_WEIGHT
    assert DEFAULT_BLEND["rule_base_credit"] == RULE_BASE_CREDIT
    assert DEFAULT_BLEND["benign_damping"] == BENIGN_DETECTOR_DAMPING
    assert DEFAULT_BLEND["thresholds"] == RISK_THRESHOLDS


def test_passing_no_blend_is_identical_to_passing_the_default():
    from ml.risk import DEFAULT_BLEND

    payload = make_anomaly(anomaly_score=0.8, rules=[("FAN-OUT", 0.5)])
    assert risk(payload) == risk(payload, blend=DEFAULT_BLEND)


def test_blend_overrides_are_partial():
    """A sweep varies one knob at a time; unspecified keys must keep their defaults rather
    than becoming None."""
    payload = make_anomaly(anomaly_score=1.0)
    louder = risk(payload, blend={"detector_weight": 0.9})
    assert louder["risk_score"] == pytest.approx(0.9)
    assert louder["pattern_detected"] == NO_PATTERN


def test_custom_thresholds_change_the_assigned_level():
    payload = make_anomaly(anomaly_score=1.0)
    strict = risk(payload, blend={"thresholds": [("CRITICAL", 0.9), ("HIGH", 0.8),
                                                 ("MEDIUM", 0.7), ("LOW", 0.0)]})
    loose = risk(payload, blend={"thresholds": [("CRITICAL", 0.2), ("HIGH", 0.1),
                                                ("MEDIUM", 0.05), ("LOW", 0.0)]})
    assert strict["risk_level"] == "LOW"
    assert loose["risk_level"] == "CRITICAL"


def test_rule_base_credit_sets_the_floor_for_a_zero_confidence_hit():
    payload = make_anomaly(rules=[("SCATTER-GATHER", 0.0)])
    assert risk(payload, blend={"rule_base_credit": 0.25})["risk_score"] == pytest.approx(
        0.25 * RULE_WEIGHTS["SCATTER-GATHER"]
    )


def test_blend_is_not_exposed_on_the_agent_tool_surface():
    """The loop must not be able to retune the risk model mid-query."""
    from ml.tools import TOOL_SCHEMAS, dispatch

    schema = next(s for s in TOOL_SCHEMAS if s["name"] == "risk")
    assert "blend" not in schema["input_schema"]["properties"]
    assert "error" in dispatch("risk", {"anomaly_result": {}, "blend": {}})

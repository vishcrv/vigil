"""
Phase 6 explanation tests.

The load-bearing requirement from `ml_spec.md` Phase 6 is coverage: *every* possible `risk()`
output must reach a template, with no silent "no explanation available" case. Most of what
follows exists to prove that — every motif, every non-motif path, and every degenerate input
must produce usable prose with no unfilled placeholders.
"""
import pytest

from ml.explain import (
    ALL_TEMPLATES,
    FLAG_MENTION_FLOOR,
    MOTIF_TEMPLATES,
    NON_MOTIF_TEMPLATES,
    PURE_ANOMALY_FLOOR,
    explain,
    select_template,
)
from ml.risk import RULE_WEIGHTS

# Evidence shaped exactly as ml/rules.py writes it, so the templates are exercised against the
# real field names rather than an idealised stand-in.
EVIDENCE = {
    "FAN-OUT": {"unique_receivers": 14, "window_hours": 24,
                "window_start": "2022/09/05 08:00", "window_amount": 250000.0},
    "FAN-IN": {"unique_senders": 11, "window_hours": 24,
               "window_start": "2022/09/05 08:00", "window_amount": 180000.0},
    "CYCLE": {"hops": 3, "cycles_found": 2, "out_amount": 100000.0,
              "returned_amount": 95000.0, "retention_pct": 95.0,
              "started_at": "2022/09/05 08:00", "closed_at": "2022/09/06 10:00"},
    "STACK": {"hops": 3, "chains_found": 4, "amount_in": 100000.0, "amount_out": 88000.0,
              "retention_pct": 88.0, "started_at": "2022/09/05 08:00",
              "ended_at": "2022/09/06 10:00"},
    "GATHER-SCATTER": {"unique_senders": 7, "unique_receivers": 9, "amount_in": 500000.0,
                       "amount_out": 480000.0, "passthrough_pct": 96.0,
                       "first_inflow": "2022/09/05 08:00", "last_outflow": "2022/09/08 12:00"},
    "SCATTER-GATHER": {"intermediaries": 16, "amount_in": 10046630.09,
                       "amount_out": 261785298.42, "started_at": "2022/09/10 02:46",
                       "ended_at": "2022/09/13 21:42"},
    "BIPARTITE": {"block_senders": 6, "shared_receivers": 5},
    "RANDOM": {"txn_count": 240, "structured_match": False},
}


def make_risk(rules=(), detector=0.0, adjusted=None, rows=50, flags=None,
              level="MEDIUM", action="REVIEW", score=0.4) -> dict:
    """A Phase 5-shaped result. `rules` is an iterable of rule names."""
    rules = list(rules)
    return {
        "risk_level": level,
        "pattern_detected": rules[0] if rules else None,
        "anomaly_score": detector,
        "escalation_action": action,
        "customer_id": "ACC1",
        "transaction_id": "tx_deadbeefdeadbeef",
        "risk_score": score,
        "contributing_signals": {
            "rule_component": RULE_WEIGHTS.get(rules[0], 0.0) if rules else 0.0,
            "detector_component_raw": detector,
            "detector_component_adjusted": detector if adjusted is None else adjusted,
            "benign_profile_fraction": 0.0,
            "rules_fired": [
                {"rule": r, "account": "ACC1", "weight": RULE_WEIGHTS.get(r, 0.0),
                 "rule_confidence": 0.8, "contribution": 0.5,
                 "evidence": EVIDENCE.get(r, {})}
                for r in rules
            ],
            "rows_scored": rows,
            "row_flags": flags or {"currency_mismatch": 0.0, "elevated_payment_format": 0.0},
        },
    }


def assert_usable(text: str) -> None:
    """Prose must be complete: no unfilled placeholders, no leaked Python repr."""
    assert isinstance(text, str) and len(text) > 40
    for leak in ("{", "}", "None", "nan", "NaN"):
        assert leak not in text, f"{leak!r} leaked into: {text}"


# --- motif coverage -------------------------------------------------------------------------

@pytest.mark.parametrize("motif", sorted(MOTIF_TEMPLATES))
def test_every_motif_has_a_template_naming_its_pattern(motif):
    result = explain(make_risk(rules=[motif]))
    assert result["template"] == motif
    assert motif in result["explanation"]
    assert_usable(result["explanation"])


@pytest.mark.parametrize("motif", sorted(MOTIF_TEMPLATES))
def test_motif_templates_quote_their_own_evidence(motif):
    """The sentence must contain values from the rule's evidence, not just restate the score —
    that is what makes the explanation tied to the firing rule per spec.md."""
    result = explain(make_risk(rules=[motif]))
    numbers = [v for v in EVIDENCE[motif].values() if isinstance(v, (int, float)) and v > 1]
    rendered = result["explanation"].replace(",", "")
    assert any(str(int(n)) in rendered for n in numbers), result["explanation"]


@pytest.mark.parametrize("motif", sorted(MOTIF_TEMPLATES))
def test_motif_templates_survive_missing_evidence(motif):
    """Evidence is JSON parsed from a parquet column; a missing key must degrade to prose, not
    to a KeyError or a literal 'None' in text shown to a compliance judge."""
    payload = make_risk(rules=[motif])
    payload["contributing_signals"]["rules_fired"][0]["evidence"] = {}
    result = explain(payload)
    assert result["template"] == motif
    assert_usable(result["explanation"])


def test_secondary_rules_are_mentioned_without_displacing_the_top_one():
    result = explain(make_risk(rules=["SCATTER-GATHER", "FAN-IN", "STACK"]))
    assert result["template"] == "SCATTER-GATHER"
    assert "FAN-IN" in result["explanation"] and "STACK" in result["explanation"]
    assert_usable(result["explanation"])


# --- non-motif paths ------------------------------------------------------------------------

def test_pure_anomaly_path_when_no_rule_fired():
    result = explain(make_risk(detector=PURE_ANOMALY_FLOOR + 0.01))
    assert result["template"] == "PURE_ANOMALY"
    assert_usable(result["explanation"])


def test_row_flags_path_when_no_rule_and_no_outlier():
    result = explain(make_risk(detector=0.1, flags={"currency_mismatch": 0.6,
                                                    "elevated_payment_format": 0.0}))
    assert result["template"] == "ROW_FLAGS_ONLY"
    assert "currencies" in result["explanation"]
    assert_usable(result["explanation"])


def test_no_concern_path_when_nothing_fired():
    result = explain(make_risk(detector=0.05, level="LOW", action="MONITOR", score=0.02))
    assert result["template"] == "NO_CONCERN"
    assert_usable(result["explanation"])


def test_no_data_path():
    result = explain(make_risk(rows=0, level="LOW", action="MONITOR", score=0.0))
    assert result["template"] == "NO_DATA"
    assert_usable(result["explanation"])


def test_benign_damping_is_narrated_when_it_applied():
    result = explain(make_risk(detector=0.99, adjusted=0.50))
    assert "reduced" in result["explanation"]
    assert_usable(result["explanation"])


# --- the coverage guarantee -------------------------------------------------------------------

def test_every_declared_template_is_reachable():
    """ALL_TEMPLATES must not contain a template no input can select — a dead template is a
    silent gap waiting to happen."""
    reachable = {
        select_template(make_risk(rules=[m])) for m in MOTIF_TEMPLATES
    } | {
        select_template({"error": "x", "risk_level": None}),
        select_template(make_risk(rows=0)),
        select_template(make_risk(detector=PURE_ANOMALY_FLOOR + 0.01)),
        select_template(make_risk(detector=0.1,
                                  flags={"currency_mismatch": FLAG_MENTION_FLOOR})),
        select_template(make_risk(detector=0.05)),
    }
    assert reachable == set(ALL_TEMPLATES)


@pytest.mark.parametrize("template", NON_MOTIF_TEMPLATES)
def test_non_motif_templates_are_declared(template):
    assert template in ALL_TEMPLATES


# --- error handling ---------------------------------------------------------------------------

def test_error_explanation_does_not_imply_the_activity_is_clean():
    """A failed analysis narrated as 'nothing found' is the dangerous failure mode here."""
    result = explain({"error": "models not found", "risk_level": None,
                      "escalation_action": None})
    assert result["template"] == "ERROR"
    text = result["explanation"]
    assert "not a finding that the activity is clean" in text
    assert "models not found" in text


@pytest.mark.parametrize("bad", [None, "string", 42, [], {}])
def test_garbage_input_still_produces_an_explanation(bad):
    result = explain(bad)
    assert result["template"] == "ERROR"
    assert isinstance(result["explanation"], str) and result["explanation"]


def test_malformed_signals_do_not_raise():
    payload = make_risk(rules=["CYCLE"])
    payload["contributing_signals"]["rules_fired"] = [None, "junk", {"rule": "CYCLE"}]
    payload["contributing_signals"]["row_flags"] = "not-a-dict"
    payload["contributing_signals"]["detector_component_raw"] = "x"
    result = explain(payload)
    assert_usable(result["explanation"])


def test_missing_customer_id_falls_back_to_a_phrase():
    payload = make_risk(detector=0.05)
    payload["customer_id"] = None
    result = explain(payload)
    assert "the account in scope" in result["explanation"]
    assert_usable(result["explanation"])


# --- output contract ---------------------------------------------------------------------------

@pytest.mark.parametrize("motif", sorted(MOTIF_TEMPLATES))
def test_explanation_states_level_and_action(motif):
    result = explain(make_risk(rules=[motif], level="CRITICAL", action="REPORT", score=0.9))
    assert "CRITICAL" in result["explanation"]
    assert "REPORT" in result["explanation"]


def test_returns_the_spec_required_key():
    result = explain(make_risk(rules=["FAN-OUT"]))
    assert "explanation" in result and isinstance(result["explanation"], str)


# --- counterparty attribution ------------------------------------------------------------

def _with_counterparties(base: dict, hits) -> dict:
    base["contributing_signals"]["counterparty_rules"] = [
        {"rule": r, "account": a, "weight": 1.0, "rule_confidence": 1.0,
         "contribution": 1.0, "evidence": {}}
        for r, a in hits
    ]
    return base


def test_counterparty_patterns_are_named_but_disclaimed():
    result = explain(_with_counterparties(
        make_risk(detector=0.1), [("SCATTER-GATHER", "OTHER9")]))
    text = result["explanation"]
    assert "OTHER9" in text and "SCATTER-GATHER" in text
    assert "not of the one assessed here" in text
    assert_usable(text)


def test_counterparty_note_appears_alongside_a_subject_motif():
    result = explain(_with_counterparties(
        make_risk(rules=["CYCLE"]), [("STACK", "OTHER9")]))
    assert result["template"] == "CYCLE"
    assert "OTHER9" in result["explanation"]
    assert_usable(result["explanation"])


def test_many_counterparties_on_one_rule_are_summarised():
    result = explain(_with_counterparties(
        make_risk(detector=0.1), [("STACK", f"A{i}") for i in range(5)]))
    assert "5 accounts (STACK)" in result["explanation"]
    assert_usable(result["explanation"])


def test_no_counterparty_sentence_when_there_are_none():
    assert "Separately, counterparties" not in explain(make_risk(rules=["CYCLE"]))["explanation"]

"""
Tool-registry tests — the hand-off surface described in ml_spec.md's interface contract.

These are contract tests, not behaviour tests: the teammate's loop only ever sees this module,
so what matters is that every tool is declared, every schema is valid JSON, and no input
reaches the loop as an exception.
"""
import json
from pathlib import Path

import pytest

from ml.data import ENRICHED_PATH
from ml.risk import ESCALATION_ACTIONS
from ml.tools import (
    FLAGS_COLUMN_MAP,
    TOOL_SCHEMAS,
    TOOLS,
    VALID_RISK_LEVELS,
    dispatch,
)

EXPECTED_TOOLS = {"eda", "feature_eng", "anomaly", "risk", "explain"}

needs_data = pytest.mark.skipif(
    not Path(ENRICHED_PATH).exists(),
    reason="enriched Parquet not built - run scripts/enrich.py",
)


def test_registry_covers_exactly_the_ml_owned_tools():
    """`escalate` is the teammate's — it writes SQLite, which this workstream must not."""
    assert set(TOOLS) == EXPECTED_TOOLS
    assert {s["name"] for s in TOOL_SCHEMAS} == EXPECTED_TOOLS


def test_schemas_are_json_serializable():
    json.dumps(TOOL_SCHEMAS)


@pytest.mark.parametrize("schema", TOOL_SCHEMAS, ids=lambda s: s["name"])
def test_each_schema_is_well_formed(schema):
    assert schema["description"].strip()
    body = schema["input_schema"]
    assert body["type"] == "object"
    assert body["properties"]
    for name in body.get("required", []):
        assert name in body["properties"]


@pytest.mark.parametrize("schema", TOOL_SCHEMAS, ids=lambda s: s["name"])
def test_every_property_is_described(schema):
    """The description is the only thing steering the model's argument choice — an
    undescribed enum is how it ends up guessing a date format."""
    for name, prop in schema["input_schema"]["properties"].items():
        assert prop.get("description") or prop.get("enum"), name


def test_unknown_tool_returns_a_structured_error():
    result = dispatch("escalate", {})
    assert "unknown tool" in result["error"]


@pytest.mark.parametrize("args", [None, [], "scope", 3])
def test_non_object_arguments_are_rejected(args):
    result = dispatch("anomaly", args)
    assert isinstance(result, dict)
    assert "error" in result or "row_count_scored" in result


def test_unexpected_keyword_is_reported_not_raised():
    assert "error" in dispatch("explain", {"risk_result": {}, "surprise": 1})


@needs_data
def test_eda_accepts_the_spec_wrapped_and_flat_argument_shapes():
    """Models wrap inconsistently; both `{"query_spec": {...}}` and a flat spec must work."""
    wrapped = dispatch("eda", {"query_spec": {"operation": "count"}})
    flat = dispatch("eda", {"operation": "count"})
    assert wrapped["records"] == flat["records"]


@needs_data
def test_full_chain_runs_through_dispatch():
    scored = dispatch("anomaly", {"scope": {"account_id": "8000EBD30"}})
    assessed = dispatch("risk", {"anomaly_result": scored})
    explained = dispatch("explain", {"risk_result": assessed})

    assert assessed["risk_level"] in VALID_RISK_LEVELS
    assert assessed["escalation_action"] == ESCALATION_ACTIONS[assessed["risk_level"]]
    assert explained["explanation"]
    json.dumps([scored, assessed, explained])


@needs_data
def test_risk_rejects_something_that_is_not_an_anomaly_result():
    """Falling through to LOW/MONITOR here would report an unexamined account as clean."""
    result = dispatch("risk", {"anomaly_result": {"bogus": 1}})
    assert "error" in result
    assert result["risk_level"] is None


@needs_data
def test_flags_column_map_matches_what_risk_actually_returns():
    scored = dispatch("anomaly", {"scope": {"account_id": "8000EBD30"}})
    assessed = dispatch("risk", {"anomaly_result": scored})
    for column, source_key in FLAGS_COLUMN_MAP.items():
        if source_key.startswith("<"):
            continue   # supplied by explain(), not risk()
        assert source_key in assessed, column

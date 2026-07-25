"""The Phase 3b integration gate: does tool output survive the loop boundary?

Every test here is written against the *contract* (docs/IMPLEMENTATION_PLAN.md Phase 2), never
against a specific tool implementation, so this file is the thing to run the moment the ML
owner's real `backend/tools/*` functions replace the stubs in `agent.loop.STUBS`. Nothing here
should need editing at that point — if a test starts failing, a real contract violation landed.

The contract: plain dicts/primitives in, plain dict out, never raise on bad input, no side
effects beyond reading Parquet.

Why this is defensive rather than trusting: the ML tools are pandas/numpy/DuckDB-backed, and
that stack leaks NaN, numpy scalars, Timestamps and NaT into otherwise-innocent-looking dicts.
Each of those corrupts a run *silently* under a plain `json.dumps(..., default=str)`.

Run from backend/:  .venv\\Scripts\\python.exe -m pytest tests/test_tool_contract.py -q
"""

import json
import math

import pytest
from agent.loop import (
    MAX_TOOL_RESULT_CHARS,
    STUBS,
    TOOL_SCHEMAS,
    _dispatch,
    _encode_result,
    _json_safe,
)

np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")


# --- serialisation: the silent-corruption class of bug ---------------------------------------


def test_nan_and_inf_become_null_not_invalid_json():
    """pandas yields NaN constantly (std of a single row, 0/0, unmatched joins).

    Plain json.dumps emits a bare `NaN` token, which is not valid JSON and is rejected by
    strict parsers upstream at the provider.
    """
    encoded = _encode_result("feature_eng", {"deviation_from_avg": float("nan"), "r": float("inf")})

    # parse_constant fires only for NaN/Infinity/-Infinity tokens, so this asserts strictness
    def reject(token):
        raise AssertionError(f"emitted non-JSON constant {token!r}")

    decoded = json.loads(encoded, parse_constant=reject)
    assert decoded == {"deviation_from_avg": None, "r": None}


def test_numpy_scalars_stay_numbers():
    """The nastiest one: default=str turns np.int64(37) into "37" with no error anywhere,
    and the model then reasons over a string instead of a count."""
    decoded = json.loads(
        _encode_result(
            "feature_eng",
            {"txn_count": np.int64(37), "avg": np.float64(12400.5), "hit": np.bool_(True)},
        )
    )
    assert decoded["txn_count"] == 37 and isinstance(decoded["txn_count"], int)
    assert decoded["avg"] == 12400.5 and isinstance(decoded["avg"], float)
    assert decoded["hit"] is True


def test_pandas_timestamps_and_nat_survive():
    decoded = json.loads(
        _encode_result("feature_eng", {"ts": pd.Timestamp("2022-09-03T02:14:00"), "closed": pd.NaT})
    )
    assert "2022-09-03" in decoded["ts"]
    assert decoded["closed"] in (None, "NaT")  # either is parseable; a bare NaN token is not


def test_nested_structures_are_sanitised_all_the_way_down():
    """Real tool output is nested: {"records": [{...}], "method_scores": {...}}."""
    decoded = json.loads(
        _encode_result(
            "anomaly",
            {
                "records": [{"score": np.float64(0.9), "gap": float("nan")}],
                "method_scores": {"lof": np.float32(0.79)},
                "rule_hits": ("FAN-OUT", "CYCLE"),  # tuple, not list
            },
        )
    )
    assert decoded["records"][0]["score"] == pytest.approx(0.9)
    assert decoded["records"][0]["gap"] is None
    assert decoded["method_scores"]["lof"] == pytest.approx(0.79, rel=1e-6)
    assert decoded["rule_hits"] == ["FAN-OUT", "CYCLE"]


def test_oversized_result_is_truncated_and_says_so():
    """A tool scoped too broadly over ~5M rows must not blow the context window silently."""
    huge = {"records": [{"id": i, "amount": i * 1.5} for i in range(50_000)]}
    decoded = json.loads(_encode_result("eda", huge))
    assert decoded["truncated"] is True
    assert "eda" in decoded["note"], "the model needs to know which tool to re-scope"
    assert len(_encode_result("eda", huge)) < MAX_TOOL_RESULT_CHARS * 2


def test_every_stub_result_is_json_safe():
    """Contract conformance, run over whatever is registered — stubs now, real tools later."""
    for name, payload in _sample_calls().items():
        encoded = _encode_result(name, _dispatch(name, payload))

        def reject(token):
            raise AssertionError(f"{name} emitted non-JSON constant {token!r}")

        json.loads(encoded, parse_constant=reject)


# --- failure isolation: one bad tool must not take down the request ---------------------------


@pytest.mark.parametrize(
    "exc",
    [
        TypeError("unexpected keyword 'scope'"),
        ValueError("empty scope"),
        KeyError("Amount Received"),  # the single most likely real failure: renamed column
        RuntimeError("duckdb: no such file"),
        ZeroDivisionError("std of one row"),
    ],
)
def test_a_raising_tool_is_reported_not_propagated(exc, monkeypatch):
    """Contract says tools never raise. This asserts the backstop for when one does anyway —
    the agent should get a structured error it can react to, not a 500."""

    def boom(**_kwargs):
        raise exc

    monkeypatch.setitem(STUBS, "anomaly", boom)
    out = _dispatch("anomaly", {"scope": {}})

    assert isinstance(out, dict) and "error" in out, "a raising tool must degrade to an error dict"
    assert type(exc).__name__ in out["error"] or "bad arguments" in out["error"]
    json.loads(_encode_result("anomaly", out))  # and the error itself must serialise


def test_unknown_tool_name_is_reported_not_crashed():
    out = _dispatch("does_not_exist", {})
    assert "error" in out and "unknown tool" in out["error"]


def test_wrong_argument_names_are_reported_to_the_model():
    """The model invents arg names; it must be told, so it can retry with the right ones."""
    out = _dispatch("anomaly", {"totally_wrong_kwarg": 1})
    assert "error" in out


# --- registration: schemas and dispatch table must not drift apart ---------------------------


def test_declared_tool_schemas_match_registered_tools():
    """A tool advertised to the model but missing from STUBS is a guaranteed runtime error;
    the reverse is a tool the model can never reach. Both are easy to introduce at Phase 3b."""
    declared = {t["name"] for t in TOOL_SCHEMAS}
    registered = set(STUBS)
    assert declared == registered, (
        f"declared-but-unregistered: {declared - registered}; "
        f"registered-but-undeclared: {registered - declared}"
    )


def test_every_tool_schema_is_well_formed():
    for tool in TOOL_SCHEMAS:
        assert tool["description"].strip(), f"{tool['name']} needs a description to be selectable"
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        for required in schema.get("required", []):
            assert required in schema["properties"], (
                f"{tool['name']} requires {required!r} but never declares it"
            )


def test_every_tool_returns_a_dict():
    """Everything downstream — _encode_result, the error path, the model — assumes a dict."""
    for name, payload in _sample_calls().items():
        assert isinstance(_dispatch(name, payload), dict), f"{name} must return a plain dict"


def _sample_calls() -> dict:
    """Minimal valid arguments per tool, taken from TOOL_SCHEMAS' required fields."""
    return {
        "eda": {"query_spec": {}},
        "feature_eng": {"scope": {"account_id": "8000EBD30"}},
        "anomaly": {"scope": {"account_id": "8000EBD30"}},
        "risk": {"anomaly_result": {"anomaly_score": 0.87}},
        "explain": {"risk_result": {"risk_level": "HIGH", "pattern_detected": "FAN-OUT"}},
    }


# --- the sanitiser itself ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
        (np.int64(5), 5),
        (np.bool_(False), False),
        (None, None),
        ("text", "text"),
        (True, True),
        (0, 0),
    ],
)
def test_json_safe_scalars(raw, expected):
    assert _json_safe(raw) == expected or (
        expected is None and _json_safe(raw) is None
    )


def test_json_safe_leaves_ordinary_values_untouched():
    payload = {"a": 1, "b": [1, 2, {"c": "x"}], "d": None, "e": 1.5}
    assert _json_safe(payload) == payload


def test_json_safe_never_raises_on_an_arbitrary_object():
    class Weird:
        def __repr__(self):
            return "<weird>"

    assert not math.isnan(0)  # sanity
    assert _json_safe({"o": Weird()})["o"] == "<weird>"

"""
Repeat-query cache tests.

A memoized tool is only safe under two properties: identical calls must not re-run, and a
caller mutating a returned result must not change what the next caller sees. Both are checked
here against a counting stub, plus one end-to-end check that the real tools are wrapped.
"""
import pytest

from ml.cache import cached_tool


@pytest.fixture
def counting_tool():
    calls = []

    @cached_tool(maxsize=4)
    def tool(scope: dict, method: str = "all") -> dict:
        calls.append((scope, method))
        return {"scope": scope, "method": method, "records": [1, 2, 3]}

    tool.calls = calls
    return tool


def test_identical_call_is_served_from_cache(counting_tool):
    counting_tool({"account_id": "A"})
    counting_tool({"account_id": "A"})
    assert len(counting_tool.calls) == 1


def test_key_ignores_dict_ordering(counting_tool):
    counting_tool({"account_id": "A", "role": "both"})
    counting_tool({"role": "both", "account_id": "A"})
    assert len(counting_tool.calls) == 1


def test_different_arguments_are_separate_entries(counting_tool):
    counting_tool({"account_id": "A"})
    counting_tool({"account_id": "B"})
    counting_tool({"account_id": "A"}, method="lof")
    assert len(counting_tool.calls) == 3


def test_caller_mutation_does_not_poison_the_cache(counting_tool):
    first = counting_tool({"account_id": "A"})
    first["records"].append("MUTATED")
    first["scope"]["account_id"] = "TAMPERED"

    second = counting_tool({"account_id": "A"})
    assert second["records"] == [1, 2, 3]
    assert second["scope"]["account_id"] == "A"


def test_unserializable_arguments_bypass_the_cache_rather_than_failing(counting_tool):
    """Garbage input is the tool's own error to report, with its own message."""
    result = counting_tool({"account_id": object()})
    assert "scope" in result
    assert len(counting_tool.calls) == 1


def test_cache_can_be_inspected_and_cleared(counting_tool):
    counting_tool({"account_id": "A"})
    counting_tool({"account_id": "A"})
    assert counting_tool.cache_info().hits == 1
    counting_tool.cache_clear()
    counting_tool({"account_id": "A"})
    assert len(counting_tool.calls) == 2


def test_data_reading_tools_are_wrapped():
    """risk/explain are deliberately not cached — their arguments are whole result dicts, so
    building the key costs more than recomputing."""
    from ml.anomaly import anomaly
    from ml.eda import eda
    from ml.explain import explain
    from ml.feature_eng import feature_eng
    from ml.risk import risk

    for tool in (eda, feature_eng, anomaly):
        assert hasattr(tool, "cache_info"), tool.__name__
    for tool in (risk, explain):
        assert not hasattr(tool, "cache_info"), tool.__name__

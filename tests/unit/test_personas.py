"""Voice personas: declared tools must match implementations, and the
implementations must actually work (they run server-side, unsupervised)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agents" / "06_voice"))
import personas  # noqa: E402


def test_every_declared_tool_has_an_implementation():
    for pid, p in personas.PERSONAS.items():
        declared = {t["name"] for t in p["tools"]}
        implemented = set(p["implementations"])
        assert declared == implemented, f"{pid}: schema/impl mismatch"


def test_tool_schemas_are_valid_function_declarations():
    for p in personas.PERSONAS.values():
        for t in p["tools"]:
            assert t["type"] == "function"
            assert t["parameters"]["type"] == "object"
            for req in t["parameters"].get("required", []):
                assert req in t["parameters"]["properties"]


def test_trade_recommendation_tool_returns_engine_output():
    out = json.loads(personas.trade_recommendation("SPY"))
    assert out["ticker"] == "SPY" and out["legs"]


def test_run_tool_dispatch_and_error_paths():
    ok = json.loads(personas.run_tool("marcus", "desk_status", {"ticker": "SPY"}))
    assert ok["ticker"] == "SPY"
    bad_tool = json.loads(personas.run_tool("marcus", "nuke", {}))
    assert "error" in bad_tool
    bad_args = json.loads(personas.run_tool("marcus", "desk_status", {"nope": 1}))
    assert "error" in bad_args  # TypeError caught, not raised

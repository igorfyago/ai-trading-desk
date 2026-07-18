"""Graph topology: the wiring IS the design — pin it.

These tests build the real LangGraph graphs (no LLM calls happen at build
time) and assert the structure the READMEs advertise: router, parallel
fan-out, join, critique loop, HITL node.
"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_research_graph_topology():
    g = _load("a4", "agents/04_research_graph/main.py").build_graph().get_graph()
    assert {"plan", "researcher", "tools", "draft", "reflect", "revise", "finalize"} <= set(g.nodes)
    edges = {(e.source, e.target) for e in g.edges}
    assert ("tools", "researcher") in edges          # inner tool loop closes
    assert ("revise", "researcher") in edges         # outer reflection loop closes
    assert ("plan", "researcher") in edges


def test_analyst_graph_topology():
    g = _load("a5", "agents/05_desk_analyst/main.py").build_graph().get_graph()
    nodes = set(g.nodes)
    assert {"fetch", "long_gamma_playbook", "short_gamma_playbook",
            "positioning", "flow", "risk",
            "synthesize", "risk_review", "human_approval", "publish"} <= nodes
    edges = {(e.source, e.target) for e in g.edges}
    for playbook in ("long_gamma_playbook", "short_gamma_playbook"):
        for specialist in ("positioning", "flow", "risk"):
            assert (playbook, specialist) in edges   # fan-out from both branches
    for specialist in ("positioning", "flow", "risk"):
        assert (specialist, "synthesize") in edges   # join before synthesis
    assert ("synthesize", "risk_review") in edges


def test_registry_metadata_consistent():
    from common import tickers as _tk; from web import registry

    ids = [a["id"] for a in registry.AGENT_META]
    assert ids == ["brief", "sql", "repo", "research", "analyst"]
    assert len(set(ids)) == len(ids)
    assert _tk.TICKER_RE.search("run the desk on qqq please").group(1).upper() == "QQQ"
    assert _tk.TICKER_RE.search("no ticker here") is None

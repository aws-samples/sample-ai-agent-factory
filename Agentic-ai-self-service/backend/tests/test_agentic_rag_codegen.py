"""Phase 3 Gap 3C — agentic retrieval codegen tests.

EXEC the generated @tool source against boto3 stubs (mirrors
tests/test_hitl_codegen.py's exec-against-stubs discipline — AST-parse alone is
NOT sufficient, Bug 125). Verifies:

  * each strategy tool function is defined at module scope after exec;
  * the source has NO unresolved module-symbol dependency (exec ALONE in a fresh
    namespace seeded only with @tool + stubbed boto3/os/json — proves it does not
    rely on the host template's REGION/MODEL_ID symbols);
  * KB_ID env gating (no-KB error JSON unset, valid JSON set) mirrors
    retrieve_from_kb's error contract;
  * reranked issues one retrieve (numberOfResults==top_n) then one converse using
    RERANK_JUDGE_MODEL_ID, returning <= return_n;
  * hybrid sends overrideSearchType='HYBRID' and falls back to SEMANTIC on
    ValidationException;
  * multi_hop performs >1 (and <= max_hops) retrieves, dedupes, synthesizes;
  * integration through generate_agent_code swaps the strategy tool in for
    non-simple strategies and keeps retrieve_from_kb for simple/absent — and the
    full generated module imports against strands stubs.
"""

from __future__ import annotations

import json
import sys
import types

sys.path.insert(0, "src")

import pytest

from app.models.deployment_models import RuntimeConfig
from app.services.agentic_rag_codegen import (
    STRATEGY_TOOL_NAMES,
    agentic_rag_tool_name,
    agentic_rag_tool_source,
)
from app.services.code_generator import generate_agent_code


@pytest.fixture(autouse=True)
def _restore_boto3():
    """Bug 139: tests here replace sys.modules['boto3'] with a fake module (no
    Session attr) to exec generated tool source in isolation, and never restore
    it. Because this file sorts first alphabetically, the fake leaked into the
    whole pytest process and broke moto's lazy boto3.Session import in 16
    downstream tests. Snapshot the real boto3 (+ submodules) before each test and
    restore it after, so the swap is strictly local to this file.
    """
    saved = {
        name: mod
        for name, mod in list(sys.modules.items())
        if name == "boto3" or name.startswith("boto3.")
    }
    try:
        yield
    finally:
        for name in [
            n for n in list(sys.modules) if n == "boto3" or n.startswith("boto3.")
        ]:
            del sys.modules[name]
        sys.modules.update(saved)


# ---------------------------------------------------------------------------
# Fake boto3 — records retrieve/converse calls, returns canned responses.
# ---------------------------------------------------------------------------


class _FakeKBClient:
    def __init__(self, recorder, fail_hybrid=False):
        self._rec = recorder
        self._fail_hybrid = fail_hybrid

    def retrieve(self, **kwargs):
        self._rec["retrieve_calls"].append(kwargs)
        vsc = kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]
        if self._fail_hybrid and vsc.get("overrideSearchType") == "HYBRID":
            raise Exception("ValidationException: overrideSearchType HYBRID not supported")
        n = vsc.get("numberOfResults", 5)
        return {
            "retrievalResults": [
                {
                    "content": {"text": "passage-%d for %s" % (i, kwargs["retrievalQuery"]["text"])},
                    "score": 1.0 - i * 0.01,
                    "location": {"type": "S3", "s3Location": {"uri": "s3://b/doc%d" % i}},
                }
                for i in range(n)
            ]
        }


class _FakeRuntimeClient:
    """bedrock-runtime stub. converse() returns scripted texts in order."""

    def __init__(self, recorder, converse_texts):
        self._rec = recorder
        self._texts = list(converse_texts)

    def converse(self, **kwargs):
        self._rec["converse_calls"].append(kwargs)
        text = self._texts.pop(0) if self._texts else "DONE"
        return {"output": {"message": {"content": [{"text": text}]}}}


def _make_fake_boto3(recorder, converse_texts=None, fail_hybrid=False):
    fake = types.ModuleType("boto3")
    kb = _FakeKBClient(recorder, fail_hybrid=fail_hybrid)
    rt = _FakeRuntimeClient(recorder, converse_texts or [])

    def client(service, **kwargs):
        recorder["client_services"].append(service)
        if service == "bedrock-agent-runtime":
            return kb
        if service == "bedrock-runtime":
            return rt
        raise AssertionError("unexpected client: %s" % service)

    fake.client = client
    return fake


def _exec_tool_source(strategy, recorder, converse_texts=None, fail_hybrid=False):
    """Exec a single strategy's tool source ALONE in a fresh namespace seeded
    only with a no-op @tool decorator + fake boto3 in sys.modules. Returns the
    callable tool function. NameError here = a forward/module-symbol dependency
    (Bug 125)."""
    src = agentic_rag_tool_source(strategy)
    assert src, "expected non-empty source for %s" % strategy

    fake_boto3 = _make_fake_boto3(recorder, converse_texts, fail_hybrid)
    sys.modules["boto3"] = fake_boto3

    def tool(f=None, **k):
        return f if f else (lambda g: g)

    ns: dict = {"tool": tool, "__name__": "rag_tool_under_test"}
    exec(compile(src, "<rag_tool.py>", "exec"), ns)
    name = STRATEGY_TOOL_NAMES[strategy]
    fn = ns.get(name)
    assert callable(fn), "%s not defined at module scope" % name
    return fn


def _new_recorder():
    return {"retrieve_calls": [], "converse_calls": [], "client_services": []}


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def test_tool_name_mapping():
    assert agentic_rag_tool_name("multi_hop") == "retrieve_multi_hop"
    assert agentic_rag_tool_name("hybrid") == "retrieve_hybrid"
    assert agentic_rag_tool_name("reranked") == "retrieve_reranked"
    # simple / absent / unknown → None (caller keeps retrieve_from_kb)
    assert agentic_rag_tool_name("simple") is None
    assert agentic_rag_tool_name(None) is None
    assert agentic_rag_tool_name("bogus") is None
    # camelCase robustness handled in normalization (case-insensitive)
    assert agentic_rag_tool_name("MULTI_HOP") == "retrieve_multi_hop"


def test_simple_source_is_empty():
    assert agentic_rag_tool_source("simple") == ""
    assert agentic_rag_tool_source(None) == ""
    assert agentic_rag_tool_source("bogus") == ""


# ---------------------------------------------------------------------------
# Exec each strategy alone (no module-symbol dependency) + KB_ID gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", ["multi_hop", "hybrid", "reranked"])
def test_source_execs_alone_and_defines_tool(strategy, monkeypatch):
    monkeypatch.delenv("KB_ID", raising=False)
    rec = _new_recorder()
    fn = _exec_tool_source(strategy, rec, converse_texts=["DONE"])
    # No KB_ID → error JSON, mirrors retrieve_from_kb contract. No retrieve issued.
    out = json.loads(fn("what is x?"))
    assert "error" in out and "KB_ID" in out["error"]
    assert rec["retrieve_calls"] == []


@pytest.mark.parametrize("strategy", ["multi_hop", "hybrid", "reranked"])
def test_kb_id_set_returns_results(strategy, monkeypatch):
    monkeypatch.setenv("KB_ID", "ABCDE12345")
    rec = _new_recorder()
    # multi_hop needs a DONE to stop after first hop; reranked needs a judge reply.
    fn = _exec_tool_source(strategy, rec, converse_texts=["DONE", "[0,1]"])
    out = json.loads(fn("what is x?"))
    assert "error" not in out, out
    assert out["query"] == "what is x?"
    assert out["count"] >= 1
    assert len(rec["retrieve_calls"]) >= 1


# ---------------------------------------------------------------------------
# reranked specifics
# ---------------------------------------------------------------------------


def test_reranked_uses_judge_model_and_caps_return_n(monkeypatch):
    monkeypatch.setenv("KB_ID", "ABCDE12345")
    monkeypatch.setenv("RERANK_JUDGE_MODEL_ID", "us.anthropic.sentinel-judge-v1:0")
    rec = _new_recorder()
    # Judge returns indices in a custom order.
    fn = _exec_tool_source("reranked", rec, converse_texts=["[2, 0, 1]"])
    out = json.loads(fn("q", top_n=12, return_n=3))

    # Exactly one retrieve, numberOfResults == top_n.
    assert len(rec["retrieve_calls"]) == 1
    vsc = rec["retrieve_calls"][0]["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert vsc["numberOfResults"] == 12
    # Exactly one converse using the sentinel judge model.
    assert len(rec["converse_calls"]) == 1
    assert rec["converse_calls"][0]["modelId"] == "us.anthropic.sentinel-judge-v1:0"
    # <= return_n results, judge order honored (index 2 first).
    assert out["count"] <= 3
    assert out["results"][0]["text"].startswith("passage-2")
    assert out["strategy"] == "reranked"


def test_reranked_default_judge_model_is_haiku(monkeypatch):
    """Bug 113 — judge must default to the haiku-4-5 model in the window."""
    monkeypatch.setenv("KB_ID", "ABCDE12345")
    monkeypatch.delenv("RERANK_JUDGE_MODEL_ID", raising=False)
    rec = _new_recorder()
    fn = _exec_tool_source("reranked", rec, converse_texts=["[0]"])
    fn("q", top_n=5, return_n=1)
    assert rec["converse_calls"][0]["modelId"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_reranked_judge_failure_falls_back_to_score(monkeypatch):
    monkeypatch.setenv("KB_ID", "ABCDE12345")
    rec = _new_recorder()
    # Judge returns non-JSON garbage → fall back to vector-score ordering.
    fn = _exec_tool_source("reranked", rec, converse_texts=["I cannot do that"])
    out = json.loads(fn("q", top_n=6, return_n=2))
    assert out["strategy"] == "reranked_fallback_score"
    assert out["count"] == 2
    # Highest score first (passage-0 has score 1.0).
    assert out["results"][0]["text"].startswith("passage-0")


# ---------------------------------------------------------------------------
# hybrid specifics
# ---------------------------------------------------------------------------


def test_hybrid_sends_override_search_type(monkeypatch):
    monkeypatch.setenv("KB_ID", "ABCDE12345")
    rec = _new_recorder()
    fn = _exec_tool_source("hybrid", rec)
    out = json.loads(fn("specific-term-123"))
    assert len(rec["retrieve_calls"]) == 1
    vsc = rec["retrieve_calls"][0]["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert vsc["overrideSearchType"] == "HYBRID"
    assert out["strategy"] == "hybrid"
    assert out["count"] >= 1


def test_hybrid_falls_back_to_semantic_on_validation_exception(monkeypatch):
    monkeypatch.setenv("KB_ID", "ABCDE12345")
    rec = _new_recorder()
    fn = _exec_tool_source("hybrid", rec, fail_hybrid=True)
    out = json.loads(fn("q"))
    # Two retrieve calls: first HYBRID (raises), second SEMANTIC (no override).
    assert len(rec["retrieve_calls"]) == 2
    assert rec["retrieve_calls"][0]["retrievalConfiguration"]["vectorSearchConfiguration"].get("overrideSearchType") == "HYBRID"
    assert "overrideSearchType" not in rec["retrieve_calls"][1]["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert out["strategy"] == "hybrid_fallback_semantic"
    assert "error" not in out
    assert out["count"] >= 1


# ---------------------------------------------------------------------------
# multi_hop specifics
# ---------------------------------------------------------------------------


def test_multi_hop_iterates_and_dedupes(monkeypatch):
    monkeypatch.setenv("KB_ID", "ABCDE12345")
    rec = _new_recorder()
    # Decomposer yields one follow-up sub-question, then DONE → 2 hops total.
    fn = _exec_tool_source("multi_hop", rec, converse_texts=["follow-up question", "DONE"])
    out = json.loads(fn("complex multi-part question", max_hops=3))
    assert out["strategy"] == "multi_hop"
    assert out["hops"] == 2
    assert len(rec["retrieve_calls"]) == 2
    assert out["sub_questions"][0] == "complex multi-part question"
    assert out["sub_questions"][1] == "follow-up question"
    # Two distinct sub-queries → 5 + 5 distinct passages (texts embed the query).
    assert out["count"] == 10


def test_multi_hop_dedupes_identical_passages(monkeypatch):
    monkeypatch.setenv("KB_ID", "ABCDE12345")
    rec = _new_recorder()
    # Decomposer echoes the SAME query → both hops return identical passages,
    # which must be deduped down to a single copy set.
    fn = _exec_tool_source("multi_hop", rec, converse_texts=["q", "DONE"])
    out = json.loads(fn("q", max_hops=3))
    assert len(rec["retrieve_calls"]) == 2
    assert out["count"] == 5


def test_multi_hop_respects_max_hops_cap(monkeypatch):
    monkeypatch.setenv("KB_ID", "ABCDE12345")
    rec = _new_recorder()
    # Decomposer never says DONE — cap must stop at max_hops retrieves.
    fn = _exec_tool_source(
        "multi_hop", rec, converse_texts=["q1", "q2", "q3", "q4", "q5", "q6"]
    )
    out = json.loads(fn("q", max_hops=2))
    assert len(rec["retrieve_calls"]) == 2
    assert out["hops"] == 2


# ---------------------------------------------------------------------------
# Integration through generate_agent_code
# ---------------------------------------------------------------------------


def _cfg():
    return RuntimeConfig(
        name="rag_t",
        model={"modelId": "us.anthropic.claude-sonnet-5"},
        systemPrompt="You answer from the knowledge base.",
        modelProvider="bedrock",
    )


def _agent_constructor_args(code: str) -> str:
    """Concatenated args of every Agent(...) call (paren-balanced)."""
    import re

    out = []
    for m in re.finditer(r"\bAgent\(", code):
        start = m.end()
        depth = 1
        j = start
        while j < len(code) and depth:
            if code[j] == "(":
                depth += 1
            elif code[j] == ")":
                depth -= 1
            j += 1
        out.append(code[start : j - 1])
    return " ".join(out)


@pytest.mark.parametrize("strategy", ["multi_hop", "hybrid", "reranked"])
def test_generate_agent_code_swaps_in_strategy_tool(strategy):
    code = generate_agent_code(
        config=_cfg(),
        tools=["knowledge_base"],
        kb_config={"retrievalStrategy": strategy},
    )
    tool_name = STRATEGY_TOOL_NAMES[strategy]
    # Strategy tool defined AND inlined into the Agent(...) tools=[...] arg.
    assert "def %s" % tool_name in code
    assert tool_name in _agent_constructor_args(code)
    # SWAP, not add: retrieve_from_kb must be gone.
    assert "def retrieve_from_kb" not in code


def test_generate_agent_code_simple_keeps_retrieve_from_kb():
    for kb_config in (None, {}, {"retrievalStrategy": "simple"}):
        code = generate_agent_code(
            config=_cfg(), tools=["knowledge_base"], kb_config=kb_config
        )
        assert "def retrieve_from_kb" in code
        assert "retrieve_from_kb" in _agent_constructor_args(code)
        for tool_name in STRATEGY_TOOL_NAMES.values():
            assert "def %s" % tool_name not in code


def test_generate_agent_code_snake_case_strategy_key():
    """Reader accepts snake_case retrieval_strategy too (serialization robustness)."""
    code = generate_agent_code(
        config=_cfg(),
        tools=["knowledge_base"],
        kb_config={"retrieval_strategy": "hybrid"},
    )
    assert "def retrieve_hybrid" in code
    assert "def retrieve_from_kb" not in code


def _install_strands_stubs():
    strands = types.ModuleType("strands")

    class Agent:
        def __init__(self, *a, **k):
            self.kwargs = k

        def __call__(self, *a, **k):
            return "stub-response"

    def tool(f=None, **k):
        return f if f else (lambda g: g)

    strands.Agent = Agent
    strands.tool = tool

    smodels = types.ModuleType("strands.models")

    class BedrockModel:
        def __init__(self, *a, **k):
            pass

    smodels.BedrockModel = BedrockModel
    strands.models = smodels

    sbedrock = types.ModuleType("strands.models.bedrock")
    sbedrock.BedrockModel = BedrockModel
    smodels.bedrock = sbedrock

    bac = types.ModuleType("bedrock_agentcore")
    bacr = types.ModuleType("bedrock_agentcore.runtime")

    class App:
        def entrypoint(self, f):
            return f

        def run(self):
            pass

    bacr.BedrockAgentCoreApp = App
    bac.runtime = bacr

    mods = {
        "strands": strands,
        "strands.models": smodels,
        "strands.models.bedrock": sbedrock,
        "bedrock_agentcore": bac,
        "bedrock_agentcore.runtime": bacr,
    }
    for n, m in mods.items():
        sys.modules[n] = m


@pytest.mark.parametrize("strategy", ["multi_hop", "hybrid", "reranked"])
def test_full_generated_module_imports(strategy, monkeypatch):
    """The whole agent module (built-in tools skeleton + agentic tool) must
    import cleanly against strands stubs — catches any Bug-125 forward ref."""
    monkeypatch.delenv("KB_ID", raising=False)
    code = generate_agent_code(
        config=_cfg(),
        tools=["knowledge_base"],
        kb_config={"retrievalStrategy": strategy},
    )
    _install_strands_stubs()
    rec = _new_recorder()
    sys.modules["boto3"] = _make_fake_boto3(rec)
    g: dict = {"__name__": "agent_under_test"}
    exec(compile(code, "<agent.py>", "exec"), g)
    assert callable(g.get(STRATEGY_TOOL_NAMES[strategy]))
    # The agent factory resolves with the strategy tool inlined.
    assert callable(g.get("_get_agent"))
    g["_get_agent"]()

"""Agentic-retrieval codegen (Phase 3 Gap 3C).

Emits self-contained ``@tool`` Python source strings that replace the single-shot
``retrieve_from_kb`` tool when a connected Knowledge Base declares a non-trivial
``retrievalStrategy``. Three strategies are supported:

  * ``multi_hop``  → ``retrieve_multi_hop``  — LLM query decomposition + iterative
                      Retrieve over several hops, then a synthesized passage set.
  * ``hybrid``     → ``retrieve_hybrid``     — single Retrieve with
                      ``overrideSearchType='HYBRID'`` (vector + keyword), with a
                      graceful fallback to default SEMANTIC search on stores that
                      reject HYBRID (S3 Vectors, etc.).
  * ``reranked``   → ``retrieve_reranked``   — Retrieve a wide set then a Claude
                      Haiku judge call reorders / trims to the best ``return_n``.

Design constraints (Bug 125 — codegen injection safety):

  * Each tool source is FULLY self-contained. It performs its own local
    ``import os/json/boto3`` (and stdlib helpers), uses lazy boto3 client getters,
    reads ``KB_ID`` from ``os.environ`` and region from ``AWS_REGION`` /
    ``APP_AWS_REGION`` — it does NOT reference the host template's module-level
    ``REGION`` / ``MODEL_ID`` symbols. (Strictly safer than ``retrieve_from_kb``,
    which assumes a module-level ``REGION``.)
  * Sources are concatenated into ``tool_defs`` BEFORE the ``Agent(...)``
    constructor and the tool NAME is inlined into ``tools=[...]`` — never a
    forward reference to an appended symbol. The caller
    (``code_generator._generate_tools_agent``) holds that invariant.
  * The agent's own model (``MODEL_ID`` env) drives multi-hop decomposition; the
    reranker judge defaults to ``us.anthropic.claude-haiku-4-5-20251001-v1:0``
    (Bedrock model window Oct-2025..May-2026, Bug 113), overridable via
    ``RERANK_JUDGE_MODEL_ID``.

No new DDB table, no new IAM: the shared runtime exec role already grants
``bedrock:Retrieve`` + ``bedrock:InvokeModel*`` / ``Converse*`` on ``*``.
"""

from typing import Optional

# Public mapping: strategy key → generated tool function name.
STRATEGY_TOOL_NAMES: dict[str, str] = {
    "multi_hop": "retrieve_multi_hop",
    "hybrid": "retrieve_hybrid",
    "reranked": "retrieve_reranked",
}

# Default judge model for the reranker. Pinned to the Bedrock model window
# (Bug 113). Overridable at runtime via RERANK_JUDGE_MODEL_ID env.
_DEFAULT_JUDGE_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


# ---------------------------------------------------------------------------
# Shared lazy-client preamble (region + KB_ID read entirely from env so the
# source has NO dependency on the host module's REGION/MODEL_ID symbols).
# ---------------------------------------------------------------------------

_RAG_COMMON_PREAMBLE = '''
# ── Agentic retrieval helpers (injected by AgentCore Flows, Gap 3C) ──
import os as _rag_os
import json as _rag_json
import boto3 as _rag_boto3


def _rag_region():
    return _rag_os.environ.get("AWS_REGION", _rag_os.environ.get("APP_AWS_REGION", "us-east-1"))


def _rag_kb_id():
    return _rag_os.environ.get("KB_ID", "")


_rag_kb_client = None
_rag_judge_client = None


def _get_kb_client():
    global _rag_kb_client
    if _rag_kb_client is None:
        _rag_kb_client = _rag_boto3.client("bedrock-agent-runtime", region_name=_rag_region())
    return _rag_kb_client


def _get_judge_client():
    global _rag_judge_client
    if _rag_judge_client is None:
        _rag_judge_client = _rag_boto3.client("bedrock-runtime", region_name=_rag_region())
    return _rag_judge_client


def _rag_raw_retrieve(query, num_results, override_search_type=None):
    """Single Retrieve call. Returns list of {text, score, location}. May raise.

    Bug 130: MANAGED knowledge bases (S3 Vectors / managed mode) reject
    ``vectorSearchConfiguration`` with a ValidationException ("not supported for
    managed knowledge bases. Use managedSearchConfiguration instead.") — only
    OpenSearch/Aurora-backed KBs accept it. So we try the explicit
    vectorSearchConfiguration first (it carries numberOfResults + the optional
    HYBRID override) and, if the store turns out to be managed, fall back to a
    bare retrievalQuery (the form that works for managed KBs and matches the
    simple retrieve_from_kb tool). overrideSearchType is best-effort and is
    simply dropped on managed stores that don't support it.
    """
    n = max(1, min(int(num_results), 100))
    kb_id = _rag_kb_id()
    client = _get_kb_client()
    vsc = {"numberOfResults": n}
    if override_search_type:
        vsc["overrideSearchType"] = override_search_type
    try:
        resp = client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": vsc},
        )
    except Exception as _e:  # noqa: BLE001
        _msg = str(_e)
        if "managed" in _msg or "vectorSearchConfiguration is not supported" in _msg:
            # Managed KB: retry without any retrievalConfiguration (Bedrock
            # applies the managed store's own defaults).
            resp = client.retrieve(
                knowledgeBaseId=kb_id,
                retrievalQuery={"text": query},
            )
        else:
            raise
    out = []
    for r in resp.get("retrievalResults", []):
        out.append({
            "text": r.get("content", {}).get("text", ""),
            "score": r.get("score", 0.0),
            "location": r.get("location", {}),
        })
    return out
'''


# ---------------------------------------------------------------------------
# multi_hop
# ---------------------------------------------------------------------------

_MULTI_HOP_SRC = '''

@tool
def retrieve_multi_hop(query: str, max_hops: int = 3) -> str:
    """Answer complex questions by decomposing them into sub-questions and
    running several knowledge-base lookups (multi-hop retrieval). Use this when
    a question needs facts that span multiple documents or requires chaining
    several lookups before answering.
    """
    kb_id = _rag_kb_id()
    if not kb_id:
        return _rag_json.dumps({"error": "No KB_ID configured for this runtime.", "query": query})

    try:
        max_hops = max(1, min(int(max_hops), 5))
    except Exception:
        max_hops = 3

    agent_model = _rag_os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

    def _decompose(q, prior):
        """Ask the agent model for the next sub-question (or DONE)."""
        prior_txt = "\\n".join("- %s" % p for p in prior) if prior else "(none yet)"
        instr = (
            "You are decomposing a complex question into sequential search "
            "sub-questions for a knowledge base. Given the main question and the "
            "sub-questions already asked, output ONLY the single next search "
            "query that would gather missing information. If enough has been "
            "gathered, output exactly DONE.\\n\\n"
            "Main question: %s\\n\\nAlready asked:\\n%s\\n\\nNext query:" % (q, prior_txt)
        )
        try:
            resp = _get_judge_client().converse(
                modelId=agent_model,
                messages=[{"role": "user", "content": [{"text": instr}]}],
                inferenceConfig={"maxTokens": 128, "temperature": 0.0},
            )
            text = resp["output"]["message"]["content"][0].get("text", "").strip()
            return text
        except Exception:
            return "DONE"

    asked = []
    seen = set()
    passages = []
    # First hop always uses the literal query so we never make zero retrievals.
    hop_query = query
    for _hop in range(max_hops):
        if not hop_query or hop_query.upper().startswith("DONE"):
            break
        asked.append(hop_query)
        try:
            results = _rag_raw_retrieve(hop_query, 5)
        except Exception as e:
            return _rag_json.dumps({
                "error": "Multi-hop retrieve failed: %s" % str(e),
                "query": query,
                "sub_questions": asked,
            })
        for r in results:
            key = r["text"][:200]
            if key and key not in seen:
                seen.add(key)
                passages.append(r)
        hop_query = _decompose(query, asked)

    passages.sort(key=lambda p: p.get("score", 0.0), reverse=True)
    return _rag_json.dumps({
        "query": query,
        "strategy": "multi_hop",
        "sub_questions": asked,
        "hops": len(asked),
        "results": passages,
        "count": len(passages),
    })
'''


# ---------------------------------------------------------------------------
# hybrid
# ---------------------------------------------------------------------------

_HYBRID_SRC = '''

@tool
def retrieve_hybrid(query: str, alpha: float = 0.5, num_results: int = 8) -> str:
    """Retrieve passages from the knowledge base using HYBRID search (vector
    similarity fused with keyword/BM25 matching). Best when the user's question
    contains specific names, codes, or rare terms that pure semantic search may
    miss. alpha is a documented hint (0=keyword-leaning, 1=vector-leaning);
    Bedrock fuses the two server-side.
    """
    kb_id = _rag_kb_id()
    if not kb_id:
        return _rag_json.dumps({"error": "No KB_ID configured for this runtime.", "query": query})

    used_hybrid = True
    try:
        results = _rag_raw_retrieve(query, num_results, override_search_type="HYBRID")
    except Exception as e:
        # Not every vector store supports HYBRID (e.g. S3 Vectors managed stores
        # reject it with ValidationException). Fall back to default SEMANTIC
        # search rather than hard-failing.
        msg = str(e)
        if "ValidationException" in msg or "HYBRID" in msg or "overrideSearchType" in msg:
            used_hybrid = False
            try:
                results = _rag_raw_retrieve(query, num_results)
            except Exception as e2:
                return _rag_json.dumps({"error": "Hybrid retrieve failed: %s" % str(e2), "query": query})
        else:
            return _rag_json.dumps({"error": "Hybrid retrieve failed: %s" % msg, "query": query})

    return _rag_json.dumps({
        "query": query,
        "strategy": "hybrid" if used_hybrid else "hybrid_fallback_semantic",
        "alpha_hint": alpha,
        "results": results,
        "count": len(results),
    })
'''


# ---------------------------------------------------------------------------
# reranked
# ---------------------------------------------------------------------------

_RERANKED_SRC = '''

@tool
def retrieve_reranked(query: str, top_n: int = 20, return_n: int = 5) -> str:
    """Retrieve a wide set of candidate passages from the knowledge base, then
    use a fast judge model to re-rank them by genuine relevance to the question
    and return only the best ones. Use this when retrieval precision matters and
    the raw vector scores are noisy.
    """
    kb_id = _rag_kb_id()
    if not kb_id:
        return _rag_json.dumps({"error": "No KB_ID configured for this runtime.", "query": query})

    try:
        top_n = max(1, min(int(top_n), 50))
    except Exception:
        top_n = 20
    try:
        return_n = max(1, min(int(return_n), top_n))
    except Exception:
        return_n = min(5, top_n)

    try:
        candidates = _rag_raw_retrieve(query, top_n)
    except Exception as e:
        return _rag_json.dumps({"error": "Reranked retrieve failed: %s" % str(e), "query": query})

    if not candidates:
        return _rag_json.dumps({"query": query, "strategy": "reranked", "results": [], "count": 0})

    judge_model = _rag_os.environ.get("RERANK_JUDGE_MODEL_ID", "''' + _DEFAULT_JUDGE_MODEL_ID + '''")

    numbered = "\\n\\n".join(
        "[%d] %s" % (i, (c["text"] or "")[:1000]) for i, c in enumerate(candidates)
    )
    instr = (
        "You are a passage re-ranker. Given a user question and numbered candidate "
        "passages, return ONLY a JSON array of the passage indices ordered from most "
        "to least relevant to the question. Include at most %d indices. Example: [3,0,7].\\n\\n"
        "Question: %s\\n\\nPassages:\\n%s\\n\\nJSON array:" % (return_n, query, numbered)
    )

    order = None
    try:
        resp = _get_judge_client().converse(
            modelId=judge_model,
            messages=[{"role": "user", "content": [{"text": instr}]}],
            inferenceConfig={"maxTokens": 256, "temperature": 0.0},
        )
        text = resp["output"]["message"]["content"][0].get("text", "")
        lb = text.find("[")
        rb = text.rfind("]")
        if lb >= 0 and rb > lb:
            parsed = _rag_json.loads(text[lb:rb + 1])
            order = [int(x) for x in parsed if isinstance(x, (int, float))]
    except Exception:
        order = None

    if order:
        seen = set()
        ranked = []
        for idx in order:
            if 0 <= idx < len(candidates) and idx not in seen:
                seen.add(idx)
                ranked.append(candidates[idx])
        # Append any judge-omitted candidates by original score as a safety net.
        for i, c in enumerate(candidates):
            if i not in seen:
                ranked.append(c)
        reranked = True
    else:
        # Judge failed — fall back to the raw vector-score ordering.
        ranked = sorted(candidates, key=lambda c: c.get("score", 0.0), reverse=True)
        reranked = False

    final = ranked[:return_n]
    return _rag_json.dumps({
        "query": query,
        "strategy": "reranked" if reranked else "reranked_fallback_score",
        "judge_model": judge_model,
        "candidates_considered": len(candidates),
        "results": final,
        "count": len(final),
    })
'''


_STRATEGY_SOURCES: dict[str, str] = {
    "multi_hop": _MULTI_HOP_SRC,
    "hybrid": _HYBRID_SRC,
    "reranked": _RERANKED_SRC,
}


def _normalize_strategy(strategy: Optional[str]) -> str:
    """Normalize a raw strategy value to a known key (or 'simple')."""
    s = (strategy or "simple").strip().lower()
    return s if s in STRATEGY_TOOL_NAMES else "simple"


def agentic_rag_tool_name(strategy: Optional[str]) -> Optional[str]:
    """Return the generated tool function name for a strategy, or None for
    'simple'/absent/unknown (caller keeps the default retrieve_from_kb)."""
    s = _normalize_strategy(strategy)
    return STRATEGY_TOOL_NAMES.get(s)


def agentic_rag_tool_source(strategy: Optional[str]) -> str:
    """Return a self-contained @tool source block for the given strategy.

    Returns the empty string for 'simple'/absent/unknown strategies (the caller
    then keeps the existing single-shot retrieve_from_kb byte-for-byte).

    The returned block begins with the shared lazy-client preamble (idempotent
    per emission since only one agentic tool is emitted per agent) followed by
    the strategy-specific @tool definition. It assumes a module-level ``@tool``
    decorator is importable (every Strands built-in-tools template imports
    ``from strands import Agent, tool``).
    """
    s = _normalize_strategy(strategy)
    if s == "simple":
        return ""
    return _RAG_COMMON_PREAMBLE + _STRATEGY_SOURCES[s]

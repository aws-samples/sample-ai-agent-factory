"""Regression tests for the canonical DuckDuckGo search connector."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.services import deployment
from app.services.codegen_templates import dynamic_tools_impl as tools

_SEARCH_HTML = b"""
<html><body>
  <div class="result">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Faws.amazon.com%2Fbedrock%2Fagentcore%2F&amp;rut=abc">
      Amazon Bedrock <b>AgentCore</b> - AWS
    </a>
    <a class="result__snippet">Build and operate production-ready agents.</a>
  </div>
  <div class="result">
    <a class="result__a" href="https://docs.aws.amazon.com/bedrock-agentcore/">Documentation</a>
    <div class="result__snippet">Developer guide.</div>
  </div>
</body></html>
"""


def _install_response(monkeypatch: pytest.MonkeyPatch, body: bytes) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []

    def fake_get(url: str, timeout: int = 10, retries: int = 2) -> bytes:
        del retries
        calls.append((url, timeout))
        return body

    monkeypatch.setattr(tools, "_http_get", fake_get)
    return calls


def test_duckduckgo_search_uses_real_html_results(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_response(monkeypatch, _SEARCH_HTML)

    result = json.loads(tools._do_duckduckgo_search("Amazon Bedrock AgentCore"))

    assert calls == [
        (
            "https://html.duckduckgo.com/html/?q=Amazon+Bedrock+AgentCore",
            12,
        )
    ]
    assert result == [
        {
            "title": "Amazon Bedrock AgentCore - AWS",
            "snippet": "Build and operate production-ready agents.",
            "url": "https://aws.amazon.com/bedrock/agentcore/",
        },
        {
            "title": "Documentation",
            "snippet": "Developer guide.",
            "url": "https://docs.aws.amazon.com/bedrock-agentcore/",
        },
    ]


def test_duckduckgo_search_rejects_unsafe_result_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"""
    <a class="result__a" href="https://example.com/safe">safe</a>
    <a class="result__snippet">allowed</a>
    <a class="result__a" href="javascript:alert(1)">unsafe</a>
    <a class="result__snippet">must not overwrite the safe result</a>
    """
    _install_response(monkeypatch, body)

    result = json.loads(tools._do_duckduckgo_search("safety"))

    assert result == [
        {
            "title": "safe",
            "snippet": "allowed",
            "url": "https://example.com/safe",
        }
    ]


def test_duckduckgo_search_empty_query_fails_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    network = pytest.fail
    monkeypatch.setattr(tools, "_http_get", network)

    result = json.loads(tools._do_duckduckgo_search("  "))

    assert result == {"error": "A non-empty search query is required"}


def test_mcp_codegen_reuses_canonical_search_implementation() -> None:
    generated = deployment.generate_mcp_server_code(
        server_name="search",
        tools=["duckduckgo_search"],
    )

    assert "https://html.duckduckgo.com/html/?" in generated
    assert "return _do_duckduckgo_search(query)" in generated
    assert "https://api.duckduckgo.com/" not in generated


def test_removal_mutation_reintroduces_unsafe_search_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = Path(tools.__file__)
    source = source_path.read_text(encoding="utf-8")
    guard = '        if parsed.scheme not in {"http", "https"} or not parsed.hostname:\n            return ""\n'
    assert source.count(guard) == 1
    mutant = source.replace(guard, "", 1)
    namespace: dict[str, Any] = {"__name__": "duckduckgo_url_guard_mutant"}
    exec(compile(mutant, str(source_path), "exec"), namespace)
    monkeypatch.setitem(
        namespace,
        "_http_get",
        lambda *_args, **_kwargs: (
            b'<a class="result__a" href="javascript:alert(1)">unsafe</a><a class="result__snippet">escaped</a>'
        ),
    )

    result = json.loads(namespace["_do_duckduckgo_search"]("safety"))

    assert result[0]["url"].startswith("javascript:")

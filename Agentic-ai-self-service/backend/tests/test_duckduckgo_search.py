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

_LITE_SEARCH_HTML = b"""
<html><body>
  <a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Faws.amazon.com%2Fbedrock%2Fagentcore%2F">
    Amazon Bedrock AgentCore - AWS
  </a>
  <td class="result-snippet">Build and operate production-ready agents.</td>
  <a class="result-link" href="https://docs.aws.amazon.com/bedrock-agentcore/">Documentation</a>
  <td class="result-snippet">Developer guide.</td>
</body></html>
"""


def _install_responses(
    monkeypatch: pytest.MonkeyPatch,
    bodies: list[bytes],
) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []
    remaining = iter(bodies)

    def fake_get(url: str, timeout: int = 10, retries: int = 2) -> bytes:
        del retries
        calls.append((url, timeout))
        try:
            return next(remaining)
        except StopIteration as error:
            raise AssertionError(f"Unexpected extra search request: {url}") from error

    monkeypatch.setattr(tools, "_http_get", fake_get)
    return calls


def _install_response(monkeypatch: pytest.MonkeyPatch, body: bytes) -> list[tuple[str, int]]:
    return _install_responses(monkeypatch, [body])


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


def test_duckduckgo_search_falls_back_to_lite_for_unexpected_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_responses(
        monkeypatch,
        [b'<html><form id="challenge-form">unavailable</form></html>', _LITE_SEARCH_HTML],
    )

    result = json.loads(tools._do_duckduckgo_search("Amazon Bedrock AgentCore"))

    assert calls == [
        ("https://html.duckduckgo.com/html/?q=Amazon+Bedrock+AgentCore", 12),
        ("https://lite.duckduckgo.com/lite/?q=Amazon+Bedrock+AgentCore", 12),
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


def test_duckduckgo_search_returns_no_results_only_for_explicit_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_responses(
        monkeypatch,
        [
            b"<html><body>unexpected primary response</body></html>",
            b"<html><body>No results found for the quoted query</body></html>",
        ],
    )

    result = json.loads(tools._do_duckduckgo_search("quoted impossible query"))

    assert result == {"message": "No results found for: quoted impossible query"}


def test_duckduckgo_search_fails_closed_for_two_unexpected_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_responses(
        monkeypatch,
        [
            b"<html><body>This page may have no results.</body></html>",
            b"<html><body>unexpected fallback response</body></html>",
        ],
    )

    with pytest.raises(tools.ToolUnavailable, match="unavailable or unexpected"):
        tools._do_duckduckgo_search("Amazon Bedrock AgentCore")


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
    assert "https://lite.duckduckgo.com/lite/?" in generated
    assert "return _do_duckduckgo_search(query)" in generated
    assert "https://api.duckduckgo.com/" not in generated


def test_removal_mutation_disables_lite_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    source_path = Path(tools.__file__)
    source = source_path.read_text(encoding="utf-8")
    fallback = '    ("https://lite.duckduckgo.com/lite/?", "result-link", "result-snippet"),\n'
    assert source.count(fallback) == 1
    mutant = source.replace(fallback, "", 1)
    namespace: dict[str, Any] = {"__name__": "duckduckgo_fallback_mutant"}
    exec(compile(mutant, str(source_path), "exec"), namespace)
    responses = iter(
        [
            b"<html><body>unexpected primary response</body></html>",
            _LITE_SEARCH_HTML,
        ]
    )
    monkeypatch.setitem(namespace, "_http_get", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(namespace["ToolUnavailable"], match="unavailable or unexpected"):
        namespace["_do_duckduckgo_search"]("Amazon Bedrock AgentCore")


def test_removal_mutation_reintroduces_unsafe_search_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = Path(tools.__file__)
    source = source_path.read_text(encoding="utf-8")
    guard = '    if parsed.scheme not in {"http", "https"} or not parsed.hostname:\n        return ""\n'
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

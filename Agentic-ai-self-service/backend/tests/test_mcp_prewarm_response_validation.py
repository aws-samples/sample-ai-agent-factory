"""An MCP prewarm is successful only after a valid initialize result.

HTTP 2xx proves that the AgentCore data-plane endpoint answered. It does not
prove that the MCP server initialized: streamable HTTP can return a JSON-RPC
error in either a JSON body or an SSE ``data:`` frame. Accepting that response
would let deployment continue to gateway discovery with a runtime already
known not to speak MCP successfully.
"""

from __future__ import annotations

import json
import urllib.request

import pytest
from app.step_handlers import mcp_server_step


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self._body
        return self._body[:size]


def _prewarm_with_response(monkeypatch, body: bytes) -> bool:
    responses = [
        _Response(json.dumps({"access_token": "token"}).encode()),
        _Response(body),
    ]
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: responses.pop(0),
    )

    return mcp_server_step._prewarm_mcp_runtime(
        "us-east-1",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/mcp",
        "https://pool.auth.us-east-1.amazoncognito.com/oauth2/token",
        "client-id",
        "client-secret",
        "resource/invoke",
        attempts=1,
    )


@pytest.mark.parametrize(
    "body",
    [
        b'{"jsonrpc":"2.0","id":1,"result":{}}',
        b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n',
        (
            b"event: ping\ndata: {}\n\n"
            b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26"}}\n\n'
        ),
    ],
)
def test_prewarm_accepts_only_a_matching_initialize_result(monkeypatch, body):
    assert _prewarm_with_response(monkeypatch, body) is True


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{}",
        b'{"jsonrpc":"2.0","id":2,"result":{}}',
        b'{"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"initialization failed"}}',
        b'event: message\ndata: {"jsonrpc":"2.0","id":1,"error":{"code":-32603}}\n\n',
        b"event: message\ndata: not-json\n\n",
        b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":[]}\n\n',
    ],
)
def test_http_success_without_a_valid_initialize_result_is_not_warm(monkeypatch, body):
    assert _prewarm_with_response(monkeypatch, body) is False


def test_oversized_initialize_response_is_rejected(monkeypatch):
    oversized = b'{"jsonrpc":"2.0","id":1,"result":{"padding":"' + (b"x" * (1024 * 1024)) + b'"}}'

    assert _prewarm_with_response(monkeypatch, oversized) is False

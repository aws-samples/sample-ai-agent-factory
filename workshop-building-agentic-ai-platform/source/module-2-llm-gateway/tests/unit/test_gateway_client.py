# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the LLM Gateway Python client (LiteLLM Proxy)."""

from __future__ import annotations

import json

import pytest
import responses

from llm_gateway_client import LLMGatewayClient
from llm_gateway_client.models import (
    ChatCompletionResponse,
    KeyResponse,
    ModelsListResponse,
    TeamResponse,
)


class TestLLMGatewayClientInit:
    def test_strips_trailing_slash(self, proxy_url: str, api_key: str):
        client = LLMGatewayClient(proxy_url=proxy_url + "/", api_key=api_key)
        assert client.proxy_url == proxy_url

    def test_stores_api_key(self, proxy_url: str, api_key: str):
        client = LLMGatewayClient(proxy_url=proxy_url, api_key=api_key)
        assert client.api_key == api_key

    def test_default_timeout(self, proxy_url: str):
        client = LLMGatewayClient(proxy_url=proxy_url)
        assert client.timeout == 60


class TestChatCompletion:
    @responses.activate
    def test_sends_correct_payload(self, proxy_url: str, api_key: str):
        responses.add(
            responses.POST,
            f"{proxy_url}/chat/completions",
            json={
                "id": "chatcmpl-123",
                "object": "chat.completion",
                "model": "claude-sonnet",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hello!"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
            status=200,
        )

        client = LLMGatewayClient(proxy_url=proxy_url, api_key=api_key)
        result = client.chat_completion(
            messages=[{"role": "user", "content": "Hi"}],
            model="claude-sonnet",
            temperature=0.5,
            max_tokens=100,
        )

        assert isinstance(result, ChatCompletionResponse)
        assert result.choices[0].message.content == "Hello!"
        assert result.usage.total_tokens == 15

        req_body = json.loads(responses.calls[0].request.body)
        assert req_body["model"] == "claude-sonnet"
        assert req_body["temperature"] == 0.5
        assert req_body["max_tokens"] == 100

    @responses.activate
    def test_includes_auth_header(self, proxy_url: str, api_key: str):
        responses.add(
            responses.POST,
            f"{proxy_url}/chat/completions",
            json={
                "id": "x",
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ],
            },
            status=200,
        )

        client = LLMGatewayClient(proxy_url=proxy_url, api_key=api_key)
        client.chat_completion(messages=[{"role": "user", "content": "test"}])

        auth = responses.calls[0].request.headers.get("Authorization")
        assert auth == f"Bearer {api_key}"

    @responses.activate
    def test_no_auth_header_when_no_key(self, proxy_url: str):
        responses.add(
            responses.POST,
            f"{proxy_url}/chat/completions",
            json={
                "id": "x",
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ],
            },
            status=200,
        )

        client = LLMGatewayClient(proxy_url=proxy_url)
        client.chat_completion(messages=[{"role": "user", "content": "test"}])

        auth = responses.calls[0].request.headers.get("Authorization")
        assert auth is None

    @responses.activate
    def test_raises_on_error(self, proxy_url: str):
        responses.add(
            responses.POST,
            f"{proxy_url}/chat/completions",
            json={"error": "unauthorized"},
            status=401,
        )

        client = LLMGatewayClient(proxy_url=proxy_url)
        with pytest.raises(Exception):
            client.chat_completion(messages=[{"role": "user", "content": "test"}])


class TestChat:
    @responses.activate
    def test_convenience_method(self, proxy_url: str):
        responses.add(
            responses.POST,
            f"{proxy_url}/chat/completions",
            json={
                "id": "x",
                "choices": [
                    {"message": {"role": "assistant", "content": "Hi there!"}}
                ],
            },
            status=200,
        )

        client = LLMGatewayClient(proxy_url=proxy_url)
        result = client.chat("Hello")
        assert result == "Hi there!"

    @responses.activate
    def test_includes_system_message(self, proxy_url: str):
        responses.add(
            responses.POST,
            f"{proxy_url}/chat/completions",
            json={
                "id": "x",
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ],
            },
            status=200,
        )

        client = LLMGatewayClient(proxy_url=proxy_url)
        client.chat("Hello", system="You are helpful.")

        req_body = json.loads(responses.calls[0].request.body)
        assert len(req_body["messages"]) == 2
        assert req_body["messages"][0]["role"] == "system"
        assert req_body["messages"][1]["role"] == "user"


class TestListModels:
    @responses.activate
    def test_returns_models(self, proxy_url: str):
        responses.add(
            responses.GET,
            f"{proxy_url}/models",
            json={
                "object": "list",
                "data": [
                    {"id": "claude-sonnet", "object": "model", "owned_by": "openai"},
                    {"id": "nova-2-lite", "object": "model", "owned_by": "openai"},
                ],
            },
            status=200,
        )

        client = LLMGatewayClient(proxy_url=proxy_url)
        result = client.list_models()

        assert isinstance(result, ModelsListResponse)
        assert len(result.data) == 2
        assert result.data[0].id == "claude-sonnet"


class TestHealthCheck:
    @responses.activate
    def test_health_ok(self, proxy_url: str):
        responses.add(
            responses.GET,
            f"{proxy_url}/health/liveliness",
            json={"status": "healthy"},
            status=200,
        )

        client = LLMGatewayClient(proxy_url=proxy_url)
        result = client.health_check()
        assert result["status"] == "healthy"


class TestModelHealth:
    @responses.activate
    def test_model_health(self, proxy_url: str, api_key: str):
        responses.add(
            responses.GET,
            f"{proxy_url}/health",
            json={
                "healthy_endpoints": [{"model": "claude-sonnet"}],
                "unhealthy_endpoints": [],
            },
            status=200,
        )

        client = LLMGatewayClient(proxy_url=proxy_url, api_key=api_key)
        result = client.model_health()
        assert len(result["healthy_endpoints"]) == 1


class TestVirtualKeys:
    @responses.activate
    def test_create_key(self, proxy_url: str, api_key: str):
        responses.add(
            responses.POST,
            f"{proxy_url}/key/generate",
            json={
                "key": "sk-new-virtual-key-xyz",
                "token": "sk-new-virtual-key-xyz",
                "key_name": "test-key",
                "team_id": "",
                "max_budget": 5.0,
            },
            status=200,
        )

        client = LLMGatewayClient(proxy_url=proxy_url, api_key=api_key)
        result = client.create_key(models=["claude-sonnet"], max_budget=5.0)

        assert isinstance(result, KeyResponse)
        assert result.key == "sk-new-virtual-key-xyz"
        assert result.max_budget == 5.0

    @responses.activate
    def test_get_key_info(self, proxy_url: str, api_key: str):
        responses.add(
            responses.GET,
            f"{proxy_url}/key/info",
            json={"key": "sk-test", "spend": 0.05, "max_budget": 5.0},
            status=200,
        )

        client = LLMGatewayClient(proxy_url=proxy_url, api_key=api_key)
        result = client.get_key_info("sk-test")
        assert result["spend"] == 0.05

    @responses.activate
    def test_get_key_identity_with_metadata(self, proxy_url: str, api_key: str):
        responses.add(
            responses.GET,
            f"{proxy_url}/key/info",
            json={
                "key": "sk-test",
                "info": {
                    "metadata": {
                        "cognito_group": "developers",
                        "cognito_user_pool_id": "us-west-2_AbCdEf",
                        "identity_provider": "cognito",
                    }
                },
            },
            status=200,
        )

        client = LLMGatewayClient(proxy_url=proxy_url, api_key=api_key)
        identity = client.get_key_identity("sk-test")
        assert identity["cognito_group"] == "developers"
        assert identity["cognito_user_pool_id"] == "us-west-2_AbCdEf"
        assert identity["identity_provider"] == "cognito"

    @responses.activate
    def test_get_key_identity_without_metadata(self, proxy_url: str, api_key: str):
        responses.add(
            responses.GET,
            f"{proxy_url}/key/info",
            json={"key": "sk-test", "info": {}},
            status=200,
        )

        client = LLMGatewayClient(proxy_url=proxy_url, api_key=api_key)
        identity = client.get_key_identity("sk-test")
        assert identity["cognito_group"] == ""
        assert identity["identity_provider"] == ""


class TestTeams:
    @responses.activate
    def test_create_team(self, proxy_url: str, api_key: str):
        responses.add(
            responses.POST,
            f"{proxy_url}/team/new",
            json={
                "team_id": "team-abc123",
                "team_alias": "platform-team",
                "max_budget": 10.0,
                "models": ["claude-sonnet"],
            },
            status=200,
        )

        client = LLMGatewayClient(proxy_url=proxy_url, api_key=api_key)
        result = client.create_team(
            team_alias="platform-team",
            models=["claude-sonnet"],
            max_budget=10.0,
        )

        assert isinstance(result, TeamResponse)
        assert result.team_alias == "platform-team"
        assert result.team_id == "team-abc123"


class TestSpendTracking:
    @responses.activate
    def test_get_spend_logs(self, proxy_url: str, api_key: str):
        responses.add(
            responses.GET,
            f"{proxy_url}/spend/logs",
            json=[
                {"model": "claude-sonnet", "total_tokens": 100, "spend": 0.01},
                {"model": "nova-2-lite", "total_tokens": 50, "spend": 0.005},
            ],
            status=200,
        )

        client = LLMGatewayClient(proxy_url=proxy_url, api_key=api_key)
        result = client.get_spend_logs()

        assert len(result) == 2
        assert result[0]["model"] == "claude-sonnet"
        assert result[0]["spend"] == 0.01

    @responses.activate
    def test_get_spend_logs_wrapped(self, proxy_url: str, api_key: str):
        responses.add(
            responses.GET,
            f"{proxy_url}/spend/logs",
            json={"data": [{"model": "claude-sonnet", "spend": 0.02}]},
            status=200,
        )

        client = LLMGatewayClient(proxy_url=proxy_url, api_key=api_key)
        result = client.get_spend_logs()

        assert len(result) == 1
        assert result[0]["spend"] == 0.02

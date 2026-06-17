# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Python client for the LiteLLM Proxy API.

Wraps the LiteLLM proxy endpoints for use in workshop exercises, helper
scripts, and the Jupyter notebook walkthrough.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from llm_gateway_client.models import (
    ChatCompletionResponse,
    KeyResponse,
    ModelsListResponse,
    TeamResponse,
)

logger = logging.getLogger(__name__)


class LLMGatewayClient:
    """Client for the LiteLLM Proxy (LLM Gateway).

    Args:
        proxy_url: Base URL of the LiteLLM proxy — use the HTTPS API Gateway
            endpoint (e.g. https://<api-gateway-endpoint>).
        api_key: Virtual key or admin key for authentication.
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        proxy_url: str,
        api_key: str = "",
        timeout: int = 60,
    ) -> None:
        self.proxy_url = proxy_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._session = requests.Session()

    @property
    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    # -----------------------------------------------------------------
    # Chat Completions (OpenAI-compatible)
    # -----------------------------------------------------------------
    def chat_completion(
        self,
        messages: list[dict[str, str]],
        model: str = "claude-sonnet",
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> ChatCompletionResponse:
        """Send a chat completion request through the LiteLLM proxy."""
        url = f"{self.proxy_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }
        resp = self._session.post(
            url, headers=self._headers, json=payload, timeout=self.timeout
        )
        resp.raise_for_status()
        return ChatCompletionResponse.model_validate(resp.json())

    def chat(
        self,
        prompt: str,
        model: str = "claude-sonnet",
        system: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Convenience: send a single user message and return the text."""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        result = self.chat_completion(messages, model=model, **kwargs)
        if result.choices:
            return result.choices[0].message.content
        return ""

    # -----------------------------------------------------------------
    # Models
    # -----------------------------------------------------------------
    def list_models(self) -> ModelsListResponse:
        """List available models through the proxy."""
        url = f"{self.proxy_url}/models"
        resp = self._session.get(url, headers=self._headers, timeout=self.timeout)
        resp.raise_for_status()
        return ModelsListResponse.model_validate(resp.json())

    # -----------------------------------------------------------------
    # Virtual Keys (requires admin key)
    # -----------------------------------------------------------------
    def create_key(
        self,
        models: list[str] | None = None,
        team_id: str = "",
        max_budget: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> KeyResponse:
        """Create a virtual key via /key/generate."""
        url = f"{self.proxy_url}/key/generate"
        payload: dict[str, Any] = {}
        if models:
            payload["models"] = models
        if team_id:
            payload["team_id"] = team_id
        if max_budget is not None:
            payload["max_budget"] = max_budget
        if metadata:
            payload["metadata"] = metadata
        resp = self._session.post(
            url, headers=self._headers, json=payload, timeout=self.timeout
        )
        resp.raise_for_status()
        return KeyResponse.model_validate(resp.json())

    def get_key_info(self, key: str) -> dict[str, Any]:
        """Get info about a virtual key including spend."""
        url = f"{self.proxy_url}/key/info"
        resp = self._session.get(
            url,
            headers=self._headers,
            params={"key": key},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_key_identity(self, key: str) -> dict[str, str]:
        """Get the Cognito identity mapping for a virtual key.

        Returns the identity metadata attached during setup (cognito_group,
        cognito_user_pool_id, identity_provider). Empty strings if no
        identity mapping exists.
        """
        info = self.get_key_info(key)
        metadata = info.get("info", {}).get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        return {
            "cognito_group": metadata.get("cognito_group", ""),
            "cognito_user_pool_id": metadata.get("cognito_user_pool_id", ""),
            "identity_provider": metadata.get("identity_provider", ""),
        }

    # -----------------------------------------------------------------
    # Teams (requires admin key)
    # -----------------------------------------------------------------
    def create_team(
        self,
        team_alias: str,
        models: list[str] | None = None,
        max_budget: float | None = None,
    ) -> TeamResponse:
        """Create a team via /team/new."""
        url = f"{self.proxy_url}/team/new"
        payload: dict[str, Any] = {"team_alias": team_alias}
        if models:
            payload["models"] = models
        if max_budget is not None:
            payload["max_budget"] = max_budget
        resp = self._session.post(
            url, headers=self._headers, json=payload, timeout=self.timeout
        )
        resp.raise_for_status()
        return TeamResponse.model_validate(resp.json())

    # -----------------------------------------------------------------
    # Spend Tracking
    # -----------------------------------------------------------------
    def get_spend_logs(self, **params: Any) -> list[dict[str, Any]]:
        """Get spend logs from /spend/logs."""
        url = f"{self.proxy_url}/spend/logs"
        resp = self._session.get(
            url, headers=self._headers, params=params, timeout=self.timeout
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("data", [])

    # -----------------------------------------------------------------
    # Health
    # -----------------------------------------------------------------
    def health_check(self) -> dict[str, Any]:
        """Check the proxy health endpoint."""
        url = f"{self.proxy_url}/health/liveliness"
        resp = self._session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def model_health(self) -> dict[str, Any]:
        """Check health of configured models."""
        url = f"{self.proxy_url}/health"
        resp = self._session.get(url, headers=self._headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

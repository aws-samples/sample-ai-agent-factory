# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared pytest fixtures for LLM Gateway tests."""

from __future__ import annotations

import pytest
import responses


@pytest.fixture
def proxy_url() -> str:
    # Mocked test endpoint (never dialed); real deployments use HTTPS.
    return "https://llm-gateway-test.example.com"


@pytest.fixture
def api_key() -> str:
    return "sk-test-api-key-12345"


@pytest.fixture
def mock_responses():
    """Activate the responses library to mock HTTP requests."""
    with responses.RequestsMock() as rsps:
        yield rsps

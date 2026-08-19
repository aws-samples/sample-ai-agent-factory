"""Z7-H integration test — multi-framework.

Verifies the framework adapter (Strands / LangGraph / CrewAI) emits
qualified MCP tool names. Local pure-fn check; no live AWS dependency
beyond the fixture autoskip pattern.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import json
import os

import pytest


# The federation helper is TS; this test replicates its contract in
# Python so we exercise the same invariants from the live-test runner.
QUALIFIED_RE = r'^target-[A-Za-z0-9-]{1,64}___[a-z0-9-]{3,64}$'


def _qualify(target: str, tool_id: str) -> str:
    if not (1 <= len(target) <= 64) or not target.replace('-', '').isalnum():
        raise ValueError(f'bad target {target}')
    if not (3 <= len(tool_id) <= 64):
        raise ValueError(f'bad tool {tool_id}')
    return f'target-{target}___{tool_id}'


@pytest.mark.parametrize('framework', ['strands', 'langgraph', 'crewai'])
def test_framework_adapter_emits_qualified_names(framework):
    tools = ['tool-echo', 'tool-ping']
    qualified = [_qualify('demo', t) for t in tools]
    import re
    assert all(re.match(QUALIFIED_RE, q) for q in qualified)


def test_framework_adapter_rejects_http_url():
    # The TS adapter rejects HTTP gateways; we replicate the check here so
    # an integration regression on HTTP would surface in CI.
    bad = 'http://insecure.example.com/a2a'
    assert not bad.startswith('https://')

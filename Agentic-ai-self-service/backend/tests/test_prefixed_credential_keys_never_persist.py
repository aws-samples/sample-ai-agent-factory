"""F-11: a PREFIXED credential key (``GITHUB_TOKEN``, ``OPENAI_API_KEY``, ``X-Api-Key``) is a
credential key.

``credential_scrub`` matched the canonical key by exact membership, so ``GITHUB_TOKEN``
(``githubtoken``) and ``X-Api-Key`` (``xapikey``) sailed through the persistence boundary and were
stored in plaintext in the workflow and flow tables, then returned on every GET.
``error_sanitizer`` had already grown a leading ``[a-z0-9_]*`` for exactly this class of name
(``PROVIDER_API_KEY``, ``GATEWAY_API_KEY``); the persistence boundary had not.

ARCC cnt_dwzZ05hLnqhYXQ (a plaintext secret accepted as API input is the anti-pattern) and
cnt_n8LpZcqYi2t3I2 (the value lives in Secrets Manager and is reached by reference).

The widening is a PREFIX only, mirroring error_sanitizer's deliberate choice not to add a trailing
wildcard: ``apiKeyRef``, ``tokenEndpoint``, ``secretName`` and ``token_count`` are references,
locators and counters, and they must survive.
"""

from __future__ import annotations

import json

import pytest
from app.models.flow import FlowUpdateRequest
from app.services.credential_scrub import (
    contains_write_only_credential,
    credential_token_for,
    is_write_only_credential_key,
    scrub_write_only_credentials,
)
from app.services.flow_storage import _serialize_flow

SENTINEL = "ghp_FAKE-prefixed-sentinel-NEVER-PERSIST-0123456789"
ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:llm-AbCdEf"

#: The keys the review named, plus the spellings this platform itself sets on a runtime.
PREFIXED_CREDENTIAL_KEYS = [
    "GITHUB_TOKEN",
    "github_token",
    "OPENAI_API_KEY",
    "openaiApiKey",
    "X-Api-Key",
    "x-api-key",
    "X_API_KEY",
    "slack_token",
    "SLACK_BOT_TOKEN",
    "connection_string",
    "CONNECTION_STRING",
    "connectionString",
    "PROVIDER_API_KEY",
    "GATEWAY_API_KEY",
    "LITELLM_MASTER_KEY_PASSWORD",
    "db_password",
    "DATABASE_PASSWORD",
    "aws_secret_key",
    "jwt_secret",
    "proxy-authorization",
    "x-auth-token",
    "rsa_private_key",
]

#: Names that END with a token-looking word but are not credential values. The trailing-wildcard
#: shapes (``*Ref``, ``*Endpoint``, ``*Count``) and the pagination cursors.
LEGITIMATE_KEYS = {
    "token_count": 128,
    "tokens_used": 12,
    "tokensUsed": 12,
    "maxTokens": 4096,
    "max_tokens": 4096,
    "input_tokens": 10,
    "output_tokens": 20,
    "total_tokens": 30,
    "tokenEndpoint": "https://idp.example/oauth2/token",
    "token_endpoint": "https://idp.example/oauth2/token",
    "tokenUrl": "https://idp.example/token",
    "apiKeyRef": ARN,
    "OPENAI_API_KEY_REF": ARN,
    "github_token_ref": ARN,
    "githubTokenArn": ARN,
    "clientSecretRef": ARN,
    "apiKeyHeader": "X-API-Key",
    "api_key_header_name": "X-API-Key",
    "secretName": "llm-key",
    "secretId": "llm-key",
    "secretArn": ARN,
    "passwordField": "pw",
    "nextToken": "opaque-cursor-1",
    "NextToken": "opaque-cursor-1",
    "continuationToken": "opaque-cursor-2",
    "paginationToken": "opaque-cursor-3",
    "tokenizer": "cl100k",
    "key": "budget-key",
    "accessKeyId": "AKIAFAKEFAKEFAKEFAKE",
    "keyName": "my-key",
}

#: Flags and counters whose NAME ends with a token but whose VALUE is not a string. A boolean
#: ``showPassword`` or an integer ``max_token`` is UI/config metadata, not a grant.
NON_STRING_UNDER_PREFIXED_KEY = {
    "showPassword": True,
    "requiresApiKey": False,
    "hasToken": True,
    "isSecret": True,
    "max_token": 4096,
    "retry_token": None,
}


@pytest.mark.parametrize("key", PREFIXED_CREDENTIAL_KEYS)
def test_a_prefixed_credential_key_is_a_credential_key(key):
    assert is_write_only_credential_key(key), key
    assert credential_token_for(key) is not None, key


@pytest.mark.parametrize("key", sorted(LEGITIMATE_KEYS))
def test_a_reference_locator_or_counter_is_never_a_credential_key(key):
    assert not is_write_only_credential_key(key), key
    assert credential_token_for(key) is None, key


@pytest.mark.parametrize("key", PREFIXED_CREDENTIAL_KEYS)
def test_a_string_under_a_prefixed_key_is_dropped_at_every_depth(key):
    record = {
        "env": {key: SENTINEL},
        "nodes": [{"data": {"configuration": {"extraHeaders": {key: SENTINEL}}}}],
        key: SENTINEL,
    }
    out = scrub_write_only_credentials(record)
    assert SENTINEL not in json.dumps(out), key
    assert key not in out and key not in out["env"]
    assert key not in out["nodes"][0]["data"]["configuration"]["extraHeaders"]
    assert contains_write_only_credential(record)


def test_the_review_scenarios_end_to_end():
    """An MCP node with ``env: {GITHUB_TOKEN}`` and an Observability node with
    ``extraHeaders: {X-Api-Key}`` -- the two shapes the review named -- through the real flow
    request model and the real flow serializer that autosave writes."""
    from datetime import UTC, datetime

    from app.models.flow import Flow

    workflow = {
        "nodes": [
            {
                "id": "mcp-1",
                "type": "mcp",
                "data": {"componentType": "mcp", "configuration": {"env": {"GITHUB_TOKEN": SENTINEL}}},
            },
            {
                "id": "obs-1",
                "type": "observability",
                "data": {
                    "componentType": "observability",
                    "configuration": {"extraHeaders": {"X-Api-Key": SENTINEL, "Accept": "application/json"}},
                },
            },
            {
                "id": "db-1",
                "type": "tool",
                "data": {"componentType": "tool", "configuration": {"connection_string": SENTINEL}},
            },
        ],
        "edges": [],
    }
    request = FlowUpdateRequest(name="f", workflow=workflow)
    dumped = request.model_dump(mode="json")
    assert SENTINEL not in json.dumps(dumped), "the request model itself must already have dropped the value"
    nodes = dumped["workflow"]["nodes"]
    assert nodes[1]["data"]["configuration"]["extraHeaders"] == {"Accept": "application/json"}
    assert "env" in nodes[0]["data"]["configuration"] and nodes[0]["data"]["configuration"]["env"] == {}
    assert nodes[2]["data"]["configuration"] == {}

    now = datetime(2026, 9, 28, tzinfo=UTC)
    stored = _serialize_flow(
        Flow(id="c1c4b6a6-4f0c-4a6b-9d0e-0f0e0d0c0b0a", name="f", workflow=workflow, created_at=now, updated_at=now)
    )
    assert SENTINEL not in json.dumps(stored, default=str)


def test_legitimate_keys_survive_the_scrub_with_their_values():
    out = scrub_write_only_credentials(dict(LEGITIMATE_KEYS))
    assert out == LEGITIMATE_KEYS
    assert not contains_write_only_credential(LEGITIMATE_KEYS)


def test_non_string_values_under_a_prefixed_key_survive():
    """A prefixed name is a heuristic; a boolean or number under it cannot be a credential, and
    dropping ``showPassword: true`` would silently change UI behaviour."""
    out = scrub_write_only_credentials(dict(NON_STRING_UNDER_PREFIXED_KEY))
    assert out == NON_STRING_UNDER_PREFIXED_KEY
    assert not contains_write_only_credential(NON_STRING_UNDER_PREFIXED_KEY)


def test_an_exact_token_still_drops_every_scalar():
    """The value-shape allowance is for the PREFIXED class only. ``password: 1234`` was dropped
    before this change and still is."""
    out = scrub_write_only_credentials({"password": 1234, "token": True, "apiKey": None})
    assert out == {}


def test_a_reference_under_a_prefixed_key_object_survives():
    out = scrub_write_only_credentials({"GITHUB_TOKEN": {"value": SENTINEL, "secretArn": ARN}})
    assert out == {"GITHUB_TOKEN": {"secretArn": ARN}}


def test_a_prefixed_key_inside_a_credentials_container_is_dropped():
    out = scrub_write_only_credentials(
        {"credentials": {"clientId": "abc", "OPENAI_API_KEY": SENTINEL, "openaiApiKeyRef": ARN}}
    )
    assert out == {"credentials": {"clientId": "abc", "openaiApiKeyRef": ARN}}


def test_the_warning_names_the_canonical_token_never_the_prefix(caplog):
    """The log names fields from the closed vocabulary. A caller-controlled prefix
    (``ACME_PROD_...``) is caller text and stays out of CloudWatch."""
    with caplog.at_level("WARNING", logger="app.services.credential_scrub"):
        scrub_write_only_credentials({"ACME_PROD_GITHUB_TOKEN": SENTINEL})
    messages = [r.getMessage() for r in caplog.records if "write-only credential" in r.getMessage()]
    assert messages, caplog.text
    assert "token" in messages[0]
    assert "acme" not in messages[0].lower()
    assert SENTINEL not in messages[0]

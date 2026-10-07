"""Contract tests for workforce JWT -> Runtime -> Gateway OBO handoff."""

from __future__ import annotations

import base64
import json
import time
from unittest.mock import MagicMock

import pytest
from app.services import delegated_identity as di
from app.services import runtime_invocation as ri


def _jwt(*, exp: float | None = None, sub: str = "user-123", issuer: str = "https://issuer.example") -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","kid":"k1"}').rstrip(b"=").decode()
    claims = {"sub": sub, "iss": issuer, "exp": exp if exp is not None else time.time() + 300}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.signature"


def test_obo_is_explicit_and_non_obo_is_unchanged():
    assert di.obo_requested([{"delegationMode": "obo"}]) is True
    assert di.obo_requested([{"delegation_mode": "OBO"}]) is True
    assert di.obo_requested([{"delegationMode": "m2m"}, {}]) is False
    assert di.obo_requested(None) is False
    assert di.deployment_requires_obo({"gateway_result": {"obo_required": True}}) is True
    assert di.deployment_requires_obo({"gateway_result": {}}) is False


def test_workforce_authorizer_has_no_machine_credential(monkeypatch):
    monkeypatch.setenv("WORKFORCE_USER_POOL_ID", "eu-west-1_pool123")
    monkeypatch.setenv("WORKFORCE_CLIENT_ID", "workforce-client")
    monkeypatch.setenv("WORKFORCE_REGION", "eu-west-1")

    identity = di.workforce_gateway_identity("eu-west-1")

    assert identity["authorizer_config"] == {
        "customJWTAuthorizer": {
            "discoveryUrl": (
                "https://cognito-idp.eu-west-1.amazonaws.com/"
                "eu-west-1_pool123/.well-known/openid-configuration"
            ),
            "allowedClients": ["workforce-client"],
        }
    }
    assert identity["client_info"] == {
        "provider": "workforce_jwt",
        "delegation_mode": "obo",
    }
    serialized = json.dumps(identity).lower()
    assert "secret" not in serialized
    assert "client_credentials" not in serialized


def test_workforce_authorizer_fails_closed_when_stack_wiring_is_missing(monkeypatch):
    monkeypatch.delenv("WORKFORCE_USER_POOL_ID", raising=False)
    monkeypatch.delenv("WORKFORCE_CLIENT_ID", raising=False)
    with pytest.raises(di.DelegatedIdentityError, match="workforce"):
        di.workforce_gateway_identity("eu-west-1")


def test_runtime_authorizer_uses_exact_supported_header_shape(monkeypatch):
    monkeypatch.setenv("WORKFORCE_USER_POOL_ID", "eu-west-1_pool123")
    monkeypatch.setenv("WORKFORCE_CLIENT_ID", "workforce-client")
    monkeypatch.setenv("WORKFORCE_REGION", "eu-west-1")
    authorizer, headers = di.runtime_obo_authorizer("eu-west-1")
    assert authorizer["customJWTAuthorizer"]["allowedClients"] == ["workforce-client"]
    assert headers == {"requestHeaderAllowlist": ["Authorization"]}


def test_fresh_user_bearer_is_accepted():
    token = _jwt(exp=200)
    assert di.extract_user_bearer({"Authorization": f"Bearer {token}"}, now=100) == token


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Basic dXNlcjpwYXNz",
        "AWS4-HMAC-SHA256 Credential=AKIA/20261007/eu-west-1/service/aws4_request",
        "Bearer junk",
        "Bearer a.b",
        "Bearer a.b.c extra",
    ],
)
def test_non_user_credentials_and_mutated_bearers_are_rejected(header):
    with pytest.raises(di.DelegatedIdentityError):
        di.extract_user_bearer({"authorization": header}, now=100)


def test_expired_subject_is_rejected_and_never_cached():
    expired = _jwt(exp=99)
    fresh = _jwt(exp=101)
    with pytest.raises(di.DelegatedIdentityError, match="expired"):
        di.extract_user_bearer({"authorization": f"Bearer {expired}"}, now=100)
    assert di.extract_user_bearer({"authorization": f"Bearer {fresh}"}, now=100) == fresh


class _Response:
    status = 200
    headers = {"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "session-returned"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"response":"ok"}'


def test_bearer_runtime_invoke_is_bound_to_recorded_endpoint_and_token_is_header_only(monkeypatch):
    captured = {}
    token = _jwt()
    runtime_arn = "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/orders_abc-1234567890"

    def _urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(ri.urllib.request, "urlopen", _urlopen)
    response = ri.invoke_agent_runtime_with_bearer(
        runtime_arn=runtime_arn,
        deployment_state={"runtime_endpoint": runtime_arn + "/runtime-endpoint/DEFAULT"},
        region="eu-west-1",
        payload={"prompt": "hello"},
        bearer_token=token,
        session_id="session-123456789012345678901234567890123",
    )

    request = captured["request"]
    assert request.full_url.endswith("/invocations?qualifier=DEFAULT")
    assert request.get_header("Authorization") == "Bearer " + token
    assert token.encode() not in request.data
    assert json.loads(request.data) == {"prompt": "hello"}
    assert response["runtimeSessionId"] == "session-returned"


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/other/runtime-endpoint/DEFAULT",
        "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/orders_abc-1234567890/runtime-endpoint/bad-name",
    ],
)
def test_mutated_runtime_endpoint_never_receives_the_user_token(monkeypatch, endpoint):
    urlopen = MagicMock()
    monkeypatch.setattr(ri.urllib.request, "urlopen", urlopen)
    runtime_arn = "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/orders_abc-1234567890"
    with pytest.raises(ValueError):
        ri.invoke_agent_runtime_with_bearer(
            runtime_arn=runtime_arn,
            deployment_state={"runtime_endpoint": endpoint},
            region="eu-west-1",
            payload={"prompt": "hello"},
            bearer_token=_jwt(),
            session_id=None,
        )
    urlopen.assert_not_called()


def test_identity_evidence_accepts_only_non_credential_contract():
    evidence = {
        "subject": "user-123",
        "issuer": "https://issuer.example",
        "expires_at": 200,
        "correlation_id": "trace-1",
        "gateway_exchange": "ON_BEHALF_OF_TOKEN_EXCHANGE",
        "downstream_principal": None,
    }
    body = json.dumps({"response": "ok", "identity_evidence": evidence})
    assert ri.parse_identity_evidence(body) == evidence
    evidence["access_token"] = "must-not-pass"
    assert ri.parse_identity_evidence(json.dumps({"identity_evidence": evidence})) is None


def test_generated_gateway_agent_uses_runtime_headers_and_never_payload_tokens():
    from app.services import code_generator

    code = code_generator._generate_strands_gateway(
        "Use the connector.",
        "eu.anthropic.claude-sonnet-5",
        {},
        region="eu-west-1",
    )
    compile(code, "generated-gateway-agent.py", "exec")
    assert "def invoke(payload, context=None):" in code
    assert 'request_headers.get("Authorization")' in code
    assert 'GATEWAY_AUTH_MODE == "obo"' in code
    assert "MCPClient(lambda: _create_transport(subject_token))" in code
    assert "obo_client.stop" in code
    assert "_user_bearer_token" not in code
    assert 'if GATEWAY_URL and GATEWAY_AUTH_MODE != "obo"' in code


def test_generated_memory_agent_does_not_cache_obo_agent_or_token():
    from app.services import code_generator

    code = code_generator._generate_memory_agent(
        "Use the connector.",
        "eu.anthropic.claude-sonnet-5",
        "eu-west-1",
        has_gateway=True,
        creds={},
    )
    compile(code, "generated-memory-agent.py", "exec")
    assert "def invoke(payload, context=None):" in code
    assert "if OBO_GATEWAY:" in code
    assert "return Agent(model=_model, system_prompt=SYSTEM_PROMPT, **extra_kwargs)" in code
    assert "MCPClient(lambda: _create_transport(subject_token))" in code
    assert "_user_bearer_token" not in code


def test_runtime_create_and_update_preserve_request_header_configuration(monkeypatch):
    from app.services import runtime_deployer

    class _Ctrl:
        class exceptions:
            class ResourceNotFoundException(Exception):
                pass

        def __init__(self):
            self.created = None

        def create_agent_runtime(self, **kwargs):
            self.created = kwargs
            return {
                "agentRuntimeId": "orders_agent-1234567890",
                "agentRuntimeArn": "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/orders_agent-1234567890",
                "status": "CREATING",
            }

    ctrl = _Ctrl()
    result = runtime_deployer.create_agent_runtime(
        ctrl,
        "orders_agent",
        "arn:aws:iam::123456789012:role/runtime",
        "bucket",
        "key",
        authorizer_config={"customJWTAuthorizer": {"discoveryUrl": "https://issuer/.well-known/openid-configuration"}},
        request_header_config={"requestHeaderAllowlist": ["Authorization"]},
        region="eu-west-1",
    )
    assert result["runtime_id"] == "orders_agent-1234567890"
    assert ctrl.created["requestHeaderConfiguration"] == {
        "requestHeaderAllowlist": ["Authorization"]
    }

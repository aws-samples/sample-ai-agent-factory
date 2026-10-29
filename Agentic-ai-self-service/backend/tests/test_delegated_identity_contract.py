"""Contract tests for workforce JWT -> Runtime -> Gateway OBO handoff."""

from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import botocore.session
import pytest
from app.services import delegated_identity as di
from app.services import runtime_invocation as ri
from botocore.validate import validate_parameters


def _jwt(
    *,
    exp: float | None = None,
    sub: str = "user-123",
    issuer: str = "https://issuer.example",
    client_id: str = "workforce-client",
    token_use: str = "access",
) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","kid":"k1"}').rstrip(b"=").decode()
    claims = {
        "sub": sub,
        "iss": issuer,
        "exp": exp if exp is not None else time.time() + 300,
        "client_id": client_id,
        "token_use": token_use,
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.signature"


def _extract(token: str, *, now: float = 100) -> str:
    return di.extract_user_bearer(
        {"Authorization": f"Bearer {token}"},
        expected_subject="user-123",
        expected_issuer="https://issuer.example",
        expected_client_id="workforce-client",
        now=now,
    )


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

    identity = di.workforce_gateway_identity("eu-west-1", "user-123")

    assert identity["authorizer_config"] == {
        "customJWTAuthorizer": {
            "discoveryUrl": (
                "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_pool123/.well-known/openid-configuration"
            ),
            "allowedClients": ["workforce-client"],
            "customClaims": [
                {
                    "inboundTokenClaimName": "sub",
                    "inboundTokenClaimValueType": "STRING",
                    "authorizingClaimMatchValue": {
                        "claimMatchValue": {"matchValueString": "user-123"},
                        "claimMatchOperator": "EQUALS",
                    },
                }
            ],
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
        di.workforce_gateway_identity("eu-west-1", "user-123")


def test_workforce_authorizer_rejects_missing_or_invalid_owner(monkeypatch):
    monkeypatch.setenv("WORKFORCE_USER_POOL_ID", "eu-west-1_pool123")
    monkeypatch.setenv("WORKFORCE_CLIENT_ID", "workforce-client")
    monkeypatch.setenv("WORKFORCE_REGION", "eu-west-1")
    for owner in ("", "user/other", "x" * 256):
        with pytest.raises(di.DelegatedIdentityError, match="owner"):
            di.workforce_gateway_identity("eu-west-1", owner)


def test_runtime_obo_transport_is_iam_only_with_exact_custom_header(monkeypatch):
    monkeypatch.setenv("WORKFORCE_USER_POOL_ID", "eu-west-1_pool123")
    monkeypatch.setenv("WORKFORCE_CLIENT_ID", "workforce-client")
    monkeypatch.setenv("WORKFORCE_REGION", "eu-west-1")
    authorizer, headers = di.runtime_obo_transport_configuration("eu-west-1", "user-123")
    assert authorizer is None
    assert headers == {"requestHeaderAllowlist": ["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Workforce-Token"]}


def test_fresh_user_bearer_is_accepted():
    token = _jwt(exp=200)
    assert _extract(token) == token


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
        di.extract_user_bearer(
            {"authorization": header},
            expected_subject="user-123",
            expected_issuer="https://issuer.example",
            expected_client_id="workforce-client",
            now=100,
        )


@pytest.mark.parametrize(
    ("token", "message"),
    [
        (_jwt(sub="other-user"), "authenticated caller"),
        (_jwt(issuer="https://other.example"), "wrong issuer"),
        (_jwt(client_id="other-client"), "wrong client"),
        (_jwt(token_use="id"), "not an access token"),
    ],
)
def test_substituted_identity_claims_are_rejected(token, message):
    with pytest.raises(di.DelegatedIdentityError, match=message):
        _extract(token)


def test_expired_subject_is_rejected_and_never_cached():
    expired = _jwt(exp=99)
    fresh = _jwt(exp=101)
    with pytest.raises(di.DelegatedIdentityError, match="expired"):
        _extract(expired)
    assert _extract(fresh) == fresh


class _Headers:
    def __init__(self):
        self.values: dict[str, str] = {}

    def add_header(self, name: str, value: str) -> None:
        self.values[name] = value


class _Events:
    def __init__(self):
        self.handler = None

    def register_first(self, event_name, handler):
        assert event_name == "before-sign.bedrock-agentcore.InvokeAgentRuntime"
        self.handler = handler

    def unregister(self, event_name, handler=None):
        assert event_name == "before-sign.bedrock-agentcore.InvokeAgentRuntime"
        assert handler is self.handler
        self.handler = None


class _SignedRuntimeClient:
    def __init__(self, *, fail: bool = False):
        self.meta = SimpleNamespace(events=_Events())
        self.fail = fail
        self.invoke_params = None
        self.signed_headers = None

    def invoke_agent_runtime(self, **kwargs):
        request = SimpleNamespace(headers=_Headers())
        assert self.meta.events.handler is not None
        self.meta.events.handler(request)
        self.invoke_params = kwargs
        self.signed_headers = request.headers.values
        if self.fail:
            raise RuntimeError("network failed")
        return {"statusCode": 200, "response": b'{"response":"ok"}'}


def test_obo_runtime_invoke_is_sigv4_with_request_local_token_header():
    token = _jwt()
    runtime_arn = "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/orders_abc-1234567890"
    client = _SignedRuntimeClient()
    dispatched = MagicMock()

    response = ri.invoke_agent_runtime_with_workforce_token(
        agentcore_client=client,
        runtime_arn=runtime_arn,
        deployment_state={"runtime_endpoint": runtime_arn + "/runtime-endpoint/DEFAULT"},
        payload={"prompt": "hello"},
        bearer_token=token,
        session_id="session-123456789012345678901234567890123",
        on_dispatch=dispatched,
    )

    assert client.invoke_params == {
        "agentRuntimeArn": runtime_arn,
        "qualifier": "DEFAULT",
        "payload": json.dumps({"prompt": "hello"}),
        "runtimeSessionId": "session-123456789012345678901234567890123",
    }
    assert client.signed_headers == {
        di.RUNTIME_WORKFORCE_TOKEN_HEADER: token,
    }
    assert token not in client.invoke_params["payload"]
    assert client.meta.events.handler is None
    dispatched.assert_called_once_with()
    assert response["statusCode"] == 200


def test_obo_runtime_header_handler_is_removed_after_invoke_failure():
    client = _SignedRuntimeClient(fail=True)
    runtime_arn = "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/orders_abc-1234567890"

    with pytest.raises(RuntimeError, match="network failed"):
        ri.invoke_agent_runtime_with_workforce_token(
            agentcore_client=client,
            runtime_arn=runtime_arn,
            deployment_state={"runtime_endpoint": runtime_arn + "/runtime-endpoint/DEFAULT"},
            payload={"prompt": "hello"},
            bearer_token=_jwt(),
            session_id=None,
        )

    assert client.meta.events.handler is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/other/runtime-endpoint/DEFAULT",
        "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/orders_abc-1234567890/runtime-endpoint/bad-name",
    ],
)
def test_mutated_runtime_endpoint_never_receives_the_user_token(endpoint):
    client = MagicMock()
    runtime_arn = "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/orders_abc-1234567890"
    with pytest.raises(ValueError):
        ri.invoke_agent_runtime_with_workforce_token(
            agentcore_client=client,
            runtime_arn=runtime_arn,
            deployment_state={"runtime_endpoint": endpoint},
            payload={"prompt": "hello"},
            bearer_token=_jwt(),
            session_id=None,
        )
    client.meta.events.register_first.assert_not_called()
    client.invoke_agent_runtime.assert_not_called()


def test_identity_evidence_accepts_only_caller_bound_non_credential_contract():
    evidence = {
        "subject": "user-123",
        "issuer": "https://issuer.example",
        "expires_at": 200,
        "correlation_id": "trace-1",
        "gateway_exchange": "ON_BEHALF_OF_TOKEN_EXCHANGE",
        "downstream_principal": None,
    }
    body = json.dumps({"response": "ok", "identity_evidence": evidence})
    expectations = {
        "expected_subject": "user-123",
        "expected_issuer": "https://issuer.example",
        "expected_expiry": 200,
    }
    assert ri.parse_identity_evidence(body, **expectations) == evidence
    for key, value in (
        ("subject", "other-user"),
        ("issuer", "https://other.example"),
        ("expires_at", 201),
        ("downstream_principal", ""),
    ):
        mutated = {**evidence, key: value}
        assert (
            ri.parse_identity_evidence(
                json.dumps({"identity_evidence": mutated}),
                **expectations,
            )
            is None
        )
    credential_bearing = {**evidence, "access_token": "must-not-pass"}
    assert (
        ri.parse_identity_evidence(
            json.dumps({"identity_evidence": credential_bearing}),
            **expectations,
        )
        is None
    )


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
    assert "WORKFORCE_TOKEN_HEADER" in code
    assert "request_headers.get(workforce_token_header)" in code
    assert 'request_headers.get("Authorization")' not in code
    assert 'globals().get("GATEWAY_AUTH_MODE", "") == "obo"' in code
    assert "MCPClient(lambda: _create_transport(subject_token))" in code
    assert "invalid delegated identity claims" in code
    assert code.index('try:\n        if globals().get("GATEWAY_AUTH_MODE", "") == "obo":') < code.index(
        "obo_client.start()"
    )
    assert "if delegated_evidence is not None:" in code
    assert code.index("finally:\n        if obo_client is not None") > code.index("get_full_tools_list(obo_client)")
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
    assert 'if globals().get("OBO_GATEWAY", False):' in code
    assert "return Agent(model=_model, system_prompt=SYSTEM_PROMPT, **extra_kwargs)" in code
    assert "MCPClient(lambda: _create_transport(subject_token))" in code
    assert "invalid delegated identity claims" in code
    assert "else:\n            agent = _get_agent()" in code
    assert "if delegated_evidence is not None:" in code
    assert code.index('try:\n        if globals().get("OBO_GATEWAY", False):') < code.index("obo_client.start()")
    assert code.index("finally:\n        if obo_client is not None") > code.index("get_full_tools_list(obo_client)")
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
    assert ctrl.created["requestHeaderConfiguration"] == {"requestHeaderAllowlist": ["Authorization"]}


def test_obo_gateway_and_runtime_transport_match_pinned_botocore_models(monkeypatch):
    monkeypatch.setenv("WORKFORCE_USER_POOL_ID", "eu-west-1_pool123")
    monkeypatch.setenv("WORKFORCE_CLIENT_ID", "workforce-client")
    monkeypatch.setenv("WORKFORCE_REGION", "eu-west-1")
    identity = di.workforce_gateway_identity("eu-west-1", "user-123")
    service = botocore.session.get_session().get_service_model("bedrock-agentcore-control")

    gateway_operation = service.operation_model("CreateGateway")
    validate_parameters(
        identity["authorizer_config"],
        gateway_operation.input_shape.members["authorizerConfiguration"],
    )

    runtime_authorizer, runtime_headers = di.runtime_obo_transport_configuration(
        "eu-west-1",
        "user-123",
    )
    assert runtime_authorizer is None
    runtime_operation = service.operation_model("CreateAgentRuntime")
    validate_parameters(
        runtime_headers,
        runtime_operation.input_shape.members["requestHeaderConfiguration"],
    )


def _generated_header_resolution(code: str) -> str:
    """The exact generated lines from `request_headers = ...` through `subject_token = str(...)`."""
    start = code.index("    request_headers = ")
    end = code.index('        or ""\n    )', start) + len('        or ""\n    )')
    return "\n".join(line[4:] if line.startswith("    ") else line for line in code[start:end].splitlines())


def test_generated_agent_treats_a_none_request_headers_context_as_no_headers():
    """Found live: the exported Python project's run.sh starts a local BedrockAgentCoreApp, whose
    context carries request_headers=None when the caller sent no AgentCore headers. The generated
    invoke() dereferenced it -- `'NoneType' object has no attribute 'get'`, HTTP 500 on every
    local /invocations. getattr's default only applies when the attribute is ABSENT."""
    from types import SimpleNamespace

    from app.services import code_generator

    code = code_generator._generate_strands_gateway(
        "Use the connector.", "us.anthropic.claude-sonnet-5", {}, region="us-east-1"
    )
    snippet = _generated_header_resolution(code)
    for context in (SimpleNamespace(request_headers=None), None, SimpleNamespace()):
        namespace = {"context": context, "globals": lambda: {}}
        exec(snippet, namespace)  # noqa: S102 -- executing the project's own generated code under test
        assert namespace["subject_token"] == "", context
    with_header = SimpleNamespace(request_headers={"X-Amzn-Bedrock-AgentCore-Runtime-Custom-Workforce-Token": "tok"})
    namespace = {"context": with_header, "globals": lambda: {}}
    exec(snippet, namespace)  # noqa: S102
    assert namespace["subject_token"] == "tok"
    # Mutation: the former getattr-default form must fail on request_headers=None.
    broken = snippet.replace(
        'request_headers = (getattr(context, "request_headers", None) if context is not None else None) or {}',
        'request_headers = getattr(context, "request_headers", {}) if context is not None else {}',
    )
    assert broken != snippet
    # The former form fails on request_headers=None: AttributeError at `.get` before the
    # case-insensitive resolution existed, TypeError at `dict(None)` since it does.
    with pytest.raises((AttributeError, TypeError)):
        exec(broken, {"context": SimpleNamespace(request_headers=None), "globals": lambda: {}})  # noqa: S102


@pytest.mark.parametrize(
    "wire_name",
    [
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Workforce-Token",
        "x-amzn-bedrock-agentcore-runtime-custom-workforce-token",
        "X-Amzn-Bedrock-Agentcore-Runtime-Custom-Workforce-Token",
        "X-AMZN-BEDROCK-AGENTCORE-RUNTIME-CUSTOM-WORKFORCE-TOKEN",
    ],
)
def test_generated_agent_resolves_the_workforce_header_regardless_of_wire_casing(wire_name):
    """Found live (OBO journey on the deployed stack): the backend signed and sent the exact
    allowlisted header and the Runtime forwarded it, yet the generated agent raised
    "OBO invocation requires one authenticated workforce JWT". The pinned bedrock-agentcore
    SDK forwards custom headers with their WIRE casing (HTTP/2 lowercases, HTTP/1.1 may
    canonicalise), and the agent only tried two fixed spellings. Header names are
    case-insensitive; the generated code must resolve by lowercased key."""
    from types import SimpleNamespace

    from app.services import code_generator

    code = code_generator._generate_strands_gateway(
        "Use the connector.", "us.anthropic.claude-sonnet-5", {}, region="us-east-1"
    )
    snippet = _generated_header_resolution(code)
    namespace = {"context": SimpleNamespace(request_headers={wire_name: "tok"}), "globals": lambda: {}}
    exec(snippet, namespace)  # noqa: S102 -- executing the project's own generated code under test
    assert namespace["subject_token"] == "tok", wire_name
    # Negative: an unrelated custom header never becomes the subject token.
    namespace = {
        "context": SimpleNamespace(request_headers={"X-Amzn-Bedrock-AgentCore-Runtime-Custom-Other": "tok"}),
        "globals": lambda: {},
    }
    exec(snippet, namespace)  # noqa: S102
    assert namespace["subject_token"] == ""
    # Mutation: the former two-spelling lookup misses the canonicalised casing.
    broken = snippet.replace(
        "or _lowered_headers.get(workforce_token_header.lower())",
        "or request_headers.get(workforce_token_header.lower())",
    )
    assert broken != snippet
    namespace = {
        "context": SimpleNamespace(request_headers={"X-Amzn-Bedrock-Agentcore-Runtime-Custom-Workforce-Token": "tok"}),
        "globals": lambda: {},
    }
    exec(broken, namespace)  # noqa: S102
    assert namespace["subject_token"] == ""


def test_generated_agent_falls_back_to_the_documented_request_headers():
    """Found live (OBO journey on candidate 9e34c65): with the header signed, sent and allowlisted,
    the agent still raised "requires one authenticated workforce JWT" -- the SDK's request_headers
    did not carry it. The Runtime documentation names ``context.request.headers`` (the underlying
    request, case-insensitive) as the way to read any allowlisted header, so the generated code
    must consult it before concluding the token is absent."""
    from types import SimpleNamespace

    from app.services import code_generator
    from starlette.datastructures import Headers

    code = code_generator._generate_strands_gateway(
        "Use the connector.", "us.anthropic.claude-sonnet-5", {}, region="us-east-1"
    )
    snippet = _generated_header_resolution(code)
    raw = Headers({"x-amzn-bedrock-agentcore-runtime-custom-workforce-token": "tok", "host": "runtime"})
    for sdk_headers in (None, {}, {"X-Amzn-Bedrock-AgentCore-Runtime-Custom-Other": "x"}):
        context = SimpleNamespace(request_headers=sdk_headers, request=SimpleNamespace(headers=raw))
        namespace = {"context": context, "globals": lambda: {}}
        exec(snippet, namespace)  # noqa: S102 -- executing the project's own generated code under test
        assert namespace["subject_token"] == "tok", sdk_headers
    # Negative: an unrelated raw header never becomes the subject token, and a request without
    # headers (or without a request object at all) still resolves to no token.
    for request in (SimpleNamespace(headers=Headers({"x-api-key": "k"})), SimpleNamespace(), None):
        namespace = {"context": SimpleNamespace(request_headers=None, request=request), "globals": lambda: {}}
        exec(snippet, namespace)  # noqa: S102
        assert namespace["subject_token"] == ""
    # Mutation: without the fallback the documented source is ignored.
    broken = snippet.replace("    or _raw_token\n", "")
    assert broken != snippet
    namespace = {
        "context": SimpleNamespace(request_headers=None, request=SimpleNamespace(headers=raw)),
        "globals": lambda: {},
    }
    exec(broken, namespace)  # noqa: S102
    assert namespace["subject_token"] == ""
    # The failure names the headers it saw (names only, never values) so a live refusal is diagnosable.
    assert "forwarded custom header names" in code and "request header names" in code
    assert "_seen_custom" in code and "_seen_raw" in code


def test_otel_entrypoint_wrapper_forwards_request_context():
    """Found live (OBO journey, candidate a6f806a, acf-production us-east-1): on the
    instrumented staging / production-gate path the agent raised "OBO invocation requires one
    authenticated workforce JWT" with BOTH header sources empty (forwarded custom header
    names: []; request header names: []) -- i.e. context was None. The injected OTEL span
    wrapper was ``def _otel_invoke_wrap(*args, **kwargs)``; the bedrock-agentcore SDK forwards
    the RequestContext only when the REGISTERED handler's second parameter is named "context"
    (BedrockAgentCoreApp._takes_context), so a varargs wrapper never received it. The wrapper
    must advertise ``(payload, context=None)`` and forward context to a context-taking inner,
    while leaving context-less (non-gateway) agents intact."""
    import inspect

    from app.services import code_generator

    base = (
        "app = BedrockAgentCoreApp()\n"
        "@app.entrypoint\n"
        "def invoke(payload, context=None):\n"
        "    return {'ctx': context, 'payload': payload}\n"
    )
    injected = code_generator._inject_otel(base)
    assert "def _otel_invoke_wrap(payload, context=None):" in injected
    assert "def _otel_invoke_wrap(*args" not in injected

    # Exec just the appended wrapper block with stubs -- no agent-framework import needed.
    start = injected.index("import inspect as _otel_inspect")
    end = injected.index("invoke = app.entrypoint(_otel_invoke_wrap)") + len(
        "invoke = app.entrypoint(_otel_invoke_wrap)"
    )
    block = injected[start:end]

    class _App:
        def __init__(self):
            self.handler = None

        def entrypoint(self, func):
            self.handler = func
            return func

    def _span(invoke_fn, *args, **kwargs):
        return invoke_fn(*args, **kwargs)

    # Context-taking inner (gateway / OBO agent): context must reach it.
    ns = {
        "invoke": (lambda payload, context=None: {"ctx": context, "payload": payload}),
        "app": _App(),
        "_otel_invoke_with_evaluation_span": _span,
        "_otel_force_flush": (lambda: None),
    }
    exec(block, ns)  # noqa: S102 -- executing the project's own generated wrapper under test
    handler = ns["app"].handler
    params = list(inspect.signature(handler).parameters.keys())
    assert len(params) >= 2 and params[1] == "context", params  # SDK _takes_context -> True
    sentinel = object()
    assert handler("p", sentinel)["ctx"] is sentinel  # RequestContext forwarded to the inner

    # Context-less inner (non-gateway agent): the wrapper must not break it.
    ns2 = {
        "invoke": (lambda payload: {"payload": payload}),
        "app": _App(),
        "_otel_invoke_with_evaluation_span": _span,
        "_otel_force_flush": (lambda: None),
    }
    exec(block, ns2)  # noqa: S102
    assert ns2["app"].handler("p", sentinel)["payload"] == "p"

    # Mutation: the former varargs wrapper fails the SDK's second-parameter-named-context rule.
    broken = block.replace("def _otel_invoke_wrap(payload, context=None):", "def _otel_invoke_wrap(*args, **kwargs):")
    broken = broken.replace(
        "        if _otel_inner_takes_context:\n"
        "            return _otel_invoke_with_evaluation_span(_otel_inner_invoke, payload, context)\n"
        "        return _otel_invoke_with_evaluation_span(_otel_inner_invoke, payload)\n",
        "        return _otel_invoke_with_evaluation_span(_otel_inner_invoke, *args, **kwargs)\n",
    )
    ns3 = {
        "invoke": (lambda payload, context=None: {"ctx": context}),
        "app": _App(),
        "_otel_invoke_with_evaluation_span": _span,
        "_otel_force_flush": (lambda: None),
    }
    exec(broken, ns3)  # noqa: S102
    bparams = list(inspect.signature(ns3["app"].handler).parameters.keys())
    assert not (len(bparams) >= 2 and bparams[1] == "context")  # SDK would NOT forward context

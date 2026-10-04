"""The MCP runtime prewarm must use the runtime's configured JWT auth mode."""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import MagicMock

import pytest
from app.step_handlers import mcp_server_step


class _Response:
    def __init__(self, body=b"{}"):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def test_prewarm_mints_oauth_token_and_calls_runtime_with_bearer(monkeypatch):
    calls = []

    def _open(request, timeout):
        calls.append((request, timeout))
        if len(calls) == 1:
            return _Response(json.dumps({"access_token": "jwt-value"}).encode())
        return _Response(b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n')

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    monkeypatch.setattr(
        mcp_server_step.step_clients,
        "client",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("SigV4 SDK path must not be used")),
    )

    runtime_arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/mcp_server-abc"
    ok = mcp_server_step._prewarm_mcp_runtime(
        "us-east-1",
        runtime_arn,
        "https://pool.auth.us-east-1.amazoncognito.com/oauth2/token",
        "client-id",
        "client-secret",
        "resource/invoke",
        attempts=1,
    )

    assert ok is True
    assert len(calls) == 2

    token_request, token_timeout = calls[0]
    assert token_timeout == 30
    assert token_request.full_url.endswith("/oauth2/token")
    assert urllib.parse.parse_qs(token_request.data.decode()) == {
        "grant_type": ["client_credentials"],
        "scope": ["resource/invoke"],
    }
    basic = token_request.get_header("Authorization").removeprefix("Basic ")
    assert base64.b64decode(basic).decode() == "client-id:client-secret"

    invoke_request, invoke_timeout = calls[1]
    assert invoke_timeout == 120
    assert urllib.parse.quote(runtime_arn, safe="") in invoke_request.full_url
    assert invoke_request.full_url.endswith("/invocations?qualifier=DEFAULT")
    assert invoke_request.get_header("Authorization") == "Bearer jwt-value"
    assert invoke_request.get_header("Content-type") == "application/json"
    payload = json.loads(invoke_request.data)
    assert payload["method"] == "initialize"
    assert payload["params"]["clientInfo"]["name"] == "agentcore-flows-prewarm"


@pytest.mark.parametrize(
    ("region", "runtime_arn", "token_endpoint"),
    [
        (
            "us-east-1",
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/mcp",
            "https://attacker.example/oauth2/token",
        ),
        (
            "us-east-1",
            "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/mcp",
            "https://pool.auth.us-east-1.amazoncognito.com/oauth2/token",
        ),
        (
            "us-east-1",
            "arn:aws:lambda:us-east-1:123456789012:function:mcp",
            "https://pool.auth.us-east-1.amazoncognito.com/oauth2/token",
        ),
        (
            "us-east-1",
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/mcp",
            "https://pool.auth.us-east-1.amazoncognito.com/oauth2/token?redirect=1",
        ),
    ],
)
def test_prewarm_refuses_non_service_endpoints_before_network(
    monkeypatch,
    region,
    runtime_arn,
    token_endpoint,
):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("an untrusted pre-warm endpoint was contacted"),
    )

    assert (
        mcp_server_step._prewarm_mcp_runtime(
            region,
            runtime_arn,
            token_endpoint,
            "client-id",
            "client-secret",
            "resource/invoke",
            attempts=1,
        )
        is False
    )


def test_prewarm_retries_token_or_runtime_readiness_without_leaking_auth(monkeypatch):
    calls = []
    sleeps = []

    def _open(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise urllib.error.URLError("Cognito domain not ready")
        if len(calls) == 2:
            return _Response(json.dumps({"access_token": "second-token"}).encode())
        return _Response(b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n')

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    monkeypatch.setattr(mcp_server_step.time, "sleep", sleeps.append)

    assert (
        mcp_server_step._prewarm_mcp_runtime(
            "eu-west-1",
            "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/mcp",
            "https://pool.auth.eu-west-1.amazoncognito.com/oauth2/token",
            "client",
            "secret",
            "scope/invoke",
            attempts=2,
        )
        is True
    )
    assert sleeps == [5]
    assert len(calls) == 3


def test_every_rejected_prewarm_attempt_is_logged_at_warning_without_the_credentials(monkeypatch, caplog):
    """Live (2026-09-28): a stripped authorizer made the runtime reject every bearer attempt,
    and the reasons were logged at INFO, which this Lambda never emits; the only visible
    line blamed a deadline that had 540 s left. The reason must reach the log, bounded, and
    never carry the token or the client secret."""
    import logging

    def _open(request, timeout):
        if request.full_url.endswith("/oauth2/token"):
            return _Response(json.dumps({"access_token": "tok-SECRET-VALUE"}).encode())
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden: authorizer rejected bearer", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    monkeypatch.setattr(mcp_server_step.time, "sleep", lambda _s: None)
    caplog.set_level(logging.WARNING, logger=mcp_server_step.logger.name)

    ok = mcp_server_step._prewarm_mcp_runtime(
        "eu-west-1",
        "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/mcp",
        "https://pool.auth.eu-west-1.amazoncognito.com/oauth2/token",
        "client",
        "client-SECRET-VALUE",
        "scope/invoke",
        attempts=3,
    )

    assert ok is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    rejected = [r.getMessage() for r in warnings if "rejected" in r.getMessage()]
    assert len(rejected) == 3, [r.getMessage() for r in warnings]
    assert all("403" in m or "Forbidden" in m for m in rejected), rejected
    assert any("exhausted 3 attempts" in r.getMessage() for r in warnings)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "SECRET-VALUE" not in joined


class _Store:
    def __init__(self, *, fail_strict: bool = False):
        self.fail_strict = fail_strict
        self.rows = []
        self.strict_rows = []
        self.events = []

    def update_step(self, *args, **kwargs):
        return None

    def record_resource(self, deployment_id, resource):
        self.rows.append((deployment_id, resource))
        self.events.append(("record", resource["type"], resource.get("id") or resource.get("name")))

    def record_resource_strict(self, deployment_id, resource):
        if self.fail_strict:
            raise RuntimeError("durability unavailable")
        self.strict_rows.append((deployment_id, resource))


def _wire_handler(monkeypatch, *, fail_strict: bool = False):
    store = _Store(fail_strict=fail_strict)
    monkeypatch.setattr(mcp_server_step, "_get_deployment_store", lambda: store)
    monkeypatch.setenv("ARTIFACTS_BUCKET_NAME", "artifacts-bucket")
    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")

    upload_s3 = MagicMock()
    deps_s3 = MagicMock()
    deps_s3.get_object.return_value = {"Body": _Response(b"dependency-zip")}
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "999999999999"}
    cognito = MagicMock()
    cognito.create_user_pool.return_value = {"UserPool": {"Id": "us-east-1_POOL1234"}}
    cognito.describe_user_pool_domain.return_value = {"DomainDescription": {"Status": "ACTIVE"}}
    monkeypatch.setattr(
        mcp_server_step.authoritative_dns,
        "authoritative_answer",
        lambda *_a, **_k: mcp_server_step.authoritative_dns.PRESENT,
    )
    cognito.create_user_pool_client.return_value = {
        "UserPoolClient": {
            "ClientId": "mcp-client-id",
            "ClientSecret": "generated-client-secret",  # pragma: allowlist secret
        }
    }
    secrets = MagicMock()
    agentcore = MagicMock()
    logs = MagicMock()
    clients = {
        "s3": upload_s3,
        "sts": sts,
        "iam": MagicMock(),
        "cognito-idp": cognito,
        "secretsmanager": secrets,
        "bedrock-agentcore-control": agentcore,
        "logs": logs,
    }

    def _client(event, service, **kwargs):
        return clients[service]

    monkeypatch.setattr(mcp_server_step.step_clients, "client", _client)
    monkeypatch.setattr(
        mcp_server_step.boto3,
        "client",
        lambda service, **_kwargs: deps_s3 if service == "s3" else pytest.fail(service),
    )
    monkeypatch.setattr(mcp_server_step, "generate_mcp_server_code", lambda **kwargs: "print('mcp')")
    upload_code = MagicMock()
    monkeypatch.setattr(mcp_server_step, "upload_code_to_s3", upload_code)
    monkeypatch.setattr(
        mcp_server_step,
        "create_runtime_iam_role",
        lambda *args, **kwargs: "arn:aws:iam::999999999999:role/AgentCoreMCP-server",
    )

    runtime_create = MagicMock(
        return_value={
            "runtime_id": "mcp_server-AbCdEf1234",
            "arn": ("arn:aws:bedrock-agentcore:us-east-1:999999999999:runtime/mcp_server-AbCdEf1234"),
        }
    )
    monkeypatch.setattr(mcp_server_step, "create_agent_runtime", runtime_create)
    governance = MagicMock(
        side_effect=lambda _client, runtime_id: store.events.append(("govern", "agent_runtime", runtime_id))
    )
    monkeypatch.setattr(mcp_server_step, "govern_default_runtime_log_group", governance)
    store.runtime_logs_client = logs
    store.runtime_log_governance = governance
    monkeypatch.setattr(
        mcp_server_step,
        "wait_for_runtime_ready",
        lambda *args, **kwargs: {"success": True},
    )
    monkeypatch.setattr(
        mcp_server_step,
        "wait_for_default_endpoint_ready",
        lambda *args, **kwargs: {"success": True},
    )

    secret_arn = "arn:aws:secretsmanager:us-east-1:999999999999:secret:agentcore-connector/owner/current-AbCdEf"
    bound = []

    def _bind(**kwargs):
        bound.append(kwargs)
        return secret_arn, True

    deleted = []

    def _delete(**kwargs):
        deleted.append(kwargs)
        return True

    prewarmed = []

    def _prewarm(*args, **kwargs):
        prewarmed.append((args, kwargs))
        return True

    monkeypatch.setattr(mcp_server_step, "bind_connector_secret_for_deployment", _bind)
    monkeypatch.setattr(mcp_server_step, "delete_deployment_bound_secret", _delete)
    monkeypatch.setattr(mcp_server_step, "_prewarm_mcp_runtime", _prewarm)
    return (
        store,
        runtime_create,
        secret_arn,
        bound,
        deleted,
        prewarmed,
        upload_code,
        deps_s3,
    )


def _handler_event():
    return {
        "deployment_id": "dep-current",
        "owner_sub": "owner",
        "target_account_id": "999999999999",
        "target_region": "us-east-1",
        "target_artifact_bucket": "customer-runtime-artifacts",
        "target_runtime_role_arn": ("arn:aws:iam::999999999999:role/custom/StableRuntimeRole"),
        "target_mcp_runtime_role_arn": ("arn:aws:iam::999999999999:role/custom/StableMCPRuntimeRole"),
        "mcp_server_config": {"name": "server", "tools": ["time"]},
        "gateway_config": {"name": "gateway"},
    }


def test_mcp_handler_returns_only_a_reference_but_prewarms_with_the_generated_secret(monkeypatch):
    (
        store,
        runtime_create,
        secret_arn,
        bound,
        deleted,
        prewarmed,
        upload_code,
        deps_s3,
    ) = _wire_handler(monkeypatch)

    out = mcp_server_step.handler(_handler_event(), None)

    serialized = json.dumps(out)
    assert "generated-client-secret" not in serialized
    assert "client_secret" not in out["mcp_oauth"]
    assert out["mcp_oauth"]["client_secret_ref"] == secret_arn
    assert out["recorded_secret_arns"] == [secret_arn]

    assert bound[0]["raw_value"] == "generated-client-secret"
    assert bound[0]["payload_key"] == "clientSecret"
    assert prewarmed[0][0][4] == "generated-client-secret"
    assert store.strict_rows == [
        (
            "dep-current",
            {
                "type": "secret",
                "id": secret_arn,
                "region": "us-east-1",
                "account": "999999999999",
                "created_by_deployment": True,
            },
        )
    ]
    runtime_create.assert_called_once()
    assert runtime_create.call_args.kwargs["s3_bucket"] == "customer-runtime-artifacts"
    assert runtime_create.call_args.kwargs["role_arn"] == ("arn:aws:iam::999999999999:role/custom/StableMCPRuntimeRole")
    assert not any(resource["type"] == "iam_role" for _, resource in store.rows)
    deps_s3.get_object.assert_called_once_with(
        Bucket="artifacts-bucket",
        Key="agentcore-deps/mcp-lean.zip",
    )
    upload_code.assert_called_once()
    assert upload_code.call_args.args[0] is not deps_s3
    assert upload_code.call_args.args[1] == "customer-runtime-artifacts"
    assert upload_code.call_args.kwargs["expected_bucket_owner"] == "999999999999"
    assert deleted == []


def test_the_step_waits_for_the_auth_domain_to_be_active_and_resolvable(monkeypatch):
    """Live (2026-09-28, run 13): the domain was created, this Lambda minted a token from it
    ~30 s later, and the AgentCore service still failed the gateway target update with
    "Failed to resolve hostname: <domain>.auth.us-east-1.amazoncognito.com". Nothing may be
    pointed at a domain that is not yet ACTIVE and resolvable."""
    wired = _wire_handler(monkeypatch)
    cognito = mcp_server_step.step_clients.client({}, "cognito-idp")
    statuses = iter(["CREATING", "CREATING", "ACTIVE", "ACTIVE"])
    cognito.describe_user_pool_domain.side_effect = lambda **_k: {
        "DomainDescription": {"Status": next(statuses, "ACTIVE")}
    }
    verdicts = iter([mcp_server_step.authoritative_dns.ABSENT, mcp_server_step.authoritative_dns.PRESENT])
    asked: list[str] = []

    def _authoritative(host, zone, **_k):
        asked.append(f"{host}|{zone}")
        return next(verdicts)

    monkeypatch.setattr(mcp_server_step.authoritative_dns, "authoritative_answer", _authoritative)
    sleeps = []
    monkeypatch.setattr(mcp_server_step.time, "sleep", sleeps.append)

    out = mcp_server_step.handler(_handler_event(), None)

    assert out["mcp_oauth"]["client_secret_ref"] == wired[2]
    assert cognito.describe_user_pool_domain.call_count >= 4  # CREATING, CREATING, ACTIVE+unresolved, ACTIVE+resolved
    assert asked == ["ac-mcp-gateway-pool1234.auth.us-east-1.amazoncognito.com|auth.us-east-1.amazoncognito.com"] * 2
    assert sleeps.count(3.0) == 3


def test_an_auth_domain_that_never_resolves_fails_the_deployment_closed(monkeypatch):
    _wire_handler(monkeypatch)
    cognito = mcp_server_step.step_clients.client({}, "cognito-idp")
    cognito.describe_user_pool_domain.return_value = {"DomainDescription": {"Status": "CREATING"}}
    monkeypatch.setattr(mcp_server_step.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        mcp_server_step, "_lambda_bounded_readiness_deadline", lambda _ctx: mcp_server_step.time.monotonic() + 8.0
    )

    with pytest.raises(RuntimeError) as excinfo:
        mcp_server_step.handler(_handler_event(), None)

    assert "did not become ACTIVE and resolvable" in str(excinfo.value)
    assert "ac-mcp-gateway-pool1234.auth.us-east-1.amazoncognito.com" in str(excinfo.value)


def test_a_failed_auth_domain_creation_is_fatal_not_a_warning(monkeypatch):
    _wire_handler(monkeypatch)
    cognito = mcp_server_step.step_clients.client({}, "cognito-idp")
    cognito.create_user_pool_domain.side_effect = RuntimeError("InvalidParameterException: domain taken")

    with pytest.raises(RuntimeError) as excinfo:
        mcp_server_step.handler(_handler_event(), None)

    assert "could not be created" in str(excinfo.value)


def test_a_rejected_prewarm_with_time_to_spare_is_not_reported_as_a_deadline(monkeypatch):
    """Live (2026-09-28): four bearer attempts were rejected in 15 s (stripped authorizer) while
    the Lambda-bounded deadline had ~540 s left, and the step still said "before the
    deployment deadline". The two causes need two messages."""
    _wire_handler(monkeypatch)
    monkeypatch.setattr(mcp_server_step, "_prewarm_mcp_runtime", lambda *a, **k: False)
    monkeypatch.setattr(
        mcp_server_step, "_lambda_bounded_readiness_deadline", lambda _ctx: mcp_server_step.time.monotonic() + 500.0
    )

    with pytest.raises(RuntimeError) as excinfo:
        mcp_server_step.handler(_handler_event(), None)

    message = str(excinfo.value)
    assert "rejected on every attempt" in message
    assert "deadline" not in message


def test_an_expired_deadline_is_still_reported_as_a_deadline(monkeypatch):
    _wire_handler(monkeypatch)
    monkeypatch.setattr(mcp_server_step, "_prewarm_mcp_runtime", lambda *a, **k: False)
    monkeypatch.setattr(
        mcp_server_step, "_lambda_bounded_readiness_deadline", lambda _ctx: mcp_server_step.time.monotonic() - 1.0
    )

    with pytest.raises(RuntimeError) as excinfo:
        mcp_server_step.handler(_handler_event(), None)

    assert "before the deployment deadline" in str(excinfo.value)


def test_mcp_handler_records_runtime_before_governing_its_target_region_log(monkeypatch):
    (
        store,
        _runtime_create,
        _secret_arn,
        _bound,
        _deleted,
        _prewarmed,
        _upload_code,
        _deps_s3,
    ) = _wire_handler(monkeypatch)

    mcp_server_step.handler(_handler_event(), None)

    runtime_event = ("record", "agent_runtime", "mcp_server-AbCdEf1234")
    governance_event = ("govern", "agent_runtime", "mcp_server-AbCdEf1234")
    assert store.events.index(runtime_event) < store.events.index(governance_event)
    store.runtime_log_governance.assert_called_once_with(
        store.runtime_logs_client,
        "mcp_server-AbCdEf1234",
    )


def test_mcp_handler_keeps_the_teardown_handle_when_log_governance_fails(monkeypatch):
    (
        store,
        _runtime_create,
        _secret_arn,
        _bound,
        _deleted,
        prewarmed,
        _upload_code,
        _deps_s3,
    ) = _wire_handler(monkeypatch)
    failure = RuntimeError("retention could not be applied")
    store.runtime_log_governance.side_effect = failure
    wait_for_runtime = MagicMock(return_value={"success": True})
    monkeypatch.setattr(mcp_server_step, "wait_for_runtime_ready", wait_for_runtime)

    with pytest.raises(RuntimeError) as raised:
        mcp_server_step.handler(_handler_event(), None)

    assert raised.value is failure
    assert any(
        resource
        == {
            "type": "agent_runtime",
            "id": "mcp_server-AbCdEf1234",
            "region": "us-east-1",
            "created_by_deployment": False,
        }
        for _deployment_id, resource in store.rows
    )
    wait_for_runtime.assert_not_called()
    assert prewarmed == []


def test_mcp_handler_compensates_when_the_secret_handle_cannot_be_persisted(monkeypatch):
    (
        store,
        runtime_create,
        secret_arn,
        _bound,
        deleted,
        prewarmed,
        _upload_code,
        _deps_s3,
    ) = _wire_handler(monkeypatch, fail_strict=True)

    with pytest.raises(RuntimeError, match="durability unavailable"):
        mcp_server_step.handler(_handler_event(), None)

    assert store.strict_rows == []
    assert deleted == [
        {
            "region": "us-east-1",
            "deployment_id": "dep-current",
            "secret_ref": secret_arn,
            "secrets_client": deleted[0]["secrets_client"],
        }
    ]
    runtime_create.assert_not_called()
    assert prewarmed == []


# ---------------------------------------------------------------------------
# Redeploy audit 2026-09-28 row 1: an ADOPTED runtime was updated, so its DEFAULT endpoint
# is READY on the previous version until the new one goes live. The step must read the
# runtime's current version and hand it to the endpoint wait, or the pre-warm warms the
# old container and the gateway target is pointed at a runtime that is still switching.
# ---------------------------------------------------------------------------


def _wire_endpoint_wait_recorder(monkeypatch):
    waits: list[dict] = []

    def _wait(*args, **kwargs):
        waits.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(mcp_server_step, "wait_for_default_endpoint_ready", _wait)
    return waits


def test_an_adopted_mcp_runtime_waits_for_its_endpoint_on_the_updated_version(monkeypatch):
    (_store, runtime_create, *_rest) = _wire_handler(monkeypatch)
    runtime_create.return_value = {**runtime_create.return_value, "created_by_deployment": False}
    agentcore = mcp_server_step.step_clients.client(None, "bedrock-agentcore-control")
    agentcore.get_agent_runtime.return_value = {"agentRuntimeVersion": "3", "status": "READY"}
    waits = _wire_endpoint_wait_recorder(monkeypatch)

    mcp_server_step.handler(_handler_event(), None)

    assert agentcore.get_agent_runtime.call_args.kwargs == {"agentRuntimeId": "mcp_server-AbCdEf1234"}
    assert [w.get("expected_version") for w in waits] == ["3"]


def test_a_runtime_this_deployment_created_does_not_pin_an_endpoint_version(monkeypatch):
    (_store, runtime_create, *_rest) = _wire_handler(monkeypatch)
    runtime_create.return_value = {**runtime_create.return_value, "created_by_deployment": True}
    agentcore = mcp_server_step.step_clients.client(None, "bedrock-agentcore-control")
    waits = _wire_endpoint_wait_recorder(monkeypatch)

    mcp_server_step.handler(_handler_event(), None)

    assert agentcore.get_agent_runtime.call_count == 0
    assert [w.get("expected_version") for w in waits] == [None]


def test_an_unreadable_adopted_version_still_gates_on_ready(monkeypatch):
    """Fail soft on the read, never on the wait: the endpoint gate stays."""
    (_store, runtime_create, *_rest) = _wire_handler(monkeypatch)
    runtime_create.return_value = {**runtime_create.return_value, "created_by_deployment": False}
    agentcore = mcp_server_step.step_clients.client(None, "bedrock-agentcore-control")
    agentcore.get_agent_runtime.side_effect = RuntimeError("throttled")
    waits = _wire_endpoint_wait_recorder(monkeypatch)

    out = mcp_server_step.handler(_handler_event(), None)

    assert out
    assert [w.get("expected_version") for w in waits] == [None]


def test_a_denied_domain_describe_fails_at_once_naming_the_missing_action(monkeypatch):
    """Live, matrix run 14 (2026-09-28): no step role granted cognito-idp:DescribeUserPoolDomain; the
    wait retried the AccessDenied for the whole deadline and reported 'status unknown'. A permission
    denial must fail on the first attempt and name the grant."""
    from botocore.exceptions import ClientError

    cognito = MagicMock()
    cognito.describe_user_pool_domain.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "DescribeUserPoolDomain"
    )
    sleeps: list[float] = []
    monkeypatch.setattr(mcp_server_step.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(RuntimeError) as error:
        mcp_server_step._wait_for_cognito_domain(
            cognito, "ac-mcp-x", "us-east-1", deadline_monotonic=mcp_server_step.time.monotonic() + 4.0
        )

    text = str(error.value)
    assert "cognito-idp:DescribeUserPoolDomain" in text
    assert "AccessDeniedException" in text
    assert cognito.describe_user_pool_domain.call_count == 1
    assert sleeps == []


def test_a_throttled_domain_describe_is_still_retried(monkeypatch):
    from botocore.exceptions import ClientError

    cognito = MagicMock()
    cognito.describe_user_pool_domain.side_effect = [
        ClientError({"Error": {"Code": "TooManyRequestsException", "Message": "slow down"}}, "DescribeUserPoolDomain"),
        {"DomainDescription": {"Status": "ACTIVE"}},
    ]
    monkeypatch.setattr(
        mcp_server_step.authoritative_dns,
        "authoritative_answer",
        lambda *_a, **_k: mcp_server_step.authoritative_dns.PRESENT,
    )
    monkeypatch.setattr(mcp_server_step.time, "sleep", lambda s: None)

    waited = mcp_server_step._wait_for_cognito_domain(
        cognito, "ac-mcp-x", "us-east-1", deadline_monotonic=mcp_server_step.time.monotonic() + 600
    )

    assert waited >= 0
    assert cognito.describe_user_pool_domain.call_count == 2


# ---------------------------------------------------------------------------
# 2026-09-29 (run 18): a transport error is not a rejection. Four attempts in fifteen seconds
# died at the socket layer with four minutes of deadline left and the deployment failed.
# ---------------------------------------------------------------------------


def _prewarm(attempts, deadline):
    return mcp_server_step._prewarm_mcp_runtime(
        "us-east-1",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/mcp",
        "https://pool.auth.us-east-1.amazoncognito.com/oauth2/token",
        "client",
        "secret",
        "scope/invoke",
        attempts=attempts,
        deadline_monotonic=deadline,
    )


def test_transport_errors_are_retried_past_the_attempt_count_while_the_deadline_allows(monkeypatch):
    calls = []
    sleeps = []

    def _open(request, timeout):
        calls.append(request)
        if len(calls) <= 6:
            raise urllib.error.URLError(OSError(99, "Cannot assign requested address"))
        if len(calls) == 7:
            return _Response(json.dumps({"access_token": "tok"}).encode())
        return _Response(b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n')

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    monkeypatch.setattr(mcp_server_step.time, "sleep", sleeps.append)

    assert _prewarm(attempts=4, deadline=mcp_server_step.time.monotonic() + 600) is True
    assert len(sleeps) == 6, "six transport failures, six waits, then success on the seventh attempt"


def test_a_real_rejection_still_stops_after_the_attempt_count(monkeypatch):
    calls = []
    sleeps = []

    def _open(request, timeout):
        calls.append(request)
        if len(calls) % 2 == 1:
            return _Response(json.dumps({"access_token": "tok"}).encode())
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    monkeypatch.setattr(mcp_server_step.time, "sleep", sleeps.append)

    assert _prewarm(attempts=4, deadline=mcp_server_step.time.monotonic() + 600) is False
    assert len(sleeps) == 3, "a 401 is permanent: exactly `attempts` tries, no deadline-driven extension"


def test_transport_retries_stop_when_the_deadline_is_nearly_spent(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(mcp_server_step.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(mcp_server_step.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

    def _open(request, timeout):
        raise urllib.error.URLError(OSError(16, "Device or resource busy"))

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    assert _prewarm(attempts=2, deadline=1000.0 + 40.0) is False
    assert clock["t"] <= 1000.0 + 40.0, "never sleeps past the deadline"
    assert clock["t"] >= 1000.0 + 15.0, "kept retrying past the two attempts while time allowed"

"""The empty-tool-plane retry loop must be honest about what failed and must
leave every resource it created findable.

All four behaviours pinned here were measured absent on a live deploy
(deployment ``3ef480e2``, SFN run ``df698a37``, us-east-1):

1. ``DeleteGateway`` was denied, the ``except`` around it logged it "(non-fatal)",
   and the loop recursed. ``deploy_gateway`` then re-adopted the SAME gateway id by
   name and re-synced the same 9 tools, so all three "recreations" were
   bit-identical -- 368s of billed Lambda that could not possibly have changed the
   outcome, and the failure was still reported as an AgentCore service flake after
   "3 gateway recreations" that never happened.
2. The exhausted-retries path ``return``ed instead of raising, so it bypassed the
   ``except`` handler that builds the teardown inventory. ``gateway_step`` got a
   dict with no ``gateway_id``, ``_record_gateway_resources`` wrote no manifest rows,
   and gateway ``agent-gateway-cs0p5dvpgi`` was still live with a READY target while
   its deployment row had no ``created_resources`` key at all.
3. The recursive call dropped ``connectors``, ``external_mcp_servers`` and
   ``owner_sub``, so a retry rebuilt a feature-stripped gateway -- and would have
   shipped it if it came up healthy.
4. ``_count_served_tools`` returns -1 for a non-200/transport error and the waiter
   clamped it to 0, so "the gateway answered with an empty tool list" and "the
   gateway never answered us" were the same number. The abort message asserted the
   first (a service-side provisioning flake) with no evidence either way.

ARCC guidance ``cnt_ua0cTwldOsODs8`` (delete trust relationships with the resource)
and ``cnt_vtSS0S3iwKjSuk`` (never move a client secret onto a diagnostic path) --
hence the client-secret assertion on the abort return, which travels into a
RuntimeError message and an SFN failure cause.
"""

import contextlib
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.services import gateway_deployer
from app.services import gateway_mutation_lock as gml
from botocore.exceptions import ClientError

GW_ID = "agent-gateway-fake0000"
GW_URL = "https://agent-gateway-fake0000.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
# The real deploy's client_info carries this. It must never reach the abort return.
SECRET = "notarealclientsecret-0000000000"


class _FakeIamExceptions:
    class EntityAlreadyExistsException(Exception):
        pass


class _FakeIam:
    exceptions = _FakeIamExceptions()

    def create_role(self, **kw):
        return {"Role": {"Arn": f"arn:aws:iam::123456789012:role/{kw['RoleName']}"}}

    def put_role_policy(self, **kw):
        return {}


class _FakeCtrl:
    """Records the control-plane calls the retry loop makes.

    ``delete_gateway`` can be made to raise so one test reproduces the live
    AccessDenied without needing IAM.
    """

    def __init__(self, *, delete_raises: Exception | None = None):
        self.delete_raises = delete_raises
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.deleted_targets: list[str] = []

    def create_gateway(self, **kw):
        self.created.append(kw["name"])
        return {
            "gatewayId": GW_ID,
            "gatewayUrl": GW_URL,
            "gatewayArn": f"arn:aws:bedrock-agentcore:us-east-1:1:gateway/{GW_ID}",
            "roleArn": kw["roleArn"],
        }

    def list_gateways(self, **kw):
        # What deploy_gateway's pre-flight reads (F-63): a gateway it created and has
        # not deleted is still there under its name.
        live = [n for n in self.created if GW_ID not in self.deleted]
        return {"items": [{"name": n, "gatewayId": GW_ID} for n in live[-1:]]}

    def list_gateway_targets(self, **kw):
        return {"items": [{"targetId": "C8PHN4DAKY", "targetConfiguration": {"mcp": {"lambda": {}}}}]}

    def delete_gateway_target(self, **kw):
        self.deleted_targets.append(kw["targetId"])
        return {}

    def delete_gateway(self, **kw):
        if self.delete_raises is not None:
            raise self.delete_raises
        self.deleted.append(kw["gatewayIdentifier"])
        return {}

    def get_gateway(self, **kw):
        # The retry deletes under a proof of absence (F-66e), so a deleted gateway
        # must read back gone, as the service's does once the delete completes.
        if kw.get("gatewayIdentifier") in self.deleted:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "GetGateway")
        return {"gatewayArn": f"arn:aws:bedrock-agentcore:us-east-1:1:gateway/{GW_ID}", "status": "READY"}


def _install(monkeypatch, *, ctrl, served=0, got_valid_response=True, expected=9):
    """Stub every AWS edge of deploy_gateway, leaving the retry logic itself real.

    ``served``/``got_valid_response`` stand in for the MCP probe, which is the one
    input that decides whether the retry block runs at all.
    """
    cleanup_calls: list[dict] = []

    monkeypatch.setattr(gateway_deployer, "_create_agentcore_control_client", lambda region: ctrl)
    monkeypatch.setattr(gateway_deployer, "_create_cognito_client", lambda region: object())
    monkeypatch.setattr(gateway_deployer, "_create_iam_client", lambda: _FakeIam())
    monkeypatch.setattr(
        gateway_deployer,
        "_create_cognito_oauth",
        lambda *a, **kw: {
            "authorizer_config": {
                "customJWTAuthorizer": {
                    "discoveryUrl": "https://x/.well-known/openid-configuration",
                    "allowedClients": ["cid"],
                }
            },
            "client_info": {
                "provider": "cognito",
                "user_pool_id": "us-east-1_FAKEPOOL",
                "client_id": "cid",
                "client_secret": SECRET,
                "token_endpoint": "https://x/oauth2/token",
            },
        },
    )
    monkeypatch.setattr(
        gateway_deployer,
        "_wait_for_gateway",
        lambda *a, **kw: {"gatewayUrl": GW_URL, "roleArn": "arn:aws:iam::1:role/r"},
    )
    # expected_tool_count comes from the LIVE gateway, not the input args -- the
    # 9/9-synced-but-0-served shape of the real failure.
    monkeypatch.setattr(
        gateway_deployer,
        "_resolve_gateway_tool_actions",
        lambda *a, **kw: ([f"DynamicTools___t{i}" for i in range(expected)], expected),
    )

    def _fake_wait(gateway_url, client_info, expected_, timeout=90, probe=None):
        if probe is not None:
            probe["got_valid_response"] = got_valid_response
            probe["last_status"] = (
                "" if got_valid_response else "every tools/list probe returned a non-200 or failed to connect"
            )
        return served

    monkeypatch.setattr(gateway_deployer, "_wait_for_gateway_to_serve_tools", _fake_wait)

    def _fake_cleanup(name, region, partial, *, deployment_id=""):
        assert deployment_id == "d-1" or deployment_id.startswith("d-")
        cleanup_calls.append(dict(partial))
        return []

    monkeypatch.setattr(gateway_deployer, "cleanup_gateway_resources", _fake_cleanup)
    # Connector / external-MCP target deployment is a different subsystem with its
    # own tests; here it only has to succeed so the retry block is reached. Both
    # return the teardown-ref shape their real counterparts do.
    monkeypatch.setattr(
        gateway_deployer,
        "_deploy_connector_targets",
        lambda *a, **kw: {
            "credential_provider_names": ["conn-github"],
            "secret_arns": ["arn:aws:secretsmanager:us-east-1:1:secret:s"],
            "spec_s3_uris": ["s3://b/k"],
        },
    )
    monkeypatch.setattr(
        gateway_deployer,
        "_deploy_external_mcp_targets",
        lambda *a, **kw: {"credential_provider_names": [], "secret_arns": []},
    )
    # The loop sleeps 5s + 8s per attempt and the role branch sleeps 10s.
    monkeypatch.setattr(gateway_deployer.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(gateway_deployer.boto3, "client", lambda *a, **kw: _FakeSts())
    return cleanup_calls


class _FakeSts:
    def get_caller_identity(self):
        return {"Account": "123456789012"}


def test_exhausted_retries_return_the_gateway_id_so_teardown_can_find_it(monkeypatch):
    """The orphan. On the last attempt the deploy must still hand back every
    resource handle it created, which only happens if it RAISES into the except
    handler that builds that inventory."""
    ctrl = _FakeCtrl()
    cleanup_calls = _install(monkeypatch, ctrl=ctrl, served=0)

    out = gateway_deployer.deploy_gateway({"name": "agent-gateway"}, "us-east-1", deployment_id="d-1", gateway_retry=2)

    assert out["success"] is False
    assert out["gateway_id"] == GW_ID, (
        "gateway_step's _record_gateway_resources keys off gateway_id. Without it "
        f"the gateway is orphaned with nothing naming it. Got: {sorted(out)}"
    )
    assert out["gateway_name"] == "agent-gateway"
    # The Cognito pool created near the top of the deploy must be recoverable too.
    assert out["client_info"]["user_pool_id"] == "us-east-1_FAKEPOOL"
    # ...but not via the secret. This dict travels into an SFN failure cause.
    assert SECRET not in repr(out)
    assert "client_secret" not in out["client_info"]
    # The abort cleanup ran, and got the same inventory.
    assert [c["gateway_id"] for c in cleanup_calls] == [GW_ID]
    # The error names the real diagnosis, not a guess.
    assert "served 0/9 tools" in out["error"]
    assert "3 gateway creation attempt(s)" in out["error"]


def test_a_failed_delete_stops_the_loop_instead_of_recreating(monkeypatch):
    """The 368 wasted seconds. If the gateway cannot be deleted, a "fresh gateway"
    retry re-adopts the same one, so it must stop and report the delete error."""
    denied = ClientError(
        {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": "User: arn:aws:sts::123456789012:assumed-role/step-gateway is not "
                "authorized to perform: bedrock-agentcore:DeleteGateway",
            }
        },
        "DeleteGateway",
    )
    ctrl = _FakeCtrl(delete_raises=denied)
    _install(monkeypatch, ctrl=ctrl, served=0)

    out = gateway_deployer.deploy_gateway({"name": "agent-gateway"}, "us-east-1", deployment_id="d-2", gateway_retry=0)

    assert out["success"] is False
    assert ctrl.created == ["agent-gateway"], (
        "A retry after a FAILED delete re-adopts the same gateway and fails "
        f"identically; it must not be attempted. create_gateway calls: {ctrl.created}"
    )
    assert "could not be deleted" in out["error"]
    assert "DeleteGateway" in out["error"], "the actionable cause must survive into the error"
    assert out["gateway_id"] == GW_ID


def test_the_retry_forwards_connectors_external_mcp_and_owner_sub(monkeypatch):
    """A retry must rebuild the gateway the user asked for, not a stripped one."""
    ctrl = _FakeCtrl()
    _install(monkeypatch, ctrl=ctrl, served=0)

    original = gateway_deployer.deploy_gateway
    recursions: list[dict] = []

    def _recorder(*a, **kw):
        recursions.append(kw)
        return {"success": True, "gateway_id": GW_ID, "recursed": True}

    # Swap the module global the recursion resolves through, then call the
    # ORIGINAL function object so only the inner call is intercepted.
    monkeypatch.setattr(gateway_deployer, "deploy_gateway", _recorder)

    connectors = [{"connectorId": "github", "apiKeyRef": "ref-1"}]
    external = [{"name": "ext", "url": "https://mcp.example.invalid/mcp"}]
    out = original(
        {"name": "agent-gateway"},
        "us-east-1",
        deployment_id="d-3",
        gateway_retry=0,
        connectors=connectors,
        external_mcp_servers=external,
        owner_sub="sub-abc",
    )

    assert out.get("recursed") is True, "the retry must actually happen when the delete succeeds"
    assert ctrl.deleted == [GW_ID]
    assert ctrl.deleted_targets == ["C8PHN4DAKY"], "targets must be deleted before the gateway"
    assert len(recursions) == 1
    kw = recursions[0]
    assert kw["connectors"] == connectors
    assert kw["external_mcp_servers"] == external
    assert kw["owner_sub"] == "sub-abc"
    assert kw["gateway_retry"] == 1, "the retry counter must advance or the loop never terminates"


def test_a_probe_that_never_got_an_answer_does_not_destroy_the_gateway(monkeypatch, caplog):
    """The doom loop, and the reason it could never terminate.

    Recreating the gateway also recreates the Cognito user pool AND its hosted
    domain, and a cold Cognito domain is why the probe got no token. Measured
    live (throwaway pool, us-east-1): create_user_pool_domain returns at once and
    describe_user_pool_domain says Status=ACTIVE within 4s, but the DNS name
    <domain>.auth.us-east-1.amazoncognito.com still did not resolve at t+245s.
    The probe window is 90s. So every "fresh gateway" retry restarted a
    provisioning clock it could not outrun -- CloudTrail for run df698a37 shows
    three pools created in three attempts (v8OiJanup, QU487tO1L, yvymML5Pm) and
    CreateGateway returning ConflictException on the last two.

    The gateway itself never depended on that domain (its customJWTAuthorizer uses
    the cognito-idp discoveryUrl) and the deployed agent mints its token later,
    when the domain is warm -- so the deploy must not delete a gateway over a
    reading that says nothing about the gateway.
    """
    ctrl = _FakeCtrl()
    _install(monkeypatch, ctrl=ctrl, served=0, got_valid_response=False)

    with caplog.at_level("WARNING", logger=gateway_deployer.logger.name):
        out = gateway_deployer.deploy_gateway(
            {"name": "agent-gateway"}, "us-east-1", deployment_id="d-4", gateway_retry=0
        )

    assert out["success"] is True, (
        "failing the deploy here fails a CORRECT deploy: the control plane had "
        "already confirmed every configured tool synced onto a READY target"
    )
    assert out["tool_plane_verified"] is False, (
        "succeeding without saying the MCP plane was never exercised is the same misdiagnosis in the other direction"
    )
    assert ctrl.deleted == [], f"the gateway must NOT be torn down; deleted: {ctrl.deleted}"
    assert ctrl.deleted_targets == []
    assert ctrl.created == ["agent-gateway"], "and no second gateway may be created"

    text = caplog.text
    assert "UNVERIFIED" in text
    assert "never answered tools/list" in text
    assert "AUTH/REACHABILITY failure" in text
    assert "served 0/9 tools" not in text, (
        "claiming the gateway served an empty tool list when it never answered at "
        "all is what sent the whole retry loop after the wrong cure"
    )


def test_a_genuinely_empty_tool_plane_still_gets_the_recreate_cure(monkeypatch):
    """The other half of the split. When the probe DID get a parseable answer and
    the answer was an empty/short tools array, the gateway really is serving the
    wrong thing and recreating it is the documented cure -- so that path must
    survive the change above, and must still mark the plane verified when it works."""
    ctrl = _FakeCtrl()
    _install(monkeypatch, ctrl=ctrl, served=0, got_valid_response=True)

    original = gateway_deployer.deploy_gateway
    recursions: list[dict] = []
    monkeypatch.setattr(
        gateway_deployer,
        "deploy_gateway",
        lambda *a, **kw: (recursions.append(kw), {"success": True, "recursed": True})[1],
    )

    out = original({"name": "agent-gateway"}, "us-east-1", deployment_id="d-4b", gateway_retry=0)

    assert out.get("recursed") is True, "a real empty tool plane must still be retried"
    assert ctrl.deleted == [GW_ID]
    assert len(recursions) == 1


def test_recreate_cleanup_deletes_gateway_targets_from_every_page(monkeypatch):
    class _PagedCtrl(_FakeCtrl):
        def __init__(self):
            super().__init__()
            self.target_list_requests: list[dict] = []

        def list_gateway_targets(self, **kw):
            self.target_list_requests.append(dict(kw))
            if kw.get("nextToken") == "page-2":
                return {
                    "gatewayTargetSummaries": [
                        {"gatewayTargetId": "target-page-2"},
                    ]
                }
            return {
                "items": [{"targetId": "target-page-1"}],
                "nextToken": "page-2",
            }

    ctrl = _PagedCtrl()
    _install(monkeypatch, ctrl=ctrl, served=0, got_valid_response=True)
    original = gateway_deployer.deploy_gateway
    monkeypatch.setattr(
        gateway_deployer,
        "deploy_gateway",
        lambda *a, **kw: {"success": True, "recursed": True},
    )

    out = original(
        {"name": "agent-gateway"},
        "us-east-1",
        deployment_id="d-page-two",
        gateway_retry=0,
    )

    assert out["recursed"] is True
    assert ctrl.target_list_requests == [
        {"gatewayIdentifier": GW_ID, "maxResults": 50},
        {
            "gatewayIdentifier": GW_ID,
            "maxResults": 50,
            "nextToken": "page-2",
        },
    ]
    assert ctrl.deleted_targets == ["target-page-1", "target-page-2"]
    assert ctrl.deleted == [GW_ID]


def test_a_confirmed_tool_plane_is_reported_as_verified(monkeypatch):
    """The happy path must set the flag too, or `tool_plane_verified` is a field
    that is only ever False and carries no information."""
    ctrl = _FakeCtrl()
    _install(monkeypatch, ctrl=ctrl, served=9, got_valid_response=True)

    out = gateway_deployer.deploy_gateway({"name": "agent-gateway"}, "us-east-1", deployment_id="d-4c", gateway_retry=0)

    assert out["success"] is True
    assert out["tool_plane_verified"] is True
    assert ctrl.deleted == []


# ---------------------------------------------------------------------------
# The probe primitives, directly
# ---------------------------------------------------------------------------


def test_the_waiter_reports_whether_it_ever_got_a_parseable_answer(monkeypatch):
    """-1 (non-200/transport error) and 0 (a real, empty tools array) both clamp to
    0 in the return value, so the out-param is the only way to tell them apart."""
    monkeypatch.setattr(gateway_deployer.time, "sleep", lambda *_a: None)

    monkeypatch.setattr(gateway_deployer, "_count_served_tools", lambda *a: -1)
    probe: dict = {}
    assert gateway_deployer._wait_for_gateway_to_serve_tools(GW_URL, {}, 9, timeout=1, probe=probe) == 0
    assert probe["got_valid_response"] is False
    assert probe["last_status"], "a never-answered probe must carry a human-readable reason"

    monkeypatch.setattr(gateway_deployer, "_count_served_tools", lambda *a: 0)
    probe = {}
    assert gateway_deployer._wait_for_gateway_to_serve_tools(GW_URL, {}, 9, timeout=1, probe=probe) == 0
    assert probe["got_valid_response"] is True, "an empty tools array IS an answer: the plane is up"
    assert probe["last_status"] == ""

    # A caller that passes no probe must keep working unchanged.
    assert gateway_deployer._wait_for_gateway_to_serve_tools(GW_URL, {}, 9, timeout=1) == 0


def test_a_non_200_tools_list_probe_logs_its_status(monkeypatch, caplog):
    """Without this line a 403 left no trace at all, which is why the original
    diagnosis had no evidence either way. The STATUS only -- never the body, which
    an MCP error can echo back along with the bearer token this probe sends."""
    import urllib3

    class _Resp:
        status = 403
        data = b'{"error":"Forbidden","echo":{"Authorization":"Bearer leaked.jwt.value"}}'

    class _Http:
        def request(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(urllib3, "PoolManager", lambda *a, **kw: _Http())
    monkeypatch.setattr(gateway_deployer, "get_cognito_token", lambda ci: "leaked.jwt.value")

    with caplog.at_level("WARNING", logger=gateway_deployer.logger.name):
        assert gateway_deployer._count_served_tools(GW_URL, {}) == -1

    text = caplog.text
    assert "403" in text and "not 200" in text
    assert "leaked.jwt.value" not in text, "the probe's own bearer token must not be logged"
    assert "Forbidden" not in text, "the response body must not be logged"


def test_tools_list_probe_skips_malformed_sse_frames_before_a_valid_result(monkeypatch):
    """One malformed stream frame must not hide a later valid tools/list result."""
    import urllib3

    class _Resp:
        status = 200
        data = (
            b'data: {"result": []}\ndata: {not-json}\ndata: {"jsonrpc":"2.0","result":{"tools":[{"name":"healthy"}]}}\n'
        )

    class _Http:
        def request(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(urllib3, "PoolManager", lambda *a, **kw: _Http())
    monkeypatch.setattr(gateway_deployer, "get_cognito_token", lambda ci: "tok")

    assert gateway_deployer._count_served_tools(GW_URL, {}) == 1


def test_qualified_tool_probe_skips_a_malformed_tool_frame(monkeypatch):
    """A non-object tool entry is ignored with its frame, not raised to the caller."""
    import urllib3

    class _Resp:
        status = 200
        data = (
            b'data: {"result":{"tools":[1]}}\n'
            b'data: {"result":{"tools":[{"name":"healthy"},{"description":"unnamed"}]}}\n'
        )

    class _Http:
        def request(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(urllib3, "PoolManager", lambda *a, **kw: _Http())
    monkeypatch.setattr(gateway_deployer, "get_cognito_token", lambda ci: "tok")

    assert gateway_deployer._qualified_tools_from_served(GW_URL, {}) == ["healthy"]


def _public_addrinfo(host, port, *_a, **_k):
    """One public A record (an AWS-owned address), what a real Cognito host resolves to."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("52.94.236.248", port))]


def test_a_failed_token_mint_reports_its_status_and_not_the_response_body(monkeypatch):
    """The token endpoint's response body is an uncontrolled payload -- for an
    external IDP it comes from a third party -- and an OAuth error_description
    commonly echoes the request, which here carries client_secret in its form body.
    So the status is the diagnosis and the body must not travel."""
    import urllib3

    class _Resp:
        status = 401
        data = (
            b'{"error":"invalid_client","error_description":"client_secret=notarealclientsecret-0000000000 rejected"}'
        )

    class _Http:
        def request(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(urllib3, "PoolManager", lambda *a, **kw: _Http())
    monkeypatch.setattr(gateway_deployer, "resolve_client_secret", lambda ci: SECRET)
    # Hermetic: the real validator runs, over a public answer from a stubbed resolver.
    monkeypatch.setattr(socket, "getaddrinfo", _public_addrinfo)

    with pytest.raises(gateway_deployer.TokenRequestError) as ei:
        gateway_deployer.get_cognito_token(
            {
                "client_id": "cid",
                "token_endpoint": "https://x.auth.us-east-1.amazoncognito.com/oauth2/token",
                "scope": "s/invoke",
            }
        )

    assert ei.value.status == 401, "callers need the status without parsing the message"
    msg = str(ei.value)
    assert "401" in msg
    assert "invalid_client" not in msg, "the response body must not travel in the exception"
    assert SECRET not in msg


def test_an_unresolvable_token_endpoint_fails_closed_before_any_request(monkeypatch):
    """No DNS answer is not a pass: nothing is POSTed and the secret is never read."""
    import urllib3

    def _gaierror(*_a, **_k):
        raise socket.gaierror(8, "nodename nor servname provided, or not known")

    monkeypatch.setattr(socket, "getaddrinfo", _gaierror)
    monkeypatch.setattr(urllib3, "PoolManager", lambda *a, **kw: pytest.fail("a request was sent"))
    monkeypatch.setattr(gateway_deployer, "resolve_client_secret", lambda ci: pytest.fail("the secret was read"))

    with pytest.raises(ValueError, match="could not be resolved"):
        gateway_deployer.get_cognito_token(
            {
                "client_id": "cid",
                "token_endpoint": "https://x.auth.us-east-1.amazoncognito.com/oauth2/token",
                "scope": "s/invoke",
            }
        )


def test_a_transport_error_in_the_probe_is_logged_at_warning_and_names_no_detail(monkeypatch, caplog):
    """This is the line that would have named the real cause and did not exist at a
    visible level. The deployed step Lambdas set no log level, so they inherit the
    Lambda runtime's root handler at WARNING and every logger.info here is
    discarded -- measured live: the gateway was created yet "Created gateway: %s"
    logged nothing. An INFO-level diagnostic is not a diagnostic in production."""
    import urllib3

    class _Http:
        def request(self, *a, **kw):
            raise AssertionError("must not be reached; the token mint fails first")

    monkeypatch.setattr(urllib3, "PoolManager", lambda *a, **kw: _Http())

    def _token_fails(_ci):
        raise gateway_deployer.TokenRequestError(400)

    monkeypatch.setattr(gateway_deployer, "get_cognito_token", _token_fails)

    with caplog.at_level("WARNING", logger=gateway_deployer.logger.name):
        assert gateway_deployer._count_served_tools(GW_URL, {}) == -1

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "an INFO-level probe diagnostic is invisible in the deployed Lambda"
    text = caplog.text
    assert "TokenRequestError" in text, "the exception TYPE is what makes the cause actionable"
    assert "token endpoint HTTP 400" in text
    # ...and still nothing that could carry a credential or a full URL.
    assert SECRET not in text


def test_the_probe_reports_a_connection_failure_by_type_alone(monkeypatch, caplog):
    """A urllib3/ssl error's str() carries the full URL, so only the type is logged.
    Pins that the branch does not regress to str(e)."""
    import urllib3

    class _Http:
        def request(self, *a, **kw):
            raise urllib3.exceptions.MaxRetryError(
                pool=None,
                url="https://agent-gateway-fake0000.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
                reason="nodename nor servname provided",
            )

    monkeypatch.setattr(urllib3, "PoolManager", lambda *a, **kw: _Http())
    monkeypatch.setattr(gateway_deployer, "get_cognito_token", lambda ci: "tok")

    with caplog.at_level("WARNING", logger=gateway_deployer.logger.name):
        assert gateway_deployer._count_served_tools(GW_URL, {}) == -1

    text = caplog.text
    assert "MaxRetryError" in text
    assert "nodename nor servname" not in text, "the branch must log the type, not str(e)"


def test_an_incomplete_target_fails_the_deploy_without_orphaning_the_gateway(monkeypatch):
    """The sibling of the orphan test above, for the *new* failure point.

    ``_deploy_config_targets`` used to skip a declared target with no payload and log a
    warning, so a gateway with a blank Lambda ARN deployed "successfully" serving no
    tool (observed live: ``Gateway lambda target #0 has no function_arn; skipping``).
    It now raises — and the reason that is safe is measured here, not assumed: the
    Step-4e call site sits inside ``deploy_gateway``'s own ``try``, whose handler runs
    the abort cleanup and returns the partial inventory. Nothing in
    ``gateway_deployer`` writes a manifest row itself; ``gateway_step`` only writes
    them from a returned dict (``gateway_step.py:304``), so an exception that escaped
    ``deploy_gateway`` instead of being converted here would leak the gateway, its IAM
    role and its Cognito pool permanently, with nothing naming them.
    """
    ctrl = _FakeCtrl()
    cleanup_calls = _install(monkeypatch, ctrl=ctrl, served=9, expected=9)

    out = gateway_deployer.deploy_gateway(
        {"name": "agent-gateway", "targets": [{"type": "lambda", "function_arn": ""}]},
        "us-east-1",
        deployment_id="d-1",
    )

    assert out["success"] is False
    # The message must be actionable: which target, and what is missing.
    assert "#0" in out["error"]
    assert "function_arn" in out["error"]
    assert "silently absent" in out["error"]
    # Everything created before the failure comes back, so teardown can reach it.
    assert out["gateway_id"] == GW_ID
    assert out["client_info"]["user_pool_id"] == "us-east-1_FAKEPOOL"
    assert [c["gateway_id"] for c in cleanup_calls] == [GW_ID]
    # This dict travels into a RuntimeError message and an SFN failure cause.
    assert SECRET not in repr(out)
    # No target was created — the gateway never reached the tool plane.
    assert ctrl.created == ["agent-gateway"]


def _custom_tool() -> dict:
    return {
        "toolName": "lookup",
        "description": "Lookup a record",
        "lambdaCode": "def lambda_handler(event, context):\n    return {'ok': True}\n",
        "inputSchema": {"type": "object", "properties": {}},
    }


def test_custom_tool_lambda_failure_returns_the_already_created_role_for_cleanup(monkeypatch):
    ctrl = _FakeCtrl()
    cleanup_calls = _install(monkeypatch, ctrl=ctrl, served=1, expected=1)
    monkeypatch.setattr(
        gateway_deployer,
        "_ensure_lambda_role",
        lambda *args, **kwargs: "arn:aws:iam::1:role/custom-role",
    )
    monkeypatch.setattr(
        gateway_deployer,
        "_create_or_update_lambda",
        MagicMock(side_effect=RuntimeError("CreateFunction failed")),
    )

    out = gateway_deployer.deploy_gateway(
        {"name": "agent-gateway"},
        "us-east-1",
        custom_tools=[_custom_tool()],
        owner_sub="owner-a",
        deployment_id="d-custom-role",
    )

    _fn, expected_role, _safe, _binding = gateway_deployer._custom_tool_resource_names(
        "lookup",
        "owner-a",
        GW_ID,
        "us-east-1",
    )
    assert out["success"] is False
    assert out["custom_tool_roles"] == [expected_role]
    assert out["custom_tool_lambdas"] == []
    assert cleanup_calls[0]["custom_tool_roles"] == [expected_role]


def test_custom_tool_target_failure_returns_both_lambda_and_role_for_cleanup(monkeypatch):
    ctrl = _FakeCtrl()
    cleanup_calls = _install(monkeypatch, ctrl=ctrl, served=1, expected=1)
    monkeypatch.setattr(
        gateway_deployer,
        "_ensure_lambda_role",
        lambda *args, **kwargs: "arn:aws:iam::1:role/custom-role",
    )
    monkeypatch.setattr(
        gateway_deployer,
        "_create_or_update_lambda",
        lambda *args, **kwargs: "arn:aws:lambda:us-east-1:1:function:custom",
    )
    monkeypatch.setattr(
        gateway_deployer,
        "_create_gateway_target_with_retry",
        MagicMock(side_effect=RuntimeError("CreateGatewayTarget failed")),
    )

    out = gateway_deployer.deploy_gateway(
        {"name": "agent-gateway"},
        "us-east-1",
        custom_tools=[_custom_tool()],
        owner_sub="owner-a",
        deployment_id="d-custom-target",
    )

    expected_fn, expected_role, _safe, _binding = gateway_deployer._custom_tool_resource_names(
        "lookup",
        "owner-a",
        GW_ID,
        "us-east-1",
    )
    assert out["success"] is False
    assert out["custom_tool_roles"] == [expected_role]
    assert out["custom_tool_lambdas"] == [expected_fn]
    assert cleanup_calls[0]["custom_tool_roles"] == [expected_role]
    assert cleanup_calls[0]["custom_tool_lambdas"] == [expected_fn]


def test_hosted_mcp_target_that_never_becomes_ready_fails_with_cleanup_inventory(monkeypatch):
    ctrl = _FakeCtrl()
    cleanup_calls = _install(monkeypatch, ctrl=ctrl, served=1, expected=1)
    monkeypatch.setattr(
        gateway_deployer,
        "_ensure_oauth2_credential_provider",
        lambda *args, **kwargs: "arn:aws:bedrock-agentcore:us-east-1:1:provider/mcp",
    )
    monkeypatch.setattr(
        gateway_deployer,
        "_create_gateway_target_with_retry",
        lambda *args, **kwargs: {"targetId": "mcp-target"},
    )
    secret_ref = "arn:aws:secretsmanager:us-east-1:1:secret:agentcore-connector/owner/ref"

    out = gateway_deployer.deploy_gateway(
        {"name": "agent-gateway"},
        "us-east-1",
        owner_sub="owner-a",
        deployment_id="d-mcp-not-ready",
        secrets_prebound=True,
        mcp_server_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/mcp",
        mcp_oauth={
            "discovery_url": "https://issuer.example/.well-known/openid-configuration",
            "client_id": "client",
            "scope": "mcp/invoke",
            "client_secret_ref": secret_ref,
        },
    )

    assert out["success"] is False
    assert "did not reach READY" in out["error"]
    assert out["lambda_function_name"] == ""
    assert out["connector_secret_arns"] == [secret_ref]
    assert out["connector_credential_providers"]
    assert cleanup_calls[0]["connector_secret_arns"] == [secret_ref]
    assert cleanup_calls[0]["connector_credential_providers"]


def _shared_pool_cognito(monkeypatch):
    """Re-point the Cognito double at the SHARED-pool shape.

    Mirrors what ``_create_cognito_oauth_in_shared_pool`` really returns: no raw
    secret, a Secrets Manager reference instead, and the ``shared_pool`` marker that
    tells every teardown path the pool is platform-owned.

    A double that claims to mirror a producer has to be re-checked when the producer
    changes, and this one drifted once already: it lost ``minted_client_secret_ref``
    the day the manifest started reading that key instead of ``client_secret_ref``,
    which made the secret-row assertion below fail for a reason that had nothing to do
    with what this test is about. The real producers' key sets are pinned directly in
    test_the_manifest_never_deletes_a_customer_owned_secret.py.
    """
    monkeypatch.setattr(
        gateway_deployer,
        "_create_cognito_oauth",
        lambda *a, **kw: {
            "authorizer_config": {
                "customJWTAuthorizer": {
                    "discoveryUrl": "https://x/.well-known/openid-configuration",
                    "allowedClients": ["cid"],
                }
            },
            "client_info": {
                "user_pool_id": "us-east-1_FAKEPOOL",
                "client_id": "cid",
                "client_secret_ref": "arn:aws:secretsmanager:us-east-1:1:secret:gw-cs-abc",
                # The same ARN under the key that means "this deploy minted it". Only
                # this one authorises a delete; the external-IDP path sets the other key
                # alone, and it points at the CUSTOMER's secret.
                "minted_client_secret_ref": "arn:aws:secretsmanager:us-east-1:1:secret:gw-cs-abc",
                "token_endpoint": "https://x/oauth2/token",
                "scope": "agentcore-agent-gateway/invoke",
                "shared_pool": True,
                # Present only to prove the allow-list excludes it. The real shared-pool
                # path never puts a raw secret in client_info.
                "client_secret": SECRET,
            },
        },
    )


def test_a_failed_shared_pool_deploy_hands_back_the_credentials_it_must_revoke(monkeypatch):
    """The abort inventory reduced ``client_info`` to {provider, user_pool_id}, which
    kept the secret out and took three teardown handles with it.

    Measured live 2026-09-21 on a failed deploy of ``gwfixaafac57e``: the app client
    ``gwfixaafac57e-client`` (secret still mintable) and resource server
    ``agentcore-gwfixaafac57e`` were left in the platform pool, because neither the
    abort cleanup nor the manifest could name them.
    """
    ctrl = _FakeCtrl()
    _install(monkeypatch, ctrl=ctrl, served=9, expected=9)
    _shared_pool_cognito(monkeypatch)

    out = gateway_deployer.deploy_gateway(
        {"name": "agent-gateway", "targets": [{"type": "lambda", "function_arn": ""}]},
        "us-east-1",
        deployment_id="d-1",
    )

    ci = out["client_info"]
    assert out["success"] is False
    # What teardown needs to revoke this gateway's access...
    assert ci["client_id"] == "cid"
    assert ci["scope"] == "agentcore-agent-gateway/invoke"
    assert ci["shared_pool"] is True
    # ...including the per-deployment Secrets Manager copy of the client secret, which
    # is a NAME for the credential and was otherwise orphaned.
    assert ci["client_secret_ref"] == "arn:aws:secretsmanager:us-east-1:1:secret:gw-cs-abc"
    # ...but never the credential itself: this dict reaches an SFN failure cause.
    assert "client_secret" not in ci
    assert SECRET not in repr(out)


def test_a_failed_shared_pool_deploy_does_not_claim_it_created_the_platform_pool(monkeypatch):
    """End-to-end over BOTH halves, because each half looked correct alone.

    With ``shared_pool`` missing from the abort inventory, the recorder took its "a pool
    THIS deployment created" branch and wrote a deletable ``cognito_user_pool`` row for
    the platform's shared gateway-auth pool — the one holding every other gateway's app
    client. Live, that row named ``us-east-1_qiYLOs3Ij``; only two later guards
    (``is_platform_owned_user_pool``, then ``classify_user_pool``) stopped the delete.
    A test of either function on its own passes either way, so this one composes the
    real deploy failure with the real recorder.
    """
    from app.step_handlers import gateway_step

    ctrl = _FakeCtrl()
    _install(monkeypatch, ctrl=ctrl, served=9, expected=9)
    _shared_pool_cognito(monkeypatch)

    out = gateway_deployer.deploy_gateway(
        {"name": "agent-gateway", "targets": [{"type": "lambda", "function_arn": ""}]},
        "us-east-1",
        deployment_id="d-1",
    )

    rows: list[dict] = []
    store = MagicMock()
    store.record_resource.side_effect = lambda _dep, row: rows.append(row)
    gateway_step._record_gateway_resources(store, "d-1", "us-east-1", out)

    types = [r["type"] for r in rows]
    assert "cognito_user_pool" not in types, (
        f"a failed deploy recorded the platform's shared gateway-auth pool as its own deletable resource: {rows}"
    )
    assert "cognito_app_client" in types
    assert "cognito_resource_server" in types
    # The minted client-secret reference gets its row too.
    assert [r["id"] for r in rows if r["type"] == "secret"] == ["arn:aws:secretsmanager:us-east-1:1:secret:gw-cs-abc"]
    app_client = next(r for r in rows if r["type"] == "cognito_app_client")
    assert app_client["id"] == "cid"
    # pool_id is what the teardown arm re-verifies ownership against; without it the
    # row is skipped rather than guessed.
    assert app_client["pool_id"] == "us-east-1_FAKEPOOL"


def test_a_failed_deploy_records_no_lambda_when_it_built_none(monkeypatch):
    """``lambda_function_name`` defaulted to the hard-coded ``AgentCoreLambdaTestFunction``
    — a name no branch creates — and every consumer treats that field as a function to
    DELETE.

    CloudTrail, 2026-09-21T00:47:32Z and 00:47:39Z: two ``DeleteFunction`` calls for
    that name, one under the gateway step's role and one under the status-update step's,
    both ``ResourceNotFoundException`` only because no such function happened to exist.
    """
    ctrl = _FakeCtrl()
    _install(monkeypatch, ctrl=ctrl, served=9, expected=9)

    out = gateway_deployer.deploy_gateway(
        {"name": "agent-gateway", "targets": [{"type": "lambda", "function_arn": ""}]},
        "us-east-1",
        deployment_id="d-1",
    )

    assert out["lambda_function_name"] == ""
    assert "AgentCoreLambdaTestFunction" not in repr(out)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def _adopting(outcome_key: str):
    """A helper stub that reports the resource already existed (a redeploy's reuse)."""

    def _stub(*args, **kwargs):
        outcome = kwargs.get("outcome")
        if outcome is None:
            outcome = next((a for a in args if isinstance(a, dict)), None)
        if outcome is not None:
            outcome.clear()
            outcome["created"] = False
        return outcome_key

    return _stub


def test_a_redeploy_never_offers_the_reused_function_and_role_to_abort_cleanup(monkeypatch):
    """F-66: the function and role are the gateway's, not this deploy's.

    A redeploy that fails after reusing them must leave them for the live deployment
    that still invokes them; abort cleanup deletes every name in the created lists.
    """
    ctrl = _FakeCtrl()
    cleanup_calls = _install(monkeypatch, ctrl=ctrl, served=1, expected=1)
    monkeypatch.setattr(gateway_deployer, "_ensure_lambda_role", _adopting("arn:aws:iam::1:role/custom-role"))
    monkeypatch.setattr(
        gateway_deployer, "_create_or_update_lambda", _adopting("arn:aws:lambda:us-east-1:1:function:custom")
    )
    monkeypatch.setattr(
        gateway_deployer,
        "_create_gateway_target_with_retry",
        MagicMock(side_effect=RuntimeError("UpdateGatewayTarget failed")),
    )

    out = gateway_deployer.deploy_gateway(
        {"name": "agent-gateway"},
        "us-east-1",
        custom_tools=[_custom_tool()],
        owner_sub="owner-a",
        deployment_id="d-redeploy",
    )

    fn, role, _safe, _binding = gateway_deployer._custom_tool_resource_names("lookup", "owner-a", GW_ID, "us-east-1")
    assert out["success"] is False
    assert out["custom_tool_lambdas"] == [] and out["custom_tool_roles"] == []
    assert out["custom_tool_lambdas_adopted"] == [fn]
    assert out["custom_tool_roles_adopted"] == [role]
    assert cleanup_calls[0]["custom_tool_lambdas"] == []
    assert cleanup_calls[0]["custom_tool_roles"] == []
    # F-74c: the adopted rows are the ones that must eventually reclaim these two, so the
    # binding and the role->function pairing have to survive to the manifest writers. This is
    # the FAILED result, assembled from `locals()`, which is the path a new field is dropped
    # from: the success result lists its keys explicitly and would fail loudly instead.
    assert out["custom_tool_bindings"] == {fn: _binding, role: _binding}
    assert out["custom_tool_pairs"] == {role: fn}


def test_a_redeploy_points_the_shared_target_at_the_tool_it_just_deployed(monkeypatch):
    """F-66a, measured live: the conflict branch reused the target unchanged.

    It kept invoking the first deployment's function with the first deployment's
    schema, so a redeploy's changed tool never took effect.
    """
    ctrl = _FakeCtrl()
    _install(monkeypatch, ctrl=ctrl, served=1, expected=1)
    monkeypatch.setattr(gateway_deployer, "_ensure_lambda_role", lambda *a, **k: "arn:aws:iam::1:role/custom-role")
    monkeypatch.setattr(
        gateway_deployer, "_create_or_update_lambda", lambda *a, **k: "arn:aws:lambda:us-east-1:1:function:custom"
    )
    target = MagicMock(return_value={"targetId": "t-1"})
    monkeypatch.setattr(gateway_deployer, "_create_gateway_target_with_retry", target)

    gateway_deployer.deploy_gateway(
        {"name": "agent-gateway"},
        "us-east-1",
        custom_tools=[_custom_tool()],
        owner_sub="owner-a",
        deployment_id="d-redeploy",
    )

    ct_calls = [c for c in target.call_args_list if str(c.args[2]).startswith("CT-")]
    assert len(ct_calls) == 1
    assert ct_calls[0].kwargs.get("update_existing") is True
    lam = ct_calls[0].args[3]["targetConfiguration"]["mcp"]["lambda"]
    assert lam["lambdaArn"] == "arn:aws:lambda:us-east-1:1:function:custom"


class _ConflictCtrl:
    """A gateway whose ``CT-lookup`` target already exists."""

    class exceptions:  # noqa: N801 - mirrors the boto3 attribute
        pass

    def __init__(self, statuses=("UPDATING", "READY"), listed=True):
        self.statuses = list(statuses)
        self.listed = listed
        self.applied: dict | None = None
        self.updates: list[dict] = []

    def create_gateway_target(self, **kw):
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": "ConflictException", "Message": "already exists"}}, "CreateGatewayTarget")

    def list_gateway_targets(self, **kw):
        items = [{"name": "CT-lookup", "targetId": "T1"}] if self.listed else []
        return {"items": items}

    def update_gateway_target(self, **kw):
        self.updates.append(kw)
        self.applied = {key: kw[key] for key in gateway_deployer._TARGET_REPLACE_KEYS if key in kw}
        return {}

    def get_gateway_target(self, **kw):
        return {
            "status": self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0],
            **(self.applied or {}),
        }


_CT_PARAMS = {
    "gatewayIdentifier": "gw-1",
    "clientToken": "create-only",
    "name": "CT-lookup",
    "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": "arn:new", "toolSchema": {}}}},
    "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
}


def test_an_existing_target_is_replaced_with_every_field_update_accepts(monkeypatch):
    monkeypatch.setattr(gateway_deployer.time, "sleep", lambda _s: None)
    ctrl = _ConflictCtrl()

    out = gateway_deployer._create_gateway_target_with_retry(
        ctrl, "gw-1", "CT-lookup", dict(_CT_PARAMS), update_existing=True
    )

    assert out["targetId"] == "T1" and out["status"] == "READY"
    assert ctrl.updates == [
        {
            "gatewayIdentifier": "gw-1",
            "targetId": "T1",
            "name": "CT-lookup",
            "targetConfiguration": _CT_PARAMS["targetConfiguration"],
            "credentialProviderConfigurations": _CT_PARAMS["credentialProviderConfigurations"],
        }
    ]


def test_every_update_field_is_in_the_real_update_gateway_target_shape():
    """Against botocore's model, not memory: an unknown field fails the call live."""
    import boto3

    shape = (
        boto3.client("bedrock-agentcore-control", region_name="us-east-1")
        .meta.service_model.operation_model("UpdateGatewayTarget")
        .input_shape
    )
    ctrl = _ConflictCtrl(statuses=("READY",))
    gateway_deployer._create_gateway_target_with_retry(
        ctrl, "gw-1", "CT-lookup", dict(_CT_PARAMS), update_existing=True
    )
    assert set(ctrl.updates[0]) <= set(shape.members)
    assert set(shape.required_members) <= set(ctrl.updates[0])


@pytest.mark.parametrize(
    ("ctrl", "match"),
    [
        (_ConflictCtrl(statuses=("FAILED",)), "reached FAILED"),
        (_ConflictCtrl(listed=False), "could not be found to update"),
    ],
)
def test_a_target_that_cannot_be_updated_fails_the_deploy(monkeypatch, ctrl, match):
    """Silently keeping the old target is the F-66a defect itself."""
    monkeypatch.setattr(gateway_deployer.time, "sleep", lambda _s: None)
    with pytest.raises(RuntimeError, match=match):
        gateway_deployer._create_gateway_target_with_retry(
            ctrl, "gw-1", "CT-lookup", dict(_CT_PARAMS), update_existing=True
        )


def test_other_targets_keep_the_reuse_on_conflict_behaviour(monkeypatch):
    ctrl = _ConflictCtrl()
    out = gateway_deployer._create_gateway_target_with_retry(ctrl, "gw-1", "CT-lookup", dict(_CT_PARAMS))
    assert ctrl.updates == []
    assert out["targetId"] == "T1"


def test_reused_function_and_role_are_recorded_as_references_not_as_created():
    from app.step_handlers.gateway_step import _gateway_manifest_resources

    rows = _gateway_manifest_resources(
        "us-east-1",
        {
            "gateway_id": GW_ID,
            "custom_tool_lambdas": ["fn-new"],
            "custom_tool_roles": ["role-new"],
            "custom_tool_lambdas_adopted": ["fn-reused"],
            "custom_tool_roles_adopted": ["role-reused"],
        },
    )
    by_name = {(r["type"], r.get("name")): r["created_by_deployment"] for r in rows if r.get("name")}
    assert by_name[("lambda", "fn-new")] is True
    assert by_name[("iam_role", "role-new")] is True
    assert by_name[("lambda", "fn-reused")] is False
    assert by_name[("iam_role", "role-reused")] is False


@contextlib.contextmanager
def _no_lock(ctrl, _region, gateway_id, **_k):
    """The mutant: the same delete-and-confirm discipline, no mutual exclusion."""
    yield gml.GatewayLock(ctrl, gateway_id, lambda _s: None)


@pytest.mark.parametrize("locked", [True, False], ids=["with-the-lock", "mutant-without-it"])
def test_the_recreate_does_not_delete_inside_another_writers_window(monkeypatch, gateway_lock_table, locked):
    """F-66e at the fourth DeleteGateway site: the retry-recreate.

    Another writer holds the gateway between its read and its update. The recreate
    must not delete it then; with the lock it waits, gives up as busy, and stops the
    loop with the busy error instead of recreating. The mutant deletes inside the window.
    """
    now = [1_000_000.0]
    monkeypatch.setattr(
        gml, "time", SimpleNamespace(time=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
    )
    ctrl = _FakeCtrl()
    _install(monkeypatch, ctrl=ctrl, served=0, got_valid_response=True)
    original = gateway_deployer.deploy_gateway
    monkeypatch.setattr(gateway_deployer, "deploy_gateway", lambda *a, **kw: {"success": True, "recursed": True})
    lock = gml.gateway_mutation_lock if locked else _no_lock
    if not locked:
        monkeypatch.setattr(gateway_deployer, "gateway_mutation_lock", _no_lock)

    with lock(ctrl, "us-east-1", GW_ID):
        out = original({"name": "agent-gateway"}, "us-east-1", deployment_id="d-lock", gateway_retry=0)
        deleted_inside_window = list(ctrl.deleted)

    if locked:
        assert deleted_inside_window == []
        assert out.get("recursed") is not True, "a recreate that could not delete must not recurse"
        assert "being changed by another operation" in out["error"]
        assert gateway_lock_table.items == {}
    else:
        assert deleted_inside_window == [GW_ID]


# ---------------------------------------------------------------------------
# F-74 (peer 71): a create whose target never serves is not a deployed tool
# ---------------------------------------------------------------------------


class _TargetCtrl:
    """create_gateway_target succeeds; get_gateway_target answers a scripted status run."""

    def __init__(self, statuses, reasons=None):
        self._statuses = list(statuses)
        self._reasons = reasons
        self.creates = 0
        self.reads = 0

    def create_gateway_target(self, **kwargs):
        self.creates += 1
        return {"targetId": "tgt-1", "name": kwargs.get("name")}

    def get_gateway_target(self, **_kwargs):
        self.reads += 1
        status = self._statuses[min(self.reads - 1, len(self._statuses) - 1)]
        out = {"status": status}
        if status in gateway_deployer._TARGET_TERMINAL_FAILURE_STATUSES and self._reasons is not None:
            out["statusReasons"] = self._reasons
        return out


@pytest.fixture
def _no_target_sleep(monkeypatch):
    monkeypatch.setattr(gateway_deployer, "time", SimpleNamespace(sleep=lambda *_a: None))


def _create_target(ctrl):
    return gateway_deployer._create_gateway_target_with_retry(
        ctrl, GW_ID, "DynamicTools", {"gatewayIdentifier": GW_ID, "name": "DynamicTools"}
    )


def test_a_target_that_reaches_ready_is_returned(_no_target_sleep):
    """Positive control for the two refusals below: the guard is not 'always raise'."""
    ctrl = _TargetCtrl(["CREATING", "CREATING", "READY"])
    assert _create_target(ctrl)["targetId"] == "tgt-1"
    assert ctrl.creates == 1


@pytest.mark.parametrize(
    "terminal",
    ["FAILED", "CREATE_FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"],
)
def test_a_terminal_target_status_raises_with_the_services_own_reasons(terminal, _no_target_sleep):
    """F-74/peer 71: a create used to be returned as success the moment the API accepted it,
    so a target that then went FAILED was reported as a deployed tool. The raise must carry
    the status AND the service's statusReasons -- ignoring the terminal statuses and letting
    the poll budget expire instead loses the only text that says WHY, so the operator is told
    'did not reach READY' about a target the service had already explained."""
    ctrl = _TargetCtrl(["CREATING", terminal], reasons=["lambda arn is not invocable by the gateway role"])
    with pytest.raises(gateway_deployer._TargetTerminalFailure) as err:
        _create_target(ctrl)
    msg = str(err.value)
    assert terminal in msg
    assert "lambda arn is not invocable by the gateway role" in msg
    assert "tgt-1" in msg and "DynamicTools" in msg
    # And it is NOT retried as a create: a second create_gateway_target on a gateway that
    # already holds the name is how one failed target becomes two.
    assert ctrl.creates == 1


def test_a_terminal_target_failure_is_never_retried_as_a_create(_no_target_sleep):
    """The re-raise past the conflict handling is the thing under test. Swallowing it sends
    the loop around again, and the second create conflicts with the target it just made."""
    ctrl = _TargetCtrl(["FAILED"], reasons=["boom"])
    with pytest.raises(gateway_deployer._TargetTerminalFailure):
        _create_target(ctrl)
    assert ctrl.creates == 1
    assert ctrl.reads == 1


def test_a_target_that_never_becomes_ready_exhausts_the_budget_and_raises(_no_target_sleep):
    ctrl = _TargetCtrl(["CREATING"])
    with pytest.raises(gateway_deployer._TargetTerminalFailure) as err:
        _create_target(ctrl)
    assert "did not reach READY" in str(err.value)
    assert "CREATING" in str(err.value)
    assert ctrl.creates == 1

"""The gateway's own OAuth client secret travels as a Secrets Manager REFERENCE.

It used to travel as a pool id plus a ``cognito-idp:DescribeUserPoolClient`` grant, on
the reasoning that a reference is only needed for an external IDP because Cognito can
always be re-read. That reasoning has a hole IAM cannot patch: ``DescribeUserPoolClient``
has exactly one IAM resource type, ``userpool``, with NO granularity below it. So a grant
that lets an agent read its own app client's secret also reads every other app client's
secret in the same pool — which in the default shared-pool mode is every co-resident
deployment's gateway credential.

The dedicated-pool mode was not safe either, and that is the part no reviewer caught: the
grant was conditioned on ``aws:ResourceTag/AgentCoreStack``, which reads like "only pools
this deployment created", but the tag VALUE is ``{project}-{env}-{region}`` — the STACK,
stamped identically by every deployment in it. A condition that looked narrow authorized
the same cross-tenant read as an exact-ARN grant.

There is no narrower Cognito grant to retreat to, so the credential moved:
``gateway_deployer._mint_client_secret_ref`` writes it to a per-deployment Secrets Manager
secret, which DOES scope per ARN. These tests pin the three places that has to hold for
the move to be real rather than additive — the runtime environment, the teardown manifest,
and the pre-existing-deployment fallback.

ARCC ``cnt_n8LpZcqYi2t3I2``: never hold a secret in an environment variable.
ARCC ``cnt_LuG2TKuO0errRp``: a secret grant belongs only where the principal needs it.
"""

import pytest

REGION = "us-east-1"
POOL = "us-east-1_SHAREDPOOL"
REF = "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-connector/u/abc-AbCdEf"


class _FakeStore:
    def __init__(self):
        self.resources: list[dict] = []
        self.steps: list[tuple] = []

    def update_step(self, *a, **kw):
        self.steps.append((a, kw))

    def record_resource(self, deployment_id, resource):
        self.resources.append(resource)


# ---------------------------------------------------------------------------
# The runtime environment
# ---------------------------------------------------------------------------


def _configure_env(monkeypatch, gateway_result: dict) -> dict:
    """Drive the real ``runtime_configure_step.handler`` and return the env vars it
    would put on the AgentCore runtime. Asserting on the handler rather than on a
    helper matters here: the pool id and the reference are chosen by an if/elif, and
    a test of a pure function cannot show that only one arm was taken."""
    from app.step_handlers import runtime_configure_step as rcs

    monkeypatch.setattr(rcs, "_get_deployment_store", lambda: _FakeStore())
    monkeypatch.setattr(rcs.step_clients, "client", lambda event, svc, **kw: object())
    monkeypatch.setattr(rcs, "sanitize_runtime_name", lambda n: "agent_x")
    monkeypatch.setattr(rcs, "build_otel_env_vars", lambda *a, **kw: {})
    monkeypatch.setattr(rcs, "get_platform_observability_defaults", lambda: {})
    # Runtime-log governance is covered independently; this helper measures only
    # which credential references enter the runtime environment.
    monkeypatch.setattr(rcs, "govern_default_runtime_log_group", lambda *_a, **_kw: None)
    monkeypatch.delenv("TAG_POLICY_TABLE_NAME", raising=False)

    captured: dict = {}

    def _fake_create(**kwargs):
        captured["env_vars"] = kwargs.get("env_vars") or {}
        return {"runtime_id": "agent_x-123", "runtime_arn": "arn:runtime"}

    monkeypatch.setattr(rcs, "create_agent_runtime", _fake_create)
    rcs.handler(
        {
            "deployment_id": "d1",
            "config": {
                "name": "agent",
                "entrypoint": "agent.py",
                "model": {"modelId": "anthropic.claude-sonnet-5"},
            },
            "role_arn": "arn:role",
            "s3_bucket": "b",
            "s3_key": "k",
            "gateway_result": gateway_result,
        },
        None,
    )
    return captured["env_vars"]


@pytest.mark.parametrize("key", ["client_secret_ref", "clientSecretRef"])
def test_the_reference_replaces_the_pool_id_not_joins_it(monkeypatch, key):
    """Both spellings, because ``client_info`` crosses a JSON boundary between the
    gateway step and this one and the API models are camelCase. Reading only snake_case
    would silently take the elif arm and re-emit the pool id."""
    env = _configure_env(
        monkeypatch,
        {
            "gateway_url": "https://gw.example.com/mcp",
            "client_info": {
                "provider": "cognito",
                "client_id": "abc123",
                "user_pool_id": POOL,
                key: REF,
                "token_endpoint": f"https://d.auth.{REGION}.amazoncognito.com/oauth2/token",
                "scope": "agentcore-gw/invoke",
            },
        },
    )
    assert env["OAUTH_CLIENT_SECRET_REF"] == REF
    assert "COGNITO_USER_POOL_ID" not in env, (
        "the pool id was emitted ALONGSIDE the reference. Nothing then reads it, but "
        "its presence is what a future 'the agent has the pool id, just add the grant' "
        "fix would point at — the mutual exclusion is the safety property, not the "
        "absence of a reader."
    )
    # The credential itself must not be here under any name. GetAgentRuntime returns a
    # runtime's environment variables in plaintext.
    assert "COGNITO_CLIENT_SECRET" not in env
    assert env["COGNITO_TOKEN_ENDPOINT"].endswith("/oauth2/token")
    assert env["COGNITO_SCOPE"] == "agentcore-gw/invoke"


def test_a_deployment_created_before_the_reference_still_configures(monkeypatch):
    """The fallback is not optional. A gateway deployed before ``_mint_client_secret_ref``
    existed has no ref in its stored ``client_info``, and a re-configure of that agent
    must still produce a runtime that can find its secret. It will FAIL to mint a token
    until it is redeployed — that is the recorded BREAKING migration step — but emitting
    neither value would turn a token failure into a configure-time crash."""
    env = _configure_env(
        monkeypatch,
        {
            "gateway_url": "https://gw.example.com/mcp",
            "client_info": {"provider": "cognito", "client_id": "abc123", "user_pool_id": POOL},
        },
    )
    assert env["COGNITO_USER_POOL_ID"] == POOL
    assert "OAUTH_CLIENT_SECRET_REF" not in env


def test_the_direct_deploy_path_agrees_with_the_step_functions_path():
    """``WorkflowExecutor.deploy`` builds the same env block for the non-Step-Functions
    deploy path, and it previously set BOTH keys unconditionally.

    This is a STRUCTURAL check on that one function's source, not a behavioural one: the
    function is a full orchestration coroutine and standing up enough of AWS to reach the
    env block would test the mocks. It is here because the two paths diverging is the
    concrete way this fix gets undone — the pool id is not read by anything, so a
    divergence produces no failure until someone re-adds the grant to make it work.
    """
    import inspect

    from app.services.deployment import WorkflowExecutor

    src = inspect.getsource(WorkflowExecutor.deploy)
    marker = 'env_vars["OAUTH_CLIENT_SECRET_REF"] = _external_ref'
    assert marker in src, "the direct deploy path does not emit the reference at all"
    tail = src[src.index(marker) : src.index(marker) + 400]
    assert 'elif client_info.get("user_pool_id")' in tail, (
        "the pool id is no longer guarded by the reference check in deployment.py, so "
        "the direct deploy path emits both keys"
    )


# ---------------------------------------------------------------------------
# The teardown manifest
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shared", [True, False], ids=["shared pool", "dedicated pool"])
def test_the_minted_secret_is_recorded_for_teardown(shared):
    """The secret is the one thing a shared-mode deploy creates that nothing else would
    clean up: the pool is deliberately NOT recorded in that mode (it is platform-owned
    and holds every other gateway's client), so without this row the secret outlives the
    deployment forever and Secrets Manager bills for it."""
    from app.step_handlers import gateway_step

    store = _FakeStore()
    # BOTH keys, because they mean different things and only one of them authorises a
    # delete. ``client_secret_ref`` is "where the runtime reads the secret from" and on
    # the external-IDP path it holds the CUSTOMER's own secret; the manifest therefore
    # reads ``minted_client_secret_ref``, which only a producer that actually minted the
    # secret sets. See test_the_manifest_never_deletes_a_customer_owned_secret.py — this
    # fixture asserted the recording half and passed with the single key for as long as
    # the manifest inferred ownership from the reference.
    client_info = {"user_pool_id": POOL, "client_secret_ref": REF, "minted_client_secret_ref": REF}
    if shared:
        client_info["shared_pool"] = True
    gateway_step._record_gateway_resources(
        store,
        "d1",
        REGION,
        {"gateway_id": "gw-1", "gateway_name": "agent-gateway", "client_info": client_info},
    )
    secrets = [r for r in store.resources if r.get("type") == "secret"]
    assert [r["id"] for r in secrets] == [REF], f"the minted client secret is not in the manifest: {store.resources}"
    assert secrets[0]["region"] == REGION, "a secret recorded without a region cannot be deleted"


def test_no_secret_row_is_invented_when_there_is_no_reference():
    """A row with an empty id is a delete call against nothing at teardown, which logs a
    failure label and makes a clean teardown look partial."""
    from app.step_handlers import gateway_step

    store = _FakeStore()
    gateway_step._record_gateway_resources(
        store, "d1", REGION, {"gateway_id": "gw-1", "client_info": {"user_pool_id": POOL}}
    )
    assert not [r for r in store.resources if r.get("type") == "secret"]

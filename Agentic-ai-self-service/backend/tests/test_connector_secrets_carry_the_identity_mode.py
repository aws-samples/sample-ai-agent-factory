"""F-01 (b): every connector secret is tagged ``IdentityMode=<shared|per_agent>`` at mint time.

The shared runtime role is one principal for every tenant in ``shared`` identity mode, and no IAM
condition on it can tell tenant A's connector secret from tenant B's (infra ledger F-01). What it
CAN do is stop reading the secrets of deployments that never use it: a ``per_agent`` deployment's
runtime runs under its own role with exact secret ARNs. Stamping the deployment's identity mode on
the secret lets infra add ``StringEquals aws:ResourceTag/IdentityMode=shared`` to the shared
role's ``GetSecretValue``, taking per-agent deployments' secrets out of its reach entirely.

The mode travels as a ContextVar set by the code that knows it (the API's credential staging and
the step handlers that mint), mirroring ``secret_intent_journal``: ``_put_connector_secret`` is
reached through many layers that never see the identity config. An unset context stamps
``shared``, which is the platform's default mode (``IdentityConfig.mode`` defaults to it) and the
value that keeps a shared-mode runtime readable -- a forgotten path therefore degrades to today's
behaviour, never to a dead agent.

Fixture values are fake.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from app.models.deployment_models import IdentityConfig
from app.services import gateway_deployer as gd

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "app"
REGION = "us-east-1"


@pytest.fixture(autouse=True)
def _stack_env(monkeypatch):
    # ENVIRONMENT must read as local: app.deployment_handler resolves its config at import and a
    # deployed-looking environment makes load_config ask SSM (conftest sets the same pair).
    monkeypatch.setenv("PROJECT_NAME", "acfe2e")
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("AWS_REGION", REGION)


class _Secrets:
    def __init__(self):
        self.created: list[dict] = []

    def create_secret(self, **kwargs):
        self.created.append(kwargs)
        return {"ARN": f"arn:aws:secretsmanager:{REGION}:123456789012:secret:{kwargs['Name']}-AbCdEf"}


def _tags(created: dict) -> dict:
    return {t["Key"]: t["Value"] for t in created["Tags"]}


# ---------------------------------------------------------------------------
# Resolving the mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("identity_config", "expected"),
    [
        (None, "shared"),
        ({}, "shared"),
        ({"mode": "shared"}, "shared"),
        ({"mode": "per_agent"}, "per_agent"),
        ({"mode": "admin"}, "shared"),  # anything that is not the per-agent opt-in is the default
        ({"mode": ""}, "shared"),
        ({"mode": "PER_AGENT"}, "shared"),  # infra pins the exact lowercase value; casing is not an opt-in
        (IdentityConfig(), "shared"),
        (IdentityConfig(mode="per_agent"), "per_agent"),
        (IdentityConfig(mode="per_agent").model_dump(mode="json", by_alias=True), "per_agent"),  # the SFN event shape
    ],
)
def test_identity_mode_of_reads_the_request_model_and_the_event_dict(identity_config, expected):
    assert gd.identity_mode_of(identity_config) == expected


def test_the_value_set_is_exactly_the_one_infra_pins_on_the_tag_grant():
    """infra: ``StringEquals aws:RequestTag/IdentityMode in ["shared", "per_agent"]``; any other
    string (or an absent tag) fails the CreateSecret itself."""
    assert {gd.IDENTITY_MODE_SHARED, gd.IDENTITY_MODE_PER_AGENT} == {"shared", "per_agent"}
    for garbage in (None, {}, {"mode": None}, {"mode": 0}, {"mode": "Shared"}, object()):
        assert gd.identity_mode_of(garbage) in {"shared", "per_agent"}


def test_the_default_when_nothing_set_the_context_is_shared():
    assert gd.current_connector_identity_mode() == "shared"


def test_the_context_is_scoped_and_nests():
    with gd.connector_identity_mode({"mode": "per_agent"}):
        assert gd.current_connector_identity_mode() == "per_agent"
        with gd.connector_identity_mode(None):
            assert gd.current_connector_identity_mode() == "shared"
        assert gd.current_connector_identity_mode() == "per_agent"
    assert gd.current_connector_identity_mode() == "shared"


# ---------------------------------------------------------------------------
# The tag on the minted secret
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["shared", "per_agent"])
def test_a_minted_connector_secret_carries_the_deployments_identity_mode(mode):
    sm = _Secrets()
    with gd.connector_identity_mode({"mode": mode}):
        gd._put_connector_secret(REGION, "owner-1", {"apiKey": "FAKE-KEY"}, "dep-1", secrets_client=sm)
    assert _tags(sm.created[0])[gd.IDENTITY_MODE_TAG_KEY] == mode


def test_a_secret_minted_outside_any_deployment_context_is_tagged_shared():
    sm = _Secrets()
    gd._put_connector_secret(REGION, "owner-1", {"apiKey": "FAKE-KEY"}, "dep-1", secrets_client=sm)
    assert _tags(sm.created[0])["IdentityMode"] == "shared"


def test_the_tag_key_is_exactly_the_one_infra_conditions_on():
    """The key must not collide with governance namespaces or ownership tags, and infra's
    aws:TagKeys allowlist names it literally."""
    assert gd.IDENTITY_MODE_TAG_KEY == "IdentityMode"


def test_the_existing_binding_tags_are_untouched_by_the_new_one():
    sm = _Secrets()
    with gd.connector_identity_mode(IdentityConfig(mode="per_agent")):
        gd._put_connector_secret(
            REGION, "owner-1", {"clientSecret": "FAKE"}, "dep-1", secrets_client=sm, purpose="probe"
        )
    tags = _tags(sm.created[0])
    assert tags["ManagedBy"] == "agentcore-flows"
    assert tags["AgentCoreStack"] == f"acfe2e-local-{REGION}"
    assert tags["Purpose"] == "probe"
    assert tags["DeploymentId"] == "dep-1"
    assert "OwnerSubHash" in tags
    assert tags["IdentityMode"] == "per_agent"
    assert "owner-1" not in str(sm.created[0]["Tags"])


def test_bind_stamps_the_mode_on_the_fresh_copy():
    sm = _Secrets()
    with gd.connector_identity_mode({"mode": "per_agent"}):
        arn, created = gd.bind_connector_secret_for_deployment(
            region=REGION,
            owner_sub="owner-1",
            deployment_id="dep-1",
            payload_key="apiKey",
            raw_value="FAKE-RAW",
            secrets_client=sm,
        )
    assert created is True and arn.startswith("arn:aws:secretsmanager:")
    assert _tags(sm.created[0])["IdentityMode"] == "per_agent"


# ---------------------------------------------------------------------------
# The API's staging sets the mode from the request
# ---------------------------------------------------------------------------


class _Store:
    def record_resource_strict(self, deployment_id, row):
        pass

    def record_resource(self, deployment_id, row):
        pass


@pytest.mark.parametrize("mode", ["shared", "per_agent"])
def test_the_api_stages_connector_credentials_under_the_requests_identity_mode(monkeypatch, mode):
    from app import deployment_handler as dh
    from app.services import step_clients

    seen: list[str] = []

    def _bind(**kwargs):
        seen.append(gd.current_connector_identity_mode())
        return f"arn:aws:secretsmanager:{REGION}:123456789012:secret:agentcore-connector/o/{len(seen)}", True

    class _Session:
        def client(self, *a, **k):
            return object()

    monkeypatch.setattr(dh, "bind_connector_secret_for_deployment", _bind)
    monkeypatch.setattr(step_clients, "session_for_event", lambda event: _Session())

    dh._prepare_deployment_credentials(
        gateway_config={"name": "gw", "litellm_api_key": "FAKE"},
        connectors=[{"connector_id": "c", "secret_value": "FAKE", "auth_method": "api_key"}],
        external_mcp_servers=[{"server_id": "m", "secret_value": "FAKE"}],
        deployment_id="dep-1",
        owner_sub="owner-1",
        target_account_id=None,
        target_region=REGION,
        target_role_arn=None,
        store=_Store(),
        identity_config=IdentityConfig(mode=mode),
    )
    assert seen == [mode, mode, mode]


def test_the_api_defaults_to_shared_when_the_request_has_no_identity_node(monkeypatch):
    from app import deployment_handler as dh
    from app.services import step_clients

    seen: list[str] = []
    monkeypatch.setattr(
        dh,
        "bind_connector_secret_for_deployment",
        lambda **kw: (seen.append(gd.current_connector_identity_mode()), ("arn:aws:secretsmanager:x:1:secret:a", True))[
            1
        ],
    )
    monkeypatch.setattr(
        step_clients, "session_for_event", lambda event: type("S", (), {"client": lambda *a, **k: object()})()
    )
    dh._prepare_deployment_credentials(
        gateway_config=None,
        connectors=[{"connector_id": "c", "secret_value": "FAKE"}],
        external_mcp_servers=None,
        deployment_id="dep-1",
        owner_sub="owner-1",
        target_account_id=None,
        target_region=REGION,
        target_role_arn=None,
        store=_Store(),
    )
    assert seen == ["shared"]


# ---------------------------------------------------------------------------
# Structural: every minting seam sets the mode
# ---------------------------------------------------------------------------


def _with_items(node: ast.With) -> list[str]:
    names = []
    for item in node.items:
        expr = item.context_expr
        if isinstance(expr, ast.Call):
            func = expr.func
            names.append(func.id if isinstance(func, ast.Name) else getattr(func, "attr", ""))
    return names


def test_every_secret_intent_journal_block_also_sets_the_identity_mode():
    """The journal marks exactly the blocks that may CreateSecret; each must also carry the mode,
    or the secret it mints is tagged with whatever the context happened to hold."""
    files = sorted((SRC / "step_handlers").rglob("*.py")) + [SRC / "deployment_handler.py"]
    missing = []
    for path in files:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.With):
                names = _with_items(node)
                if "secret_intent_journal" in names and "connector_identity_mode" not in names:
                    missing.append(f"{path.relative_to(SRC).as_posix()}:{node.lineno}")
    assert missing == []


def test_the_direct_deploy_path_wraps_deploy_gateway_in_the_mode():
    """services/deployment.py hands identity_config to deploy_gateway; the Cognito client secret
    it mints deep inside must see the same mode."""
    tree = ast.parse((SRC / "services" / "deployment.py").read_text())
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    wrapped = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "boto3_deploy_gateway" and isinstance(parents.get(node), ast.Call):
            cur = node
            while cur in parents:
                cur = parents[cur]
                if isinstance(cur, ast.With) and "connector_identity_mode" in _with_items(cur):
                    wrapped.append(node.lineno)
                    break
    assert wrapped, "the to_thread(boto3_deploy_gateway, ...) call is not inside connector_identity_mode(...)"

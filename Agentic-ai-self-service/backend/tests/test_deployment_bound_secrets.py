"""Credential lifecycle invariants for deployment-owned connector secrets."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from app.services.gateway_deployer import (
    ConnectorSecretBindingError,
    ConnectorSecretDeletionRefused,
    bind_connector_secret_for_deployment,
    connector_secret_owner_prefix,
    delete_deployment_bound_secret,
    secret_binding_tags,
)
from app.services.resource_ownership import OWNER_SUB_HASH_TAG_KEY, owner_sub_hash, owner_tag_list
from botocore.exceptions import ClientError

REGION = "us-east-1"
ACCOUNT = "123456789012"
OWNER = "54381418-7021-708e-4f3b-30505a2b82ec"
OTHER_OWNER = "b458d4f8-60e1-70fa-98bd-fb664f6e307c"
DEPLOYMENT = "dep-current"


class _NotFound(Exception):
    response = {"Error": {"Code": "ResourceNotFoundException"}}


class _Secrets:
    def __init__(self) -> None:
        self.secrets: dict[str, dict] = {}
        self.described: list[str] = []
        self.read: list[str] = []
        self.deleted: list[dict] = []
        self.created: list[dict] = []

    def add(self, name: str, payload: dict, tags: list[dict]) -> str:
        arn = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{name}-AbCdEf"
        self.secrets[arn] = {
            "ARN": arn,
            "Name": name,
            "SecretString": json.dumps(payload),
            "Tags": tags,
        }
        return arn

    def _find(self, secret_id: str) -> dict:
        if secret_id in self.secrets:
            return self.secrets[secret_id]
        for item in self.secrets.values():
            if item["Name"] == secret_id:
                return item
        raise _NotFound(secret_id)

    def create_secret(self, **kwargs):
        self.created.append(kwargs)
        arn = self.add(kwargs["Name"], json.loads(kwargs["SecretString"]), kwargs.get("Tags") or [])
        return {"ARN": arn}

    def describe_secret(self, SecretId):  # noqa: N803
        self.described.append(SecretId)
        item = self._find(SecretId)
        return {"ARN": item["ARN"], "Name": item["Name"], "Tags": item["Tags"]}

    def get_secret_value(self, SecretId):  # noqa: N803
        self.read.append(SecretId)
        return {"SecretString": self._find(SecretId)["SecretString"]}

    def delete_secret(self, **kwargs):
        self.deleted.append(kwargs)


class _Session:
    def __init__(self, secrets: _Secrets) -> None:
        self.secrets = secrets
        self.calls: list[tuple[str, dict]] = []

    def client(self, service: str, **kwargs):
        self.calls.append((service, kwargs))
        assert service == "secretsmanager"
        return self.secrets


class _Store:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.rows: list[dict] = []
        self.resources: list[dict] = []
        self.fail_after = fail_after

    def update_step(self, *args, **kwargs) -> None:
        return None

    def record_resource(self, _deployment_id: str, resource: dict) -> None:
        self.resources.append(resource)

    def record_resource_strict(self, _deployment_id: str, resource: dict) -> None:
        if self.fail_after is not None and len(self.rows) >= self.fail_after:
            raise RuntimeError("durability unavailable")
        self.rows.append(resource)


@pytest.fixture(autouse=True)
def _stack_identity(monkeypatch):
    monkeypatch.setenv("PROJECT_NAME", "secret-tests")
    monkeypatch.setenv("ENVIRONMENT", "unit")


def _tags(owner: str, deployment_id: str, *, region: str = REGION) -> list[dict]:
    return owner_tag_list(region) + secret_binding_tags(owner, deployment_id)


def _assert_journaled_pairs(rows: list[dict], recorded: list[str]) -> None:
    """Each recorded ARN's exact name was a manifest row BEFORE the create, and the
    two rows are one resource to teardown."""
    from app.services.deployment_state_store import collapse_secret_intent_rows, manifest_resource_key

    # The gateway step appends its other rows strictly too (it must know which ones
    # DynamoDB acknowledged, F-66f); only the secret rows are the journal.
    rows = [r for r in rows if r.get("type") == "secret"]
    arn_rows = [r for r in rows if str(r["id"]).startswith("arn:")]
    assert sorted(r["id"] for r in arn_rows) == sorted(recorded)
    assert len(rows) == 2 * len(arn_rows)
    for arn_row in arn_rows:
        name = arn_row["id"].partition(":secret:")[2][:-7]
        journal = [i for i, r in enumerate(rows) if r["id"] == name]
        assert journal and journal[0] < rows.index(arn_row), f"{name} was not journaled before its create"
    assert collapse_secret_intent_rows(rows) == arn_rows
    if all(r.get("account") for r in rows):
        assert len({manifest_resource_key(r) for r in rows}) == len(arn_rows)


def _secret(sm: _Secrets, owner: str, deployment_id: str, payload: dict, *, tags=None) -> str:
    return sm.add(
        f"{connector_secret_owner_prefix(owner)}existing1234",
        payload,
        _tags(owner, deployment_id) if tags is None else tags,
    )


def test_raw_value_wins_over_a_simultaneous_stale_reference():
    sm = _Secrets()
    foreign = _secret(sm, OTHER_OWNER, "old", {"apiKey": "foreign"})

    arn, created = bind_connector_secret_for_deployment(
        region=REGION,
        owner_sub=OWNER,
        deployment_id=DEPLOYMENT,
        payload_key="apiKey",
        raw_value="fresh-value",
        secret_ref=foreign,
        secrets_client=sm,
    )

    assert created is True
    assert sm.described == []
    assert json.loads(sm.secrets[arn]["SecretString"]) == {"apiKey": "fresh-value"}
    assert {t["Key"]: t["Value"] for t in sm.secrets[arn]["Tags"]}["DeploymentId"] == DEPLOYMENT


def test_exact_current_secret_is_reused_after_live_tag_and_shape_validation():
    sm = _Secrets()
    current = _secret(sm, OWNER, DEPLOYMENT, {"apiKey": "value"})

    arn, created = bind_connector_secret_for_deployment(
        region=REGION,
        owner_sub=OWNER,
        deployment_id=DEPLOYMENT,
        payload_key="apiKey",
        secret_ref=current,
        secrets_client=sm,
    )

    assert (arn, created) == (current, False)
    assert sm.read == [current]
    assert sm.created == []


@pytest.mark.parametrize(
    "tag_mode",
    [
        pytest.param("older-same-stack", id="older-same-stack"),
        pytest.param("legacy-name-only", id="legacy-name-only"),
    ],
)
def test_older_or_legacy_same_owner_reference_is_copied(tag_mode):
    sm = _Secrets()
    tags = _tags(OWNER, "older-deploy") if tag_mode == "older-same-stack" else []
    old = _secret(sm, OWNER, "older-deploy", {"clientSecret": "old-value"}, tags=tags)

    arn, created = bind_connector_secret_for_deployment(
        region=REGION,
        owner_sub=OWNER,
        deployment_id=DEPLOYMENT,
        payload_key="clientSecret",
        secret_ref=old,
        secrets_client=sm,
    )

    assert created is True
    assert arn != old
    assert json.loads(sm.secrets[arn]["SecretString"]) == {"clientSecret": "old-value"}
    assert {t["Key"]: t["Value"] for t in sm.secrets[arn]["Tags"]}["DeploymentId"] == DEPLOYMENT


def test_explicit_foreign_owner_is_rejected_before_the_value_is_read():
    sm = _Secrets()
    foreign = _secret(sm, OTHER_OWNER, "old", {"apiKey": "do-not-read"})

    with pytest.raises(ConnectorSecretBindingError, match="another caller"):
        bind_connector_secret_for_deployment(
            region=REGION,
            owner_sub=OWNER,
            deployment_id=DEPLOYMENT,
            payload_key="apiKey",
            secret_ref=foreign,
            secrets_client=sm,
        )

    assert sm.read == []
    assert sm.created == []


def test_conflicting_stack_tag_is_rejected_even_when_the_name_matches():
    sm = _Secrets()
    tags = _tags(OWNER, "old")
    next(t for t in tags if t["Key"] == "AgentCoreStack")["Value"] = "another-stack"
    ref = _secret(sm, OWNER, "old", {"apiKey": "value"}, tags=tags)

    with pytest.raises(ConnectorSecretBindingError, match="another platform stack"):
        bind_connector_secret_for_deployment(
            region=REGION,
            owner_sub=OWNER,
            deployment_id=DEPLOYMENT,
            payload_key="apiKey",
            secret_ref=ref,
            secrets_client=sm,
        )


def test_reference_without_owner_identity_fails_closed_unless_exact_current():
    sm = _Secrets()
    old = _secret(sm, OWNER, "old", {"apiKey": "value"})

    with pytest.raises(ConnectorSecretBindingError):
        bind_connector_secret_for_deployment(
            region=REGION,
            owner_sub="",
            deployment_id=DEPLOYMENT,
            payload_key="apiKey",
            secret_ref=old,
            secrets_client=sm,
        )
    assert sm.read == []


def test_reference_with_the_wrong_json_field_is_rejected():
    sm = _Secrets()
    current = _secret(sm, OWNER, DEPLOYMENT, {"clientSecret": "wrong-shape"})

    with pytest.raises(ConnectorSecretBindingError, match="apiKey"):
        bind_connector_secret_for_deployment(
            region=REGION,
            owner_sub=OWNER,
            deployment_id=DEPLOYMENT,
            payload_key="apiKey",
            secret_ref=current,
            secrets_client=sm,
        )


def test_delete_requires_exact_stack_and_deployment_tags():
    sm = _Secrets()
    current = _secret(sm, OWNER, DEPLOYMENT, {"apiKey": "value"})

    assert (
        delete_deployment_bound_secret(
            region=REGION,
            deployment_id=DEPLOYMENT,
            secret_ref=current,
            secrets_client=sm,
        )
        is True
    )
    assert sm.deleted == [{"SecretId": current, "ForceDeleteWithoutRecovery": True}]


@pytest.mark.parametrize(
    "ref_factory",
    [
        pytest.param(lambda sm: _secret(sm, OWNER, "another-deploy", {"apiKey": "value"}), id="older-deploy"),
        pytest.param(
            lambda sm: sm.add("customer/shared/key", {"apiKey": "value"}, _tags(OWNER, DEPLOYMENT)),
            id="outside-platform-namespace",
        ),
    ],
)
def test_delete_refuses_anything_not_exactly_owned(ref_factory):
    sm = _Secrets()
    ref = ref_factory(sm)

    with pytest.raises(ConnectorSecretDeletionRefused):
        delete_deployment_bound_secret(
            region=REGION,
            deployment_id=DEPLOYMENT,
            secret_ref=ref,
            secrets_client=sm,
        )

    assert sm.deleted == []


def test_api_boundary_replaces_every_plaintext_shape_before_sfn_serialization(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "local")
    from app import deployment_handler
    from app.services import step_clients

    sm = _Secrets()
    session = _Session(sm)
    monkeypatch.setattr(step_clients, "session_for_event", lambda event: session)
    store = _Store()

    gateway, connectors, external_mcp, recorded = deployment_handler._prepare_deployment_credentials(
        gateway_config={
            "name": "gw",
            "gatewayProvider": "litellm",
            "litellmApiKey": "raw-litellm",
        },
        connectors=[
            {
                "connector_id": "github",
                "auth_method": "api_key",
                "secret_value": "raw-connector",
            }
        ],
        external_mcp_servers=[
            {
                "server_id": "custom-api",
                "secretValue": "raw-mcp-api",
            },
            {
                "server_id": "custom-oauth",
                "oauth": {
                    "client_id": "cid",
                    "clientSecret": "raw-mcp-oauth",
                    "discovery_url": "https://idp.example/.well-known/openid-configuration",
                },
            },
        ],
        deployment_id=DEPLOYMENT,
        owner_sub=OWNER,
        target_account_id="999999999999",
        target_region=REGION,
        target_role_arn="arn:aws:iam::999999999999:role/Deploy",
        store=store,
    )

    serialized = json.dumps(
        {
            "gateway_config": gateway,
            "connectors": connectors,
            "external_mcp_servers": external_mcp,
        }
    )
    for plaintext in ("raw-litellm", "raw-connector", "raw-mcp-api", "raw-mcp-oauth"):
        assert plaintext not in serialized
    for forbidden_key in ("secret_value", "secretValue", "client_secret", "clientSecret", "litellmApiKey"):
        assert f'"{forbidden_key}"' not in serialized

    assert gateway and gateway["litellm_api_key_ref"] in recorded
    assert connectors[0]["secret_arn"] in recorded
    assert external_mcp[0]["secret_arn"] in recorded
    assert external_mcp[1]["oauth"]["client_secret_arn"] in recorded
    assert len(recorded) == 4
    _assert_journaled_pairs(store.rows, recorded)
    assert all(row["account"] == "999999999999" for row in store.rows)


@pytest.mark.parametrize(
    ("fail_after", "creates"),
    [
        # The journal row itself cannot be written: nothing may be created at all.
        (0, 0),
        # The journal lands, the create commits, the ARN row fails: compensate.
        (1, 1),
    ],
)
def test_strict_manifest_failure_compensation_deletes_the_just_minted_secret(monkeypatch, fail_after, creates):
    monkeypatch.setenv("ENVIRONMENT", "local")
    from app import deployment_handler
    from app.services import step_clients

    sm = _Secrets()
    monkeypatch.setattr(step_clients, "session_for_event", lambda event: _Session(sm))

    with pytest.raises(RuntimeError, match="durability unavailable"):
        deployment_handler._prepare_deployment_credentials(
            gateway_config=None,
            connectors=[
                {
                    "connector_id": "github",
                    "auth_method": "api_key",
                    "secret_value": "raw-connector",
                }
            ],
            external_mcp_servers=None,
            deployment_id=DEPLOYMENT,
            owner_sub=OWNER,
            target_account_id=None,
            target_region=REGION,
            target_role_arn=None,
            store=_Store(fail_after=fail_after),
        )

    assert len(sm.created) == creates
    assert len(sm.deleted) == creates
    if creates:
        assert sm.deleted[0]["ForceDeleteWithoutRecovery"] is True
        assert sm.deleted[0]["SecretId"] == next(iter(sm.secrets))


def test_gateway_step_removes_every_legacy_plaintext_shape_before_reemitting(monkeypatch):
    from app.services import step_clients
    from app.step_handlers import gateway_step

    sm = _Secrets()
    session = _Session(sm)
    store = _Store()
    monkeypatch.setattr(gateway_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(step_clients, "session_for_event", lambda event: session)
    monkeypatch.setattr(gateway_step, "resolve_gateway_provider", lambda config: "agentcore")
    captured = {}

    def _deploy_gateway(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "gateway_id": "gw-1",
            "gateway_name": "gw",
            "client_info": {},
        }

    monkeypatch.setattr(gateway_step, "deploy_gateway", _deploy_gateway)
    event = {
        "deployment_id": DEPLOYMENT,
        "owner_sub": OWNER,
        "target_account_id": "999999999999",
        "target_region": REGION,
        "target_artifact_bucket": "customer-runtime-artifacts",
        "gateway_config": {"name": "gw"},
        "connectors": [
            {
                "connector_id": "github",
                "auth_method": "api_key",
                "secretValue": "connector-plaintext",
            }
        ],
        "external_mcp_servers": [
            {"server_id": "exa", "secret_value": "external-api-plaintext"},
            {
                "server_id": "databricks",
                "oauth": {
                    "client_id": "cid",
                    "clientSecret": "external-oauth-plaintext",
                    "discovery_url": "https://idp.example/.well-known/openid-configuration",
                },
            },
        ],
        "mcp_server_runtime_arn": ("arn:aws:bedrock-agentcore:us-east-1:999999999999:runtime/mcp_server-AbCdEf1234"),
        "mcp_oauth": {
            "client_id": "mcp-client",
            "client_secret": "hosted-mcp-plaintext",
            "discovery_url": "https://idp.example/.well-known/openid-configuration",
            "scope": "mcp/invoke",
        },
    }

    out = gateway_step.handler(event, None)

    emitted = json.dumps(out)
    # The live-consumer reader is a callable, not data: it holds no secret and never
    # reaches the SFN output (``emitted`` above serializes without it).
    assert callable(captured.pop("gateway_consumers"))
    # So is the gateway-name claim: a closure over the platform table, not data.
    assert callable(captured.pop("claim_gateway_name"))
    deployed = json.dumps(captured)
    for plaintext in (
        "connector-plaintext",
        "external-api-plaintext",
        "external-oauth-plaintext",
        "hosted-mcp-plaintext",
    ):
        assert plaintext not in emitted
        assert plaintext not in deployed
    for forbidden_key in ("secret_value", "secretValue", "client_secret", "clientSecret"):
        assert f'"{forbidden_key}"' not in emitted
        assert f'"{forbidden_key}"' not in deployed

    assert captured["secrets_prebound"] is True
    assert captured["connectors"][0]["secret_arn"]
    assert captured["external_mcp_servers"][0]["secret_arn"]
    assert captured["external_mcp_servers"][1]["oauth"]["client_secret_arn"]
    assert captured["mcp_oauth"]["client_secret_ref"]
    assert len(out["recorded_secret_arns"]) == 4
    _assert_journaled_pairs(store.rows, out["recorded_secret_arns"])
    # Every secret row names the target account. The gateway row need not: teardown keys
    # its claim on the deployment's target, and a row's own account is only inventory.
    assert all(row["account"] == "999999999999" for row in store.rows if row["type"] == "secret")


def test_gateway_step_revalidates_a_recorded_hosted_mcp_ref_without_duplicate_row(monkeypatch):
    from app.services import step_clients
    from app.step_handlers import gateway_step

    sm = _Secrets()
    current = _secret(sm, OWNER, DEPLOYMENT, {"clientSecret": "stored-value"})
    store = _Store()
    monkeypatch.setattr(gateway_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(step_clients, "session_for_event", lambda event: _Session(sm))
    monkeypatch.setattr(gateway_step, "resolve_gateway_provider", lambda config: "agentcore")
    captured = {}

    def _deploy_gateway(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "gateway_id": "gw-1",
            "gateway_name": "gw",
            "client_info": {},
        }

    monkeypatch.setattr(gateway_step, "deploy_gateway", _deploy_gateway)
    out = gateway_step.handler(
        {
            "deployment_id": DEPLOYMENT,
            "owner_sub": OWNER,
            "target_region": REGION,
            "gateway_config": {"name": "gw"},
            "recorded_secret_arns": [current],
            "mcp_server_runtime_arn": "arn:runtime",
            "mcp_oauth": {
                "client_id": "cid",
                "client_secret_ref": current,
                "discovery_url": "https://idp.example/.well-known/openid-configuration",
                "scope": "mcp/invoke",
            },
        },
        None,
    )

    assert captured["mcp_oauth"]["client_secret_ref"] == current
    assert out["recorded_secret_arns"] == [current]
    assert [r for r in store.rows if r.get("type") == "secret"] == []
    assert sm.created == []
    assert sm.read == [current]


def test_user_delete_dispatcher_preserves_the_exact_cross_account_role(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "local")
    from app import deployment_handler
    from app.services import step_clients

    calls = []

    class _MemoryClient:
        deleted = False

        def get_memory(self, **kwargs):
            calls.append(("get_memory", kwargs))
            if self.deleted:
                # The confirmed delete polls until the memory is gone, as the real API reports it.
                raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetMemory")
            return {"memory": {"arn": ("arn:aws:bedrock-agentcore:eu-west-1:999999999999:memory/memory-1")}}

        def list_tags_for_resource(self, **kwargs):
            calls.append(("list_tags_for_resource", kwargs))
            # Tagged the way memory_step tags an owned memory: stack owner plus the
            # creator's hashed sub, which the confirmed delete re-checks.
            return {"tags": owner_tag_list("eu-west-1", {OWNER_SUB_HASH_TAG_KEY: owner_sub_hash(OWNER)})}

        def delete_memory(self, **kwargs):
            calls.append(("delete_memory", kwargs))
            self.deleted = True

    def _client(event, service, **kwargs):
        calls.append(("client", event, service, kwargs))
        return _MemoryClient()

    monkeypatch.setattr(step_clients, "client", _client)
    role_arn = "arn:aws:iam::999999999999:role/CustomerDeploymentRole"
    deployment_handler._delete_managed_resource(
        {
            "type": "memory",
            "id": "memory-1",
            "account": "999999999999",
            "region": "eu-west-1",
        },
        REGION,
        deployment_id=DEPLOYMENT,
        target_role_arn=role_arn,
        # The real dispatcher passes ``owner or caller_sub``; a memory delete with no
        # authenticated owner is refused, and that refusal is not what this test pins.
        owner_sub=OWNER,
    )

    assert calls[0] == (
        "client",
        {
            "target_account_id": "999999999999",
            "target_region": "eu-west-1",
            "target_role_arn": role_arn,
        },
        "bedrock-agentcore-control",
        {"region_name": "eu-west-1"},
    )
    assert calls[1] == ("get_memory", {"memoryId": "memory-1"})
    tag_read = (
        "list_tags_for_resource",
        {"resourceArn": ("arn:aws:bedrock-agentcore:eu-west-1:999999999999:memory/memory-1")},
    )
    # Stack ownership, then the caller binding, both through the target-account client.
    assert calls[2] == tag_read
    assert calls[3] == tag_read
    assert calls[4][0] == "delete_memory"
    assert calls[4][1]["memoryId"] == "memory-1"
    assert calls[4][1].get("clientToken"), "a retried teardown must reuse one idempotency token"
    assert calls[5:] == [("get_memory", {"memoryId": "memory-1"})], "absence is confirmed in the same account"


def test_failure_cleanup_preserves_role_and_deployment_when_a_resource_has_an_account(monkeypatch):
    from app.step_handlers import status_update_step

    role_arn = "arn:aws:iam::999999999999:role/CustomerDeploymentRole"
    seen_clients = []
    seen_deletes = []
    secrets_client = object()

    def _client(event, service, **kwargs):
        seen_clients.append((event, service, kwargs))
        return secrets_client

    monkeypatch.setattr(status_update_step.step_clients, "client", _client)
    monkeypatch.setattr(
        status_update_step,
        "delete_deployment_bound_secret",
        lambda **kwargs: seen_deletes.append(kwargs),
    )
    status_update_step._cleanup_resource(
        {
            "type": "secret",
            "id": "agentcore-connector/owner/current",
            "account": "999999999999",
            "region": "eu-west-1",
        },
        REGION,
        {
            "deployment_id": DEPLOYMENT,
            "target_role_arn": role_arn,
            "unrelated": "preserved",
        },
    )

    expected_event = {
        "deployment_id": DEPLOYMENT,
        "target_role_arn": role_arn,
        "unrelated": "preserved",
        "target_account_id": "999999999999",
        "target_region": "eu-west-1",
    }
    assert seen_clients == [
        (
            expected_event,
            "secretsmanager",
            {"region_name": "eu-west-1"},
        )
    ]
    assert seen_deletes == [
        {
            "region": "eu-west-1",
            "deployment_id": DEPLOYMENT,
            "secret_ref": "agentcore-connector/owner/current",
            "secrets_client": secrets_client,
        }
    ]


def _wire_external_mcp_deployer(monkeypatch, secrets):
    from app.services import gateway_deployer

    ctrl = MagicMock()
    ctrl.create_api_key_credential_provider.return_value = {
        "credentialProviderArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:provider/api"
    }
    monkeypatch.setattr(gateway_deployer, "_create_secrets_client", lambda region: secrets)
    target_calls = []

    def _create_target(*args, **kwargs):
        target_calls.append((args, kwargs))
        return {"targetId": "target-1", "name": "exa"}

    monkeypatch.setattr(
        gateway_deployer,
        "_create_gateway_target_with_retry",
        _create_target,
    )
    monkeypatch.setattr(
        gateway_deployer,
        "_wait_for_mcp_target_ready",
        lambda *args, **kwargs: None,
    )
    return gateway_deployer, ctrl, target_calls


def test_external_mcp_prebound_path_reuses_only_an_exact_current_secret(monkeypatch):
    sm = _Secrets()
    current = _secret(sm, OWNER, DEPLOYMENT, {"apiKey": "current-value"})
    gateway_deployer, ctrl, target_calls = _wire_external_mcp_deployer(monkeypatch, sm)

    out = gateway_deployer._deploy_external_mcp_targets(
        ctrl,
        "gateway-1",
        REGION,
        [{"server_id": "exa", "secret_arn": current}],
        owner_sub=OWNER,
        deployment_id=DEPLOYMENT,
        secrets_prebound=True,
    )

    assert out["secret_arns"] == [current]
    assert sm.read == [current]
    assert sm.created == []
    target_configuration = target_calls[0][0][3]["targetConfiguration"]
    assert target_configuration["mcp"]["mcpServer"]["endpoint"] == "https://mcp.exa.ai/mcp"


def test_external_mcp_direct_path_copies_an_older_same_owner_reference(monkeypatch):
    sm = _Secrets()
    older = _secret(sm, OWNER, "older-deployment", {"apiKey": "older-value"})
    gateway_deployer, ctrl, _target_calls = _wire_external_mcp_deployer(monkeypatch, sm)

    out = gateway_deployer._deploy_external_mcp_targets(
        ctrl,
        "gateway-1",
        REGION,
        [{"server_id": "exa", "secret_arn": older}],
        owner_sub=OWNER,
        deployment_id=DEPLOYMENT,
    )

    assert len(out["secret_arns"]) == 1
    copied = out["secret_arns"][0]
    assert copied != older
    assert json.loads(sm.secrets[copied]["SecretString"]) == {"apiKey": "older-value"}
    assert {tag["Key"]: tag["Value"] for tag in sm.secrets[copied]["Tags"]}["DeploymentId"] == DEPLOYMENT


def test_litellm_cleanup_deletes_its_exact_secret_even_without_an_agentcore_gateway(monkeypatch):
    from app.services import gateway_deployer

    sm = _Secrets()
    current = _secret(sm, OWNER, DEPLOYMENT, {"apiKey": "value"})
    monkeypatch.setattr(gateway_deployer, "_create_secrets_client", lambda region: sm)
    monkeypatch.setattr(gateway_deployer, "_create_lambda_client", lambda region: MagicMock())

    log = gateway_deployer.cleanup_gateway_resources(
        "litellm",
        REGION,
        {
            "gateway_provider": "litellm",
            "gateway_id": None,
            "connector_secret_arns": [current],
        },
        deployment_id=DEPLOYMENT,
    )

    assert sm.deleted == [{"SecretId": current, "ForceDeleteWithoutRecovery": True}]
    assert any("No gateway_id" in line for line in log)
    assert any("deleted" in line and "Connector secret" in line for line in log)


def test_gateway_cleanup_deletes_the_explicitly_minted_cognito_secret_only(monkeypatch):
    from app.services import gateway_deployer

    sm = _Secrets()
    minted = _secret(sm, OWNER, DEPLOYMENT, {"clientSecret": "generated"})
    customer = sm.add(
        "customer/shared-idp-secret",
        {"clientSecret": "customer-owned"},
        [],
    )
    monkeypatch.setattr(gateway_deployer, "_create_secrets_client", lambda region: sm)
    monkeypatch.setattr(gateway_deployer, "_create_cognito_client", lambda region: MagicMock())
    monkeypatch.setattr(gateway_deployer, "_create_lambda_client", lambda region: MagicMock())

    gateway_deployer.cleanup_gateway_resources(
        "partial",
        REGION,
        {
            "client_info": {
                "provider": "cognito",
                "client_secret_ref": customer,
                "minted_client_secret_ref": minted,
            },
        },
        deployment_id=DEPLOYMENT,
    )

    assert sm.deleted == [{"SecretId": minted, "ForceDeleteWithoutRecovery": True}]
    assert customer not in sm.described


def test_external_idp_client_secret_reference_is_never_inferred_as_deletable(monkeypatch):
    from app.services import gateway_deployer

    sm = _Secrets()
    customer = _secret(sm, OWNER, DEPLOYMENT, {"clientSecret": "customer-owned"})
    monkeypatch.setattr(gateway_deployer, "_create_secrets_client", lambda region: sm)
    monkeypatch.setattr(gateway_deployer, "_create_lambda_client", lambda region: MagicMock())

    gateway_deployer.cleanup_gateway_resources(
        "partial",
        REGION,
        {
            "client_info": {
                "provider": "okta",
                "client_secret_ref": customer,
            },
        },
        deployment_id=DEPLOYMENT,
    )

    assert sm.described == []
    assert sm.deleted == []


def test_manifest_runtime_destroy_uses_the_recorded_target_role_for_every_client(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "local")
    from app import deployment_handler
    from app.services import step_clients

    seen = []
    ownership_reads = []
    role_arn = "arn:aws:iam::999999999999:role/AgentCoreFlowsDeploymentRole"

    class _ControlClient:
        def get_agent_runtime(self, **kwargs):
            ownership_reads.append(("get_agent_runtime", kwargs))
            return {"agentRuntimeArn": ("arn:aws:bedrock-agentcore:eu-west-1:999999999999:runtime/runtime-1")}

        def list_tags_for_resource(self, **kwargs):
            ownership_reads.append(("list_tags_for_resource", kwargs))
            return {"tags": owner_tag_list("eu-west-1")}

    def _client(event, service, **kwargs):
        seen.append((event, service, kwargs))
        if service == "bedrock-agentcore-control":
            return _ControlClient()
        return object()

    def _destroy(
        runtime_id,
        region,
        *,
        client_factory,
        delete_execution_role,
    ):
        assert runtime_id == "runtime-1"
        assert region == "eu-west-1"
        assert delete_execution_role is False
        client_factory("iam")
        client_factory("cloudwatch", region_name=region)
        return {"success": True, "message": "deleted"}

    monkeypatch.setattr(step_clients, "client", _client)
    monkeypatch.setattr(deployment_handler, "destroy_runtime", _destroy)
    deployment_handler._delete_managed_resource(
        {
            "type": "agent_runtime",
            "id": "runtime-1",
            "account": "999999999999",
            "region": "eu-west-1",
        },
        REGION,
        deployment_id=DEPLOYMENT,
        target_role_arn=role_arn,
    )

    expected_event = {
        "target_account_id": "999999999999",
        "target_region": "eu-west-1",
        "target_role_arn": role_arn,
    }
    assert seen == [
        (
            expected_event,
            "bedrock-agentcore-control",
            {"region_name": "eu-west-1"},
        ),
        (expected_event, "iam", {}),
        (expected_event, "cloudwatch", {"region_name": "eu-west-1"}),
    ]
    assert ownership_reads == [
        ("get_agent_runtime", {"agentRuntimeId": "runtime-1"}),
        (
            "list_tags_for_resource",
            {"resourceArn": ("arn:aws:bedrock-agentcore:eu-west-1:999999999999:runtime/runtime-1")},
        ),
    ]


def test_legacy_cross_account_runtime_cleanup_preserves_stable_role_for_mcp_and_agent(
    monkeypatch,
):
    """Old records without a manifest must preserve the shared target Runtime role.

    Both the hosted MCP runtime and the primary agent runtime pass through legacy
    fallbacks. A future refactor must not restore the default role-deletion behavior
    on either branch.
    """
    monkeypatch.setenv("ENVIRONMENT", "local")
    from app import deployment_handler
    from app.services import step_clients

    role_arn = "arn:aws:iam::999999999999:role/AgentCoreFlowsDeploymentRole"
    record = {
        "deployment_id": "",
        "user_id": OWNER,
        "runtime_id": "runtime-1",
        "mcp_server_runtime_id": "mcp-runtime-1",
        "deployment_mode": "runtime",
        "target_account_id": "999999999999",
        "target_region": "eu-west-1",
        "target_role_arn": role_arn,
        "created_resources": [],
    }

    class _StateStore:
        _table = object()

        def get(self, deployment_id):
            return None

        def has_other_live_resource_reference(self, *args, **kwargs):
            return False

        def reset_manifest_reference_cache(self, deployment_id):
            return None

    class _Session:
        def client(self, service, **kwargs):
            return object()

    session_events = []

    def _session_for_event(event):
        session_events.append(event)
        return _Session()

    destroy_calls = []

    def _destroy(runtime_id, region, **kwargs):
        destroy_calls.append((runtime_id, region, kwargs))
        return {"success": True, "message": f"Runtime {runtime_id} deleted"}

    monkeypatch.setattr(deployment_handler, "_get_state_store", lambda: _StateStore())
    monkeypatch.setattr(
        deployment_handler,
        "_scan_for_runtime",
        lambda table, runtime_id: record,
    )
    monkeypatch.setattr(step_clients, "session_for_event", _session_for_event)
    monkeypatch.setattr(deployment_handler, "destroy_runtime", _destroy)

    result = deployment_handler._run_delete_cleanup("runtime-1", OWNER)

    assert result.success is True
    assert session_events == [
        {
            "target_account_id": "999999999999",
            "target_region": "eu-west-1",
            "target_role_arn": role_arn,
        }
    ]
    assert [call[0] for call in destroy_calls] == ["mcp-runtime-1", "runtime-1"]
    assert all(call[1] == "eu-west-1" for call in destroy_calls)
    assert all(call[2]["delete_execution_role"] is False for call in destroy_calls)
    assert all(callable(call[2]["client_factory"]) for call in destroy_calls)


def test_strict_resource_append_cannot_create_a_skeletal_deployment_record(monkeypatch):
    from app.services import deployment_state_store

    captured = {}

    def _update_item(*args, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(deployment_state_store, "_update_item", _update_item)
    store = deployment_state_store.DeploymentStateStore.__new__(deployment_state_store.DeploymentStateStore)
    store._table = object()

    store.record_resource_strict(
        DEPLOYMENT,
        {
            "type": "secret",
            "id": "arn:secret",
            "region": REGION,
            "created_by_deployment": True,
        },
    )

    assert captured["key"] == {"deployment_id": DEPLOYMENT}
    cond = captured["condition_expr"]
    # Skeletal-record prevention: the append is still conditioned on the row existing.
    assert cond.startswith("attribute_exists(deployment_id)")
    # Deploy-vs-delete barrier: an untokened writer must also prove there is no teardown
    # lifecycle and no live finalizer lease before it may append a resource row.
    assert "attribute_not_exists(#delete_status)" in cond
    assert "attribute_not_exists(#fl)" in cond
    assert "attribute_not_exists(#ft)" in cond


def test_manifest_cleanup_uses_one_persisted_target_session_and_skips_duplicate_runtime_delete(
    monkeypatch,
):
    monkeypatch.setenv("ENVIRONMENT", "local")
    from app import deployment_handler
    from app.services import step_clients

    role_arn = "arn:aws:iam::999999999999:role/CustomerDeploymentRole"
    record = {
        "deployment_id": "",
        "user_id": OWNER,
        "target_account_id": "999999999999",
        "target_region": "eu-west-1",
        "target_role_arn": role_arn,
        "created_resources": [
            {
                "type": "agent_runtime",
                "id": "runtime-1",
                "account": "999999999999",
                "region": "eu-west-1",
            }
        ],
    }

    class _StateStore:
        _table = object()

        def get(self, deployment_id):
            return None

        def has_other_live_resource_reference(self, *args, **kwargs):
            return False

        def reset_manifest_reference_cache(self, deployment_id):
            return None

    session_events = []

    class _Session:
        def client(self, service, **kwargs):
            raise AssertionError(f"unexpected fallback client: {service} {kwargs}")

    def _session_for_event(event):
        session_events.append(event)
        return _Session()

    manifest_calls = []
    monkeypatch.setattr(deployment_handler, "_get_state_store", lambda: _StateStore())
    monkeypatch.setattr(deployment_handler, "_scan_for_runtime", lambda table, runtime_id: record)
    monkeypatch.setattr(step_clients, "session_for_event", _session_for_event)
    monkeypatch.setattr(
        deployment_handler,
        "_delete_managed_resource",
        lambda *args, **kwargs: manifest_calls.append((args, kwargs)) or "runtime deleted",
    )
    monkeypatch.setattr(
        deployment_handler,
        "destroy_runtime",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("runtime was deleted twice after the manifest handled it")
        ),
    )

    result = deployment_handler._run_delete_cleanup("runtime-1", OWNER)

    assert result.success is True
    assert session_events == [
        {
            "target_account_id": "999999999999",
            "target_region": "eu-west-1",
            "target_role_arn": role_arn,
        }
    ]
    assert manifest_calls[0][0][1] == "eu-west-1"
    assert manifest_calls[0][1]["target_role_arn"] == role_arn
    assert isinstance(manifest_calls[0][1]["target_session"], _Session)


# --------------------------------------------------------------------------------------
# The exact-current branch must return the CANONICAL ARN, never the caller's reference.
#
# ``test_exact_current_secret_is_reused_after_live_tag_and_shape_validation`` above passes a full
# ARN, so ``return ref`` and ``return described["ARN"]`` are indistinguishable to it and it stays
# green if the canonicalization is reverted. These three tests exist because of that: the bug this
# branch had was invisible to an ARN-shaped input.
#
# Why it mattered. The function's docstring promises ``(arn, created)``, and two callers depend on
# it being an ARN rather than whatever the caller sent: ``_bind`` writes the value into a resource
# manifest row (deployment_handler.py:404-448), where a bare name carries neither the account nor
# the region teardown needs; and the value is appended to ``recorded_secret_arns``, whose entries
# are themselves ARN-validated -- so a bare entry made the prepared payload's own membership check
# impossible to satisfy rather than merely lax.
# --------------------------------------------------------------------------------------


def test_the_exact_current_branch_canonicalizes_a_bare_name_to_a_full_arn():
    """The mutation target: returning ``ref`` instead of ``described["ARN"]`` must fail HERE.

    The input is the secret's bare ``Name``, which is a form the platform genuinely accepts --
    ``_is_platform_connector_secret`` falls back to the whole string -- so this is not a synthetic
    shape. The assertions pin all three things at once: the returned value is the canonical ARN,
    nothing was created, and the lookups really did go through the supplied bare name (otherwise
    the test could pass against a function that ignored its argument).
    """
    sm = _Secrets()
    current = _secret(sm, OWNER, DEPLOYMENT, {"apiKey": "value"})
    bare_name = sm.secrets[current]["Name"]
    assert not bare_name.startswith("arn:"), "the fixture must exercise the bare-name form"

    arn, created = bind_connector_secret_for_deployment(
        region=REGION,
        owner_sub=OWNER,
        deployment_id=DEPLOYMENT,
        payload_key="apiKey",
        secret_ref=bare_name,
        secrets_client=sm,
    )

    assert arn == current, (
        "the exact-current branch returned the caller's own reference instead of the canonical "
        "ARN; a manifest row keyed on a bare name carries neither the account nor the region "
        "teardown needs, and a bare entry can never satisfy the prepared-payload membership check"
    )
    assert created is False
    assert sm.described == [bare_name] and sm.read == [bare_name]
    assert sm.created == []


def test_the_exact_current_branch_refuses_when_describe_returns_no_arn():
    """Fails CLOSED rather than returning an empty or bare reference.

    An empty ARN would be written into the manifest and into ``recorded_secret_arns``, where it
    would authorize nothing and be undeletable. Refusing asks the caller for the raw credential
    instead, which is a path that always works.
    """
    sm = _Secrets()
    current = _secret(sm, OWNER, DEPLOYMENT, {"apiKey": "value"})
    described = sm.describe_secret
    sm.described.clear()

    def _no_arn(SecretId):  # noqa: N803
        payload = described(SecretId)
        payload.pop("ARN", None)
        return payload

    sm.describe_secret = _no_arn

    with pytest.raises(ConnectorSecretBindingError):
        bind_connector_secret_for_deployment(
            region=REGION,
            owner_sub=OWNER,
            deployment_id=DEPLOYMENT,
            payload_key="apiKey",
            secret_ref=current,
            secrets_client=sm,
        )
    assert sm.created == [], "a refusal must not leave a secret behind"


@pytest.mark.parametrize(
    "canonical",
    [
        pytest.param("agentcore-connector/x", id="not-an-arn"),
        pytest.param(f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/x", id="wrong-service"),
        pytest.param("arn:aws:secretsmanager::secret:x", id="no-account-or-region"),
        pytest.param("arn:aws:secretsmanager:not a region:1:secret:x", id="malformed-segments"),
    ],
)
def test_the_exact_current_branch_refuses_an_unparseable_canonical_arn(canonical):
    """Re-parsing the value it is about to return is what makes the invariant a guarantee.

    ``secrets_manager_arn_location``'s anchored fullmatch is the same check every consumer of this
    value applies later. Running it here converts a downstream mid-deployment failure into a
    refusal before anything is recorded.
    """
    sm = _Secrets()
    current = _secret(sm, OWNER, DEPLOYMENT, {"apiKey": "value"})
    described = sm.describe_secret
    sm.described.clear()

    def _bad_arn(SecretId):  # noqa: N803
        return {**described(SecretId), "ARN": canonical}

    sm.describe_secret = _bad_arn

    with pytest.raises(ConnectorSecretBindingError):
        bind_connector_secret_for_deployment(
            region=REGION,
            owner_sub=OWNER,
            deployment_id=DEPLOYMENT,
            payload_key="apiKey",
            secret_ref=current,
            secrets_client=sm,
        )
    assert sm.created == []

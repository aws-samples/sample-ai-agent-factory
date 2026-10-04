"""Provider and OTEL credentials are copied into one deployment's lifecycle.

The source ARN is caller-controlled. A namespace-looking substring is therefore
not authorization to read it, and a source secret in the right namespace is not
authorization either unless live tags bind it to the caller and this stack.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from app.services.gateway_deployer import (
    ConnectorSecretBindingError,
    secrets_manager_arn_location,
    stage_runtime_secret_for_deployment,
)
from app.services.resource_ownership import (
    owner_sub_hash,
    owner_tag_list,
)

HOME_ACCOUNT = "111122223333"
TARGET_ACCOUNT = "444455556666"
FOREIGN_ACCOUNT = "777788889999"
HOME_REGION = "us-east-1"
TARGET_REGION = "eu-west-1"
OWNER = "54381418-7021-708e-4f3b-30505a2b82ec"
OTHER_OWNER = "b458d4f8-60e1-70fa-98bd-fb664f6e307c"
DEPLOYMENT = "dep-runtime-secret"


def _arn(account: str, region: str, name: str) -> str:
    return f"arn:aws:secretsmanager:{region}:{account}:secret:{name}-AbCdEf"


class _Secrets:
    def __init__(self, account: str, region: str) -> None:
        self.account = account
        self.region = region
        self.values: dict[str, dict] = {}
        self.described: list[str] = []
        self.read: list[str] = []
        self.created: list[dict] = []
        self.deleted: list[dict] = []

    def add(self, name: str, value: str, tags: list[dict] | None = None) -> str:
        arn = _arn(self.account, self.region, name)
        self.values[arn] = {
            "ARN": arn,
            "Name": name,
            "SecretString": value,
            "Tags": list(tags or []),
        }
        return arn

    def describe_secret(self, SecretId):  # noqa: N803
        self.described.append(SecretId)
        item = self.values[SecretId]
        return {"ARN": item["ARN"], "Name": item["Name"], "Tags": item["Tags"]}

    def get_secret_value(self, SecretId):  # noqa: N803
        self.read.append(SecretId)
        return {"SecretString": self.values[SecretId]["SecretString"]}

    def create_secret(self, **kwargs):
        self.created.append(kwargs)
        arn = self.add(kwargs["Name"], kwargs["SecretString"], kwargs.get("Tags"))
        return {"ARN": arn}

    def delete_secret(self, **kwargs):
        self.deleted.append(kwargs)


@dataclass
class _Session:
    secrets: _Secrets

    def client(self, service: str, **kwargs):
        assert service == "secretsmanager"
        assert kwargs.get("region_name") == self.secrets.region
        return self.secrets


class _Store:
    def __init__(self, *, fail_after: int | None = None) -> None:
        # Fail every write once this many rows are durable (0: the first write fails).
        self.fail_after = fail_after
        self.rows: list[tuple[str, dict]] = []

    def record_resource_strict(self, deployment_id: str, row: dict) -> None:
        if self.fail_after is not None and len(self.rows) >= self.fail_after:
            raise RuntimeError("manifest unavailable")
        self.rows.append((deployment_id, row))


@pytest.fixture(autouse=True)
def _stack_identity(monkeypatch):
    monkeypatch.setenv("PROJECT_NAME", "runtime-secret-tests")
    # ``local`` keeps deployment_handler.load_config() hermetic (no SSM read)
    # while still giving ownership tags an exact, deterministic stack id.
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("APP_AWS_REGION", HOME_REGION)


def _source_tags(owner: str, region: str = HOME_REGION) -> list[dict]:
    return owner_tag_list(
        region,
        extra={"OwnerSubHash": owner_sub_hash(owner), "Purpose": "source-credential"},
    )


@pytest.mark.parametrize(
    "bad",
    [
        "not-an-arn:secret:agentcore-provider/openai",
        "arn:aws:ssm:us-east-1:111122223333:parameter/:secret:agentcore-provider/openai",
        "arn:aws:secretsmanager:us-east-1:11112222333:secret:agentcore-provider/openai",
        "arn:aws:secretsmanager:us-east-1:111122223333:secret:",
        "arn:aws:secretsmanager:us-east-1:111122223333:secret:agentcore-provider/openai?stage=AWSCURRENT",
    ],
)
def test_full_arn_parser_rejects_namespace_substring_tricks(bad):
    with pytest.raises(ConnectorSecretBindingError, match="valid Secrets Manager ARN"):
        secrets_manager_arn_location(bad)


def test_full_arn_parser_returns_exact_account_region_and_name():
    ref = _arn(HOME_ACCOUNT, HOME_REGION, "agentcore-provider/openai/key")
    assert secrets_manager_arn_location(ref) == (
        HOME_ACCOUNT,
        HOME_REGION,
        "agentcore-provider/openai/key-AbCdEf",
    )


def test_foreign_owner_is_rejected_before_get_secret_value():
    source = _Secrets(HOME_ACCOUNT, HOME_REGION)
    target = _Secrets(HOME_ACCOUNT, HOME_REGION)
    ref = source.add(
        "agentcore-provider/openai/foreign",
        "must-not-be-read",
        _source_tags(OTHER_OWNER),
    )

    with pytest.raises(ConnectorSecretBindingError, match="another caller"):
        stage_runtime_secret_for_deployment(
            source_secret_ref=ref,
            source_namespace="agentcore-provider",
            purpose="model-provider-api-key",
            owner_sub=OWNER,
            deployment_id=DEPLOYMENT,
            target_region=HOME_REGION,
            source_secrets_client=source,
            target_secrets_client=target,
        )

    assert source.read == []
    assert target.created == []


@pytest.mark.parametrize("mode", ["missing", "wrong"])
def test_missing_or_wrong_stack_binding_is_rejected(mode):
    source = _Secrets(HOME_ACCOUNT, HOME_REGION)
    target = _Secrets(HOME_ACCOUNT, HOME_REGION)
    tags = _source_tags(OWNER)
    stack_tag = next(tag for tag in tags if tag["Key"] == "AgentCoreStack")
    if mode == "missing":
        tags.remove(stack_tag)
    else:
        stack_tag["Value"] = "another-stack"
    ref = source.add("agentcore-provider/openai/key", "must-not-be-read", tags)

    with pytest.raises(ConnectorSecretBindingError, match="another platform stack"):
        stage_runtime_secret_for_deployment(
            source_secret_ref=ref,
            source_namespace="agentcore-provider",
            purpose="model-provider-api-key",
            owner_sub=OWNER,
            deployment_id=DEPLOYMENT,
            target_region=HOME_REGION,
            source_secrets_client=source,
            target_secrets_client=target,
        )

    assert source.read == []
    assert target.created == []


def test_provider_value_is_copied_raw_and_bound_to_the_exact_deployment():
    source = _Secrets(HOME_ACCOUNT, HOME_REGION)
    target = _Secrets(TARGET_ACCOUNT, TARGET_REGION)
    raw_key = "sk-fake-provider-key-with-formatting"
    ref = source.add("agentcore-provider/openai/key", raw_key, _source_tags(OWNER))

    copied = stage_runtime_secret_for_deployment(
        source_secret_ref=ref,
        source_namespace="agentcore-provider",
        purpose="model-provider-api-key",
        owner_sub=OWNER,
        deployment_id=DEPLOYMENT,
        target_region=TARGET_REGION,
        source_secrets_client=source,
        target_secrets_client=target,
    )

    assert target.values[copied]["SecretString"] == raw_key
    tags = {tag["Key"]: tag["Value"] for tag in target.values[copied]["Tags"]}
    assert tags["AgentCoreStack"] == f"runtime-secret-tests-local-{TARGET_REGION}"
    assert tags["OwnerSubHash"] == owner_sub_hash(OWNER)
    assert tags["DeploymentId"] == DEPLOYMENT
    assert tags["Purpose"] == "model-provider-api-key"


def test_third_account_source_is_rejected_before_any_secret_client_is_created(monkeypatch):
    from app import deployment_handler
    from app.services import observability, step_clients

    target = _Secrets(TARGET_ACCOUNT, TARGET_REGION)
    monkeypatch.setattr(step_clients, "session_for_event", lambda event: _Session(target))
    monkeypatch.setattr(observability, "get_platform_observability_defaults", lambda: None)

    class _Sts:
        def get_caller_identity(self):
            return {"Account": HOME_ACCOUNT}

    def _client(service: str, **kwargs):
        if service == "sts":
            return _Sts()
        pytest.fail(f"foreign source unexpectedly opened a {service} client")

    monkeypatch.setattr(deployment_handler.boto3, "client", _client)

    with pytest.raises(ConnectorSecretBindingError, match="neither the platform account"):
        deployment_handler._prepare_runtime_credentials(
            runtime_config={
                "name": "agent",
                "providerApiKeyRef": _arn(
                    FOREIGN_ACCOUNT,
                    HOME_REGION,
                    "agentcore-provider/openai/key",
                ),
            },
            observability_config=None,
            deployment_id=DEPLOYMENT,
            owner_sub=OWNER,
            target_account_id=TARGET_ACCOUNT,
            target_region=TARGET_REGION,
            target_role_arn=f"arn:aws:iam::{TARGET_ACCOUNT}:role/Deploy",
            store=_Store(),
        )

    assert target.created == []


def test_platform_otel_is_copied_to_target_and_manifested_without_reformatting(monkeypatch):
    from app import deployment_handler
    from app.services import observability, step_clients

    home = _Secrets(HOME_ACCOUNT, HOME_REGION)
    target = _Secrets(TARGET_ACCOUNT, TARGET_REGION)
    raw_header = "Authorization=Basic ZmFrZTpmYWtl"
    source_ref = home.add("agentcore-otel/platform/default", raw_header)
    monkeypatch.setattr(
        observability,
        "get_platform_observability_defaults",
        lambda: {
            "enabled": True,
            "provider": "custom",
            "otlp_endpoint": "https://otel.example.test/v1/traces",
            "auth_header_secret_arn": source_ref,
            "sample_rate": 1.0,
        },
    )
    monkeypatch.setattr(step_clients, "session_for_event", lambda event: _Session(target))

    class _Sts:
        def get_caller_identity(self):
            return {"Account": HOME_ACCOUNT}

    def _client(service: str, **kwargs):
        if service == "sts":
            return _Sts()
        if service == "secretsmanager":
            assert kwargs.get("region_name") == HOME_REGION
            return home
        pytest.fail(f"unexpected boto3 client {service}")

    monkeypatch.setattr(deployment_handler.boto3, "client", _client)
    store = _Store()

    config, obs, defaults, recorded = deployment_handler._prepare_runtime_credentials(
        runtime_config={"name": "agent", "modelProvider": "bedrock"},
        observability_config=None,
        deployment_id=DEPLOYMENT,
        owner_sub=OWNER,
        target_account_id=TARGET_ACCOUNT,
        target_region=TARGET_REGION,
        target_role_arn=f"arn:aws:iam::{TARGET_ACCOUNT}:role/Deploy",
        store=store,
    )

    assert config["name"] == "agent"
    assert obs is None
    assert defaults is not None
    copied = defaults["auth_header_secret_arn"]
    assert recorded == [copied]
    assert target.values[copied]["SecretString"] == raw_header
    # The exact name is journaled before the copy exists, then the ARN row follows.
    assert store.rows == [
        (
            DEPLOYMENT,
            {
                "type": "secret",
                "id": copied.partition(":secret:")[2][:-7],
                "region": TARGET_REGION,
                "created_by_deployment": True,
                "account": TARGET_ACCOUNT,
            },
        ),
        (
            DEPLOYMENT,
            {
                "type": "secret",
                "id": copied,
                "region": TARGET_REGION,
                "created_by_deployment": True,
                "account": TARGET_ACCOUNT,
            },
        ),
    ]


@pytest.mark.parametrize(
    ("fail_after", "creates"),
    [
        # The journal row cannot be written: no copy may be created.
        (0, 0),
        # The journal lands, the copy commits, its ARN row fails: compensate.
        (1, 1),
    ],
)
def test_manifest_failure_compensates_the_target_copy(monkeypatch, fail_after, creates):
    from app import deployment_handler
    from app.services import observability, step_clients

    source = _Secrets(HOME_ACCOUNT, HOME_REGION)
    ref = source.add("agentcore-provider/openai/key", "raw-provider-key", _source_tags(OWNER))
    session = _Session(source)
    monkeypatch.setattr(step_clients, "session_for_event", lambda event: session)
    monkeypatch.setattr(observability, "get_platform_observability_defaults", lambda: None)

    class _Sts:
        def get_caller_identity(self):
            return {"Account": HOME_ACCOUNT}

    monkeypatch.setattr(
        deployment_handler.boto3,
        "client",
        lambda service, **kwargs: _Sts() if service == "sts" else source,
    )

    with pytest.raises(RuntimeError, match="manifest unavailable"):
        deployment_handler._prepare_runtime_credentials(
            runtime_config={"name": "agent", "providerApiKeyRef": ref},
            observability_config=None,
            deployment_id=DEPLOYMENT,
            owner_sub=OWNER,
            target_account_id=None,
            target_region=HOME_REGION,
            target_role_arn=None,
            store=_Store(fail_after=fail_after),
        )

    assert len(source.created) == creates
    assert len(source.deleted) == creates
    if not creates:
        return
    assert source.deleted[0]["ForceDeleteWithoutRecovery"] is True
    assert source.deleted[0]["SecretId"] != ref
    assert ":secret:agentcore-connector/" in source.deleted[0]["SecretId"]


def test_no_active_credentials_create_no_sessions_or_aws_clients(monkeypatch):
    from app import deployment_handler
    from app.services import observability, step_clients

    monkeypatch.setattr(observability, "get_platform_observability_defaults", lambda: None)
    monkeypatch.setattr(
        step_clients,
        "session_for_event",
        lambda event: pytest.fail("a credential-free deploy opened a target session"),
    )
    monkeypatch.setattr(
        deployment_handler.boto3,
        "client",
        lambda *args, **kwargs: pytest.fail("a credential-free deploy opened an AWS client"),
    )

    config, obs, defaults, recorded = deployment_handler._prepare_runtime_credentials(
        runtime_config={"name": "agent", "modelProvider": "bedrock"},
        observability_config=None,
        deployment_id=DEPLOYMENT,
        owner_sub=OWNER,
        target_account_id=None,
        target_region=HOME_REGION,
        target_role_arn=None,
        store=_Store(),
    )

    assert config == {"name": "agent", "modelProvider": "bedrock"}
    assert obs is None
    assert defaults is None
    assert recorded == []


def test_deployment_bound_otel_connector_is_trusted_only_by_exact_recorded_arn():
    from app.services.observability import build_otel_env_vars

    staged = _arn(HOME_ACCOUNT, HOME_REGION, "agentcore-connector/owner/staged")
    config = {
        "enabled": True,
        "provider": "custom",
        "otlp_endpoint": "https://otel.example.test/v1/traces",
        "auth_header_secret_arn": staged,
    }

    env = build_otel_env_vars(
        config,
        runtime_name="agent",
        trusted_auth_secret_arns=[staged],
    )
    assert env["OTEL_AUTH_SECRET_ARN"] == staged

    with pytest.raises(ValueError, match="agentcore-otel"):
        build_otel_env_vars(
            config,
            runtime_name="agent",
            trusted_auth_secret_arns=[_arn(HOME_ACCOUNT, HOME_REGION, "agentcore-connector/owner/someone-else")],
        )


def test_iam_accepts_a_staged_otel_secret_only_when_manifested():
    from app.step_handlers.iam_step import _resolve_otel_secret_arn

    staged = _arn(HOME_ACCOUNT, HOME_REGION, "agentcore-connector/owner/staged")
    event = {
        "observability_config": {"auth_header_secret_arn": staged},
        "recorded_secret_arns": [staged],
    }
    assert _resolve_otel_secret_arn(event) == staged
    assert _resolve_otel_secret_arn({**event, "recorded_secret_arns": []}) is None

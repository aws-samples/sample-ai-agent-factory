"""Unit tests for the live PolicyEngine spike orchestration.

No AWS call is made: boto3 clients are constructed offline (which needs no
credentials) and every orchestration test drives a fake API recorder. The tests
that matter most here are structural guarantees:

* the mutation registry is complete — a new mutating SDK call cannot be added
  without registering it;
* no mutating operation is reachable from the ``verify`` command, statically or
  at runtime;
* ``rollback`` can reach exactly one mutating operation, the named gateway
  policy-engine mode transition;
* cleanup deletes in dependency order, refuses resources it does not own, and is
  idempotent.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import pytest

boto3 = pytest.importorskip("boto3", reason="the live spike requires the pinned AWS SDK")

import policy_engine_model as model  # noqa: E402
from gateway_spike import REQUIRED_BOTO3_VERSION, SpikeError  # noqa: E402

if boto3.__version__ != REQUIRED_BOTO3_VERSION:  # pragma: no cover - env guard
    pytest.skip(
        f"boto3 {REQUIRED_BOTO3_VERSION} is required; found {boto3.__version__}",
        allow_module_level=True,
    )

import policy_engine_spike as spike_module  # noqa: E402

ACCOUNT = "123456789012"
REGION = "us-west-2"
PREFIX = "aiaf-pe-test"
GATEWAY_ID = "aiaf-pe-test-pe-gw-abc123"
GATEWAY_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/{GATEWAY_ID}"
ENGINE_ID = "aiaf_pe_test_engine-abcdefghij"
ENGINE_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:policy-engine/{ENGINE_ID}"
)
LAMBDA_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:aiaf-pe-test-pe-echo"
SUB_ALPHA = "11111111-2222-4333-8444-555555555555"
SUB_BETA = "66666666-7777-4888-8999-aaaaaaaaaaaa"
SUB_GAMMA = "22222222-3333-4444-8555-666666666666"
SUB_DELTA = "33333333-4444-4555-8666-777777777777"
SUBJECTS = {
    model.ALPHA: SUB_ALPHA,
    model.BETA: SUB_BETA,
    model.GAMMA: SUB_GAMMA,
    model.DELTA: SUB_DELTA,
}


# --------------------------------------------------------------------------
# Static analysis of the module's call graph
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def module_ast() -> ast.Module:
    source = Path(spike_module.__file__).read_text(encoding="utf-8")
    return ast.parse(source)


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    found: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("__"):
                continue
            assert node.name not in found, f"duplicate function name {node.name}"
            found[node.name] = node  # type: ignore[assignment]
    return found


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Attribute):
                names.add(child.func.attr)
            elif isinstance(child.func, ast.Name):
                names.add(child.func.id)
    return names


def _reachable(functions: Mapping[str, ast.FunctionDef], entry: str) -> set[str]:
    visited: set[str] = set()
    pending = [entry]
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        node = functions.get(current)
        if node is None:
            continue
        pending.extend(_called_names(node))
    return visited


def test_every_mutating_sdk_call_is_registered(module_ast: ast.Module) -> None:
    """A mutating boto3 call on a spike client must appear in the registry."""
    clients = {"iam", "lam", "logs", "cognito", "control", "sts"}
    prefixes = (
        "create_",
        "delete_",
        "update_",
        "put_",
        "add_",
        "remove_",
        "tag_",
        "untag_",
        "attach_",
        "detach_",
        "associate_",
        "disassociate_",
        "admin_",
        "set_",
    )
    unregistered: set[str] = set()
    for node in ast.walk(module_ast):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func.value
        if not (isinstance(target, ast.Attribute) and target.attr in clients):
            continue
        name = node.func.attr
        if not name.startswith(prefixes):
            continue
        if name in spike_module.READ_ONLY_EXCEPTIONS:
            continue
        if name not in spike_module.MUTATING_API_CALLS:
            unregistered.add(name)
    assert not unregistered, f"unregistered mutating calls: {sorted(unregistered)}"


def _mutators_reachable_from(
    functions: Mapping[str, ast.FunctionDef], entry: str
) -> set[str]:
    reachable = _reachable(functions, entry)
    called: set[str] = set()
    for name in reachable:
        node = functions.get(name)
        if node is not None:
            called |= _called_names(node)
    return (called | reachable) & spike_module.MUTATING_API_CALLS


def test_no_mutating_operation_is_reachable_from_verify(module_ast: ast.Module) -> None:
    leaked = _mutators_reachable_from(_functions(module_ast), "verify")
    assert not leaked, f"verify can reach mutating operations: {sorted(leaked)}"


def test_rollback_reaches_only_the_named_mode_transition(module_ast: ast.Module) -> None:
    functions = _functions(module_ast)
    reached = _mutators_reachable_from(functions, "rollback")
    # ``update_gateway`` is the SDK call underneath the named wrapper, so it is
    # expected — but only via that wrapper, which the next assertion pins.
    assert reached == {spike_module.ROLLBACK_MUTATION, "update_gateway"}, sorted(reached)
    assert "detach_policy_engine" not in reached
    assert "delete_gateway" not in reached


def test_update_gateway_is_only_called_by_association_wrappers(
    module_ast: ast.Module,
) -> None:
    functions = _functions(module_ast)
    callers = {
        name
        for name, node in functions.items()
        if "update_gateway" in _called_names(node)
    }
    assert callers == {"associate_policy_engine", "detach_policy_engine"}


def test_cleanup_reaches_deletions_but_never_creations(module_ast: ast.Module) -> None:
    reached = _mutators_reachable_from(_functions(module_ast), "cleanup")
    creations = {name for name in reached if name.startswith("create_")}
    assert not creations, f"cleanup can create resources: {sorted(creations)}"
    assert "delete_gateway" in reached and "delete_policy_engine" in reached


def test_registry_and_read_only_exceptions_do_not_overlap() -> None:
    assert not (
        spike_module.MUTATING_API_CALLS & spike_module.READ_ONLY_EXCEPTIONS
    )
    assert spike_module.ROLLBACK_MUTATION in spike_module.MUTATING_API_CALLS


# --------------------------------------------------------------------------
# Configuration validation
# --------------------------------------------------------------------------


def _args(**overrides: Any) -> Any:
    import argparse

    base = {
        "command": "verify",
        "account_id": ACCOUNT,
        "region": REGION,
        "prefix": PREFIX,
        "state_file": None,
        "evidence_file": None,
        "include_expiry_wait": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture()
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_SCRATCH", str(tmp_path))
    return tmp_path


def test_config_places_state_and_evidence_under_scratch(scratch: Path) -> None:
    config = spike_module.build_config(_args())
    assert config.state_path.parent == scratch
    assert config.evidence_path.parent == scratch
    assert config.names.prefix == PREFIX


def test_config_refuses_a_missing_scratch_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KIROCREW_SCRATCH", raising=False)
    with pytest.raises(SpikeError, match="KIROCREW_SCRATCH"):
        spike_module.build_config(_args())


@pytest.mark.parametrize("account", ["1234", "12345678901a", "", "1234567890123"])
def test_config_refuses_a_malformed_account_id(scratch: Path, account: str) -> None:
    with pytest.raises(SpikeError, match="12 digits"):
        spike_module.build_config(_args(account_id=account))


@pytest.mark.parametrize("prefix", ["Prod", "x", "under_score", "-lead"])
def test_config_refuses_a_malformed_prefix(scratch: Path, prefix: str) -> None:
    with pytest.raises(model.CedarPolicyError):
        spike_module.build_config(_args(prefix=prefix))


@pytest.mark.parametrize("region", ["me-south-1", "ap-east-1", "not-a-region"])
def test_config_refuses_a_region_without_documented_policy_support(
    scratch: Path, region: str
) -> None:
    with pytest.raises(SpikeError, match="region list"):
        spike_module.build_config(_args(region=region))


def test_config_accepts_the_emea_regions(scratch: Path) -> None:
    for region in sorted(model.POLICY_EMEA_REGIONS):
        assert spike_module.build_config(_args(region=region)).region == region


def test_cli_rejects_an_unknown_command() -> None:
    with pytest.raises(SystemExit):
        spike_module.parse_args(["promote", "--account-id", ACCOUNT])


def test_cli_exposes_exactly_the_bounded_commands() -> None:
    assert spike_module.COMMANDS == ("deploy", "verify", "rollback", "cleanup", "all")


# --------------------------------------------------------------------------
# IAM documents and the Lambda artefact
# --------------------------------------------------------------------------


@pytest.fixture()
def config(scratch: Path) -> spike_module.PolicyEngineConfig:
    return spike_module.build_config(_args())


def test_gateway_authorize_policies_use_documented_actions_and_scopes() -> None:
    engine = spike_module.gateway_authorize_engine_policy(ENGINE_ARN)
    engine_statement = engine["Statement"][0]
    assert engine_statement["Action"] == ["bedrock-agentcore:GetPolicyEngine"]
    assert engine_statement["Resource"] == ENGINE_ARN

    evaluation = spike_module.gateway_authorize_eval_policy(ENGINE_ARN, GATEWAY_ARN)
    evaluation_statement = evaluation["Statement"][0]
    assert evaluation_statement["Action"] == [
        "bedrock-agentcore:AuthorizeAction",
        "bedrock-agentcore:PartiallyAuthorizeActions",
    ]
    assert evaluation_statement["Resource"] == [ENGINE_ARN, GATEWAY_ARN]
    assert "*" not in json.dumps([engine, evaluation])


def test_gateway_invoke_policy_is_scoped_to_one_exact_function() -> None:
    document = spike_module.gateway_invoke_policy(LAMBDA_ARN)
    statement = document["Statement"][0]
    assert statement["Action"] == "lambda:InvokeFunction"
    assert statement["Resource"] == LAMBDA_ARN


@pytest.mark.parametrize(
    "builder,arguments",
    [
        (spike_module.gateway_invoke_policy, (f"{LAMBDA_ARN}*",)),
        (spike_module.gateway_authorize_engine_policy, ("arn:aws:*",)),
        (spike_module.gateway_authorize_eval_policy, (ENGINE_ARN, "arn:aws:*")),
    ],
)
def test_wildcard_resources_are_refused(
    builder: Callable[..., Any], arguments: tuple[str, ...]
) -> None:
    with pytest.raises(SpikeError, match="wildcard"):
        builder(*arguments)


def test_gateway_trust_policy_is_confused_deputy_scoped(
    config: spike_module.PolicyEngineConfig,
) -> None:
    statement = spike_module.gateway_trust_policy(config)["Statement"][0]
    assert statement["Principal"]["Service"] == "bedrock-agentcore.amazonaws.com"
    condition = statement["Condition"]
    assert condition["StringEquals"]["aws:SourceAccount"] == ACCOUNT
    assert condition["ArnLike"]["aws:SourceArn"].endswith(
        f"gateway/{config.names.gateway_name}-*"
    )


def test_lambda_logs_policy_is_scoped_to_its_own_log_group(
    config: spike_module.PolicyEngineConfig,
) -> None:
    resources = spike_module.lambda_logs_policy(config)["Statement"][0]["Resource"]
    assert all(config.names.lambda_name in resource for resource in resources)


def test_lambda_zip_is_deterministic_and_carries_the_marker() -> None:
    first = spike_module.build_lambda_zip()
    assert first == spike_module.build_lambda_zip()
    assert model.ECHO_MARKER in spike_module.LAMBDA_SOURCE


def test_lambda_source_never_logs_and_never_echoes_argument_values() -> None:
    source = spike_module.LAMBDA_SOURCE
    for forbidden in ("print(", "logging", "logger", "json.dumps(event"):
        assert forbidden not in source
    # Only the sorted field names are returned, never the values.
    assert "sorted(event)" in source


@pytest.mark.parametrize(
    "caller,expected",
    [
        (
            f"arn:aws:sts::{ACCOUNT}:assumed-role/Admin/session-name",
            f"arn:aws:iam::{ACCOUNT}:role/Admin",
        ),
        (f"arn:aws:iam::{ACCOUNT}:user/builder", f"arn:aws:iam::{ACCOUNT}:user/builder"),
        (f"arn:aws:sts::{ACCOUNT}:federated-user/x", None),
    ],
)
def test_role_arn_for_simulation(caller: str, expected: str | None) -> None:
    assert spike_module.role_arn_for_simulation(caller, ACCOUNT) == expected


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


def test_evidence_refuses_nested_credential_values(
    config: spike_module.PolicyEngineConfig,
) -> None:
    evidence = spike_module.SpikeEvidence(config.evidence_path, config)
    with pytest.raises(model.SecretLeakError):
        evidence.add("probe", detail={"inner": {"accessToken": "abc"}})
    with pytest.raises(model.SecretLeakError):
        evidence.add("probe", detail="eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0.")


def test_evidence_refuses_credential_field_names(
    config: spike_module.PolicyEngineConfig,
) -> None:
    # The value scanner runs before the inherited key guard and is a superset of
    # it, so a credential-named field is rejected as a leak.
    evidence = spike_module.SpikeEvidence(config.evidence_path, config)
    with pytest.raises(model.SecretLeakError):
        evidence.add("probe", authorization="Basic abc")
    with pytest.raises(model.SecretLeakError):
        evidence.add("probe", clientSecret="abc")


def test_evidence_records_unknowns_separately(
    config: spike_module.PolicyEngineConfig,
) -> None:
    evidence = spike_module.SpikeEvidence(config.evidence_path, config)
    evidence.add_unknown("expired_token_denial_not_attempted", "needs a 6 minute wait")
    document = json.loads(config.evidence_path.read_text(encoding="utf-8"))
    assert document["unknowns"][0]["unknown"] == "expired_token_denial_not_attempted"
    assert document["run"]["accountSuffix"] == ACCOUNT[-4:]
    assert ACCOUNT not in config.evidence_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Runtime mutation guard
# --------------------------------------------------------------------------


@pytest.fixture()
def api(config: spike_module.PolicyEngineConfig) -> spike_module.PolicyEngineApi:
    # Offline client construction: no credentials and no network are required.
    session = boto3.Session(region_name=config.region)
    return spike_module.PolicyEngineApi(session, config)


def test_read_only_scope_blocks_every_mutating_wrapper(
    api: spike_module.PolicyEngineApi,
) -> None:
    api.mutation_scope = frozenset()
    with pytest.raises(SpikeError, match="read-only"):
        api.delete_gateway(GATEWAY_ID)
    with pytest.raises(SpikeError, match="read-only"):
        api.create_policy(ENGINE_ID, "p", "permit(principal, action, resource);", "t" * 40)


def test_rollback_scope_permits_only_the_mode_transition(
    api: spike_module.PolicyEngineApi,
) -> None:
    api.mutation_scope = frozenset({spike_module.ROLLBACK_MUTATION})
    with pytest.raises(SpikeError, match="mutation scope"):
        api.delete_policy(ENGINE_ID, "policy-id")
    with pytest.raises(SpikeError, match="Unsupported policy engine mode"):
        api.associate_policy_engine(
            {"gatewayId": GATEWAY_ID, "name": "n", "roleArn": "r"},
            "pool",
            "client",
            ENGINE_ARN,
            "DISABLED",
        )


def test_guard_rejects_an_unregistered_operation(
    api: spike_module.PolicyEngineApi,
) -> None:
    with pytest.raises(SpikeError, match="mutation registry"):
        api._require_mutation("create_something_new")


def test_create_policy_refuses_a_substring_matching_statement(
    api: spike_module.PolicyEngineApi,
) -> None:
    api.mutation_scope = frozenset({"create_policy"})
    with pytest.raises(model.CedarPolicyError):
        api.create_policy(
            ENGINE_ID,
            "p",
            'permit(principal, action, resource) when { principal.getTag("g") == "x" };',
            "t" * 40,
        )


def test_capability_probe_reads_the_pinned_service_model(
    api: spike_module.PolicyEngineApi,
) -> None:
    assert api.capability("CreatePolicyEngine", "tags") is True
    assert api.capability("CreatePolicyEngine", "notAField") is False


# --------------------------------------------------------------------------
# Cleanup ordering, ownership, and idempotence
# --------------------------------------------------------------------------


class FakeApi:
    """Records wrapper calls in order and simulates deletion."""

    def __init__(self, config: spike_module.PolicyEngineConfig, *, populated: bool) -> None:
        self.config = config
        self.calls: list[str] = []
        self.mutation_scope: frozenset[str] = frozenset()
        self.gateway_present = populated
        self.engine_present = populated
        self.pool_present = populated
        self.function_present = populated
        self.log_group_present = populated
        self.lambda_policy: dict[str, Any] | None = None
        self.roles = (
            {config.names.gateway_role_name, config.names.lambda_role_name}
            if populated
            else set()
        )
        self.targets = (
            [{"targetId": "tgt0000001", "name": config.names.target_name}] if populated else []
        )
        self.policies = (
            [
                {
                    "policyId": "aiaf_pe_test_subject_only_alpha-abcdefghij",
                    "name": config.names.policy_name("subject_only_alpha"),
                    "status": "ACTIVE",
                }
            ]
            if populated
            else []
        )
        self.associated = populated

    def _log(self, name: str) -> None:
        self.calls.append(name)

    # identity
    def get_caller_identity(self) -> Mapping[str, Any]:
        self._log("get_caller_identity")
        return {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/tester"}

    # gateway
    def jwt_authorizer(self, pool_id: str, client_id: str) -> dict[str, Any]:
        return {
            "customJWTAuthorizer": {
                "discoveryUrl": (
                    f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}/"
                    ".well-known/openid-configuration"
                ),
                "allowedClients": [client_id],
            }
        }

    def get_gateway(self, gateway_id: str) -> Mapping[str, Any] | None:
        self._log("get_gateway")
        if not self.gateway_present:
            return None
        gateway: dict[str, Any] = {
            "gatewayId": GATEWAY_ID,
            "gatewayArn": GATEWAY_ARN,
            "name": self.config.names.gateway_name,
            "roleArn": f"arn:aws:iam::{ACCOUNT}:role/{self.config.names.gateway_role_name}",
            "status": "READY",
            "protocolType": "MCP",
            "authorizerType": "CUSTOM_JWT",
            "authorizerConfiguration": {
                "customJWTAuthorizer": {
                    "discoveryUrl": (
                        f"https://cognito-idp.{REGION}.amazonaws.com/us-west-2_pool/"
                        ".well-known/openid-configuration"
                    ),
                    "allowedClients": ["primaryclient"],
                }
            },
        }
        if self.associated:
            gateway["policyEngineConfiguration"] = {"arn": ENGINE_ARN, "mode": "ENFORCE"}
        return gateway

    def find_gateway(self, name: str) -> Mapping[str, Any] | None:
        self._log("find_gateway")
        return self.get_gateway(GATEWAY_ID) if self.gateway_present else None

    def list_tags_for_resource(self, arn: str) -> Mapping[str, str]:
        self._log("list_tags_for_resource")
        return self.config.tags

    def detach_policy_engine(self, gateway, pool_id, client_id):  # type: ignore[no-untyped-def]
        self._log("detach_policy_engine")
        self.associated = False
        return {}

    def list_gateway_targets(self, gateway_id: str) -> list[Mapping[str, Any]]:
        self._log("list_gateway_targets")
        return list(self.targets)

    def get_gateway_target(self, gateway_id: str, target_id: str) -> Mapping[str, Any] | None:
        self._log("get_gateway_target")
        return next((t for t in self.targets if t["targetId"] == target_id), None)

    def delete_gateway_target(self, gateway_id: str, target_id: str) -> None:
        self._log("delete_gateway_target")
        self.targets = [t for t in self.targets if t["targetId"] != target_id]

    def delete_gateway(self, gateway_id: str) -> None:
        self._log("delete_gateway")
        self.gateway_present = False

    # policy engine
    def get_policy_engine(self, engine_id: str) -> Mapping[str, Any] | None:
        self._log("get_policy_engine")
        if not self.engine_present:
            return None
        return {
            "policyEngineId": ENGINE_ID,
            "policyEngineArn": ENGINE_ARN,
            "name": self.config.names.engine_name,
            "status": "ACTIVE",
        }

    def find_policy_engine(self, name: str) -> Mapping[str, Any] | None:
        self._log("find_policy_engine")
        return self.get_policy_engine(ENGINE_ID) if self.engine_present else None

    def list_policy_summaries(self, engine_id: str) -> list[Mapping[str, Any]]:
        self._log("list_policy_summaries")
        return list(self.policies)

    def get_policy_summary(self, engine_id: str, policy_id: str) -> Mapping[str, Any] | None:
        self._log("get_policy_summary")
        return next((policy for policy in self.policies if policy["policyId"] == policy_id), None)

    def delete_policy(self, engine_id: str, policy_id: str) -> None:
        self._log("delete_policy")
        self.policies = [p for p in self.policies if p["policyId"] != policy_id]

    def delete_policy_engine(self, engine_id: str) -> None:
        self._log("delete_policy_engine")
        self.engine_present = False

    # compute
    def get_lambda_policy(self, name: str) -> Mapping[str, Any] | None:
        self._log("get_lambda_policy")
        return self.lambda_policy

    def add_permission(
        self, name: str, statement_id: str, principal_arn: str
    ) -> Mapping[str, Any]:
        self._log("add_permission")
        self.lambda_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": statement_id,
                    "Effect": "Allow",
                    "Action": "lambda:InvokeFunction",
                    "Principal": {"AWS": principal_arn},
                }
            ],
        }
        return {}

    def get_function(self, name: str) -> Mapping[str, Any] | None:
        self._log("get_function")
        if not self.function_present:
            return None
        return {
            "Configuration": {
                "FunctionName": self.config.names.lambda_name,
                "FunctionArn": LAMBDA_ARN,
            },
            "Tags": dict(self.config.tags),
        }

    def delete_function(self, name: str) -> None:
        self._log("delete_function")
        self.function_present = False

    def get_log_group(self, name: str) -> Mapping[str, Any] | None:
        self._log("get_log_group")
        if not self.log_group_present:
            return None
        return {"logGroupName": self.config.names.log_group_name}

    def get_log_group_tags(self, name: str) -> Mapping[str, str]:
        self._log("get_log_group_tags")
        return self.config.tags

    def delete_log_group(self, name: str) -> None:
        self._log("delete_log_group")
        self.log_group_present = False

    def get_role(self, name: str) -> Mapping[str, Any] | None:
        self._log("get_role")
        if name not in self.roles:
            return None
        return {
            "Arn": f"arn:aws:iam::{ACCOUNT}:role/{name}",
            "Tags": [{"Key": k, "Value": v} for k, v in self.config.tags.items()],
        }

    def delete_role_policy(self, role_name: str, policy_name: str) -> None:
        self._log("delete_role_policy")

    def delete_role(self, role_name: str) -> None:
        self._log("delete_role")
        self.roles.discard(role_name)

    # identity provider
    def describe_user_pool(self, pool_id: str) -> Mapping[str, Any] | None:
        self._log("describe_user_pool")
        if not self.pool_present:
            return None
        return {
            "Id": "us-west-2_pool",
            "Name": self.config.names.user_pool_name,
            "UserPoolTags": dict(self.config.tags),
        }

    def find_user_pool(self, name: str) -> Mapping[str, Any] | None:
        self._log("find_user_pool")
        return self.describe_user_pool("us-west-2_pool") if self.pool_present else None

    def delete_user_pool(self, pool_id: str) -> None:
        self._log("delete_user_pool")
        self.pool_present = False


def _spike(
    config: spike_module.PolicyEngineConfig, *, populated: bool
) -> tuple[spike_module.PolicyEngineSpike, FakeApi]:
    spike = spike_module.PolicyEngineSpike(config)
    fake = FakeApi(config, populated=populated)
    spike.api = fake  # type: ignore[assignment]
    if populated:
        spike.save_state(
            gatewayId=GATEWAY_ID,
            gatewayArn=GATEWAY_ARN,
            gatewayUrl=f"https://{GATEWAY_ID}.gateway.example/mcp",
            policyEngineId=ENGINE_ID,
            policyEngineArn=ENGINE_ARN,
            lambdaArn=LAMBDA_ARN,
            gatewayRoleArn=f"arn:aws:iam::{ACCOUNT}:role/{config.names.gateway_role_name}",
            userPoolId="us-west-2_pool",
            primaryClientId="primaryclient",
            foreignClientId="foreignclient",
            subjects=dict(SUBJECTS),
        )
    return spike, fake


@pytest.fixture()
def populated(
    config: spike_module.PolicyEngineConfig,
) -> Iterator[tuple[spike_module.PolicyEngineSpike, FakeApi]]:
    spike, fake = _spike(config, populated=True)
    yield spike, fake
    spike.close()


def test_cleanup_deletes_in_dependency_order(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike, fake = populated
    spike.cleanup()
    order = [call for call in fake.calls if call.startswith(("delete_", "detach_"))]
    assert order == [
        "detach_policy_engine",
        "delete_gateway_target",
        "delete_gateway",
        "delete_policy",
        "delete_policy_engine",
        "delete_function",
        "delete_role_policy",
        "delete_role_policy",
        "delete_role_policy",
        "delete_role",
        "delete_role_policy",
        "delete_role",
        "delete_log_group",
        "delete_user_pool",
    ]


def test_cleanup_clears_state_and_records_zero_residue(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike, _ = populated
    spike.cleanup()
    assert spike.state == {}
    document = json.loads(spike.config.evidence_path.read_text(encoding="utf-8"))
    events = [event["event"] for event in document["events"]]
    assert "zero_residual_verified" in events
    residue = next(
        event for event in document["events"] if event["event"] == "zero_residual_verified"
    )
    assert all(not value for value in residue["inventory"].values())


def test_cleanup_is_idempotent_on_an_empty_account(
    config: spike_module.PolicyEngineConfig,
) -> None:
    spike, fake = _spike(config, populated=False)
    try:
        spike.cleanup()
        spike.cleanup()
    finally:
        spike.close()
    assert not [call for call in fake.calls if call.startswith("delete_")]


def test_cleanup_fails_closed_when_residue_remains(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    """A successful delete sequence still fails if the final sweep sees residue."""
    spike, fake = populated
    fake.find_user_pool = lambda name: {  # type: ignore[method-assign]
        "Id": "us-west-2_pool",
        "Name": name,
    }
    with pytest.raises(SpikeError, match="remain after cleanup"):
        spike.cleanup()


def test_cleanup_refuses_a_foreign_gateway_target(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike, fake = populated
    fake.targets = [{"targetId": "tgt0000002", "name": "prod-payments-target"}]
    with pytest.raises(SpikeError, match="unexpected gateway target"):
        spike.cleanup()


def test_cleanup_refuses_a_foreign_policy(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike, fake = populated
    fake.policies = [{"policyId": "other-abcdefghij", "name": "prod_admin_permit"}]
    with pytest.raises(SpikeError, match="unexpected policy"):
        spike.cleanup()


def test_cleanup_refuses_a_user_pool_with_foreign_tags(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike, fake = populated

    def foreign_pool(pool_id: str) -> Mapping[str, Any]:
        return {
            "Id": pool_id,
            "Name": spike.names.user_pool_name,
            "UserPoolTags": {"application-id": "someone-else"},
        }

    fake.describe_user_pool = foreign_pool  # type: ignore[method-assign]
    with pytest.raises(SpikeError, match="ownership tags differ"):
        spike.cleanup()


def test_cleanup_refuses_a_gateway_whose_tags_differ(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike, fake = populated
    fake.list_tags_for_resource = lambda arn: {"application-id": "prod"}  # type: ignore[method-assign]
    with pytest.raises(SpikeError, match="ownership tags differ"):
        spike.cleanup()


def test_verify_identity_refuses_a_different_account(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike, fake = populated
    fake.get_caller_identity = lambda: {  # type: ignore[method-assign]
        "Account": "999999999999",
        "Arn": "arn:aws:iam::999999999999:user/other",
    }
    with pytest.raises(SpikeError, match="does not match expected"):
        spike.verify_identity()


def test_verify_sets_a_read_only_mutation_scope(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike, fake = populated
    spike.allow_mutations()
    assert fake.mutation_scope == frozenset()
    spike.allow_mutations(spike_module.ROLLBACK_MUTATION)
    assert fake.mutation_scope == frozenset({spike_module.ROLLBACK_MUTATION})


# --------------------------------------------------------------------------
# Verification-step guards that need no live gateway
# --------------------------------------------------------------------------


def test_gateway_configuration_check_rejects_log_only_mode(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike, fake = populated
    original = fake.get_gateway

    def log_only(gateway_id: str) -> Mapping[str, Any]:
        gateway = dict(original(gateway_id) or {})
        gateway["policyEngineConfiguration"] = {"arn": ENGINE_ARN, "mode": "LOG_ONLY"}
        return gateway

    fake.get_gateway = log_only  # type: ignore[method-assign]
    with pytest.raises(SpikeError, match="expected ENFORCE"):
        spike.verify_gateway_configuration()


def test_gateway_configuration_check_rejects_a_foreign_engine(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike, fake = populated
    original = fake.get_gateway

    def other_engine(gateway_id: str) -> Mapping[str, Any]:
        gateway = dict(original(gateway_id) or {})
        gateway["policyEngineConfiguration"] = {
            "arn": ENGINE_ARN.replace("aiaf_pe_test", "someone_else"),
            "mode": "ENFORCE",
        }
        return gateway

    fake.get_gateway = other_engine  # type: ignore[method-assign]
    with pytest.raises(SpikeError, match="different policy engine"):
        spike.verify_gateway_configuration()


def test_rendered_group_policies_use_only_the_audited_candidate(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike, _ = populated
    statements = spike.rendered_statements().values()
    group_statements = [
        statement for statement in statements if model.GROUP_CLAIM_NAME in statement
    ]
    assert len(group_statements) == 4
    for statement in group_statements:
        model.assert_no_pattern_matching(statement)
        assert f'principal.hasTag("{model.GROUP_CLAIM_NAME}")' in statement
        assert f'principal.getTag("{model.GROUP_CLAIM_NAME}")' in statement


def test_rendered_statements_use_the_live_gateway_arn_from_state(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike_instance, _ = populated
    statements = spike_instance.rendered_statements()
    assert len(statements) == 8
    for statement in statements.values():
        assert GATEWAY_ARN in statement
        assert f'"{spike_instance.names.target_name}___' in statement


def test_enforcement_probe_is_modelled_as_a_denial(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike_instance, _ = populated
    probe = spike_instance.enforcement_probe()
    decision = model.evaluate(
        spike_instance.policy_set(),
        subject=SUB_DELTA,
        groups=model.groups_for_label(spike_instance.names, model.DELTA),
        tool=probe.tool,
        arguments=probe.arguments,
    )
    assert decision is model.Decision.DENY
    assert probe.subject_label == model.DELTA


def test_require_state_fails_closed_when_deploy_has_not_run(
    config: spike_module.PolicyEngineConfig,
) -> None:
    spike, _ = _spike(config, populated=False)
    try:
        with pytest.raises(SpikeError, match="run deploy first"):
            spike.require_state("gatewayArn")
    finally:
        spike.close()


def test_tokens_cannot_be_minted_across_processes(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike_instance, _ = populated
    with pytest.raises(SpikeError, match="'all' command"):
        spike_instance.access_token(model.ALPHA)


# --------------------------------------------------------------------------
# Regressions for live-safety defects found during implementation review
# --------------------------------------------------------------------------


def test_deploy_associates_engine_before_policy_creation(module_ast: ast.Module) -> None:
    deploy = _functions(module_ast)["deploy"]
    calls = [
        node.func.attr
        for node in ast.walk(deploy)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    ordered = sorted(
        (
            (node.lineno, node.col_offset, node.func.attr)
            for node in ast.walk(deploy)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        )
    )
    names = [name for _, _, name in ordered]
    assert "ensure_policies" in calls
    assert names.index("ensure_target") < names.index("attach_authorize_policies")
    assert names.index("attach_authorize_policies") < names.index("associate_log_only")
    assert names.index("associate_log_only") < names.index("ensure_policies")
    assert names.index("ensure_policies") < names.index("associate_enforce")


def test_agentcore_creates_and_updates_use_bounded_propagation(
    module_ast: ast.Module,
) -> None:
    functions = _functions(module_ast)
    assert spike_module.PROPAGATION_TIMEOUT_SECONDS >= 300
    for name in ("ensure_gateway", "ensure_target", "associate_log_only", "associate_enforce"):
        assert "call_with_propagation" in _called_names(functions[name]), name


def test_list_policy_summaries_contract_matches_pinned_sdk(
    api: spike_module.PolicyEngineApi,
) -> None:
    operation = api.control.meta.service_model.operation_model(
        model.LIST_POLICIES_OPERATION
    )
    assert "policyEngineId" in operation.input_shape.members
    policies = operation.output_shape.members["policies"]
    summary = policies.member
    assert set(model.POLICY_SUMMARY_MEMBERS) <= set(summary.members)


def test_full_run_always_includes_expired_token_proof(scratch: Path) -> None:
    assert spike_module.build_config(_args(command="all")).include_expiry_wait
    assert not spike_module.build_config(_args(command="verify")).include_expiry_wait


def test_lambda_permission_is_idempotent_and_exact(
    config: spike_module.PolicyEngineConfig,
) -> None:
    spike, fake = _spike(config, populated=False)
    role_arn = f"arn:aws:iam::{ACCOUNT}:role/{config.names.gateway_role_name}"
    try:
        spike.ensure_lambda_permission(role_arn)
        spike.ensure_lambda_permission(role_arn)
    finally:
        spike.close()
    assert fake.calls.count("add_permission") == 1
    statement = (fake.lambda_policy or {})["Statement"][0]
    assert statement["Principal"] == {"AWS": role_arn}
    assert statement["Action"] == "lambda:InvokeFunction"


def test_lambda_permission_refuses_same_sid_with_different_principal(
    config: spike_module.PolicyEngineConfig,
) -> None:
    spike, fake = _spike(config, populated=False)
    fake.lambda_policy = {
        "Statement": [
            {
                "Sid": config.names.lambda_permission_id,
                "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:role/foreign"},
                "Action": "lambda:InvokeFunction",
            }
        ]
    }
    try:
        with pytest.raises(SpikeError, match="differs from the exact Gateway role"):
            spike.ensure_lambda_permission(
                f"arn:aws:iam::{ACCOUNT}:role/{config.names.gateway_role_name}"
            )
    finally:
        spike.close()
    assert "add_permission" not in fake.calls


def test_policy_delete_is_polled_absent_before_engine_delete(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike, fake = populated
    spike.cleanup_policy_engine()
    assert fake.calls.index("delete_policy") < fake.calls.index("get_policy_summary")
    assert fake.calls.index("get_policy_summary") < fake.calls.index("delete_policy_engine")


def test_cleanup_refuses_policy_engine_with_foreign_tags(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike, fake = populated
    fake.list_tags_for_resource = lambda arn: {"application-id": "foreign"}  # type: ignore[method-assign]
    with pytest.raises(SpikeError, match="ownership tags differ"):
        spike.cleanup_policy_engine()
    assert "delete_policy" not in fake.calls


def test_cleanup_refuses_lambda_with_foreign_tags(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike, fake = populated
    original = fake.get_function

    def foreign_function(name: str) -> Mapping[str, Any] | None:
        function = original(name)
        if function is not None:
            function = dict(function)
            function["Tags"] = {"application-id": "foreign"}
        return function

    fake.get_function = foreign_function  # type: ignore[method-assign]
    with pytest.raises(SpikeError, match="ownership tags differ"):
        spike.cleanup_compute()
    assert "delete_function" not in fake.calls


def test_cleanup_refuses_log_group_with_foreign_tags(
    populated: tuple[spike_module.PolicyEngineSpike, FakeApi],
) -> None:
    spike, fake = populated
    fake.function_present = False
    fake.roles.clear()
    fake.get_log_group_tags = lambda name: {"application-id": "foreign"}  # type: ignore[method-assign]
    with pytest.raises(SpikeError, match="ownership tags differ"):
        spike.cleanup_compute()
    assert "delete_log_group" not in fake.calls


def test_lambda_permission_retries_invalid_principal_propagation(
    config: spike_module.PolicyEngineConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spike, fake = _spike(config, populated=False)
    role_arn = f"arn:aws:iam::{ACCOUNT}:role/{config.names.gateway_role_name}"
    original = fake.add_permission
    attempts = 0

    def add_permission(name: str, statement_id: str, principal_arn: str) -> Mapping[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            fake._log("add_permission")
            raise spike_module.ClientError(
                {
                    "Error": {
                        "Code": "InvalidParameterValueException",
                        "Message": "The provided principal was invalid.",
                    }
                },
                "AddPermission",
            )
        return original(name, statement_id, principal_arn)

    fake.add_permission = add_permission  # type: ignore[method-assign]
    monkeypatch.setattr(spike_module.time, "sleep", lambda _: None)
    try:
        spike.ensure_lambda_permission(role_arn)
    finally:
        spike.close()
    assert attempts == 2
    assert fake.calls.count("add_permission") == 2


def test_lambda_permission_does_not_retry_unrelated_invalid_parameter(
    config: spike_module.PolicyEngineConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spike, fake = _spike(config, populated=False)
    attempts = 0

    def add_permission(name: str, statement_id: str, principal_arn: str) -> Mapping[str, Any]:
        nonlocal attempts
        attempts += 1
        raise spike_module.ClientError(
            {
                "Error": {
                    "Code": "InvalidParameterValueException",
                    "Message": "The function name is invalid.",
                }
            },
            "AddPermission",
        )

    fake.add_permission = add_permission  # type: ignore[method-assign]
    monkeypatch.setattr(spike_module.time, "sleep", lambda _: None)
    try:
        with pytest.raises(spike_module.ClientError):
            spike.ensure_lambda_permission(
                f"arn:aws:iam::{ACCOUNT}:role/{config.names.gateway_role_name}"
            )
    finally:
        spike.close()
    assert attempts == 1

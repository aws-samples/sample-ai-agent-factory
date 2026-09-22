#!/usr/bin/env python3
"""Offline unit tests for :mod:`identity_m2m_spike`.

All AWS access is faked. No network, no real credentials. These tests prove the
runner's control logic: config validation, the scope guard, deploy/verify/cleanup
orchestration, cleanup ordering (provider then workload) with continue-after-
failure, partial-create recovery, provenance/completed-run refusal, the negative
token wrappers, and -- the crux -- that an injected sentinel client secret and
sentinel tokens NEVER reach the state file, the evidence file, stdout, or an
error message.

They also assert the pinned SDK models every operation/member the spike calls
(a real ``botocore`` capability check, still AWS-free).

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import boto3
import pytest

import identity_m2m_model as model
import identity_m2m_spike as spike

SENTINEL_SECRET = "SENTINEL-CLIENT-SECRET-do-not-persist-000"
SENTINEL_WORKLOAD_TOKEN = "SENTINEL-WORKLOAD-TOKEN-" + "w" * 40
SENTINEL_RESOURCE_TOKEN = "SENTINEL-RESOURCE-TOKEN-" + "r" * 40

ACCOUNT = "123456789012"
REGION = "us-west-2"
PREFIX = "aiaf-idm2m-test"
SOURCE_REVISION = "a" * 40
USER_POOL_ID = "us-west-2_ExamplePool"
COGNITO_HOST = "example.auth.us-west-2.amazoncognito.com"
ISSUER = f"https://cognito-idp.us-west-2.amazonaws.com/{USER_POOL_ID}"
AUTHORIZATION_ENDPOINT = f"https://{COGNITO_HOST}/oauth2/authorize"
TOKEN_ENDPOINT = f"https://{COGNITO_HOST}/oauth2/token"
GATEWAY_URL = "https://example.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
MODEL_ID = "bedrock-mantle/openai.gpt-oss-120b"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeControl:
    """In-memory bedrock-agentcore-control fake with a shared service model."""

    def __init__(self) -> None:
        self.workloads: dict[str, dict[str, Any]] = {}
        self.providers: dict[str, dict[str, Any]] = {}
        self.tags_by_arn: dict[str, dict[str, str]] = {}
        self.calls: list[str] = []
        self.provider_status = "READY"
        # A real service model so capability() checks exercise the pinned SDK.
        self.meta = boto3.client(
            model.CONTROL_SERVICE, region_name=REGION
        ).meta

    def create_workload_identity(self, *, name, tags):
        self.calls.append("create_workload_identity")
        arn = (
            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
            f"workload-identity-directory/default/workload-identity/{name}"
        )
        self.workloads[name] = {"name": name, "workloadIdentityArn": arn}
        self.tags_by_arn[arn] = dict(tags)
        return {**self.workloads[name], "ResponseMetadata": {"RequestId": "req-wl"}}

    def get_workload_identity(self, *, name):
        if name not in self.workloads:
            raise _not_found()
        return dict(self.workloads[name])

    def list_workload_identities(self, **_):
        return {"workloadIdentities": [{"name": n, "workloadIdentityArn": v["workloadIdentityArn"]}
                                       for n, v in self.workloads.items()]}

    def delete_workload_identity(self, *, name):
        self.calls.append("delete_workload_identity")
        record = self.workloads.pop(name, None)
        if record:
            self.tags_by_arn.pop(record["workloadIdentityArn"], None)
        return {}

    def create_oauth2_credential_provider(
        self, *, name, credentialProviderVendor, oauth2ProviderConfigInput, tags
    ):
        self.calls.append("create_oauth2_credential_provider")
        # The secret must arrive here (proving it was passed to create) ...
        inner = oauth2ProviderConfigInput[model.PROVIDER_CONFIG_MEMBER]
        self.seen_secret = inner["clientSecret"]
        arn = (
            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
            f"token-vault/default/oauth2credentialprovider/{name}"
        )
        self.providers[name] = {
            "name": name,
            "credentialProviderVendor": credentialProviderVendor,
            "credentialProviderArn": arn,
            "status": self.provider_status,
        }
        self.tags_by_arn[arn] = dict(tags)
        return {
            "name": name,
            "credentialProviderArn": arn,
            "clientSecretArn": "arn:aws:secretsmanager:...:secret:x",
            "status": self.provider_status,
            "ResponseMetadata": {"RequestId": "req-cp"},
        }

    def get_oauth2_credential_provider(self, *, name):
        if name not in self.providers:
            raise _not_found()
        return dict(self.providers[name])

    def list_oauth2_credential_providers(self, **_):
        items = []
        for n, v in self.providers.items():
            item = {"name": n, "credentialProviderVendor": v["credentialProviderVendor"]}
            if "credentialProviderArn" in v:
                item["credentialProviderArn"] = v["credentialProviderArn"]
            items.append(item)
        return {"credentialProviders": items}

    def delete_oauth2_credential_provider(self, *, name):
        self.calls.append("delete_oauth2_credential_provider")
        record = self.providers.pop(name, None)
        if record:
            self.tags_by_arn.pop(record["credentialProviderArn"], None)
        return {}

    def list_tags_for_resource(self, *, resourceArn):
        return {"tags": dict(self.tags_by_arn.get(resourceArn, {}))}


class FakeData:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.meta = boto3.client(model.DATA_SERVICE, region_name=REGION).meta

    def get_workload_access_token(self, *, workloadName):
        self.calls.append("get_workload_access_token")
        return {"workloadAccessToken": SENTINEL_WORKLOAD_TOKEN}

    def get_resource_oauth2_token(
        self, *, workloadIdentityToken, resourceCredentialProviderName, scopes, oauth2Flow
    ):
        self.calls.append("get_resource_oauth2_token")
        assert oauth2Flow == model.OAUTH2_FLOW_M2M
        if workloadIdentityToken != SENTINEL_WORKLOAD_TOKEN:
            raise _client_error("ValidationException", 400)
        if resourceCredentialProviderName.endswith("_absent"):
            raise _client_error("ResourceNotFoundException", 404)
        if list(scopes) != ["res-server/invoke"]:
            raise _client_error("AccessDeniedException", 403)
        self.seen_scopes = list(scopes)
        return {"accessToken": SENTINEL_RESOURCE_TOKEN}


class FakeSts:
    def __init__(self, account=ACCOUNT) -> None:
        self.account = account

    def get_caller_identity(self):
        return {"Account": self.account}


class FakeCloudFormation:
    def __init__(self, *, overrides=None, status="UPDATE_COMPLETE") -> None:
        self.status = status
        self.outputs = {
            "CognitoUserPoolId": USER_POOL_ID,
            "CognitoClientId": "exampleclientid123",
            "TokenEndpoint": TOKEN_ENDPOINT,
            "GatewayUrl": GATEWAY_URL,
            "OAuthScope": "res-server/invoke",
            "InferenceTargetName": "bedrock-mantle",
        }
        self.outputs.update(overrides or {})

    def describe_stacks(self, *, StackName):
        assert StackName == "Prod-InferenceGateway"
        return {
            "Stacks": [
                {
                    "StackStatus": self.status,
                    "Outputs": [
                        {"OutputKey": key, "OutputValue": value}
                        for key, value in self.outputs.items()
                    ],
                }
            ]
        }


class FakeSession:
    def __init__(self, control, data, sts, cloudformation) -> None:
        self._control = control
        self._data = data
        self._sts = sts
        self._cloudformation = cloudformation

    def client(self, service, **_):
        if service == model.CONTROL_SERVICE:
            return self._control
        if service == model.DATA_SERVICE:
            return self._data
        if service == "sts":
            return self._sts
        if service == "cloudformation":
            return self._cloudformation
        raise AssertionError(f"unexpected client {service}")


class FakeSecretReader:
    def __init__(self) -> None:
        self.reads = 0
        self.domain_checks = 0

    def verify_pool_domain(self, *, user_pool_id, region, token_endpoint):
        self.domain_checks += 1
        assert user_pool_id == USER_POOL_ID
        assert region == REGION
        assert token_endpoint == TOKEN_ENDPOINT

    def read_client_secret(self, *, user_pool_id, client_id, resource_scope):
        self.reads += 1
        assert resource_scope == "res-server/invoke"
        return SENTINEL_SECRET


class FakeInference:
    """Records the bearer token it received; never logs it."""

    def __init__(self, model_id, *, fail_stream=False) -> None:
        self.model_id = model_id
        self.tokens_seen: list[str] = []
        self.fail_stream = fail_stream

    def discover_models(self, bearer_token):
        self.tokens_seen.append(bearer_token)
        return [self.model_id, "other-model"]

    def run_litellm(self, bearer_token, *, stream):
        self.tokens_seen.append(bearer_token)
        if stream and self.fail_stream:
            return 0
        return 1


def _client_error(code: str, status: int):
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": code, "Message": "redacted test denial"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "op",
    )


def _not_found():
    return _client_error("ResourceNotFoundException", 404)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_SCRATCH", str(tmp_path))
    return tmp_path


def _config(scratch: Path) -> spike.SpikeConfig:
    args = spike.parse_args(
        [
            "deploy",
            "--account-id", ACCOUNT,
            "--region", REGION,
            "--prefix", PREFIX,
            "--source-revision", SOURCE_REVISION,
            "--user-pool-id", USER_POOL_ID,
            "--client-id", "exampleclientid123",
            "--issuer", ISSUER,
            "--authorization-endpoint", AUTHORIZATION_ENDPOINT,
            "--token-endpoint", TOKEN_ENDPOINT,
            "--resource-scope", "res-server/invoke",
            "--gateway-url", GATEWAY_URL,
            "--model-id", MODEL_ID,
        ]
    )
    return spike.build_config(args)


def _make_spike(scratch, *, control=None, data=None, sts=None,
                cloudformation=None, secret_reader=None, inference=None):
    config = _config(scratch)
    control = control or FakeControl()
    data = data or FakeData()
    sts = sts or FakeSts()
    cloudformation = cloudformation or FakeCloudFormation()
    session = FakeSession(control, data, sts, cloudformation)
    s = spike.IdentityM2mSpike(
        config,
        session=session,
        secret_reader=secret_reader or FakeSecretReader(),
        inference_probe=inference or FakeInference(config.model_id),
    )
    return s, control, data


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


# --------------------------------------------------------------------------
# Config validation
# --------------------------------------------------------------------------


def test_build_config_rejects_bad_account(scratch):
    args = spike.parse_args(
        ["deploy", "--source-revision", SOURCE_REVISION, "--account-id", "abc", "--user-pool-id", "p", "--client-id", "c",
         "--issuer", "https://i.example.com", "--authorization-endpoint", "https://i.example.com/a",
         "--token-endpoint", "https://i.example.com/t", "--resource-scope", "r/s",
         "--gateway-url", "https://g.example.com", "--model-id", "m"]
    )
    with pytest.raises(spike.SpikeError):
        spike.build_config(args)


def test_build_config_rejects_unsupported_region(scratch):
    args = spike.parse_args(
        ["deploy", "--source-revision", SOURCE_REVISION, "--account-id", ACCOUNT, "--region", "ap-south-1",
         "--user-pool-id", "p", "--client-id", "c",
         "--issuer", "https://i.example.com", "--authorization-endpoint", "https://i.example.com/a",
         "--token-endpoint", "https://i.example.com/t", "--resource-scope", "r/s",
         "--gateway-url", "https://g.example.com", "--model-id", "m"]
    )
    with pytest.raises(spike.SpikeError):
        spike.build_config(args)


def test_build_config_requires_scratch(tmp_path, monkeypatch):
    monkeypatch.delenv("KIROCREW_SCRATCH", raising=False)
    args = spike.parse_args(
        ["deploy", "--source-revision", SOURCE_REVISION,
         "--account-id", ACCOUNT, "--user-pool-id", USER_POOL_ID,
         "--client-id", "exampleclientid123", "--issuer", ISSUER,
         "--authorization-endpoint", AUTHORIZATION_ENDPOINT,
         "--token-endpoint", TOKEN_ENDPOINT, "--resource-scope", "res-server/invoke",
         "--gateway-url", GATEWAY_URL, "--model-id", MODEL_ID]
    )
    with pytest.raises(spike.SpikeError):
        spike.build_config(args)


# --------------------------------------------------------------------------
# SDK capability preflight -- exercises the real pinned service model
# --------------------------------------------------------------------------


def test_preflight_passes_against_pinned_sdk(scratch):
    s, control, data = _make_spike(scratch)
    s.preflight()
    assert s.evidence.has_event("preflight")
    assert "create_workload_identity" not in control.calls  # read-only phase


def test_capability_detects_all_required_members(scratch):
    s, _, _ = _make_spike(scratch)
    for op, required in model.CONTROL_OPERATIONS.items():
        for member in required:
            assert s.api.capability(model.CONTROL_SERVICE, op, member)
    for op, required in model.DATA_OPERATIONS.items():
        for member in required:
            assert s.api.capability(model.DATA_SERVICE, op, member)


def test_pinned_nested_provider_and_status_shapes(scratch):
    s, _, _ = _make_spike(scratch)
    service = s.api.control.meta.service_model
    create_shape = service.operation_model(
        "CreateOauth2CredentialProvider"
    ).input_shape
    provider_union = create_shape.members["oauth2ProviderConfigInput"]
    assert model.PROVIDER_CONFIG_MEMBER in provider_union.members
    included = provider_union.members[model.PROVIDER_CONFIG_MEMBER]
    assert {
        "clientId",
        "clientSecret",
        "issuer",
        "authorizationEndpoint",
        "tokenEndpoint",
    } <= set(included.members)
    workload_create = service.operation_model("CreateWorkloadIdentity").input_shape
    assert "tags" in workload_create.members
    get_output = service.operation_model("GetOauth2CredentialProvider").output_shape
    assert frozenset(get_output.members["status"].enum) == model.PROVIDER_STATUSES


def test_list_page_size_respects_sdk_max_results_cap(scratch):
    """Regression: both list ops cap maxResults at 20.

    A larger page size fails closed live with a ValidationException, so the
    paginator's requested size must not exceed the SDK-modeled maximum for
    either ListWorkloadIdentities or ListOauth2CredentialProviders.
    """
    s, _, _ = _make_spike(scratch)
    service = s.api.control.meta.service_model
    for op in ("ListWorkloadIdentities", "ListOauth2CredentialProviders"):
        max_results = service.operation_model(op).input_shape.members["maxResults"]
        assert max_results.metadata.get("max") == 20, (
            f"{op} maxResults SDK cap changed; update LIST_PAGE_SIZE guard"
        )
        assert spike.LIST_PAGE_SIZE <= max_results.metadata["max"]
    assert spike._page_kwargs(None)["maxResults"] <= 20


# --------------------------------------------------------------------------
# Scope guard
# --------------------------------------------------------------------------


def test_scope_guard_blocks_out_of_scope_mutation(scratch):
    s, _, _ = _make_spike(scratch)
    s.set_scope([])  # read-only
    with pytest.raises(spike.SpikeError):
        s.api.create_workload_identity(s.names.workload_name, s.names.allocation_tags())


def test_scope_guard_blocks_token_calls_outside_verify(scratch):
    s, _, _ = _make_spike(scratch)
    s.set_scope(["create_workload_identity"])
    with pytest.raises(spike.SpikeError):
        s.api.get_workload_access_token(s.names.workload_name)


def test_set_scope_rejects_unknown_entry(scratch):
    s, _, _ = _make_spike(scratch)
    with pytest.raises(spike.SpikeError):
        s.set_scope(["not_a_real_call"])


# --------------------------------------------------------------------------
# Identity gate
# --------------------------------------------------------------------------


def test_wrong_account_is_refused(scratch):
    s, _, _ = _make_spike(scratch, sts=FakeSts(account="000000000000"))
    with pytest.raises(spike.SpikeError):
        s.verify_identity()


def test_stack_output_mismatch_blocks_deploy_before_create(scratch):
    cloudformation = FakeCloudFormation(overrides={"GatewayUrl": "https://drift.invalid"})
    s, control, _ = _make_spike(scratch, cloudformation=cloudformation)
    with pytest.raises(spike.SpikeError, match="do not match"):
        s.deploy()
    assert control.workloads == {}
    assert control.providers == {}


# --------------------------------------------------------------------------
# Deploy + verify happy path
# --------------------------------------------------------------------------


def test_deploy_creates_both_resources_and_passes_secret(scratch):
    s, control, _ = _make_spike(scratch)
    s.deploy()
    assert set(control.workloads) == {s.names.workload_name}
    assert set(control.providers) == {s.names.provider_name}
    expected_tags = s.names.allocation_tags()
    assert len(control.tags_by_arn) == 2
    assert all(tags == expected_tags for tags in control.tags_by_arn.values())
    # The sentinel secret reached CreateOauth2CredentialProvider ...
    assert control.seen_secret == SENTINEL_SECRET
    # ... but never the state or evidence file.
    assert SENTINEL_SECRET not in _read(s.config.state_path)
    assert SENTINEL_SECRET not in _read(s.config.evidence_path)


def test_verify_mints_tokens_and_runs_inference(scratch):
    inference = FakeInference("bedrock-mantle/openai.gpt-oss-120b")
    s, _, data = _make_spike(scratch, inference=inference)
    s.deploy()
    s.verify()
    assert data.calls.count("get_workload_access_token") == 1
    assert data.calls.count("get_resource_oauth2_token") == 4
    # Inference received exactly the resource token, for discovery + 2 llm calls.
    assert inference.tokens_seen == [SENTINEL_RESOURCE_TOKEN] * 3
    assert data.seen_scopes == ["res-server/invoke"]


def test_tokens_and_secret_never_touch_disk_or_stdout(scratch, capsys):
    s, _, _ = _make_spike(scratch)
    s.deploy()
    s.verify()
    state = _read(s.config.state_path)
    evidence = _read(s.config.evidence_path)
    captured = capsys.readouterr()
    for sensitive in (SENTINEL_SECRET, SENTINEL_WORKLOAD_TOKEN, SENTINEL_RESOURCE_TOKEN):
        assert sensitive not in state
        assert sensitive not in evidence
        assert sensitive not in captured.out
        assert sensitive not in captured.err
    # Evidence is still valid JSON recording the safe booleans/lengths.
    doc = json.loads(evidence)
    events = {e["event"] for e in doc["events"]}
    assert {"workload-token-obtained", "resource-token-obtained",
            "resource-token-denied", "model-discovery", "litellm-inference"} <= events
    denial_vectors = {
        e["vector"] for e in doc["events"]
        if e["event"] == "resource-token-denied"
    }
    assert denial_vectors == {"wrong-scope", "wrong-provider", "wrong-workload-token"}


def test_verify_fails_when_model_absent(scratch):
    s, _, _ = _make_spike(scratch, inference=FakeInference("some-other-model"))
    s.deploy()
    with pytest.raises(spike.SpikeError):
        s.verify()


def test_verify_fails_when_streaming_returns_no_content(scratch):
    inference = FakeInference("bedrock-mantle/openai.gpt-oss-120b", fail_stream=True)
    s, _, _ = _make_spike(scratch, inference=inference)
    s.deploy()
    with pytest.raises(spike.SpikeError):
        s.verify()


# --------------------------------------------------------------------------
# Provider status handling
# --------------------------------------------------------------------------


def test_deploy_refuses_terminal_provider_status(scratch):
    control = FakeControl()
    control.provider_status = "CREATE_FAILED"
    s, _, _ = _make_spike(scratch, control=control)
    with pytest.raises(model.StatusError):
        s.deploy()


# --------------------------------------------------------------------------
# Cleanup ordering + continue-after-failure
# --------------------------------------------------------------------------


def test_cleanup_deletes_provider_before_workload(scratch):
    s, control, _ = _make_spike(scratch)
    s.deploy()
    s.cleanup()
    deletes = [c for c in control.calls if c.startswith("delete_")]
    assert deletes == ["delete_oauth2_credential_provider", "delete_workload_identity"]
    assert control.providers == {}
    assert control.workloads == {}
    assert s.state.get("completed") is True


def test_cleanup_continues_after_provider_failure(scratch):
    s, control, _ = _make_spike(scratch)
    s.deploy()

    original = control.delete_oauth2_credential_provider

    def boom(*, name):
        raise RuntimeError("provider delete blew up")

    control.delete_oauth2_credential_provider = boom
    with pytest.raises(spike.SpikeError):
        s.cleanup()
    # Workload delete still attempted despite provider failure.
    assert "delete_workload_identity" in control.calls
    assert s.evidence.has_event("cleanup-step-failed")
    control.delete_oauth2_credential_provider = original


def test_cleanup_is_idempotent_when_nothing_exists(scratch):
    s, control, _ = _make_spike(scratch)
    # No deploy: nothing owned. Cleanup should pass with empty residue.
    s.cleanup()
    assert s.state.get("completed") is True


# --------------------------------------------------------------------------
# Partial-create recovery + collision refusal
# --------------------------------------------------------------------------


def test_deploy_recovers_existing_owned_workload(scratch):
    s, control, _ = _make_spike(scratch)
    # Pre-seed an owned workload as if a prior deploy half-completed.
    name = s.names.workload_name
    control.create_workload_identity(name=name, tags=s.names.allocation_tags())
    s.set_scope(["create_workload_identity", "create_oauth2_credential_provider"])
    recovered = s.discover_owned_workload()
    assert recovered == name


def test_discover_refuses_foreign_collision(scratch):
    s, control, _ = _make_spike(scratch)
    # A provider with our exact name but a foreign vendor.
    control.providers[s.names.provider_name] = {
        "name": s.names.provider_name,
        "credentialProviderVendor": "GoogleOauth2",
    }
    with pytest.raises(spike.SpikeError):
        s.discover_owned_provider()


# --------------------------------------------------------------------------
# Provenance / completed-run refusal / tampered state
# --------------------------------------------------------------------------


def test_completed_run_refuses_redeploy(scratch):
    s, _, _ = _make_spike(scratch)
    s.deploy()
    s.cleanup()
    # Reload against the now-completed state file.
    s2, _, _ = _make_spike(scratch)
    with pytest.raises(spike.SpikeError):
        s2.deploy()


def test_tampered_state_account_is_refused(scratch):
    s, _, _ = _make_spike(scratch)
    s.deploy()
    # Tamper: rewrite the state file's account.
    doc = json.loads(_read(s.config.state_path))
    doc["accountId"] = "000000000000"
    s.config.state_path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(model.ProvenanceError):
        _make_spike(scratch)


def test_evidence_from_a_different_run_is_refused(scratch):
    s, _, _ = _make_spike(scratch)
    s.deploy()
    # Corrupt the evidence run fingerprint.
    doc = json.loads(_read(s.config.evidence_path))
    doc["run"]["runFingerprint"] = "f" * 32
    s.config.evidence_path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(spike.SpikeError):
        _make_spike(scratch)


# --------------------------------------------------------------------------
# Negative token wrappers
# --------------------------------------------------------------------------


def test_get_workload_access_token_requires_scope(scratch):
    s, _, _ = _make_spike(scratch)
    s.set_scope([])
    with pytest.raises(spike.SpikeError):
        s.api.get_workload_access_token(s.names.workload_name)


def test_get_resource_token_requires_scope(scratch):
    s, _, _ = _make_spike(scratch)
    s.set_scope(["get_workload_access_token"])
    with pytest.raises(spike.SpikeError):
        s.api.get_resource_oauth2_token(SENTINEL_WORKLOAD_TOKEN, s.names.provider_name, ["r/s"])


def test_empty_workload_token_is_rejected(scratch):
    class EmptyData(FakeData):
        def get_workload_access_token(self, *, workloadName):
            self.calls.append("get_workload_access_token")
            return {"workloadAccessToken": ""}

    s, _, _ = _make_spike(scratch, data=EmptyData())
    s.deploy()
    with pytest.raises(spike.SpikeError):
        s.verify()


def test_unexpected_adversarial_token_is_release_blocking(scratch):
    class PermissiveData(FakeData):
        def get_resource_oauth2_token(self, **kwargs):
            self.calls.append("get_resource_oauth2_token")
            return {"accessToken": SENTINEL_RESOURCE_TOKEN}

    s, _, _ = _make_spike(scratch, data=PermissiveData())
    s.deploy()
    with pytest.raises(spike.SpikeError, match="unexpectedly returned"):
        s.verify()
    assert SENTINEL_RESOURCE_TOKEN not in _read(s.config.evidence_path)


# --------------------------------------------------------------------------
# Scope registries stay in sync with the model
# --------------------------------------------------------------------------


def test_side_effecting_calls_are_exactly_the_token_calls():
    assert spike.SIDE_EFFECTING_API_CALLS == {
        "get_workload_access_token",
        "get_resource_oauth2_token",
    }


def test_mutating_calls_cover_all_lifecycle_ops():
    assert spike.MUTATING_API_CALLS == {
        "create_workload_identity",
        "delete_workload_identity",
        "create_oauth2_credential_provider",
        "delete_oauth2_credential_provider",
    }

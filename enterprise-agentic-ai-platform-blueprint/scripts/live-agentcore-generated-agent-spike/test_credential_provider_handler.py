"""Offline behavioral tests for the RuntimeMemory credential-provider handler.

The handler is an inline Python constant (``CREDENTIAL_PROVIDER_HANDLER``) in
``apps/workload-account/lib/d03-workstream-runtime-memory-stack.ts``. String
assertions in phase-23 pin its shape; these tests EXECUTE it against a fake
AgentCore control client that models the behaviors observed live on
2026-09-23 across five consecutive nonprod rollbacks:

* ``CreateWorkloadIdentity`` on an existing name -> ``ValidationException``
  whose message contains ``already exists`` (not ``ConflictException``);
* ``TagResource`` on an EXISTING WorkloadIdentity -> deterministic
  ``InternalServerErrorException``;
* after ``DeleteWorkloadIdentity`` the name is TOMBSTONED: ``Get`` reports
  not-found while ``Create`` keeps reporting ``already exists``;
* create-time ``tags`` ARE persisted.

Contract under test: every Create mints ``<prefix>_<12 hex from RequestId>``,
never tags in place, never adopts a retained fixed name, surfaces the minted
name as the ``WorkloadName`` attribute, records it in the PhysicalResourceId,
and Update/Delete only ever touch the identity this resource owns.

No AWS call is made: ``boto3.client`` is monkeypatched per test.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import importlib.util
import json
import re
import time
from pathlib import Path
from types import ModuleType

import boto3
import pytest
from botocore.exceptions import ClientError

STACK_TS = (
    Path(__file__).resolve().parents[2]
    / "apps"
    / "workload-account"
    / "lib"
    / "d03-workstream-runtime-memory-stack.ts"
)
TAGS = {
    "application-id": "demo",
    "agent-id": "primary",
    "tenant-id": "demo",
    "cost-centre": "engineering",
    "environment": "nonprod",
}
PREFIX = "AgenticAI_D03_nonprod_demo_primary"
DIRECTORY = "arn:aws:bedrock-agentcore:us-west-2:444444444444:workload-identity-directory/default"
PR_ARN = "arn:aws:bedrock-agentcore:us-west-2:444444444444:token-vault/default/oauth2credentialprovider/P"
REQUEST_ID = "6a340de1-2e6f-46bf-8f40-c560f402943f"
EXPECTED_NAME = f"{PREFIX}_6a340de12e6f"


def _extract_handler() -> ModuleType:
    source = STACK_TS.read_text()
    match = re.search(r"CREDENTIAL_PROVIDER_HANDLER = `\n?(.*?)\n`;", source, re.S)
    assert match, "CREDENTIAL_PROVIDER_HANDLER constant not found"
    code = match.group(1).replace("\\`", "`").replace("\\\\", "\\")
    spec = importlib.util.spec_from_loader("idprov_handler", loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(code, "CREDENTIAL_PROVIDER_HANDLER", "exec"), module.__dict__)
    return module


def _err(code: str, message: str = "x", status: int = 400) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "op",
    )


class FakeControl:
    """Models the live AgentCore Identity control plane, including tombstones."""

    def __init__(self) -> None:
        self.identities: dict[str, dict] = {}   # name -> {"tags": {...}}
        self.tombstones: set[str] = set()
        self.provider: dict | None = None
        self.calls: list[str] = []

    # -- workload identities -------------------------------------------
    def create_workload_identity(self, name, tags):
        self.calls.append(f"create_wl:{name}")
        if name in self.identities or name in self.tombstones:
            raise _err("ValidationException", f"Workload identity with name '{name}' already exists")
        self.identities[name] = {"tags": dict(tags)}
        return {"name": name, "workloadIdentityArn": f"{DIRECTORY}/workload-identity/{name}"}

    def get_workload_identity(self, name):
        if name not in self.identities:
            raise _err("ResourceNotFoundException", f"WorkloadIdentity with id {name} not found.", 404)
        return {"name": name, "workloadIdentityArn": f"{DIRECTORY}/workload-identity/{name}"}

    def delete_workload_identity(self, name):
        self.calls.append(f"delete_wl:{name}")
        self.identities.pop(name, None)
        self.tombstones.add(name)
        return {}

    def list_tags_for_resource(self, resourceArn):
        if "/workload-identity/" in resourceArn:
            name = resourceArn.rsplit("/", 1)[-1]
            return {"tags": dict(self.identities[name]["tags"])}
        return {"tags": dict(self.provider["tags"])}

    def tag_resource(self, resourceArn, tags):
        self.calls.append("tag_in_place")
        raise _err("InternalServerErrorException", "Internal server error", 500)

    # -- credential provider -------------------------------------------
    def create_oauth2_credential_provider(self, name, credentialProviderVendor, oauth2ProviderConfigInput, tags):
        self.calls.append("create_provider")
        assert credentialProviderVendor == "CognitoOauth2"
        if self.provider is not None:
            raise _err("ConflictException", "exists", 409)
        self.provider = {"tags": dict(tags), "status": "READY"}
        return {"name": name, "credentialProviderArn": PR_ARN}

    def update_oauth2_credential_provider(self, name, credentialProviderVendor, oauth2ProviderConfigInput):
        self.calls.append("update_provider")
        return {"name": name, "credentialProviderArn": PR_ARN}

    def delete_oauth2_credential_provider(self, name):
        self.calls.append("delete_provider")
        self.provider = None
        return {}

    def get_oauth2_credential_provider(self, name):
        if self.provider is None:
            raise _err("ResourceNotFoundException", "nf", 404)
        return {"name": name, "credentialProviderArn": PR_ARN, "status": self.provider["status"]}


class FakeSecrets:
    def get_secret_value(self, SecretId):
        return {
            "SecretString": json.dumps(
                {
                    "scope": "s",
                    "clientId": "c",
                    "clientSecret": "not-a-real-secret",
                    "issuer": "i",
                    "authorizationEndpoint": "a",
                    "tokenEndpoint": "t",
                }
            )
        }


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    return _extract_handler()


def _event(request_type: str, physical_id: str | None = None, request_id: str = REQUEST_ID) -> dict:
    event = {
        "RequestType": request_type,
        "RequestId": request_id,
        "ResourceProperties": {
            "Region": "us-west-2",
            "ProviderName": "P",
            "ProviderArn": PR_ARN,
            "WorkloadNamePrefix": PREFIX,
            "WorkloadDirectoryArn": DIRECTORY,
            "Tags": TAGS,
            "SecretArn": "arn:aws:secretsmanager:us-west-2:111111111111:secret:x",
            "Scope": "s",
        },
    }
    if physical_id is not None:
        event["PhysicalResourceId"] = physical_id
    return event


def _run(monkeypatch, handler, fake: FakeControl, event: dict):
    monkeypatch.setattr(
        boto3, "client", lambda svc, region_name=None: fake if svc == "bedrock-agentcore-control" else FakeSecrets()
    )
    return handler.on_event(event, None)


def test_create_mints_unique_name_from_request_id_and_returns_it(monkeypatch, handler):
    fake = FakeControl()
    result = _run(monkeypatch, handler, fake, _event("Create"))
    assert result["Data"]["WorkloadName"] == EXPECTED_NAME
    assert result["PhysicalResourceId"] == f"P|{EXPECTED_NAME}"
    assert fake.identities[EXPECTED_NAME]["tags"] == TAGS
    assert fake.provider["tags"] == TAGS
    assert "tag_in_place" not in fake.calls


def test_create_never_touches_a_retained_fixed_name(monkeypatch, handler):
    # The old fixed deterministic name is retained (untagged) from a failed run.
    fake = FakeControl()
    fake.identities[PREFIX] = {"tags": {}}
    result = _run(monkeypatch, handler, fake, _event("Create"))
    assert result["Data"]["WorkloadName"] == EXPECTED_NAME
    assert PREFIX in fake.identities                       # left alone
    assert not any(c.startswith("delete_wl") for c in fake.calls)
    assert "tag_in_place" not in fake.calls


def test_create_fails_closed_on_tombstoned_minted_name(monkeypatch, handler):
    # A per-request name can only collide through a real defect or tombstone.
    fake = FakeControl()
    fake.tombstones.add(EXPECTED_NAME)
    with pytest.raises(ClientError, match="already exists"):
        _run(monkeypatch, handler, fake, _event("Create"))
    assert fake.provider is None


def test_create_fails_closed_on_ownership_mismatch(monkeypatch, handler):
    fake = FakeControl()
    real_get = fake.get_workload_identity
    fake.get_workload_identity = lambda name: {**real_get(name), "workloadIdentityArn": f"{DIRECTORY}/workload-identity/other"}
    with pytest.raises(RuntimeError, match="exact expected identity"):
        _run(monkeypatch, handler, fake, _event("Create"))
    assert fake.provider is None


def test_update_keeps_owned_intact_identity(monkeypatch, handler):
    fake = FakeControl()
    created = _run(monkeypatch, handler, fake, _event("Create"))
    fake.calls.clear()
    updated = _run(
        monkeypatch, handler, fake,
        _event("Update", physical_id=created["PhysicalResourceId"], request_id="11111111-2222-3333-4444-555555555555"),
    )
    assert updated["Data"]["WorkloadName"] == EXPECTED_NAME
    assert updated["PhysicalResourceId"] == created["PhysicalResourceId"]
    assert not any(c.startswith("create_wl") for c in fake.calls)
    assert "update_provider" in fake.calls


def test_update_mints_replacement_when_owned_identity_is_gone(monkeypatch, handler):
    fake = FakeControl()
    created = _run(monkeypatch, handler, fake, _event("Create"))
    fake.identities.pop(EXPECTED_NAME)  # vanished out-of-band
    updated = _run(
        monkeypatch, handler, fake,
        _event("Update", physical_id=created["PhysicalResourceId"], request_id="abcdefabcdef-0000-0000-0000-000000000000"),
    )
    assert updated["Data"]["WorkloadName"] == f"{PREFIX}_abcdefabcdef"
    assert updated["PhysicalResourceId"] == f"P|{PREFIX}_abcdefabcdef"


def test_update_refuses_foreign_tags_on_owned_name(monkeypatch, handler):
    fake = FakeControl()
    created = _run(monkeypatch, handler, fake, _event("Create"))
    fake.identities[EXPECTED_NAME]["tags"] = {"owner": "someone-else"}
    with pytest.raises(RuntimeError, match="missing or foreign ownership tags"):
        _run(monkeypatch, handler, fake, _event("Update", physical_id=created["PhysicalResourceId"]))
    assert not any(c.startswith("delete_wl") for c in fake.calls)


def test_delete_removes_only_the_owned_identity_and_provider(monkeypatch, handler):
    fake = FakeControl()
    created = _run(monkeypatch, handler, fake, _event("Create"))
    fake.identities["AgenticAI_D03_nonprod_demo_primary_runtime-4FWnYiEydR"] = {"tags": {}}  # Runtime-managed sibling
    fake.calls.clear()
    result = _run(monkeypatch, handler, fake, _event("Delete", physical_id=created["PhysicalResourceId"]))
    assert result["PhysicalResourceId"] == created["PhysicalResourceId"]
    assert fake.calls == ["delete_provider", f"delete_wl:{EXPECTED_NAME}"]
    assert "AgenticAI_D03_nonprod_demo_primary_runtime-4FWnYiEydR" in fake.identities


def test_delete_with_legacy_physical_id_is_idempotent_and_touches_no_identity(monkeypatch, handler):
    fake = FakeControl()
    fake.identities[PREFIX] = {"tags": {}}
    result = _run(monkeypatch, handler, fake, _event("Delete", physical_id="P"))
    assert result["PhysicalResourceId"] == "P"
    assert not any(c.startswith("delete_wl") for c in fake.calls)
    assert PREFIX in fake.identities


def test_delete_refuses_foreign_owned_name_outside_prefix(monkeypatch, handler):
    fake = FakeControl()
    fake.identities["Other_prefix_abcdefabcdef"] = {"tags": TAGS}
    _run(monkeypatch, handler, fake, _event("Delete", physical_id="P|Other_prefix_abcdefabcdef"))
    assert not any(c.startswith("delete_wl") for c in fake.calls)


def test_unique_name_requires_twelve_hex_chars(handler):
    with pytest.raises(RuntimeError, match="12-hex"):
        handler._unique_workload_name(PREFIX, "not-hex-at-all")
    assert handler._unique_workload_name(PREFIX, REQUEST_ID) == EXPECTED_NAME

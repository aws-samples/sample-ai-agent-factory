"""Offline behavioral tests for the RuntimeMemory credential-provider handler.

The handler is an inline Python constant (``CREDENTIAL_PROVIDER_HANDLER``) in
``apps/workload-account/lib/d03-workstream-runtime-memory-stack.ts``. String
assertions in phase-23 pin its shape; these tests EXECUTE it against a fake
AgentCore control client that models the exact behaviors observed live on
2026-09-23:

* ``CreateWorkloadIdentity`` on an existing name -> ``ValidationException``
  whose message contains ``already exists`` (not ``ConflictException``);
* ``TagResource`` on an EXISTING WorkloadIdentity -> deterministic
  ``InternalServerErrorException`` (in-place tag migration is not available);
* ``DeleteWorkloadIdentity`` is asynchronous -- the identity stays readable for
  a few polls after the call returns;
* create-time ``tags`` ARE persisted.

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
WL_ARN = "arn:aws:bedrock-agentcore:us-west-2:444444444444:workload-identity-directory/default/workload-identity/W"
PR_ARN = "arn:aws:bedrock-agentcore:us-west-2:444444444444:token-vault/default/oauth2credentialprovider/P"


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
    def __init__(self, existing_tags: dict | None = None, existing: bool = True) -> None:
        self.wl = {"tags": dict(existing_tags or {})} if existing else None
        self.deleting = 0
        self.provider: dict | None = None
        self.calls: list[str] = []

    def create_workload_identity(self, name, tags):
        self.calls.append("create_wl")
        if self.wl is not None:
            raise _err("ValidationException", f"WorkloadIdentity {name} already exists")
        self.wl = {"tags": dict(tags)}
        self.deleting = 0
        return {"name": name, "workloadIdentityArn": WL_ARN}

    def get_workload_identity(self, name):
        if self.wl is None:
            if 0 < self.deleting < 3:  # asynchronous delete still visible
                self.deleting += 1
                return {"name": name, "workloadIdentityArn": WL_ARN}
            raise _err("ResourceNotFoundException", "nf", 404)
        return {"name": name, "workloadIdentityArn": WL_ARN}

    def delete_workload_identity(self, name):
        self.calls.append("delete_wl")
        self.wl = None
        self.deleting = 1
        return {}

    def list_tags_for_resource(self, resourceArn):
        target = self.wl if resourceArn == WL_ARN else self.provider
        return {"tags": dict(target["tags"])}

    def tag_resource(self, resourceArn, tags):
        self.calls.append("tag_in_place")
        raise _err("InternalServerErrorException", "Internal server error", 500)

    def create_oauth2_credential_provider(self, name, credentialProviderVendor, oauth2ProviderConfigInput, tags):
        self.calls.append("create_provider")
        assert credentialProviderVendor == "CognitoOauth2"
        self.provider = {"tags": dict(tags), "status": "READY"}
        return {"name": name, "credentialProviderArn": PR_ARN}

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


def _run_create(monkeypatch, handler, fake: FakeControl):
    monkeypatch.setattr(
        boto3, "client", lambda svc, region_name=None: fake if svc == "bedrock-agentcore-control" else FakeSecrets()
    )
    return handler.on_event(
        {
            "RequestType": "Create",
            "ResourceProperties": {
                "Region": "us-west-2",
                "ProviderName": "P",
                "ProviderArn": PR_ARN,
                "WorkloadName": "W",
                "WorkloadArn": WL_ARN,
                "Tags": TAGS,
                "SecretArn": "arn:aws:secretsmanager:us-west-2:111111111111:secret:x",
                "Scope": "s",
            },
        },
        None,
    )


def test_zero_tag_retained_identity_is_recreated_with_tags_never_tagged_in_place(monkeypatch, handler):
    fake = FakeControl(existing_tags={})
    _run_create(monkeypatch, handler, fake)
    assert fake.calls == ["create_wl", "delete_wl", "create_wl", "create_provider"]
    assert fake.wl["tags"] == TAGS
    assert fake.provider["tags"] == TAGS


def test_foreign_tagged_identity_is_refused_and_never_deleted(monkeypatch, handler):
    fake = FakeControl(existing_tags={"owner": "someone-else"})
    with pytest.raises(RuntimeError, match="foreign or partial"):
        _run_create(monkeypatch, handler, fake)
    assert "delete_wl" not in fake.calls
    assert fake.provider is None


def test_partially_tagged_identity_is_refused_and_never_deleted(monkeypatch, handler):
    partial = dict(TAGS)
    partial.pop("cost-centre")
    fake = FakeControl(existing_tags=partial)
    with pytest.raises(RuntimeError, match="foreign or partial"):
        _run_create(monkeypatch, handler, fake)
    assert "delete_wl" not in fake.calls


def test_fresh_create_is_a_single_tagged_create(monkeypatch, handler):
    fake = FakeControl(existing=False)
    _run_create(monkeypatch, handler, fake)
    assert fake.calls == ["create_wl", "create_provider"]
    assert fake.wl["tags"] == TAGS


def test_fully_tagged_identity_is_adopted_without_mutation(monkeypatch, handler):
    fake = FakeControl(existing_tags=TAGS)
    _run_create(monkeypatch, handler, fake)
    assert "delete_wl" not in fake.calls
    assert "tag_in_place" not in fake.calls


def test_unexpected_arn_is_refused(monkeypatch, handler):
    fake = FakeControl(existing_tags={})
    fake.get_workload_identity = lambda name: {"name": name, "workloadIdentityArn": WL_ARN + "-other"}
    with pytest.raises(RuntimeError, match="exact expected identity"):
        _run_create(monkeypatch, handler, fake)
    assert "delete_wl" not in fake.calls

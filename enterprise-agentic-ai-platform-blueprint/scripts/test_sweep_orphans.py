"""Offline contract tests for scripts/sweep_orphans.py.

The sweep deletes residue that no stack owns, so its safety rules carry the
whole weight: Object-Locked data and the keys that encrypt it must never be
touched, the sweep must refuse while stacks are live, and apply must never act
on anything the reviewed plan did not name.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

SPEC = importlib.util.spec_from_file_location("sweep_orphans", Path(__file__).with_name("sweep_orphans.py"))
so = importlib.util.module_from_spec(SPEC)
sys.modules["sweep_orphans"] = so
SPEC.loader.exec_module(so)

NOW = dt.datetime(2026, 9, 25, tzinfo=dt.timezone.utc)
LOCKED_KEY = "11111111-1111-1111-1111-111111111111"
FREE_KEY = "22222222-2222-2222-2222-222222222222"
UNRELATED_KEY = "33333333-3333-3333-3333-333333333333"


def err(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Op")


class World:
    """In-memory account: what exists, and every mutating call made."""

    def __init__(self) -> None:
        self.stacks = [{"StackName": "IagRegistryStack", "StackStatus": "CREATE_COMPLETE"}]
        self.log_groups = [
            {"logGroupName": "/aws/lambda/AgenticAI-D03-WorkstreamG-LogRetention-abc"},
            {"logGroupName": "/aws/lambda/IagRegistryStack-Handler"},
        ]
        self.identities = [{"name": "agenticaid03demoprimary-48yE4D9CYC"}, {"name": "iag-identity"}]
        self.pools = [{"Id": "us-east-1_A", "Name": "agenticai-killswitch-stub-live-demo-primary"}, {"Id": "us-east-1_B", "Name": "iag-pool"}]
        self.pool_protection = {"us-east-1_A": "INACTIVE"}
        self.buckets = {
            "agenticai-aiact-nonprod-x": {"lock": True, "key": LOCKED_KEY, "versions": [("r.json", "v1", NOW + dt.timedelta(days=2555))]},
            "agenticai-eval-corpus-nonprod-x": {"lock": True, "key": FREE_KEY, "versions": []},
            "someone-elses-bucket": {"lock": False, "key": UNRELATED_KEY, "versions": []},
        }
        self.keys = {
            LOCKED_KEY: "EU AI Act record-keeping CMK (nonprod/demo/primary).",
            FREE_KEY: "CMK for AgentCore evaluation-gates corpus + run history (nonprod).",
            UNRELATED_KEY: "IAG AgentCore registry key",
        }
        self.mutations: list[tuple[str, str]] = []


class Fake:
    def __init__(self, world: World, service: str) -> None:
        self.w, self.s = world, service

    def can_paginate(self, op: str) -> bool:
        return False

    # cloudformation
    def list_stacks(self, **_):
        return {"StackSummaries": self.w.stacks}

    # logs
    def describe_log_groups(self, **_):
        return {"logGroups": self.w.log_groups}

    def delete_log_group(self, logGroupName):  # noqa: N803
        self.w.mutations.append(("logs", logGroupName))

    # agentcore
    def list_workload_identities(self, **_):
        return {"workloadIdentities": self.w.identities}

    def delete_workload_identity(self, name):
        self.w.mutations.append(("identity", name))

    # cognito
    def list_user_pools(self, **_):
        return {"UserPools": self.w.pools}

    def describe_user_pool(self, UserPoolId):  # noqa: N803
        return {"UserPool": {"DeletionProtection": self.w.pool_protection.get(UserPoolId, "INACTIVE")}}

    def update_user_pool(self, **kw):
        self.w.mutations.append(("pool-update", kw["UserPoolId"]))

    def delete_user_pool(self, UserPoolId):  # noqa: N803
        self.w.mutations.append(("pool", UserPoolId))

    # s3
    def list_buckets(self):
        return {"Buckets": [{"Name": n} for n in self.w.buckets]}

    def get_bucket_location(self, Bucket):  # noqa: N803
        return {"LocationConstraint": None}

    def get_bucket_encryption(self, Bucket):  # noqa: N803
        return {"ServerSideEncryptionConfiguration": {"Rules": [{"ApplyServerSideEncryptionByDefault": {"KMSMasterKeyID": self.w.buckets[Bucket]["key"]}}]}}

    def get_object_lock_configuration(self, Bucket):  # noqa: N803
        if not self.w.buckets[Bucket]["lock"]:
            raise err("ObjectLockConfigurationNotFoundError")
        return {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}}

    def list_object_versions(self, Bucket, **_):  # noqa: N803
        return {"Versions": [{"Key": k, "VersionId": v} for k, v, _ in self.w.buckets[Bucket]["versions"]], "DeleteMarkers": []}

    def head_object(self, Bucket, Key, VersionId):  # noqa: N803
        return {"SSEKMSKeyId": self.w.buckets[Bucket]["key"]}

    def get_object_retention(self, Bucket, Key, VersionId):  # noqa: N803
        until = next(u for k, v, u in self.w.buckets[Bucket]["versions"] if k == Key and v == VersionId)
        return {"Retention": {"Mode": "COMPLIANCE", "RetainUntilDate": until}}

    def get_object_legal_hold(self, **_):
        raise err("NoSuchObjectLockConfiguration")

    def delete_objects(self, Bucket, Delete):  # noqa: N803
        self.w.mutations.append(("objects", Bucket))
        self.w.buckets[Bucket]["versions"] = []
        return {}

    def delete_bucket(self, Bucket):  # noqa: N803
        self.w.mutations.append(("bucket", Bucket))

    # kms
    def list_aliases(self, **_):
        return {"Aliases": []}

    def list_keys(self, **_):
        return {"Keys": [{"KeyId": k} for k in self.w.keys]}

    def describe_key(self, KeyId):  # noqa: N803
        return {"KeyMetadata": {"KeyId": KeyId, "KeyManager": "CUSTOMER", "KeyState": "Enabled", "Description": self.w.keys.get(KeyId, "")}}

    def schedule_key_deletion(self, KeyId, PendingWindowInDays):  # noqa: N803
        assert PendingWindowInDays == 7
        self.w.mutations.append(("key", KeyId))


class FakeSession:
    def __init__(self, world: World) -> None:
        self.world = world

    def client(self, name, **_):
        return Fake(self.world, name)


def sweep(world: World) -> so.Sweep:
    return so.Sweep(FakeSession(world), "us-east-1", now=NOW)


def test_plan_protects_locked_bucket_and_its_key():
    plan = sweep(World()).plan()
    assert "agenticai-aiact-nonprod-x" not in plan["delete"]["buckets"]
    assert LOCKED_KEY not in plan["delete"]["kmsKeys"]
    protected = {entry.get("bucket") or entry.get("kmsKey") for entry in plan["protected"]}
    assert {"agenticai-aiact-nonprod-x", LOCKED_KEY} <= protected


def test_plan_frees_the_empty_locked_bucket_and_its_key():
    # Object Lock enabled but holding no retained version: nothing is locked.
    plan = sweep(World()).plan()
    assert "agenticai-eval-corpus-nonprod-x" in plan["delete"]["buckets"]
    assert FREE_KEY in plan["delete"]["kmsKeys"]


def test_plan_never_includes_other_teams_resources():
    plan = sweep(World()).plan()
    flat = str(plan["delete"])
    for foreign in ("IagRegistryStack", "iag-identity", "iag-pool", "someone-elses-bucket", UNRELATED_KEY):
        assert foreign not in flat


def test_expired_retention_is_not_protected():
    world = World()
    world.buckets["agenticai-aiact-nonprod-x"]["versions"] = [("r.json", "v1", NOW - dt.timedelta(days=1))]
    plan = sweep(world).plan()
    assert "agenticai-aiact-nonprod-x" in plan["delete"]["buckets"]
    assert LOCKED_KEY in plan["delete"]["kmsKeys"]


def test_refuses_while_project_stacks_are_live():
    world = World()
    world.stacks.append({"StackName": "AgenticAI-demo-primary-prod-RuntimeMemory", "StackStatus": "UPDATE_COMPLETE"})
    with pytest.raises(SystemExit, match="still live"):
        sweep(world).plan()


def test_apply_touches_only_plan_entries_and_keys_last():
    world = World()
    s = sweep(world)
    plan = s.plan()
    s.apply(plan)
    kinds = [kind for kind, _ in world.mutations]
    assert kinds[-1] == "key", "keys must be scheduled after everything else"
    assert ("key", LOCKED_KEY) not in world.mutations
    assert ("bucket", "agenticai-aiact-nonprod-x") not in world.mutations
    assert ("objects", "agenticai-aiact-nonprod-x") not in world.mutations
    assert ("logs", "/aws/lambda/IagRegistryStack-Handler") not in world.mutations
    assert ("pool-update", "us-east-1_A") not in world.mutations, "protection was INACTIVE; UpdateUserPool must not be called"


def test_apply_never_widens_a_narrow_plan():
    world = World()
    s = sweep(world)
    plan = s.plan()
    plan["delete"] = {"logGroups": [], "workloadIdentities": [], "userPools": [], "buckets": [], "kmsKeys": []}
    s.apply(plan)
    assert world.mutations == []


def test_apply_skips_an_entry_that_became_protected():
    world = World()
    s = sweep(world)
    plan = s.plan()
    assert "agenticai-eval-corpus-nonprod-x" in plan["delete"]["buckets"]
    # Between plan and apply, a retained object lands in the bucket.
    world.buckets["agenticai-eval-corpus-nonprod-x"]["versions"] = [("new.json", "v9", NOW + dt.timedelta(days=90))]
    s.apply(plan)
    assert ("bucket", "agenticai-eval-corpus-nonprod-x") not in world.mutations
    assert ("key", FREE_KEY) not in world.mutations


def test_apply_refuses_a_plan_for_another_region():
    s = sweep(World())
    plan = s.plan()
    plan["region"] = "eu-west-1"
    with pytest.raises(SystemExit, match="plan is for eu-west-1"):
        s.apply(plan)


def test_key_reference_forms_resolve_to_key_ids():
    class Kms:
        def describe_key(self, KeyId):  # noqa: N803
            assert KeyId == "alias/agenticai/x"
            return {"KeyMetadata": {"KeyId": FREE_KEY}}

    assert so.key_id_of(Kms(), FREE_KEY) == FREE_KEY
    assert so.key_id_of(Kms(), f"arn:aws:kms:us-east-1:123456789012:key/{FREE_KEY}") == FREE_KEY
    assert so.key_id_of(Kms(), "alias/agenticai/x") == FREE_KEY
    assert so.key_id_of(Kms(), "arn:aws:kms:us-east-1:123456789012:alias/agenticai/x") == FREE_KEY

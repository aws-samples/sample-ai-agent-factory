"""Offline contract tests for scripts/final_teardown.py.

These drive the tool against an in-memory fake of the AWS calls it makes, so
they prove its ordering and fail-closed rules without an account. They are
not a substitute for the live teardown (see the evidence document); they pin
the behaviour that must not regress.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, WaiterError

SPEC = importlib.util.spec_from_file_location("final_teardown", Path(__file__).with_name("final_teardown.py"))
ft = importlib.util.module_from_spec(SPEC)
sys.modules["final_teardown"] = ft
SPEC.loader.exec_module(ft)

ACCOUNT = "123456789012"


def client_error(code: str, message: str = "x") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Op")


class FakeWaiter:
    def __init__(self, cfn: "FakeCfn") -> None:
        self.cfn = cfn

    def wait(self, StackName: str, WaiterConfig: dict) -> None:  # noqa: N803 - boto3 names
        if StackName in self.cfn.fail_delete:
            raise WaiterError("StackDeleteComplete", "failed", {})
        self.cfn.stacks.pop(StackName, None)


class FakeCfn:
    def __init__(self, stacks: dict[str, dict]) -> None:
        # name -> {"status", "resources": [(lid, type, pid)], "policies": {lid: policy}}
        self.stacks = stacks
        self.deleted: list[str] = []
        self.fail_delete: set[str] = set()

    def describe_stacks(self, StackName: str) -> dict:  # noqa: N803
        if StackName not in self.stacks:
            raise client_error("ValidationError", f"Stack with id {StackName} does not exist")
        return {"Stacks": [{"StackStatus": self.stacks[StackName]["status"]}]}

    def get_template(self, StackName: str, TemplateStage: str) -> dict:  # noqa: N803
        policies = self.stacks[StackName].get("policies", {})
        return {"TemplateBody": {"Resources": {lid: ({"DeletionPolicy": p} if p else {}) for lid, p in policies.items()}}}

    def get_paginator(self, name: str):
        assert name == "list_stack_resources"
        cfn = self

        class Pager:
            def paginate(self, StackName: str):  # noqa: N803
                yield {
                    "StackResourceSummaries": [
                        {"LogicalResourceId": lid, "ResourceType": t, "PhysicalResourceId": pid}
                        for lid, t, pid in cfn.stacks[StackName]["resources"]
                    ]
                }

        return Pager()

    def delete_stack(self, StackName: str) -> None:  # noqa: N803
        self.deleted.append(StackName)

    def get_waiter(self, name: str) -> FakeWaiter:
        assert name == "stack_delete_complete"
        return FakeWaiter(self)

    def describe_stack_events(self, StackName: str) -> dict:  # noqa: N803
        return {"StackEvents": [{"LogicalResourceId": "Thing", "ResourceStatus": "DELETE_FAILED", "ResourceStatusReason": "boom"}]}


class Recorder:
    """Generic fake client: records calls, returns canned responses."""

    def __init__(self, name: str, calls: list, responses: dict | None = None, errors: dict | None = None) -> None:
        self.name, self.calls = name, calls
        self.responses = responses or {}
        self.errors = errors or {}

    def __getattr__(self, op: str):
        def call(*args, **kwargs):
            self.calls.append((self.name, op, kwargs))
            if op in self.errors:
                raise self.errors[op]
            response = self.responses.get(op, {})
            return response(*args, **kwargs) if callable(response) else response

        return call


class FakeAccount:
    def __init__(self, cfn: FakeCfn, responses: dict | None = None, errors: dict | None = None) -> None:
        self.account = ACCOUNT
        self.cfn = cfn
        self.calls: list = []
        self._responses = responses or {}
        self._errors = errors or {}
        self._clients: dict = {}

    def client(self, name: str):
        if name == "logs":
            return self._logs()
        if name not in self._clients:
            self._clients[name] = Recorder(name, self.calls, self._responses.get(name), self._errors.get(name))
        return self._clients[name]

    def _logs(self):
        if "logs" not in self._clients:
            existing = set(self._responses.get("logs-existing", []))
            calls = self.calls

            class Logs:
                def describe_log_groups(self, logGroupNamePrefix: str):  # noqa: N803
                    return {"logGroups": [{"logGroupName": g} for g in sorted(existing) if g.startswith(logGroupNamePrefix)]}

                def get_paginator(self, name: str):
                    outer = self

                    class Pager:
                        def paginate(self, logGroupNamePrefix: str):  # noqa: N803
                            yield outer.describe_log_groups(logGroupNamePrefix=logGroupNamePrefix)

                    return Pager()

                def delete_log_group(self, logGroupName: str):  # noqa: N803
                    calls.append(("logs", "delete_log_group", {"logGroupName": logGroupName}))
                    existing.discard(logGroupName)

            self._clients["logs"] = Logs()
        return self._clients["logs"]


def run_main(monkeypatch, tmp_path: Path, fake: FakeAccount, *argv: str) -> int:
    monkeypatch.setattr(ft, "Account", lambda expected, region: fake)
    monkeypatch.setattr(sys, "argv", ["final_teardown.py", "--expected-account", ACCOUNT, "--state-dir", str(tmp_path), *argv])
    return ft.main()


def workstream_stacks() -> dict:
    names = ft.build_plan("demo", "primary")["workstream"]
    return {n: {"status": "UPDATE_COMPLETE", "resources": []} for n in names}


# --------------------------------------------------------------------------- plan


def test_plan_orders_consumers_before_producers():
    plan = ft.build_plan("demo", "primary")
    ws = plan["workstream"]
    for env in ("prod", "nonprod"):
        assert ws.index(f"AgenticAI-demo-primary-{env}-RuntimeMemory") < ws.index(f"AgenticAI-demo-primary-{env}-ToolGateway")
        assert ws.index(f"AgenticAI-demo-primary-{env}-ToolGateway") < ws.index(f"AgenticAI-demo-primary-{env}-RegistryRoles")
    pf = plan["platform"]
    assert pf[:2] == ["AgenticAI-WorkloadPipelineStack", "AgenticAI-PlatformPipelineStack"]
    for env in ("Prod", "Nonprod"):
        assert pf.index(f"{env}-InferenceGateway") < pf.index(f"{env}-Guardrail")
    assert plan["management"] == ["Nonprod-Audit", "Nonprod-LogArchive"]


def test_plan_names_follow_tenant_and_agent():
    assert ft.build_plan("acme", "helper")["workstream"][0] == "AgenticAI-acme-helper-prod-RuntimeMemory"


# ------------------------------------------------------------------- dry run


def test_dry_run_deletes_nothing(monkeypatch, tmp_path):
    cfn = FakeCfn(workstream_stacks())
    fake = FakeAccount(cfn)
    assert run_main(monkeypatch, tmp_path, fake, "--account-role", "workstream") == 0
    assert cfn.deleted == []
    assert not any(op.startswith("delete") or op.startswith("schedule") for _, op, _ in fake.calls)
    assert not list(tmp_path.iterdir()), "a dry run must not write a plan"


# --------------------------------------------------------------- fail-closed


def test_first_failed_delete_stops_before_the_next_stack(monkeypatch, tmp_path):
    stacks = workstream_stacks()
    cfn = FakeCfn(stacks)
    first = ft.build_plan("demo", "primary")["workstream"][0]
    cfn.fail_delete.add(first)
    with pytest.raises(SystemExit, match="later stacks were NOT deleted"):
        run_main(monkeypatch, tmp_path, FakeAccount(cfn), "--account-role", "workstream", "--apply")
    assert cfn.deleted == [first]


def test_in_progress_stack_refuses(monkeypatch, tmp_path):
    stacks = workstream_stacks()
    first = ft.build_plan("demo", "primary")["workstream"][0]
    stacks[first]["status"] = "UPDATE_IN_PROGRESS"
    cfn = FakeCfn(stacks)
    with pytest.raises(SystemExit, match="UPDATE_IN_PROGRESS"):
        run_main(monkeypatch, tmp_path, FakeAccount(cfn), "--account-role", "workstream", "--apply")
    assert cfn.deleted == []


def test_apply_deletes_in_plan_order(monkeypatch, tmp_path):
    cfn = FakeCfn(workstream_stacks())
    assert run_main(monkeypatch, tmp_path, FakeAccount(cfn), "--account-role", "workstream", "--apply") == 0
    assert cfn.deleted == ft.build_plan("demo", "primary")["workstream"]


def test_unexpected_error_on_retained_cleanup_is_not_swallowed(monkeypatch, tmp_path):
    stacks = {"Prod-Registry": {"status": "UPDATE_COMPLETE", "resources": [("P", "AWS::SSM::Parameter", "/p")], "policies": {"P": "Retain"}}}
    cfn = FakeCfn(stacks)
    fake = FakeAccount(cfn, errors={"ssm": {"delete_parameter": client_error("ValidationException", "bad")}})
    monkeypatch.setattr(ft, "build_plan", lambda t, a: {"platform": ["Prod-Registry"]})
    with pytest.raises(ClientError):
        run_main(monkeypatch, tmp_path, fake, "--account-role", "platform", "--apply")


def test_already_deleted_retained_resource_is_tolerated(monkeypatch, tmp_path):
    stacks = {"Prod-Registry": {"status": "UPDATE_COMPLETE", "resources": [("P", "AWS::SSM::Parameter", "/p")], "policies": {"P": "Retain"}}}
    fake = FakeAccount(FakeCfn(stacks), errors={"ssm": {"delete_parameter": client_error("ParameterNotFound")}})
    monkeypatch.setattr(ft, "build_plan", lambda t, a: {"platform": ["Prod-Registry"]})
    assert run_main(monkeypatch, tmp_path, fake, "--account-role", "platform", "--apply") == 0


# ------------------------------------------------------------- resumability


def test_plan_is_saved_before_the_stack_is_deleted(monkeypatch, tmp_path):
    stacks = {"Prod-Registry": {"status": "UPDATE_COMPLETE", "resources": [("P", "AWS::SSM::Parameter", "/p")], "policies": {"P": "Retain"}}}
    cfn = FakeCfn(stacks)
    seen = {}

    original = cfn.delete_stack

    def spying_delete(StackName):  # noqa: N803
        path = ft.plan_path(tmp_path, "platform", "us-west-2")
        seen["plan"] = json.loads(path.read_text()) if path.exists() else None
        original(StackName=StackName)

    cfn.delete_stack = spying_delete
    monkeypatch.setattr(ft, "build_plan", lambda t, a: {"platform": ["Prod-Registry"]})
    run_main(monkeypatch, tmp_path, FakeAccount(cfn), "--account-role", "platform", "--apply")
    assert seen["plan"] is not None
    assert seen["plan"]["stacks"]["Prod-Registry"]["retained"] == [["AWS::SSM::Parameter", "/p"]]


def test_resume_residue_finishes_cleanup_after_the_stack_is_gone(monkeypatch, tmp_path):
    plan = {
        "stacks": {"Prod-Registry": {"retained": [["AWS::SSM::Parameter", "/p"]], "logGroups": [], "images": [], "runtimeIds": [], "residueDone": False}},
        "images": [],
    }
    ft.save_plan(tmp_path, "platform", "us-west-2", plan)
    fake = FakeAccount(FakeCfn({}))
    monkeypatch.setattr(ft, "build_plan", lambda t, a: {"platform": ["Prod-Registry"]})
    assert run_main(monkeypatch, tmp_path, fake, "--account-role", "platform", "--resume-residue") == 0
    assert ("ssm", "delete_parameter", {"Name": "/p"}) in fake.calls
    saved = json.loads(ft.plan_path(tmp_path, "platform", "us-west-2").read_text())
    assert saved["stacks"]["Prod-Registry"]["residueDone"] is True


# ------------------------------------------------------------ retained types


def registry_gone(**_):
    raise client_error("ResourceNotFoundException", "gone")


def test_registry_records_before_registry_and_keys_last(monkeypatch, tmp_path):
    reg = f"arn:aws:agent-registry:us-west-2:{ACCOUNT}:registry/REG1"
    resources = [
        ("Key", "AWS::KMS::Key", "key-1"),
        ("Reg", "AWS::AgentRegistry::Registry", reg),
        ("Rec", "AWS::AgentRegistry::RegistryRecord", f"{reg}/record/REC1"),
        ("Tbl", "AWS::DynamoDB::Table", "agenticai-registry-tools-prod"),
    ]
    stacks = {"Prod-Registry": {"status": "UPDATE_COMPLETE", "resources": resources,
                                "policies": {"Key": "Retain", "Reg": "RetainExceptOnCreate", "Rec": "RetainExceptOnCreate", "Tbl": "Retain"}}}
    fake = FakeAccount(
        FakeCfn(stacks),
        responses={
            "kms": {"describe_key": {"KeyMetadata": {"KeyState": "Enabled"}}},
            "agent-registry-control": {"list_registry_records": {"registryRecords": []}, "get_registry": registry_gone},
            "dynamodb": {"get_waiter": lambda name: type("W", (), {"wait": lambda self, **k: None})()},
        },
    )
    monkeypatch.setattr(ft, "build_plan", lambda t, a: {"platform": ["Prod-Registry"]})
    assert run_main(monkeypatch, tmp_path, fake, "--account-role", "platform", "--apply") == 0
    order = [(svc, op) for svc, op, _ in fake.calls if op in {"delete_registry_record", "delete_registry", "delete_table", "schedule_key_deletion"}]
    assert order == [
        ("agent-registry-control", "delete_registry_record"),
        ("agent-registry-control", "delete_registry"),
        ("dynamodb", "delete_table"),
        ("kms", "schedule_key_deletion"),
    ]
    key_call = next(kw for svc, op, kw in fake.calls if op == "schedule_key_deletion")
    assert key_call == {"KeyId": "key-1", "PendingWindowInDays": 7}


def test_stray_registry_record_is_deleted_before_the_registry(monkeypatch, tmp_path):
    reg = f"arn:aws:agent-registry:us-west-2:{ACCOUNT}:registry/REG1"
    stacks = {"Prod-Registry": {"status": "UPDATE_COMPLETE", "resources": [("Reg", "AWS::AgentRegistry::Registry", reg)],
                                "policies": {"Reg": "RetainExceptOnCreate"}}}
    fake = FakeAccount(FakeCfn(stacks), responses={"agent-registry-control": {"list_registry_records": {"registryRecords": [{"recordId": "STRAY"}]}, "get_registry": registry_gone}})
    monkeypatch.setattr(ft, "build_plan", lambda t, a: {"platform": ["Prod-Registry"]})
    run_main(monkeypatch, tmp_path, fake, "--account-role", "platform", "--apply")
    ops = [(op, kw) for svc, op, kw in fake.calls if svc == "agent-registry-control" and op.startswith("delete")]
    assert ops == [("delete_registry_record", {"registryId": "REG1", "recordId": "STRAY"}), ("delete_registry", {"registryId": "REG1"})]


class AsyncRegistry:
    """DeleteRegistry conflicts N times, then the Registry is DELETING for M polls."""

    def __init__(self, conflicts: int = 0, deleting_polls: int = 0, final: str = "gone") -> None:
        self.conflicts, self.deleting_polls, self.final = conflicts, deleting_polls, final

    def delete_registry(self, **_):
        if self.conflicts:
            self.conflicts -= 1
            raise client_error("ConflictException", "records still deleting")
        return {"status": "DELETING"}

    def get_registry(self, **_):
        if self.deleting_polls:
            self.deleting_polls -= 1
            return {"status": "DELETING"}
        if self.final == "gone":
            raise client_error("ResourceNotFoundException", "gone")
        return {"status": self.final}


def _registry_stack_with_key():
    reg = f"arn:aws:agent-registry:us-west-2:{ACCOUNT}:registry/REG1"
    return {
        "Prod-Registry": {
            "status": "UPDATE_COMPLETE",
            "resources": [("Reg", "AWS::AgentRegistry::Registry", reg), ("Key", "AWS::KMS::Key", "key-1")],
            "policies": {"Reg": "RetainExceptOnCreate", "Key": "Retain"},
        }
    }


def _fake_with_registry(reg: AsyncRegistry) -> FakeAccount:
    return FakeAccount(
        FakeCfn(_registry_stack_with_key()),
        responses={
            "agent-registry-control": {
                "list_registry_records": {"registryRecords": []},
                "delete_registry": reg.delete_registry,
                "get_registry": reg.get_registry,
            },
            "kms": {"describe_key": {"KeyMetadata": {"KeyState": "Enabled"}}},
        },
    )


def test_registry_delete_retries_conflict_and_waits_before_the_key(monkeypatch, tmp_path):
    monkeypatch.setattr(ft.time, "sleep", lambda s: None)
    fake = _fake_with_registry(AsyncRegistry(conflicts=2, deleting_polls=3))
    monkeypatch.setattr(ft, "build_plan", lambda t, a: {"platform": ["Prod-Registry"]})
    assert run_main(monkeypatch, tmp_path, fake, "--account-role", "platform", "--apply") == 0
    ops = [op for svc, op, _ in fake.calls if op in {"delete_registry", "get_registry", "schedule_key_deletion"}]
    assert ops.count("delete_registry") == 3
    assert ops.index("schedule_key_deletion") > max(i for i, op in enumerate(ops) if op == "get_registry"), (
        "the key must be scheduled only after the Registry is confirmed gone"
    )


def test_registry_delete_failed_stops_before_the_key(monkeypatch, tmp_path):
    monkeypatch.setattr(ft.time, "sleep", lambda s: None)
    fake = _fake_with_registry(AsyncRegistry(final="DELETE_FAILED"))
    monkeypatch.setattr(ft, "build_plan", lambda t, a: {"platform": ["Prod-Registry"]})
    with pytest.raises(SystemExit, match="DELETE_FAILED"):
        run_main(monkeypatch, tmp_path, fake, "--account-role", "platform", "--apply")
    assert not any(op == "schedule_key_deletion" for _, op, _ in fake.calls)


@pytest.mark.parametrize("protection,expected", [("ACTIVE", ["describe_user_pool", "update_user_pool", "delete_user_pool"]),
                                                 ("INACTIVE", ["describe_user_pool", "delete_user_pool"])])
def test_cognito_pool_deletion_protection_disabled_only_when_active(monkeypatch, tmp_path, protection, expected):
    stacks = {"Prod-InferenceGateway": {"status": "UPDATE_COMPLETE", "resources": [("Pool", "AWS::Cognito::UserPool", "us-west-2_x")], "policies": {"Pool": "Retain"}}}
    fake = FakeAccount(FakeCfn(stacks), responses={"cognito-idp": {"describe_user_pool": {"UserPool": {"DeletionProtection": protection}}}})
    monkeypatch.setattr(ft, "build_plan", lambda t, a: {"platform": ["Prod-InferenceGateway"]})
    run_main(monkeypatch, tmp_path, fake, "--account-role", "platform", "--apply")
    ops = [op for svc, op, _ in fake.calls if svc == "cognito-idp"]
    assert ops == expected


def test_unknown_retained_type_is_reported_and_fails_the_run(monkeypatch, tmp_path):
    stacks = {"Prod-Registry": {"status": "UPDATE_COMPLETE", "resources": [("B", "AWS::S3::Bucket", "bucket-1")], "policies": {"B": "Retain"}}}
    monkeypatch.setattr(ft, "build_plan", lambda t, a: {"platform": ["Prod-Registry"]})
    assert run_main(monkeypatch, tmp_path, FakeAccount(FakeCfn(stacks)), "--account-role", "platform", "--apply") == 3


# ---------------------------------------------------------------- images


def test_shared_image_digests_are_deleted_once(monkeypatch, tmp_path):
    uri = f"{ACCOUNT}.dkr.ecr.us-west-2.amazonaws.com/cdk-assets@sha256:" + "a" * 64
    stacks = {
        "A": {"status": "UPDATE_COMPLETE", "resources": [("R", "AWS::BedrockAgentCore::Runtime", "arn:x:runtime/rt-a")]},
        "B": {"status": "UPDATE_COMPLETE", "resources": [("R", "AWS::BedrockAgentCore::Runtime", "arn:x:runtime/rt-b")]},
    }
    responses = {
        "bedrock-agentcore-control": {
            "list_agent_runtime_versions": {"agentRuntimes": [{"agentRuntimeVersion": "1"}]},
            "get_agent_runtime": {"agentRuntimeArtifact": {"containerConfiguration": {"containerUri": uri}}},
        },
        "ecr": {"batch_delete_image": {"imageIds": [{}], "failures": []}},
    }
    fake = FakeAccount(FakeCfn(stacks), responses=responses)
    monkeypatch.setattr(ft, "build_plan", lambda t, a: {"workstream": ["A", "B"]})
    assert run_main(monkeypatch, tmp_path, fake, "--account-role", "workstream", "--apply") == 0
    deletes = [kw for svc, op, kw in fake.calls if op == "batch_delete_image"]
    assert deletes == [{"repositoryName": "cdk-assets", "imageIds": [{"imageDigest": "sha256:" + "a" * 64}]}]


def test_image_delete_failure_other_than_not_found_stops(monkeypatch, tmp_path):
    plan = {"stacks": {}, "images": ["1.dkr.ecr.us-west-2.amazonaws.com/repo@sha256:" + "b" * 64]}
    ft.save_plan(tmp_path, "workstream", "us-west-2", plan)
    fake = FakeAccount(FakeCfn({}), responses={"ecr": {"batch_delete_image": {"failures": [{"failureCode": "AccessDenied", "failureReason": "no"}]}}})
    monkeypatch.setattr(ft, "build_plan", lambda t, a: {"workstream": []})
    with pytest.raises(SystemExit, match="image delete failed"):
        run_main(monkeypatch, tmp_path, fake, "--account-role", "workstream", "--resume-residue")


# --------------------------------------------------------- guards / preflight


def test_wrong_account_is_refused(monkeypatch):
    class Sts:
        def get_caller_identity(self):
            return {"Account": "999999999999"}

    class Session:
        region_name = "us-west-2"

        def __init__(self, region_name):
            pass

        def get_available_services(self):
            return list(ft.REQUIRED_OPERATIONS)

        def client(self, name, **_):
            if name == "sts":
                return Sts()
            model = type("M", (), {"operation_names": [op for ops in ft.REQUIRED_OPERATIONS.values() for op in ops]})()
            return type("C", (), {"meta": type("Meta", (), {"service_model": model})()})()

    monkeypatch.setattr(ft.boto3, "Session", Session)
    with pytest.raises(SystemExit, match=r"REFUSING: credentials belong to \.\.\.9999"):
        ft.Account(ACCOUNT, "us-west-2")


def test_old_sdk_is_refused_before_any_call(monkeypatch):
    class Session:
        region_name = "us-west-2"

        def __init__(self, region_name):
            pass

        def get_available_services(self):
            return [s for s in ft.REQUIRED_OPERATIONS if s != "agent-registry-control"]

        def client(self, name, **_):
            if name == "sts":
                raise AssertionError("the SDK check must run before any AWS call")
            model = type("M", (), {"operation_names": [op for ops in ft.REQUIRED_OPERATIONS.values() for op in ops]})()
            return type("C", (), {"meta": type("Meta", (), {"service_model": model})()})()

    monkeypatch.setattr(ft.boto3, "Session", Session)
    with pytest.raises(SystemExit, match="agent-registry-control"):
        ft.Account(ACCOUNT, "us-west-2")


def test_installed_sdk_models_every_required_operation():
    import boto3

    assert ft.missing_operations(boto3.Session(region_name="us-west-2")) == []


def test_state_dir_default_is_outside_the_repository():
    repo = Path(__file__).resolve().parents[1]
    assert repo not in ft.DEFAULT_STATE_DIR.resolve().parents

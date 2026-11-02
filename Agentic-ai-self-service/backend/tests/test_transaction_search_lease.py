"""Contracts for the reversible Transaction Search integration-test lease."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.transaction_search import (
    TransactionSearchLease,
    TransactionSearchLeaseError,
)

ACCOUNT = "123456789012"
REGION = "eu-west-1"


class _XRay:
    def __init__(self, destination: str = "XRay", indexing: float = 0.0) -> None:
        self.destination = destination
        self.indexing = indexing
        self.updates: list[tuple[str, object]] = []

    def get_trace_segment_destination(self):
        return {"Destination": self.destination, "Status": "ACTIVE"}

    def update_trace_segment_destination(self, *, Destination):
        self.destination = Destination
        self.updates.append(("destination", Destination))
        return {}

    def get_indexing_rules(self):
        return {
            "IndexingRules": [
                {
                    "Name": "Default",
                    "Rule": {"Probabilistic": {"DesiredSamplingPercentage": self.indexing}},
                }
            ]
        }

    def update_indexing_rule(self, *, Name, Rule):
        assert Name == "Default"
        self.indexing = float(Rule["Probabilistic"]["DesiredSamplingPercentage"])
        self.updates.append(("indexing", self.indexing))
        return {}


class _Logs:
    def __init__(self, *, delete_effective: bool = True) -> None:
        self.policies: dict[str, str] = {}
        self.delete_effective = delete_effective

    def describe_resource_policies(self):
        return {
            "resourcePolicies": [
                {"policyName": name, "policyDocument": document} for name, document in sorted(self.policies.items())
            ]
        }

    def put_resource_policy(self, *, policyName, policyDocument):
        self.policies[policyName] = policyDocument
        return {}

    def delete_resource_policy(self, *, policyName):
        if self.delete_effective:
            self.policies.pop(policyName, None)
        return {}


class _Sts:
    def __init__(self, account: str = ACCOUNT) -> None:
        self.account = account

    def get_caller_identity(self):
        return {"Account": self.account}


def _lease(
    tmp_path: Path,
    *,
    manage: bool,
    xray: _XRay | None = None,
    logs: _Logs | None = None,
    account: str = ACCOUNT,
    sleep=None,
) -> TransactionSearchLease:
    return TransactionSearchLease(
        account_id=ACCOUNT,
        region=REGION,
        manage=manage,
        state_path=tmp_path / "transaction-search.json" if manage else None,
        xray_client=xray or _XRay(),
        logs_client=logs or _Logs(),
        sts_client=_Sts(account),
        sleep=sleep or (lambda _seconds: None),
        policy_token="contract",
    )


def test_managed_lease_enables_and_restores_every_account_setting(tmp_path: Path) -> None:
    xray = _XRay()
    logs = _Logs()
    sleeps: list[float] = []
    lease = _lease(
        tmp_path,
        manage=True,
        xray=xray,
        logs=logs,
        sleep=sleeps.append,
    )

    lease.acquire()

    assert sleeps == [10 * 60], (
        "A newly enabled Transaction Search must wait AWS's documented ten-minute search-readiness window"
    )
    assert xray.destination == "CloudWatchLogs"
    assert xray.indexing == 100.0
    assert list(logs.policies) == ["agentcore-trigger-it-contract"]
    state = json.loads((tmp_path / "transaction-search.json").read_text())
    assert state["destination"] == "XRay"
    assert state["indexing_percentage"] == 0.0
    assert state["policy_created"] is True
    assert state["destination_changed"] is True
    assert state["indexing_changed"] is True

    lease.restore()

    assert xray.destination == "XRay"
    assert xray.indexing == 0.0
    assert logs.policies == {}
    assert not (tmp_path / "transaction-search.json").exists()
    assert xray.updates == [
        ("destination", "CloudWatchLogs"),
        ("indexing", 100.0),
        ("indexing", 0.0),
        ("destination", "XRay"),
    ]


def test_unmanaged_lease_fails_closed_when_transaction_search_is_off(tmp_path: Path) -> None:
    xray = _XRay()
    logs = _Logs()
    with pytest.raises(TransactionSearchLeaseError, match="requires ACTIVE/CloudWatchLogs"):
        _lease(tmp_path, manage=False, xray=xray, logs=logs).acquire()
    assert xray.updates == []
    assert logs.policies == {}


def test_context_restores_after_the_primary_test_failure(tmp_path: Path) -> None:
    xray = _XRay()
    logs = _Logs()
    with pytest.raises(RuntimeError, match="primary failure"):
        with _lease(tmp_path, manage=True, xray=xray, logs=logs):
            raise RuntimeError("primary failure")
    assert xray.destination == "XRay"
    assert xray.indexing == 0.0
    assert logs.policies == {}
    assert not (tmp_path / "transaction-search.json").exists()


def test_cleanup_mutation_is_detected_keeps_state_and_can_retry(tmp_path: Path) -> None:
    xray = _XRay()
    logs = _Logs(delete_effective=False)
    lease = _lease(tmp_path, manage=True, xray=xray, logs=logs)
    lease.acquire()

    with pytest.raises(TransactionSearchLeaseError, match="resource policy residue"):
        lease.restore()

    assert xray.destination == "XRay"
    assert xray.indexing == 0.0
    assert list(logs.policies) == ["agentcore-trigger-it-contract"]
    assert (tmp_path / "transaction-search.json").exists()
    assert lease.snapshot is not None

    # Removal mutation repaired: the same process retains its exact retry handle.
    logs.delete_effective = True
    lease.restore()
    assert logs.policies == {}
    assert lease.snapshot is None
    assert not (tmp_path / "transaction-search.json").exists()


def test_new_process_restores_an_interrupted_lease_from_state(tmp_path: Path) -> None:
    xray = _XRay()
    logs = _Logs()
    _lease(tmp_path, manage=True, xray=xray, logs=logs).acquire()

    replacement = _lease(tmp_path, manage=True, xray=xray, logs=logs)
    replacement.restore_from_state()

    assert xray.destination == "XRay"
    assert xray.indexing == 0.0
    assert logs.policies == {}
    assert replacement.snapshot is None
    assert not (tmp_path / "transaction-search.json").exists()


def test_recovery_waits_for_an_already_restored_destination_without_updating_it(
    tmp_path: Path,
) -> None:
    class PartiallyRestoredXRay(_XRay):
        def __init__(self) -> None:
            super().__init__()
            self.status = "ACTIVE"
            self.pending_target_reads = 0

        def get_trace_segment_destination(self):
            status = self.status
            if status == "PENDING":
                self.pending_target_reads += 1
                self.status = "ACTIVE"
            return {"Destination": self.destination, "Status": status}

        def update_trace_segment_destination(self, *, Destination):
            if Destination == self.destination:
                raise RuntimeError("duplicate destination update")
            return super().update_trace_segment_destination(Destination=Destination)

    xray = PartiallyRestoredXRay()
    logs = _Logs()
    _lease(tmp_path, manage=True, xray=xray, logs=logs).acquire()

    # Simulate a process that restored both settings and then lost credentials
    # while AWS was still settling the destination transition.
    xray.indexing = 0.0
    xray.destination = "XRay"
    xray.status = "PENDING"
    updates_before_retry = list(xray.updates)

    replacement = _lease(tmp_path, manage=True, xray=xray, logs=logs)
    replacement.restore_from_state()

    assert xray.status == "ACTIVE"
    assert xray.pending_target_reads == 1
    assert xray.updates == updates_before_retry
    assert logs.policies == {}
    assert replacement.snapshot is None
    assert not (tmp_path / "transaction-search.json").exists()


def test_recovery_refuses_a_foreign_policy_document_and_keeps_state(tmp_path: Path) -> None:
    xray = _XRay()
    logs = _Logs()
    _lease(tmp_path, manage=True, xray=xray, logs=logs).acquire()
    policy_name = "agentcore-trigger-it-contract"
    foreign_document = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "logs:*", "Resource": "*"}],
        }
    )
    logs.policies[policy_name] = foreign_document

    replacement = _lease(tmp_path, manage=True, xray=xray, logs=logs)
    with pytest.raises(TransactionSearchLeaseError, match="not owned by this lease"):
        replacement.restore_from_state()

    assert logs.policies[policy_name] == foreign_document
    assert replacement.snapshot is not None
    assert (tmp_path / "transaction-search.json").exists()


def test_recovery_refuses_a_tampered_policy_name_before_mutation(tmp_path: Path) -> None:
    xray = _XRay()
    logs = _Logs()
    _lease(tmp_path, manage=True, xray=xray, logs=logs).acquire()
    state_path = tmp_path / "transaction-search.json"
    state = json.loads(state_path.read_text())
    state["policy_name"] = "unrelated-account-policy"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    destination_before = xray.destination
    indexing_before = xray.indexing

    replacement = _lease(tmp_path, manage=True, xray=xray, logs=logs)
    with pytest.raises(TransactionSearchLeaseError, match="invalid resource policy name"):
        replacement.restore_from_state()

    assert xray.destination == destination_before
    assert xray.indexing == indexing_before
    assert "agentcore-trigger-it-contract" in logs.policies
    assert replacement.snapshot is None


def test_lost_policy_create_response_still_restores_by_registered_name(tmp_path: Path) -> None:
    class LostResponseLogs(_Logs):
        def put_resource_policy(self, *, policyName, policyDocument):
            super().put_resource_policy(
                policyName=policyName,
                policyDocument=policyDocument,
            )
            raise RuntimeError("response lost after policy create")

    xray = _XRay()
    logs = LostResponseLogs()
    with pytest.raises(RuntimeError, match="response lost"):
        _lease(tmp_path, manage=True, xray=xray, logs=logs).acquire()

    assert xray.destination == "XRay"
    assert xray.indexing == 0.0
    assert logs.policies == {}
    assert not (tmp_path / "transaction-search.json").exists()


def test_wrong_account_is_denied_before_any_mutation(tmp_path: Path) -> None:
    xray = _XRay()
    logs = _Logs()
    with pytest.raises(TransactionSearchLeaseError, match="Wrong AWS account"):
        _lease(
            tmp_path,
            manage=True,
            xray=xray,
            logs=logs,
            account="999999999999",
        ).acquire()
    assert xray.updates == []
    assert logs.policies == {}
    assert not (tmp_path / "transaction-search.json").exists()


def test_resource_policy_is_exact_and_source_bound(tmp_path: Path) -> None:
    lease = _lease(tmp_path, manage=True)
    policy = lease._policy_document()
    statement = policy["Statement"]
    assert len(statement) == 1
    statement = statement[0]
    assert statement["Principal"] == {"Service": "xray.amazonaws.com"}
    assert statement["Action"] == "logs:PutLogEvents"
    assert set(statement["Resource"]) == {
        f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:aws/spans:*",
        f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/application-signals/data:*",
    }
    assert statement["Condition"] == {
        "ArnLike": {"aws:SourceArn": f"arn:aws:xray:{REGION}:{ACCOUNT}:*"},
        "StringEquals": {"aws:SourceAccount": ACCOUNT},
    }

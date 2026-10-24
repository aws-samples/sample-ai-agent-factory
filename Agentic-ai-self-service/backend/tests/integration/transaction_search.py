"""Reversible CloudWatch Transaction Search lease for real AgentCore tests."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

TRANSACTION_SEARCH_ACTIVATION_SETTLE_SECONDS = 10 * 60


class TransactionSearchLeaseError(RuntimeError):
    """Transaction Search could not be enabled or restored exactly."""


@dataclass
class TransactionSearchSnapshot:
    account_id: str
    region: str
    destination: str
    indexing_percentage: float
    policy_name: str
    policy_created: bool = False
    destination_changed: bool = False
    indexing_changed: bool = False


class TransactionSearchLease:
    """Temporarily enable 100% span indexing and restore the prior account state.

    The setting is account-global within a Region, so managed mode requires a
    durable state file and refuses to overwrite one. The file contains only
    non-secret configuration and survives a failed test process for operator
    recovery.
    """

    def __init__(
        self,
        *,
        account_id: str,
        region: str,
        manage: bool,
        state_path: Path | None,
        xray_client: Any,
        logs_client: Any,
        sts_client: Any,
        sleep: Callable[[float], None] = time.sleep,
        policy_token: str | None = None,
    ) -> None:
        self.account_id = account_id
        self.region = region
        self.manage = manage
        self.state_path = state_path
        self.xray = xray_client
        self.logs = logs_client
        self.sts = sts_client
        self.sleep = sleep
        self.policy_token = policy_token or uuid.uuid4().hex[:12]
        self.snapshot: TransactionSearchSnapshot | None = None

    def _indexing_percentage(self) -> float:
        response = self.xray.get_indexing_rules()
        for item in response.get("IndexingRules", []):
            if item.get("Name") == "Default":
                value = (item.get("Rule") or {}).get("Probabilistic", {}).get("DesiredSamplingPercentage")
                return float(value or 0.0)
        raise TransactionSearchLeaseError("X-Ray Default indexing rule is absent")

    def _destination(self) -> tuple[str, str]:
        response = self.xray.get_trace_segment_destination()
        return str(response.get("Destination") or ""), str(response.get("Status") or "")

    def _wait_destination(self, desired: str, timeout: int = 600) -> None:
        deadline = time.monotonic() + timeout
        last = ("", "")
        while time.monotonic() < deadline:
            last = self._destination()
            if last == (desired, "ACTIVE"):
                return
            self.sleep(5)
        raise TransactionSearchLeaseError(f"X-Ray destination did not reach ACTIVE/{desired}; last={last}")

    def _wait_indexing(self, desired: float, timeout: int = 300) -> None:
        deadline = time.monotonic() + timeout
        last = -1.0
        while time.monotonic() < deadline:
            last = self._indexing_percentage()
            if last == desired:
                return
            self.sleep(5)
        raise TransactionSearchLeaseError(f"X-Ray indexing did not reach {desired}; last={last}")

    def _resource_policy_document(self, name: str) -> dict[str, Any] | None:
        for policy in self.logs.describe_resource_policies().get("resourcePolicies", []):
            if policy.get("policyName") != name:
                continue
            raw = policy.get("policyDocument")
            if isinstance(raw, dict):
                document = raw
            elif isinstance(raw, str):
                try:
                    document = json.loads(raw)
                except ValueError as exc:
                    raise TransactionSearchLeaseError(f"CloudWatch resource policy {name} has invalid JSON") from exc
            else:
                raise TransactionSearchLeaseError(f"CloudWatch resource policy {name} has no readable document")
            if not isinstance(document, dict):
                raise TransactionSearchLeaseError(f"CloudWatch resource policy {name} is not a JSON object")
            return document
        return None

    def _policy_exists(self, name: str) -> bool:
        return self._resource_policy_document(name) is not None

    def _policy_document(self) -> dict[str, Any]:
        partition = "aws-cn" if self.region.startswith("cn-") else "aws"
        return {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "TransactionSearchXRayAccess",
                    "Effect": "Allow",
                    "Principal": {"Service": "xray.amazonaws.com"},
                    "Action": "logs:PutLogEvents",
                    "Resource": [
                        f"arn:{partition}:logs:{self.region}:{self.account_id}:log-group:aws/spans:*",
                        (
                            f"arn:{partition}:logs:{self.region}:{self.account_id}:"
                            "log-group:/aws/application-signals/data:*"
                        ),
                    ],
                    "Condition": {
                        "ArnLike": {"aws:SourceArn": (f"arn:{partition}:xray:{self.region}:{self.account_id}:*")},
                        "StringEquals": {"aws:SourceAccount": self.account_id},
                    },
                }
            ],
        }

    def _write_state(self) -> None:
        if self.state_path is None or self.snapshot is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(self.snapshot), indent=2, sort_keys=True) + "\n"
        temporary = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.state_path)

    def _assert_identity(self) -> None:
        identity = self.sts.get_caller_identity()
        if identity.get("Account") != self.account_id:
            raise TransactionSearchLeaseError(f"Wrong AWS account: {identity.get('Account')} != {self.account_id}")

    def _snapshot_from_state(self) -> TransactionSearchSnapshot:
        if self.state_path is None:
            raise TransactionSearchLeaseError(
                "Managed Transaction Search requires INTEGRATION_TRANSACTION_SEARCH_STATE_FILE"
            )
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TransactionSearchLeaseError(
                f"Transaction Search recovery state is unreadable: {self.state_path}"
            ) from exc
        expected_keys = {
            "account_id",
            "region",
            "destination",
            "indexing_percentage",
            "policy_name",
            "policy_created",
            "destination_changed",
            "indexing_changed",
        }
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            raise TransactionSearchLeaseError("Transaction Search recovery state has an invalid schema")
        if raw["account_id"] != self.account_id or raw["region"] != self.region:
            raise TransactionSearchLeaseError("Transaction Search recovery state targets a different account or Region")
        destination = raw["destination"]
        if destination not in {"XRay", "CloudWatchLogs"}:
            raise TransactionSearchLeaseError("Transaction Search recovery state has an invalid destination")
        percentage = raw["indexing_percentage"]
        if isinstance(percentage, bool) or not isinstance(percentage, (int, float)):
            raise TransactionSearchLeaseError("Transaction Search recovery state has an invalid indexing percentage")
        percentage = float(percentage)
        if percentage != percentage or percentage in {float("inf"), float("-inf")} or not 0 <= percentage <= 100:
            raise TransactionSearchLeaseError("Transaction Search recovery state has an invalid indexing percentage")
        policy_name = raw["policy_name"]
        allowed_policy_chars = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+=,.@_-")
        policy_prefix = "agentcore-trigger-it-"
        policy_token = policy_name[len(policy_prefix) :] if isinstance(policy_name, str) else ""
        if (
            not isinstance(policy_name, str)
            or not policy_name.startswith(policy_prefix)
            or not 1 <= len(policy_token) <= 64
            or any(char not in allowed_policy_chars for char in policy_token)
        ):
            raise TransactionSearchLeaseError("Transaction Search recovery state has an invalid resource policy name")
        flags = ("policy_created", "destination_changed", "indexing_changed")
        if any(type(raw[name]) is not bool for name in flags):
            raise TransactionSearchLeaseError("Transaction Search recovery state has an invalid mutation flag")
        return TransactionSearchSnapshot(
            account_id=self.account_id,
            region=self.region,
            destination=destination,
            indexing_percentage=percentage,
            policy_name=policy_name,
            policy_created=raw["policy_created"],
            destination_changed=raw["destination_changed"],
            indexing_changed=raw["indexing_changed"],
        )

    def require_ready(self) -> None:
        destination, status = self._destination()
        indexing = self._indexing_percentage()
        if destination != "CloudWatchLogs" or status != "ACTIVE" or indexing <= 0:
            raise TransactionSearchLeaseError(
                "AgentCore Batch Evaluation requires ACTIVE/CloudWatchLogs Transaction "
                f"Search with indexing > 0%; found {status}/{destination}, {indexing}%"
            )

    def acquire(self) -> TransactionSearchLease:
        self._assert_identity()
        if not self.manage:
            self.require_ready()
            return self
        if self.state_path is None:
            raise TransactionSearchLeaseError(
                "Managed Transaction Search requires INTEGRATION_TRANSACTION_SEARCH_STATE_FILE"
            )
        if self.state_path.exists():
            raise TransactionSearchLeaseError(
                f"Stale Transaction Search lease exists at {self.state_path}; call restore_from_state() before retrying"
            )

        destination, status = self._destination()
        if status != "ACTIVE":
            raise TransactionSearchLeaseError(
                f"Cannot capture a non-terminal X-Ray destination: {status}/{destination}"
            )
        policy_name = f"agentcore-trigger-it-{self.policy_token}"
        if self._policy_exists(policy_name):
            raise TransactionSearchLeaseError(f"CloudWatch resource policy {policy_name} already exists")
        self.snapshot = TransactionSearchSnapshot(
            account_id=self.account_id,
            region=self.region,
            destination=destination,
            indexing_percentage=self._indexing_percentage(),
            policy_name=policy_name,
        )
        self._write_state()
        try:
            self.snapshot.policy_created = True
            self._write_state()
            self.logs.put_resource_policy(
                policyName=policy_name,
                policyDocument=json.dumps(
                    self._policy_document(),
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )

            if destination != "CloudWatchLogs":
                self.snapshot.destination_changed = True
                self._write_state()
                self.xray.update_trace_segment_destination(Destination="CloudWatchLogs")
            self._wait_destination("CloudWatchLogs")

            if self.snapshot.indexing_percentage != 100.0:
                self.snapshot.indexing_changed = True
                self._write_state()
                self.xray.update_indexing_rule(
                    Name="Default",
                    Rule={"Probabilistic": {"DesiredSamplingPercentage": 100.0}},
                )
            self._wait_indexing(100.0)
            if self.snapshot.destination_changed or self.snapshot.indexing_changed:
                # AWS documents that a newly enabled Transaction Search can take
                # ten minutes before spans are searchable. ACTIVE/CloudWatchLogs
                # and a 100% desired indexing rule describe control-plane state,
                # not data-plane readiness: live evaluation lost the first five
                # sessions and indexed the final three during this window.
                self.sleep(TRANSACTION_SEARCH_ACTIVATION_SETTLE_SECONDS)
                self.require_ready()
            return self
        except BaseException as primary_error:
            try:
                self.restore()
            except Exception as cleanup_error:  # noqa: BLE001 - preserve the primary failure
                primary_error.add_note(str(cleanup_error))
            raise

    def restore_from_state(self) -> None:
        """Recover an interrupted managed lease from its durable state file.

        The file is treated as untrusted input: account, Region, schema, ranges,
        flags, and policy namespace are validated before any AWS mutation. The
        policy document is independently ownership-checked by ``restore``.
        """

        if not self.manage:
            raise TransactionSearchLeaseError("Only a managed Transaction Search lease has recovery state")
        if self.snapshot is not None:
            raise TransactionSearchLeaseError("This Transaction Search lease already has in-memory recovery state")
        if self.state_path is None or not self.state_path.exists():
            raise TransactionSearchLeaseError("No Transaction Search recovery state file exists")
        self._assert_identity()
        self.snapshot = self._snapshot_from_state()
        self.restore()

    def restore(self) -> None:
        snapshot = self.snapshot
        if snapshot is None:
            return
        errors: list[str] = []
        owned_policy_exists = False
        if snapshot.policy_created:
            try:
                existing_policy = self._resource_policy_document(snapshot.policy_name)
                owned_policy_exists = existing_policy is not None
                if existing_policy is not None and existing_policy != self._policy_document():
                    raise TransactionSearchLeaseError(
                        f"CloudWatch resource policy {snapshot.policy_name} is not owned by this lease"
                    )
            except Exception as exc:  # noqa: BLE001 - mutate nothing without ownership proof
                raise TransactionSearchLeaseError(
                    f"Transaction Search restoration refused: {type(exc).__name__}: {exc}"
                ) from exc
        if snapshot.indexing_changed:
            try:
                current_indexing = self._indexing_percentage()
                if current_indexing != snapshot.indexing_percentage:
                    self.xray.update_indexing_rule(
                        Name="Default",
                        Rule={"Probabilistic": {"DesiredSamplingPercentage": snapshot.indexing_percentage}},
                    )
                self._wait_indexing(snapshot.indexing_percentage)
            except Exception as exc:  # noqa: BLE001 - aggregate every restore failure
                errors.append(f"indexing: {type(exc).__name__}: {exc}")
        if snapshot.destination_changed:
            try:
                current_destination, current_status = self._destination()
                if current_destination == snapshot.destination:
                    # A prior process can lose credentials after AWS accepts the
                    # restore but before it observes ACTIVE. Waiting is both
                    # idempotent and required: UpdateTraceSegmentDestination
                    # rejects duplicate target updates in PENDING and ACTIVE.
                    self._wait_destination(snapshot.destination)
                else:
                    if current_status != "ACTIVE":
                        self._wait_destination(current_destination)
                    self.xray.update_trace_segment_destination(Destination=snapshot.destination)
                    self._wait_destination(snapshot.destination)
            except Exception as exc:  # noqa: BLE001 - aggregate every restore failure
                errors.append(f"destination: {type(exc).__name__}: {exc}")
        if snapshot.policy_created and owned_policy_exists:
            try:
                self.logs.delete_resource_policy(policyName=snapshot.policy_name)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                    errors.append(f"policy: {type(exc).__name__}: {exc}")
            except Exception as exc:  # noqa: BLE001 - aggregate every restore failure
                errors.append(f"policy: {type(exc).__name__}: {exc}")

        try:
            destination, status = self._destination()
            indexing = self._indexing_percentage()
            policy_exists = self._policy_exists(snapshot.policy_name)
            if (destination, status) != (snapshot.destination, "ACTIVE"):
                errors.append(f"destination residue: {(destination, status)} != {(snapshot.destination, 'ACTIVE')}")
            if indexing != snapshot.indexing_percentage:
                errors.append(f"indexing residue: {indexing} != {snapshot.indexing_percentage}")
            if policy_exists:
                errors.append(f"resource policy residue: {snapshot.policy_name}")
        except Exception as exc:  # noqa: BLE001 - restoration is not proven
            errors.append(f"verification: {type(exc).__name__}: {exc}")

        if errors:
            # Keep both the in-memory snapshot and the 0600 state file so this
            # process can retry immediately and a replacement process can call
            # restore_from_state(). Discarding either turns a cleanup failure
            # into an account-global orphan with no deterministic recovery path.
            raise TransactionSearchLeaseError("Transaction Search restoration failed: " + "; ".join(errors))
        if self.state_path is not None:
            self.state_path.unlink(missing_ok=True)
        self.snapshot = None

    def __enter__(self) -> TransactionSearchLease:
        return self.acquire()

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        try:
            self.restore()
        except Exception as cleanup_error:
            if exc is not None:
                exc.add_note(str(cleanup_error))
                return False
            raise
        return False

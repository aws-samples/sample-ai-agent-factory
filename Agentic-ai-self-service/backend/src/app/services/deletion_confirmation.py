"""Bounded, evidence-based confirmation for asynchronous AWS deletion APIs."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from app.services.aws_pagination import list_all
from app.services.error_sanitizer import redact_secrets
from app.services.resource_ownership import (
    OWNER_SUB_HASH_TAG_KEY,
    ResourceDeletionRefused,
    assert_agentcore_resource_owned,
    owner_sub_hash,
    resource_is_missing,
    tag_map,
)


class DeletionFailedAfterAccept(RuntimeError):
    """The control plane accepted a delete, then reported terminal failure."""


class ConfirmationBudgetExhausted(ResourceDeletionRefused):
    """The control plane accepted a delete and the resource was still present when the
    confirmation budget ran out.

    Not a refusal of authority: nothing was protected, the service had just not finished.
    Still a ``ResourceDeletionRefused``, so every caller that retains on refusal keeps doing
    so; the async teardown reads the subclass to decide whether to confirm again in a later
    invocation instead of recording a retention (deployment_handler._continue_async_delete).
    """


def _resource_payload(response: dict) -> dict:
    """Return the nested service resource when an API wraps it."""
    for key in (
        "harness",
        "memory",
        "agentRuntime",
        "policyEngine",
        "gateway",
    ):
        nested = response.get(key)
        if isinstance(nested, dict):
            return nested
    return response


def _failure_reason(payload: dict, status: str) -> str:
    parts: list[str] = []
    for field in ("statusReasons", "failureReason"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value if item)
    if not parts:
        parts.append(f"status {status}; the service returned no failure reason")
    return redact_secrets("; ".join(parts))[:600]


def _sleep_with_deadline(
    delay_seconds: float,
    deadline_monotonic: float | None,
) -> bool:
    """Sleep within the caller's budget; return False once no budget remains."""
    if deadline_monotonic is None:
        time.sleep(delay_seconds)
        return True
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return False
    time.sleep(min(delay_seconds, remaining))
    return time.monotonic() < deadline_monotonic


def wait_until_absent(
    *,
    resource_label: str,
    read: Callable[[], dict],
    absent_response: Callable[[dict], bool] | None = None,
    max_attempts: int,
    delay_seconds: float,
    deadline_monotonic: float | None = None,
) -> None:
    """Return only after a live read proves the resource is absent.

    A non-not-found read error and an exhausted budget are uncertainty, not
    success.  Both raise ``ResourceDeletionRefused`` so the deployment remains
    a durable retry handle. AgentCore-style terminal ``*FAILED`` states are
    surfaced separately as actual cleanup failures with the service's reason.
    """
    last_status = "PRESENT"
    attempts = max(1, int(max_attempts))
    for attempt in range(attempts):
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            break
        try:
            response = read() or {}
        except Exception as exc:  # noqa: BLE001
            if resource_is_missing(exc):
                return
            raise ResourceDeletionRefused(
                f"Deletion of {resource_label} was accepted, but its final "
                f"state could not be read ({type(exc).__name__}); deletion is "
                "not confirmed."
            ) from exc

        if absent_response is not None and absent_response(response):
            return

        payload = _resource_payload(response)
        status = str(payload.get("status") or "").upper()
        if status:
            last_status = status
        if status.endswith("FAILED"):
            raise DeletionFailedAfterAccept(
                f"{resource_label} accepted the delete and then entered {status}: {_failure_reason(payload, status)}"
            )

        if attempt + 1 >= attempts:
            break
        if not _sleep_with_deadline(delay_seconds, deadline_monotonic):
            break

    raise ConfirmationBudgetExhausted(
        f"Deletion of {resource_label} was accepted, but the resource is still "
        f"{last_status} at the end of the confirmation budget; deletion is not "
        "confirmed."
    )


def delete_memory_confirmed(
    client,
    memory_id: str,
    *,
    region: str,
    owner_sub: str,
    deadline_monotonic: float | None = None,
    confirmation_attempts: int = 60,
    delay_seconds: float = 2.0,
) -> None:
    """Delete one owned AgentCore memory and prove terminal absence.

    ``DeleteMemory`` returns a ``DELETING`` acknowledgement, not evidence that
    the resource is gone. Ownership is re-read immediately before mutation, and
    a deterministic client token makes a retry of the same teardown request
    idempotent without confusing one memory with another.
    """
    if not owner_sub:
        raise ResourceDeletionRefused(
            f"Deletion refused for memory {memory_id}: no authenticated owner binding was available."
        )

    try:
        detail = assert_agentcore_resource_owned(
            client,
            "memory",
            memory_id,
            region,
        )
    except Exception as exc:  # noqa: BLE001
        if resource_is_missing(exc):
            return
        raise

    payload = _resource_payload(detail)
    arn = str(payload.get("arn") or payload.get("memoryArn") or "")
    if not arn:
        raise ResourceDeletionRefused(
            f"Deletion refused for memory {memory_id}: its ARN could not be re-read for caller ownership verification."
        )
    try:
        caller_tags = client.list_tags_for_resource(
            resourceArn=arn,
        ).get("tags")
    except Exception as exc:  # noqa: BLE001
        raise ResourceDeletionRefused(
            f"Deletion refused for memory {memory_id}: caller ownership tags could not be read ({type(exc).__name__})."
        ) from exc
    if tag_map(caller_tags).get(OWNER_SUB_HASH_TAG_KEY) != owner_sub_hash(owner_sub):
        raise ResourceDeletionRefused(
            f"Deletion refused for memory {memory_id}: its authenticated caller binding does not match this deployment."
        )

    try:
        client.delete_memory(
            memoryId=memory_id,
            clientToken=str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"agentcore-memory-delete:{memory_id}",
                )
            ),
        )
    except Exception as exc:  # noqa: BLE001
        if resource_is_missing(exc):
            return
        raise

    wait_until_absent(
        resource_label=f"memory {memory_id}",
        read=lambda: client.get_memory(memoryId=memory_id),
        max_attempts=confirmation_attempts,
        delay_seconds=delay_seconds,
        deadline_monotonic=deadline_monotonic,
    )


def delete_policy_engine_confirmed(
    client,
    engine_id: str,
    *,
    deadline_monotonic: float | None = None,
    child_rounds: int = 10,
    confirmation_attempts: int = 30,
    delay_seconds: float = 2.0,
) -> None:
    """Delete every child policy, the engine, and prove terminal absence."""
    remaining: list[dict] = []
    for round_index in range(max(1, child_rounds)):
        try:
            remaining = list_all(
                client,
                "list_policies",
                item_keys=("policies", "items"),
                request={"policyEngineId": engine_id, "maxResults": 100},
            )
        except Exception as exc:  # noqa: BLE001
            if resource_is_missing(exc):
                return
            raise ResourceDeletionRefused(
                f"Policy engine {engine_id} child policies could not be "
                f"enumerated ({type(exc).__name__}); deletion is not confirmed."
            ) from exc

        if not remaining:
            break
        for policy in remaining:
            policy_id = policy.get("policyId") or policy.get("id")
            if not policy_id:
                continue
            try:
                client.delete_policy(
                    policyEngineId=engine_id,
                    policyId=policy_id,
                )
            except Exception as exc:  # noqa: BLE001
                if not resource_is_missing(exc):
                    # Re-listing is the authority: an async delete can raise or
                    # time out after the service accepted it.
                    pass
        if round_index + 1 < child_rounds:
            if not _sleep_with_deadline(delay_seconds, deadline_monotonic):
                break

    try:
        remaining = list_all(
            client,
            "list_policies",
            item_keys=("policies", "items"),
            request={"policyEngineId": engine_id, "maxResults": 100},
        )
    except Exception as exc:  # noqa: BLE001
        if resource_is_missing(exc):
            return
        raise ResourceDeletionRefused(
            f"Policy engine {engine_id} child-policy absence could not be confirmed ({type(exc).__name__})."
        ) from exc
    if remaining:
        raise ResourceDeletionRefused(
            f"Policy engine {engine_id} still contains {len(remaining)} "
            "policy/policies; engine deletion was not attempted."
        )

    try:
        client.delete_policy_engine(policyEngineId=engine_id)
    except Exception as exc:  # noqa: BLE001
        if resource_is_missing(exc):
            return
        raise

    wait_until_absent(
        resource_label=f"policy engine {engine_id}",
        read=lambda: client.get_policy_engine(policyEngineId=engine_id),
        max_attempts=confirmation_attempts,
        delay_seconds=delay_seconds,
        deadline_monotonic=deadline_monotonic,
    )


def delete_vector_bucket_confirmed(
    client,
    bucket_name: str,
    *,
    deadline_monotonic: float | None = None,
    child_rounds: int = 10,
    confirmation_attempts: int = 30,
    delay_seconds: float = 2.0,
) -> None:
    """Delete every S3 Vectors index, the bucket, and prove terminal absence."""
    remaining: list[dict] = []
    for round_index in range(max(1, child_rounds)):
        try:
            remaining = list_all(
                client,
                "list_indexes",
                item_keys=("indexes",),
                request={
                    "vectorBucketName": bucket_name,
                    "maxResults": 100,
                },
            )
        except Exception as exc:  # noqa: BLE001
            if resource_is_missing(exc):
                return
            raise ResourceDeletionRefused(
                f"S3 Vectors bucket {bucket_name} indexes could not be "
                f"enumerated ({type(exc).__name__}); deletion is not confirmed."
            ) from exc

        if not remaining:
            break
        for index in remaining:
            index_name = index.get("indexName")
            if not index_name:
                continue
            try:
                client.delete_index(
                    vectorBucketName=bucket_name,
                    indexName=index_name,
                )
            except Exception as exc:  # noqa: BLE001
                if not resource_is_missing(exc):
                    pass
        if round_index + 1 < child_rounds:
            if not _sleep_with_deadline(delay_seconds, deadline_monotonic):
                break

    try:
        remaining = list_all(
            client,
            "list_indexes",
            item_keys=("indexes",),
            request={
                "vectorBucketName": bucket_name,
                "maxResults": 100,
            },
        )
    except Exception as exc:  # noqa: BLE001
        if resource_is_missing(exc):
            return
        raise ResourceDeletionRefused(
            f"S3 Vectors bucket {bucket_name} index absence could not be confirmed ({type(exc).__name__})."
        ) from exc
    if remaining:
        raise ResourceDeletionRefused(
            f"S3 Vectors bucket {bucket_name} still contains "
            f"{len(remaining)} index/indexes; bucket deletion was not attempted."
        )

    try:
        client.delete_vector_bucket(vectorBucketName=bucket_name)
    except Exception as exc:  # noqa: BLE001
        if resource_is_missing(exc):
            return
        raise

    wait_until_absent(
        resource_label=f"S3 Vectors bucket {bucket_name}",
        read=lambda: client.get_vector_bucket(vectorBucketName=bucket_name),
        max_attempts=confirmation_attempts,
        delay_seconds=delay_seconds,
        deadline_monotonic=deadline_monotonic,
    )

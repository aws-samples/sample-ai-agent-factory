"""Step handler: Write final deployment status.

Receives deployment_id + results, writes final state (succeeded/failed)
to the Deployment_State_Table.

Requirements: 3.6
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import json
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial

from botocore.exceptions import ClientError

import app.services._otel_platform  # noqa: F401
from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services import step_clients
from app.services.agent_versions_store import (
    FENCEABLE_SLOTS,
    RuntimeSlots,
    SlotWriteConflict,
    VersionFence,
    get_slots_store,
    get_versions_store,
    set_slot_pointers_atomically,
)
from app.services.aws_pagination import list_all
from app.services.deletion_confirmation import (
    delete_memory_confirmed,
    delete_policy_engine_confirmed,
    delete_vector_bucket_confirmed,
    wait_until_absent,
)
from app.services.deployment_state_store import (
    CO_RESIDENT_REFUSAL,
    GATEWAY_GRAPH_FIELD,
    DeploymentLifecycleConflict,
    DeploymentStateStore,
    FinalizerLeaseBusy,
    collapse_secret_intent_rows,
    describe_gateway_targets_deleted,
    gateway_graph_membership,
    gateway_targets_deleted,
    manifest_delete_refusal,
    manifest_resource_key,
    note_gateway_targets_deleted,
)
from app.services.error_sanitizer import redact_secrets
from app.services.failure_inventory import claim_token_from_error_info, rows_from_error_info
from app.services.failure_inventory import strip as strip_failure_inventory
from app.services.gateway_deployer import (
    _authorize_tool_function_deletion,
    _release_shared_tool_lambda,
    assert_role_binding,
    delete_deployment_bound_secret,
    is_shared_tool_function,
    tool_binding_requirement,
)
from app.services.gateway_mutation_lock import gateway_mutation_lock, shared_lambda_lock
from app.services.gateway_name_claim import (
    GatewayNameClaimRefused,
    claim_account,
    hold_gateway_names_for_teardown,
    recovered_gateway_rows,
    unfinished_recovery,
    with_recovered_rows,
)
from app.services.harness_deployer import destroy_harness
from app.services.policy_lifecycle import delete_policy_confirmed
from app.services.resource_ownership import (
    ResourceDeletionRefused,
    assert_agentcore_resource_owned,
    assert_aoss_policy_owned,
    assert_guardrail_owned,
    assert_knowledge_base_owned,
    assert_vector_bucket_owned,
    delete_owned_credential_provider,
    delete_owned_iam_role,
    delete_owned_s3_object,
    get_owned_aoss_collection,
)

logger = logging.getLogger(__name__)


class _NoDeleterFor(Exception):
    """Raised when the manifest carries a resource type this dispatcher cannot delete.

    Deliberately NOT a silent no-op. The two dispatchers -- this one (failure path) and
    ``deployment_handler._delete_managed_resource`` (delete path) -- drifted apart
    precisely because an unhandled type was indistinguishable from a successful delete
    here. ``test_every_recorded_resource_type_has_a_deleter_on_both_paths`` pins the
    two sets together so the next new resource type cannot land on one path only.
    """

    def __init__(self, rtype: str) -> None:
        super().__init__(f"no deleter for recorded resource type {rtype!r}")
        self.rtype = rtype


class _ResourceRetained(Exception):
    """A cleanup deliberately left a resource in place for safety or uncertainty."""

    def __init__(self, rtype: str, rid: str, reason: str) -> None:
        super().__init__(reason)
        self.rtype = rtype
        self.rid = rid
        self.reason = reason


class _DeleteRejectedAfterAccept(Exception):
    """Raised when a delete was ACCEPTED and the service then failed it server-side.

    The one failure mode a best-effort ``except`` around the delete call cannot see.
    AgentCore answers ``DeleteGateway`` with 200 and ``status=DELETING``, and only then
    makes a forward-access-session call back out UNDER THE CALLER'S CREDENTIALS to remove
    the gateway's workload identity. When that is denied the gateway parks in ``FAILED``
    — after the API already returned — so nothing raises, and the resource was counted as
    cleaned while still sitting in the customer's account.

    Measured live 2026-09-21. That specific denial is fixed by the IAM grant in
    infra/stacks/platform/step_lambdas.py, but the grant only closes the instance; this
    class closes the *shape*, so the next forward-access-session permission the service
    needs on our behalf surfaces as a loud, attributable failure instead of a silent one.

    Carries the service's own reason string rather than a message of our own, because that
    string is the only place the missing action is ever named.
    """

    def __init__(self, rtype: str, rid: str, reasons: str) -> None:
        super().__init__(f"{rtype} {rid} accepted the delete and then failed it: {reasons}")
        self.rtype = rtype
        self.rid = rid
        self.reasons = reasons


# How long to wait for a gateway delete to reach a terminal state before giving up and
# reporting it unconfirmed.
#
# Both numbers are measured, not guessed. Live 2026-09-21, timing the poll from the same
# process that issued the delete: `DELETING` at t+0.00s and gone at t+1.51s. An independent
# probe on four more gateways at 0.25s resolution put the two terminal states at t+0.425s
# (FAILED) and t+0.856s (gone), so the window in which the answer is genuinely unknown is
# under ~1.5s. 6s is a ~4x margin over the slowest of those, and 1.5s between polls keeps
# it to four calls.
#
# It has to be a bounded wait rather than a single check: the SUCCESS path and the FAILURE
# path both pass through `DELETING` — measured `DELETING` at t=0.000s in BOTH arms of a
# with/without-permission A/B — so an immediate read cannot tell them apart and would
# report almost everything as unconfirmed. And it has to be bounded rather than patient,
# because this runs inside the 120s status_update Lambda. Failure cleanup shares one
# 105s monotonic deadline across all asynchronous resources so no single resource can
# consume the whole invocation and make later manifest rows silently disappear.
#
# GATEWAY ONLY, deliberately. Other asynchronous resources use the shared
# deletion_confirmation.wait_until_absent layer below:
#   * agent_runtime — `DeleteAgentRuntime` raises AccessDeniedException synchronously, so
#     the existing `except` already sees it. A poll would add nothing.
#   * harness — a successful delete took ~464s live. The status Lambda polls only until
#     the shared 105s deadline, then records a safety retention rather than claiming
#     success; the user/background delete path has the longer budget needed to confirm
#     terminal absence on retry.
_GATEWAY_DELETE_CONFIRM_BUDGET_S = 6.0
_GATEWAY_DELETE_POLL_INTERVAL_S = 1.5

# Terminal failure states and the fields the reason hides in.
#
# Both are wider than the gateway alone strictly needs, because the cost of being wrong in
# this direction is a *silent* detection: a check that spots the failure and then reports
# no reason is nearly as bad as not checking, since the reason string is the only place the
# missing IAM action is ever named.
#
# Measured on sibling AgentCore resources of the same family: the gateway reports `FAILED`
# with a `statusReasons` LIST, while a harness reports `DELETE_FAILED` with `statusReasons`
# null and the 403 in `failureReason`, a STRING. Same service family, same failure, three
# differences in how it is expressed. `endswith("FAILED")` covers both spellings with no
# false positives (`DELETING`, `READY`, `CREATING` do not end in FAILED), and reading both
# fields means whichever one the service populates survives to the log.
_DELETE_REASON_FIELDS = ("statusReasons", "failureReason")


def _delete_reason(got: dict, status: str) -> str:
    """The service's own explanation for a failed delete, from whichever field holds it.

    Never returns an empty string. A blank reason would produce the worst version of this
    check: a loud error that names a leaked resource and cannot say why, which is precisely
    the dead end the original silent failure created.
    """
    parts: list[str] = []
    for field in _DELETE_REASON_FIELDS:
        val = got.get(field)
        if not val:
            continue
        # statusReasons is a list, failureReason is a plain string. Both shapes are real —
        # they were measured on two resource types of the same service family.
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, (list, tuple)):
            parts.extend(str(v) for v in val if v)
        else:
            parts.append(str(val))
    if not parts:
        return f"(status {status}; the service gave no reason in {'/'.join(_DELETE_REASON_FIELDS)})"
    return "; ".join(parts)


def _confirm_gateway_deleted(ctrl, rid: str) -> None:
    """Verify a 200 from ``delete_gateway`` actually removed the gateway.

    Three outcomes, all reported honestly:

    * the gateway is gone (``get_gateway`` raises a not-found) — the delete worked;
    * it reached a terminal ``*FAILED`` state — the delete did NOT work, and
      ``_DeleteRejectedAfterAccept`` carries the service's reason so the caller neither
      counts it nor stays quiet;
    * it is still ``DELETING`` when the budget runs out — genuinely unknown, so say that
      and do not claim either way.

    A read failure or timeout is not proof of a leak, but neither is it proof of
    deletion. Both raise ``_ResourceRetained`` so the deployment row remains a
    durable retry handle and the completion count never calls an unknown outcome
    cleaned.
    """
    deadline = time.monotonic() + _GATEWAY_DELETE_CONFIRM_BUDGET_S
    status = "UNKNOWN"
    while True:
        try:
            got = ctrl.get_gateway(gatewayIdentifier=rid) or {}
            status = str(got.get("status") or "UNKNOWN").upper()
        except Exception as e:  # noqa: BLE001
            if _gone(e):
                return
            raise _ResourceRetained(
                "gateway",
                rid,
                (f"the delete was accepted but its final state could not be confirmed ({type(e).__name__})"),
            ) from e

        if status.endswith("FAILED"):
            # The reason is read off the SAME response that reported the failure, not a
            # second get_gateway. A re-read would be a wasted call that can also answer
            # differently, and losing the reason is the one outcome this must not produce.
            # Redacted like every other service string we log: a botocore message echoes
            # the request parameters that produced it (ARCC cnt_rHmO501l15qr2W).
            raise _DeleteRejectedAfterAccept("gateway", rid, redact_secrets(_delete_reason(got, status))[:600])

        if time.monotonic() >= deadline:
            raise _ResourceRetained(
                "gateway",
                rid,
                (
                    f"the delete was accepted but is still {status} after "
                    f"{_GATEWAY_DELETE_CONFIRM_BUDGET_S:.0f}s — NOT confirmed deleted"
                ),
            )
        time.sleep(_GATEWAY_DELETE_POLL_INTERVAL_S)


def _pool_is_gone(pool_id: str, cognito_client) -> bool:
    """True only when Cognito itself says the pool does not exist.

    Asked only after the pool's ownership could not be proven. A pool that is gone takes its
    app clients and resource servers with it, and a missing pool can never prove ownership, so
    these rows were retained on every retry (measured live 2026-10-01 on the teardown path,
    deployment_handler._delete_managed_resource; this abort path had the same three arms).
    Any other describe failure raises, so the cleanup is retried instead of guessed.
    """
    try:
        cognito_client.describe_user_pool(UserPoolId=pool_id)
    except Exception as exc:  # noqa: BLE001
        if _gone(exc):
            return True
        raise
    return False


def _gone(exc: Exception) -> bool:
    """True if the exception signals the resource is already deleted.

    ``nosuchentity`` and ``cannot be found`` are IAM's phrasing and were both missing.
    Measured live on deployment 959b2c60 (2026-09-21): a gateway role that had never
    been created (the deploy failed before it) produced
    ``NoSuchEntity ... The role with name AgentCoreGateway-<x> cannot be found`` and was
    logged as ``Auto-cleanup failed`` — an alarming line for a correct teardown, and it
    also kept the resource out of the cleaned count, so the completion ratio understated
    a complete cleanup. ``nosuchbucket``/``nosuchkey`` are S3's, reachable from the new
    s3_object arm.
    """
    msg = str(exc).lower()
    return any(
        x in msg
        for x in (
            "notfound",
            "not found",
            "does not exist",
            "no longer exists",
            "nosuchentity",
            "nosuchbucket",
            "nosuchkey",
            "cannot be found",
        )
    )


def _message_means_retained(message: str) -> bool:
    """Whether a legacy string-returning cleanup helper deliberately kept AWS state."""
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in (
            " kept ",
            "not deleting",
            "left in place",
            "ownership unprovable",
            "ownership unreadable",
            "policy unreadable",
        )
    )


def _manifest_completion_errors(state, event: dict) -> list[str]:
    """Return missing teardown handles that make SUCCEEDED unsafe to publish.

    Per-resource append failures are normally caught by
    ``resource_manifest_error``. This structural check catches the other class:
    a producer created a primary resource but forgot to record it at all.
    Legacy records (no version) predate this protocol and remain deletable
    through the live-ownership-gated fallback path.
    """
    record = state.model_dump(mode="json") if hasattr(state, "model_dump") else dict(state or {})
    if record.get("resource_manifest_version") != 1:
        return []

    errors: list[str] = []
    if record.get("resource_manifest_error") is True:
        errors.append("one or more resource-manifest writes failed")

    default_account = record.get("target_account_id") or event.get("target_account_id")
    default_region = (
        record.get("target_region")
        or event.get("target_region")
        or _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))
    )
    actual = {
        manifest_resource_key(
            row,
            default_account=default_account,
            default_region=default_region,
        )
        for row in record.get("created_resources") or []
    }

    expected: list[dict] = []

    def _expect(resource: dict) -> None:
        if resource.get("id") or resource.get("name"):
            expected.append(resource)

    deployment_mode = event.get("deployment_mode") or record.get("deployment_mode")
    if deployment_mode == "harness":
        _expect({"type": "harness", "id": event.get("harness_id")})
    else:
        _expect({"type": "agent_runtime", "id": event.get("runtime_id")})

    mcp_runtime_id = event.get("mcp_server_runtime_id")
    if mcp_runtime_id:
        _expect({"type": "agent_runtime", "id": mcp_runtime_id})

    gateway_result = event.get("gateway_result") or {}
    if gateway_result.get("gateway_id"):
        _expect({"type": "gateway", "id": gateway_result["gateway_id"]})
    elif gateway_result.get("gateway_provider") == "litellm" and gateway_result.get("litellm_base_url"):
        _expect(
            {
                "type": "litellm_gateway",
                "id": gateway_result["litellm_base_url"],
            }
        )

    for result_key, resource_type, id_keys in (
        ("memory_result", "memory", ("memory_id",)),
        ("policy_result", "policy_engine", ("engine_id",)),
        ("knowledge_base_result", "knowledge_base", ("kb_id",)),
        ("guardrails_result", "guardrail", ("guardrail_id",)),
    ):
        result = event.get(result_key) or {}
        identifier = next(
            (result.get(key) for key in id_keys if result.get(key)),
            None,
        )
        if identifier:
            _expect({"type": resource_type, "id": identifier})

    if event.get("s3_bucket") and event.get("s3_key"):
        _expect(
            {
                "type": "s3_object",
                "id": f"s3://{event['s3_bucket']}/{event['s3_key']}",
            }
        )

    for resource in expected:
        key = manifest_resource_key(
            resource,
            default_account=default_account,
            default_region=default_region,
        )
        if key not in actual:
            errors.append(f"missing {key[0]} teardown handle for {key[1]}")
    return errors


#: The step handlers the state machine runs strictly before runtime_configure, the
#: only step that creates the agent runtime (infra/stacks/platform/step_functions.py).
_STEPS_BEFORE_RUNTIME_CONFIGURE = frozenset(
    {
        "validate_step",
        "guardrails_step",
        "mcp_server_step",
        "knowledge_base_step",
        "gateway_step",
        "memory_step",
        "policy_step",
        "codegen_step",
        "iam_step",
    }
)


def _failed_step(event: dict) -> tuple[str, str]:
    """``(errorType, handler module)`` from the Catch's cause, or empty strings.

    The Catch writes ``$.error_info`` over whatever the input held, so this is what
    the failing Lambda raised. The outermost frame of its stack trace is the handler
    module. A cause with no trace -- a timeout, an out-of-memory kill, a States.*
    error -- yields ``("", "")``, which proves nothing.
    """
    error_info = event.get("error_info")
    if not isinstance(error_info, dict):
        return "", ""
    try:
        cause = json.loads(error_info.get("Cause") or "")
        error_type = str(cause.get("errorType") or "")
        first = str((cause.get("stackTrace") or [""])[0])
    except (TypeError, ValueError, AttributeError, IndexError):
        return "", ""
    match = re.search(r"/app/step_handlers/(\w+)\.py\"", first)
    return (error_type, match.group(1)) if match else ("", "")


def _failed_before_runtime_configure(event: dict) -> bool:
    """True only when the failing handler runs before the runtime can exist."""
    return _failed_step(event)[1] in _STEPS_BEFORE_RUNTIME_CONFIGURE


def _gateway_refused_before_side_effects(event: dict) -> bool:
    """The gateway step's own statement that it created nothing (F-67)."""
    return _failed_step(event) == ("GatewayRefusedBeforeSideEffects", "gateway_step")


def _supplemental_failure_resources(
    record: dict,
    event: dict,
    region: str,
) -> list[dict]:
    """Reconstruct cleanup rows carried in a failed step's durable outputs.

    These rows are used only when the versioned manifest was not sealed
    complete. Every destructive dispatcher still performs live ownership
    verification, so this recovery inventory cannot turn a persisted id into
    delete authority.
    """
    rows: list[dict] = []
    target_account = record.get("target_account_id") or event.get("target_account_id")

    def _add(
        resource: dict,
        *,
        created_by_deployment: bool = True,
    ) -> None:
        if not (resource.get("id") or resource.get("name")):
            return
        row = {
            **resource,
            "region": resource.get("region") or region,
            "created_by_deployment": bool(created_by_deployment),
        }
        if target_account and not row.get("account"):
            row["account"] = str(target_account)
        rows.append(row)

    deployment_mode = event.get("deployment_mode") or record.get("deployment_mode")
    if deployment_mode == "harness":
        harness_id = event.get("harness_id") or record.get("harness_id")
        _add({"type": "harness", "id": harness_id})
        harness_result = event.get("harness_result") or record.get("harness_result") or {}
        provider_name = harness_result.get("gateway_outbound_provider_name")
        if provider_name:
            _add(
                {
                    "type": "oauth2_credential_provider",
                    "name": provider_name,
                }
            )
        role_arn = harness_result.get("role_arn")
        if role_arn and not target_account:
            _add(
                {
                    "type": "iam_role",
                    "name": str(role_arn).rsplit("/", 1)[-1],
                }
            )
    else:
        runtime_id = event.get("runtime_id") or record.get("runtime_id")
        configure_result = event.get("configure_result") or {}
        # F-67, measured live: a deploy refused at the gateway step got a name-only
        # runtime row for a runtime that never existed. Every runtime dispatcher
        # proves ownership by id, so the row could delete nothing; its ownership read
        # was denied, and a clean refusal was recorded delete_retained. The name
        # stands in only when the runtime step may have run.
        if runtime_id or not _failed_before_runtime_configure(event):
            _add(
                {
                    "type": "agent_runtime",
                    "id": runtime_id,
                    "name": event.get("friendly_runtime_name") or "",
                },
                created_by_deployment=(configure_result.get("created_by_deployment") is True),
            )
        role_arn = event.get("role_arn")
        if role_arn and not target_account and not str(role_arn).endswith("-shared"):
            _add(
                {
                    "type": "iam_role",
                    "name": str(role_arn).rsplit("/", 1)[-1],
                },
                created_by_deployment=(event.get("role_created_by_deployment") is True),
            )

    mcp_runtime_id = event.get("mcp_server_runtime_id") or record.get("mcp_server_runtime_id")
    if mcp_runtime_id:
        _add({"type": "agent_runtime", "id": mcp_runtime_id})

    gateway_result = event.get("gateway_result") or record.get("gateway_result") or {}
    if gateway_result:
        from app.step_handlers.gateway_step import _gateway_manifest_resources

        rows.extend(
            _gateway_manifest_resources(
                region,
                gateway_result,
                skip_secret_arns=set(event.get("recorded_secret_arns") or []),
            )
        )

    memory_result = event.get("memory_result") or record.get("memory_result") or {}
    if memory_result.get("memory_id"):
        _add({"type": "memory", "id": memory_result["memory_id"]})
    memory_role_name = memory_result.get("memory_role_name")
    if memory_role_name and not target_account:
        _add(
            {
                "type": "iam_role",
                "name": memory_role_name,
            },
            created_by_deployment=(memory_result.get("memory_role_created_by_deployment") is True),
        )
    elif memory_result.get("memory_name") and not target_account:
        # Compatibility with records created before memory_result carried the
        # exact acknowledged role name.
        _add(
            {
                "type": "iam_role",
                "name": f"AgentCoreMemory-{memory_result['memory_name']}",
            }
        )

    policy_result = event.get("policy_result") or record.get("policy_result") or {}
    if policy_result.get("engine_id"):
        _add({"type": "policy_engine", "id": policy_result["engine_id"]})

    guardrails_result = event.get("guardrails_result") or record.get("guardrails_result") or {}
    if guardrails_result.get("created_by_flow") and guardrails_result.get("guardrail_id"):
        _add({"type": "guardrail", "id": guardrails_result["guardrail_id"]})

    kb_result = event.get("knowledge_base_result") or record.get("knowledge_base_result") or {}
    if kb_result.get("kb_id"):
        _add(
            {"type": "knowledge_base", "id": kb_result["kb_id"]},
            created_by_deployment=bool(kb_result.get("created_by_flow")),
        )
    kb_role_arn = kb_result.get("kb_role_arn")
    if kb_role_arn and not target_account:
        _add(
            {
                "type": "iam_role",
                "name": str(kb_role_arn).rsplit("/", 1)[-1],
            }
        )

    kb_config = event.get("knowledge_base_config") or record.get("knowledge_base_config") or {}
    if kb_config.get("vectorStoreType") == "opensearch_serverless" and not kb_config.get("opensearchCollectionArn"):
        _add(
            {
                "type": "oss_collection",
                "name": ("kb" + str(event.get("deployment_id") or "").replace("-", "").lower())[:32],
            }
        )

    if event.get("s3_bucket") and event.get("s3_key"):
        _add(
            {
                "type": "s3_object",
                "id": f"s3://{event['s3_bucket']}/{event['s3_key']}",
            }
        )
    return rows


#: The ONLY two reasons the server itself authors for a proven-no-resources failure: the
#: ValidateWorkflow handler's own refusal (``validate_step._rejected``), and the state machine's
#: ``NoResourcesCreatedOnInvalidInput`` Pass, for the case where the handler returned no usable
#: verdict at all. Membership is ENFORCED by ``_proven_no_resources`` with an exact string
#: comparison — no normalization, no case folding, no prefix match.
#:
#: An allowlist that exists but is not enforced is worse than none, because the comment then
#: claims a provenance the code never checks. Exact membership does couple this tuple to two
#: literals in two different trees (this file and infra/stacks/platform/step_functions.py), and
#: a drifted literal would silently stop the marker working — so that coupling is PINNED, rather
#: than left to reviewer attention, by two tests in ``test_no_resources_created_marker.py``: one
#: per emitter. The state machine's literal is compared by reading that file as text, because the
#: CDK tree cannot be imported from here and the CDK interpreter does not carry this package.
#: ``infra/tests/test_f55_deployment_input_gate.py`` separately proves the ASL reaches the marker
#: on both the false-verdict and thrown-task paths.
_SERVER_AUTHORED_NO_RESOURCE_REASONS = (
    "rejected at ValidateWorkflow",
    "rejected at ValidateWorkflow, before any resource-creating task",
)


def _proven_no_resources(event: dict) -> bool:
    """True only for the EXACT server-authored no-resources marker.

    F-55. The state machine and the validate handler both set
    ``$.no_resources_created = {"proven": true, "reason": "..."}`` on the only path where an
    empty resource manifest is a CERTAINTY rather than an unproven absence: a refusal at the
    first task, before any resource-creating state can run. Without it, cleanup records
    ``delete_retained`` with "could not prove that the empty manifest represented a deployment
    that created no resources", which sends an operator hunting for orphans that cannot exist.

    The shape check is deliberately unforgiving, because this flag SUPPRESSES cleanup and it is
    read out of state input. ARCC cnt_jljdNeOwgPnFx2: nothing outside the trusted server path
    may modify the authenticated context of an action. So:

    * ``proven`` must be literally ``True`` — ``is True``, not truthiness. ``1``, ``"true"``,
      ``"yes"`` and ``[0]`` are all truthy and all rejected; a loose check would let any caller
      who can influence the execution input turn cleanup off.
    * the marker must be a Mapping with EXACTLY the keys ``proven`` and ``reason``, so a marker
      carrying extra payload cannot smuggle anything through this path.
    * ``reason`` must be EXACTLY one of ``_SERVER_AUTHORED_NO_RESOURCE_REASONS``. A non-empty
      string was not enough: the reason is the only field carrying any provenance, so accepting
      an arbitrary one meant any caller-shaped marker was admitted. Its value is still never
      persisted — the record gets a server-authored sentence — because even an allowlisted
      string should not be the text an operator is shown.

    Anything that fails these checks is treated as if the marker were absent: cleanup proceeds
    exactly as before. Failing that way round matters — a rejected marker must never be able to
    skip cleanup, and a malformed one must never be able to fail the deployment either.
    """
    marker = event.get("no_resources_created")
    if not isinstance(marker, Mapping):
        return False
    if set(marker.keys()) != {"proven", "reason"}:
        return False
    if marker.get("proven") is not True:
        return False
    return marker.get("reason") in _SERVER_AUTHORED_NO_RESOURCE_REASONS


def _write_back_carried_rows(
    store,
    deployment_id: str,
    record: dict,
    carried: list[dict],
    *,
    finalizer_token: str | None = None,
) -> list[dict]:
    """Append each carried row the manifest lacks; return the ones DynamoDB acknowledged.

    *finalizer_token* fences every append: a superseded invocation writing rows
    into a manifest whose owner has already snapshotted it is the mirror image of
    the leak the lease closes.
    """
    written: list[dict] = []
    default_account = record.get("target_account_id")
    default_region = record.get("target_region")
    held = {
        manifest_resource_key(r, default_account=default_account, default_region=default_region)
        for r in record.get("created_resources") or []
        if isinstance(r, dict)
    }
    for row in carried:
        if manifest_resource_key(row, default_account=default_account, default_region=default_region) in held:
            continue
        try:
            store.record_resource_strict(deployment_id, row, finalizer_token=finalizer_token)
        except DeploymentLifecycleConflict:
            # This invocation no longer owns the row. Stop, and do NOT mark the
            # manifest incomplete: that flag belongs to the owner now, and
            # setting it would make a healthy deployment refuse to publish
            # SUCCEEDED because a dead invocation lost a race it should lose.
            logger.warning(
                "Stopped writing back carried rows for %s: another owner holds the deployment",
                deployment_id,
            )
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("A carried manifest row for %s was not written back: %s", deployment_id, type(exc).__name__)
            try:
                store.mark_resource_manifest_error(deployment_id, finalizer_token=finalizer_token)
            except Exception:  # noqa: BLE001
                logger.warning("Could not mark the manifest of %s incomplete", deployment_id)
        else:
            written.append(row)
    return written


def _record_gateway_handle(
    store: DeploymentStateStore,
    deployment_id: str,
    row: dict,
    *,
    finalizer_token: str | None = None,
) -> None:
    """A standing gateway whose manifest row would not land: name it in the record's
    ``gateway_result`` instead, and mark the manifest incomplete, so a later DELETE
    falls back to it and another owner's adoption pre-flight still sees it. The name
    claim, kept with the gateway's id on it, is the last handle if this fails too."""
    handle = {"gateway_id": str(row["id"])}
    if row.get("name"):
        handle["gateway_name"] = str(row["name"])
    try:
        store.mark_resource_manifest_error(deployment_id, finalizer_token=finalizer_token)
        store.record_gateway_handle(deployment_id, handle, finalizer_token=finalizer_token)
    except DeploymentLifecycleConflict:
        # Losing the finalizer token is a control-flow decision, not a
        # best-effort durability failure. Swallowing it would let a stale
        # invocation continue external cleanup after another owner took over.
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Gateway %s of %s is recorded only on its name claim: %s",
            handle["gateway_id"],
            deployment_id,
            type(exc).__name__,
        )


def _keep_claims_for(name_hold, durable: list, standing: set | None) -> None:
    """F-66f: which proven gateways keep their name claim. One a durable row names
    keeps it; one only this pass knew of keeps it only while it still stands (and the
    claim then records it), because a gone gateway's claim could never be erased and
    a standing one's name must not pass to another owner. *standing* None: unknown,
    so every gateway counts as standing."""
    ids = {str(r.get("id")) for r in durable if isinstance(r, dict) and r.get("type") == "gateway" and r.get("id")}
    if standing is None:
        still = set(name_hold.gateway_keys)
    else:
        still = {rid for rtype, rid, *_ in standing if rtype == "gateway"}
    name_hold.only_durable(ids, standing=still)


def _auto_cleanup_on_failure(
    store: DeploymentStateStore,
    deployment_id: str,
    event: dict,
    *,
    finalizer_token: str | None = None,
) -> None:
    """Best-effort cleanup of created_resources on deploy failure.

    Iterates the manifest in dependency order (primary resources before
    backing stores) and deletes each. Mirrors the logic in
    deployment_handler._delete_managed_resource but runs inline in the
    status_update step so failed deployments don't orphan AWS resources.

    All exceptions are logged but swallowed — this is a best-effort cleanup
    and must not change the deployment's failure status.

    This function is the reason the barrier needs a TOKEN and not just an expiry.
    It sets ``delete_status=deleting`` and then keeps writing -- manifest rows for
    what it left standing, and a terminal delete status -- so its own writes
    cannot be gated on the absence of a delete lifecycle. *finalizer_token* is
    what separates "the lease holder's cleanup" from "an unrelated late writer".
    Step Functions retries are serialized by the exclusive lease; a replacement
    raises ``FinalizerLeaseBusy`` until the bounded lease expires.
    """
    # Legacy rows can lack their own region. Their fallback must still be the
    # deployment target, not the status Lambda's home region, or failure cleanup
    # sends destructive calls to the wrong regional control plane.
    region = event.get("target_region") or _get_env(
        "APP_AWS_REGION",
        _get_env("AWS_REGION", "us-east-1"),
    )
    name_hold = None
    try:
        state = store.get(deployment_id)
        record = state.model_dump() if state else None
        resources = list((record or {}).get("created_resources") or [])
        if (record or {}).get("resource_manifest_version") == 1 and (record or {}).get(
            "resource_manifest_complete"
        ) is not True:
            resources.extend(
                _supplemental_failure_resources(
                    record or {},
                    event,
                    region,
                )
            )
        # Rows a failed step could not append, carried out in its exception because
        # the Catch keeps nothing else it built. Written back so a later DELETE, which
        # reads only created_resources, can still find them if this pass leaves any.
        written_back = _write_back_carried_rows(
            store,
            deployment_id,
            record or {},
            rows_from_error_info(event.get("error_info")),
            finalizer_token=finalizer_token,
        )
        resources.extend(rows_from_error_info(event.get("error_info")))
        # A gateway whose every record failed but its name claim: before the empty-
        # manifest proof below, which would otherwise call it "nothing was created".
        try:
            recovered = recovered_gateway_rows(
                deployment_id=deployment_id,
                owner_sub=event.get("owner_sub") or (record or {}).get("user_id"),
                account_for=lambda: claim_account(
                    (record or {}).get("target_account_id"), step_clients.client(event, "sts")
                ),
                region=(record or {}).get("target_region") or region,
            )
        except GatewayNameClaimRefused as exc:
            logger.warning("Auto-cleanup for %s deleted nothing: %s", deployment_id, exc)
            store.update_delete_status(
                deployment_id,
                "delete_failed",
                f"Automatic cleanup deleted nothing: {exc}",
                finalizer_token=finalizer_token,
            )
            return
        resources = with_recovered_rows(resources, recovered)
        # Secrets the manifest cannot name: a lost CreateSecret response, or a step
        # killed before its row. See unrecorded_deployment_secret_rows.
        from app.services.gateway_deployer import unrecorded_deployment_secret_rows

        discovered, discovery_failures = unrecorded_deployment_secret_rows(
            deployment_id=deployment_id,
            recorded_rows=resources,
            region=(record or {}).get("target_region") or region,
            secrets_client_for=lambda r: step_clients.client(event, "secretsmanager", region_name=r),
        )
        in_gateway_graph = gateway_graph_membership(resources)
        # A secret found by its tags could be the gateway's: its producer is unknown.
        resources.extend({**r, GATEWAY_GRAPH_FIELD: True} for r in discovered)
        for failed_region, failed_type in discovery_failures:
            logger.warning(
                "Auto-cleanup could not discover unrecorded secrets for %s in %s (%s)",
                deployment_id,
                failed_region,
                failed_type,
            )
        store.update_delete_status(
            deployment_id,
            "deleting",
            "Automatic cleanup started after deployment failure.",
            finalizer_token=finalizer_token,
        )
        if not resources:
            # WARNING, like every other outcome line in this cleanup path: at info
            # level it is discarded in the deployed Lambdas, and "the manifest was
            # empty" and "the cleanup never ran" are then indistinguishable in
            # CloudWatch — which is the difference between no orphans and unknown
            # orphans.
            logger.warning("No created_resources to clean up for %s", deployment_id)
            gateway_refused = _gateway_refused_before_side_effects(event)
            if _proven_no_resources(event) or gateway_refused:
                # An empty manifest PLUS a server-authored proof that nothing was created.
                # This is checked here, inside the empty-manifest branch, rather than as an
                # early return at the top of the function: if the marker were trusted while
                # the manifest held rows, a contradiction would silently suppress the deletion
                # of real resources. Reaching this point means the manifest is genuinely
                # empty, so the marker and the evidence agree.
                #
                # The gateway proof (F-67) is the gateway step's own errorType, which
                # the Catch writes over $.error_info, so the input cannot supply it.
                # Every step before the gateway records what it creates, and their
                # results feed the supplemental rows above, so empty here means none did.
                logger.warning(
                    "Deployment %s created no resources; recording an empty manifest as complete (proof: %s)",
                    deployment_id,
                    (event.get("no_resources_created") or {}).get("reason")
                    if _proven_no_resources(event)
                    else "gateway step refused before any side effect",
                )
                try:
                    # Marking the manifest COMPLETE is the substantive half. While it is
                    # incomplete, _supplemental_failure_resources above will keep inferring
                    # rows from the event on every later cleanup attempt — inventing resources
                    # for a deployment that provably created none.
                    store.update_status(
                        deployment_id,
                        DeploymentStatusEnum.FAILED,
                        resource_manifest_complete=True,
                        error_details=event.get("error") or None,
                        finalizer_token=finalizer_token,
                    )
                except Exception:
                    # A durability failure already recorded on the manifest makes
                    # update_status raise by design. Do not let that escape: this whole
                    # function is best-effort and must not change the failure status.
                    logger.exception("Could not mark the empty manifest complete for %s", deployment_id)
                unfinished = unfinished_recovery(deployment_id)
                if unfinished:
                    store.update_delete_status(
                        deployment_id,
                        "delete_failed",
                        unfinished,
                        finalizer_token=finalizer_token,
                    )
                    return
                store.update_delete_status(
                    deployment_id,
                    "deleted",
                    # Server-authored text. The marker's own reason string is logged above but
                    # never persisted here, because this message is rendered to an operator.
                    (
                        "No resources were created: the gateway step refused the deployment "
                        "before creating anything, and no earlier step recorded a resource. "
                        "Nothing to clean up."
                    )
                    if gateway_refused and not _proven_no_resources(event)
                    else (
                        "No resources were created: the deployment was refused at the first "
                        "step, before any resource-creating step ran. Nothing to clean up."
                    ),
                    finalizer_token=finalizer_token,
                )
                return
            store.update_delete_status(
                deployment_id,
                "delete_retained",
                (
                    "Automatic cleanup could not prove that the empty manifest "
                    "represented a deployment that created no resources."
                ),
                finalizer_token=finalizer_token,
            )
            return

        # Priority order: delete primary resources first, then backing stores
        late_cleanup_priority = 9
        priority = {
            "knowledge_base": 0,
            "agent_runtime": 1,
            "harness": 1,
            "gateway": 2,
            # Informational row for a customer-run LiteLLM proxy; deletes nothing.
            # In the gateway band purely for symmetry with the delete path's map.
            "litellm_gateway": 2,
            # F-74b: provenance row, deletes nothing. Ordered BEFORE the gateway (2) even so,
            # because that is where a refcounted per-target delete will have to run -- a
            # target must go before the gateway it is attached to, never after.
            "gateway_target": 1,
            "policy": 1,
            "policy_engine": 2,
            "lambda": 5,
            "memory": 6,
            "guardrail": 6,
            "oauth2_credential_provider": 7,
            "api_key_credential_provider": 7,
            "s3_vectors_bucket": 8,
            # Must outlive the knowledge base (0) that queries it.
            "oss_collection": 8,
            "iam_role": 9,
            # Before the pool (9) so an owned pool's clients go first, and after the
            # gateway (2) whose customJWTAuthorizer pins this client id. Same order
            # as deployment_handler._run_delete_cleanup's map, deliberately.
            "cognito_app_client": 8,
            # After the client (8) — the resource-server guard is "no client is still
            # using this scope", true only once ours is gone.
            "cognito_resource_server": 9,
            "cognito_user_pool": 9,
            "secret": late_cleanup_priority,
            "s3_object": 9,
        }
        ordered = sorted(
            collapse_secret_intent_rows(
                resources,
                default_account=(record or {}).get("target_account_id"),
                default_region=(record or {}).get("target_region") or region,
            ),
            key=lambda r: priority.get(str(r.get("type")), 4),
        )
        # F-66f: hold every gateway name in the manifest before the co-residency
        # snapshot below, and until the outcome is recorded. A deploy still creating on
        # the name has no row for that snapshot to see; the lease is its one record.
        try:
            name_hold = hold_gateway_names_for_teardown(
                ordered,
                owner_sub=event.get("owner_sub") or (record or {}).get("user_id"),
                deployment_id=deployment_id,
                default_account=(record or {}).get("target_account_id"),
                default_region=(record or {}).get("target_region") or region,
                sts_client_for=lambda: step_clients.client(event, "sts"),
                ctrl_for=lambda r: step_clients.client(event, "bedrock-agentcore-control", region_name=r),
                prove_owned=lambda r, gid: assert_agentcore_resource_owned(
                    step_clients.client(event, "bedrock-agentcore-control", region_name=r), "gateway", gid, r
                ),
                fallback=(record or {}).get("gateway_result") or event.get("gateway_result"),
                token=claim_token_from_error_info(event.get("error_info")),
            )
        except GatewayNameClaimRefused as exc:
            logger.warning("Auto-cleanup for %s deleted nothing: %s", deployment_id, exc)
            store.update_delete_status(
                deployment_id,
                "delete_failed",
                f"Automatic cleanup deleted nothing: {exc}",
                finalizer_token=finalizer_token,
            )
            return
        # Refresh once per cleanup invocation, then reuse the snapshot for every
        # manifest row.  This avoids both stale authority across retries and one
        # full-table scan per resource.
        store.reset_manifest_reference_cache(deployment_id)
        # Skip a (type, id) already deleted in this pass, exactly as the delete path
        # does with its `seen_mres` set (deployment_handler.py:2155-2160).
        #
        # The manifest genuinely does carry duplicates: runtime_configure_step records
        # the agent_runtime row, and runtime_launch_step deliberately re-records the
        # same one. Without this set a failed deploy issued delete_agent_runtime twice
        # for one runtime, which is worse than wasteful — the second call lands while
        # the first delete is still in progress, and that is not a "gone" error, so it
        # was reported as `Auto-cleanup failed` for a runtime that was in fact being
        # deleted correctly. It also made the completion line read "6/6 resources" for
        # five real resources, so the count could never be used as evidence.
        default_account = (record or {}).get("target_account_id")
        default_region = (record or {}).get("target_region") or region
        # Duplicate rows are real (configure + launch both record a runtime).
        # If any producer says the resource was reused, that conservative fact
        # must dominate a sibling row that says created or carries legacy
        # provenance; processing whichever duplicate happened to appear first
        # would make list order decide deletion authority.
        reused_keys = {
            manifest_resource_key(
                res,
                default_account=default_account,
                default_region=default_region,
            )
            for res in ordered
            if res.get("created_by_deployment") is False
        }
        seen: set[tuple[str, str, str, str]] = set()
        # Rows still standing after this pass (not deleted, not handed off).
        standing: set[tuple[str, str, str, str]] = set()
        # Memory ids attempted this pass and not (yet) proven absent.
        unconfirmed_memories: set[str] = set()
        cleaned = 0
        protected = 0
        failed = 0
        # F-66c: rows left for another deployment that still lists them. They do not
        # make this tombstone delete_retained -- that would protect them back, and the
        # other deployment's teardown would keep them forever -- unless a runtime or
        # harness of ours may still be running and calling them.
        handed_off = 0
        handed_off_gateways: list[str] = []
        kept_types: set[str] = set()
        # A gateway that still stands after its delete was attempted or refused:
        # its authorizer pins the app client, its targets call the tool Lambdas and
        # credential providers, and a later DELETE needs the graph whole to finish it.
        # So nothing in that graph is touched after it; unrelated rows still are.
        frozen_gateway: str | None = None
        partial_targets: dict[str, list[str]] = {}
        frozen = 0
        # Leave time for the completion summary and durable delete-status write.
        # Individual async-resource pollers share this deadline, so a slow
        # Harness/KB cannot consume the whole 120s Lambda timeout and strand the
        # rest of the manifest without an outcome record.
        cleanup_event = {
            **event,
            "deployment_id": event.get("deployment_id") or deployment_id,
            "_cleanup_deadline_monotonic": time.monotonic() + 105.0,
        }
        for res in ordered:
            key = manifest_resource_key(
                res,
                default_account=default_account,
                default_region=default_region,
            )
            if key in seen:
                continue
            seen.add(key)
            if key in reused_keys and res.get("created_by_deployment") is not False:
                res = {**res, "created_by_deployment": False}
            if frozen_gateway and in_gateway_graph(res):
                kept_types.add(key[0])
                standing.add(key)
                protected += 1
                frozen += 1
                logger.error(
                    "Auto-cleanup left %s %s for %s untouched: gateway %s was not deleted, so its graph stays whole",
                    key[0],
                    key[1],
                    deployment_id,
                    frozen_gateway,
                )
                continue
            # F-56: memories (band 6) run before roles (band 9). A memory not proven
            # absent keeps its execution role; the manifest has no role->memory link, so
            # bind by the recorded role name and the AgentCoreMemory- prefix.
            if res.get("type") == "iam_role" and unconfirmed_memories:
                _role = str(res.get("name") or res.get("id") or "")
                _recorded = ((record or {}).get("memory_result") or {}).get("memory_role_name")
                if _role and (_role == _recorded or _role.startswith("AgentCoreMemory-")):
                    protected += 1
                    standing.add(key)
                    logger.warning(
                        "Auto-cleanup retained iam_role %s for %s: memory %s is not confirmed deleted",
                        _role,
                        deployment_id,
                        sorted(unconfirmed_memories),
                    )
                    continue
            if res.get("type") == "memory":
                unconfirmed_memories.add(key[1])
            refusal = manifest_delete_refusal(
                store,
                deployment_id,
                res,
                target_account_id=default_account,
                target_region=default_region,
            )
            if refusal == CO_RESIDENT_REFUSAL:
                handed_off += 1
                kept_types.add(key[0])
                if res.get("type") == "gateway":
                    handed_off_gateways.append(str(res.get("id")))
                logger.warning(
                    "Auto-cleanup handed off %s %s for %s: %s",
                    key[0],
                    key[1],
                    deployment_id,
                    refusal,
                )
                continue
            if refusal:
                kept_types.add(key[0])
                standing.add(key)
                protected += 1
                if key[0] == "gateway":
                    frozen_gateway = key[1]
                logger.warning(
                    "Auto-cleanup retained %s %s for %s: %s",
                    key[0],
                    key[1],
                    deployment_id,
                    refusal,
                )
                continue
            try:
                try:
                    _cleanup_resource(res, region, cleanup_event)
                except Exception as e:
                    if gateway_targets_deleted(e):
                        partial_targets[key[1]] = gateway_targets_deleted(e)
                        logger.error(
                            "Auto-cleanup for %s: %s",
                            deployment_id,
                            describe_gateway_targets_deleted(key[1], partial_targets[key[1]]),
                        )
                    raise
                cleaned += 1
                unconfirmed_memories.discard(key[1])
            except _NoDeleterFor as e:
                # NOT counted as cleaned, and said out loud: the resource is still in
                # the account and nobody tried to delete it.
                kept_types.add(key[0])
                standing.add(key)
                logger.error(
                    "Auto-cleanup has NO deleter for type %r (%s) — left in the account for %s",
                    e.rtype,
                    key[1],
                    deployment_id,
                )
                failed += 1
            except _ResourceRetained as e:
                kept_types.add(key[0])
                standing.add(key)
                protected += 1
                logger.warning(
                    "Auto-cleanup retained %s %s for %s: %s",
                    e.rtype,
                    e.rid,
                    deployment_id,
                    e.reason,
                )
            except _DeleteRejectedAfterAccept as e:
                # Deliberately before the generic handler below, and deliberately NOT
                # counted: the delete was accepted and then failed, so the resource is
                # still there. Before this arm existed the call returned cleanly and this
                # was `cleaned += 1` — a leaked gateway reported as a cleaned one.
                #
                # ERROR, not warning: this is the only notice that a resource survived a
                # teardown that believed it succeeded, and the service's own reason names
                # the permission that has to be added to fix it.
                logger.error(
                    "Auto-cleanup did NOT delete %s %s for %s — the service accepted the delete and then failed it: %s",
                    e.rtype,
                    e.rid,
                    deployment_id,
                    e.reasons,
                )
                kept_types.add(key[0])
                standing.add(key)
                failed += 1
            except Exception as e:
                if _gone(e):
                    cleaned += 1
                    unconfirmed_memories.discard(key[1])
                else:
                    kept_types.add(key[0])
                    standing.add(key)
                    failed += 1
                    logger.warning("Auto-cleanup failed for %s: %s", res, str(e)[:200])
            if key in standing and key[0] == "gateway":
                frozen_gateway = key[1]
        # len(seen), not len(resources): the denominator has to be the number of
        # distinct resources actually attempted, or a duplicated row makes a complete
        # cleanup look partial (and a skipped duplicate look like a failure).
        #
        # WARNING level, not info, for the same reason as the failure cause below:
        # logger.info is discarded in the deployed Lambdas (the module logger is at
        # NOTSET and the root sits at WARNING), so this line did not exist in
        # production at all. Measured live 2026-09-21: CloudWatch for a real
        # failure-path cleanup held only the `[ERROR] ... full cause (redacted)` line,
        # and the only way to establish what the cleanup had actually done was to
        # re-enumerate AWS by hand. This is the sole record that a destructive pass ran
        # over a customer's account, so it has to be readable.
        #
        # Read it as "delete accepted", not "resource gone", and treat the difference as
        # real rather than pedantic. `cleaned` counts calls that returned without raising,
        # and AgentCore can accept a delete and then fail it server-side — see the
        # DeleteWorkloadIdentity note in infra/stacks/platform/step_lambdas.py, where
        # DeleteGateway returned 200 and the gateway then parked in FAILED because a
        # forward-access-session call made under the caller's own credentials was denied
        # AFTER the response. Both halves of the count were wrong there: the resource was
        # not deleted, and the number said it was.
        #
        # That gap IS closable from here, contrary to what this comment first claimed:
        # get_gateway after the delete returns FAILED with the reason in statusReasons,
        # and that state is terminal (measured stable over 2.5 min). The IAM grant is the
        # primary fix because it removes the cause, but verifying instead of trusting the
        # 200 is what would make the NEXT forward-access-session gap visible rather than
        # silent, and is tracked as its own finding.
        logger.warning(
            "Auto-cleanup completed for %s: %d/%d resources (protected=%d, failed=%d, handed_off=%d)",
            deployment_id,
            cleaned,
            len(seen),
            protected,
            failed,
            handed_off,
        )
        # An unreadable region may hold a secret nothing named, so it is never "deleted".
        protected += len(discovery_failures)
        if handed_off and kept_types & {"agent_runtime", "harness"}:
            # Our runtime may still call it: a retention, and the claim stays with it.
            protected += handed_off
            handed_off_gateways = []
        if failed:
            delete_status = "delete_failed"
        elif protected:
            delete_status = "delete_retained"
        else:
            delete_status = "deleted"
        # A carried row that never landed and whose resource still stands is named
        # nowhere a later DELETE reads: retry its write now, the handle that DELETE needs.
        durable = [*((record or {}).get("created_resources") or []), *written_back]
        durable_keys = {
            manifest_resource_key(r, default_account=default_account, default_region=default_region)
            for r in durable
            if isinstance(r, dict)
        }
        for row in rows_from_error_info(event.get("error_info")):
            row_key = manifest_resource_key(row, default_account=default_account, default_region=default_region)
            if row_key in standing and row_key not in durable_keys:
                try:
                    store.record_resource_strict(deployment_id, row, finalizer_token=finalizer_token)
                except DeploymentLifecycleConflict:
                    # Ownership changed hands mid-cleanup. The new owner's teardown
                    # will re-derive this row from the error info it was handed;
                    # writing a gateway handle onto its record instead would
                    # overwrite state it is actively using.
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "Auto-cleanup left %s %s standing for %s and could not record it: %s",
                        row_key[0],
                        row_key[1],
                        deployment_id,
                        type(exc).__name__,
                    )
                    if row.get("type") == "gateway":
                        _record_gateway_handle(store, deployment_id, row, finalizer_token=finalizer_token)
                else:
                    durable.append(row)
                    durable_keys.add(row_key)
        _keep_claims_for(name_hold, durable, standing)
        # F-66f: the claim goes only with every gateway gone; anything kept keeps it,
        # except a claim created here on a gateway handed off (see settle).
        name_hold.settle(
            erase=delete_status == "deleted" and "gateway" not in kept_types, handed_off=handed_off_gateways
        )
        # A retained or failed pass is retried anyway; a "deleted" one never is, so it
        # may not strand recovery evidence or a pointer no TTL will remove.
        unfinished = unfinished_recovery(deployment_id)
        if unfinished and delete_status == "deleted":
            delete_status = "delete_failed"
        else:
            unfinished = None
        store.update_delete_status(
            deployment_id,
            delete_status,
            (
                (f"{unfinished} " if unfinished else "")
                + f"Automatic cleanup handled {cleaned}/{len(seen)} resources; protected={protected}, "
                f"failed={failed}, left for another deployment={handed_off}."
                + (
                    f" Gateway {frozen_gateway} was not deleted, so {frozen} resource(s) of its graph were left intact."
                    if frozen
                    else ""
                )
                + "".join(
                    f" The {describe_gateway_targets_deleted(gw, ids)}; the gateway no longer routes those tools."
                    for gw, ids in sorted(partial_targets.items())
                )
            ),
            finalizer_token=finalizer_token,
        )
    except DeploymentLifecycleConflict:
        # A superseded invocation must write NOTHING further, least of all a
        # terminal status. `deleted` is final -- `_deleted_is_final` answers
        # "already deleted" to every later attempt -- so a stale pass publishing
        # it over a live retry is precisely how the measured leak became
        # permanent. Returning here is the whole point of the fence: the name
        # claims are deliberately left as they are, because the owner holds them.
        logger.warning(
            "Auto-cleanup for %s stopped: this invocation no longer owns the deployment",
            deployment_id,
        )
        return
    except Exception as e:
        logger.warning("Auto-cleanup error for %s: %s", deployment_id, str(e)[:200])
        if name_hold is not None:
            # What this pass deleted is unknown, so every gateway counts as standing.
            _keep_claims_for(name_hold, (record or {}).get("created_resources") or [], None)
            name_hold.settle(erase=False)
            unfinished_recovery(deployment_id)  # reclaim if it can; this pass is delete_failed anyway
        try:
            store.update_delete_status(
                deployment_id,
                "delete_failed",
                f"Automatic cleanup could not complete ({type(e).__name__}).",
                finalizer_token=finalizer_token,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Could not persist automatic cleanup failure for %s",
                deployment_id,
                exc_info=True,
            )


def _shared_pool_children_client(pool_id: str, res_region: str, event: dict):
    """The Cognito client for a gateway's client or scope in *pool_id*.

    The shared gateway-auth pool is a platform-account resource, so its children are
    removed with platform credentials in the pool's region even when the event
    targets another account. Every other pool stays on the event's target session.
    """
    from app.services.gateway_deployer import (
        _create_platform_cognito_client,
        _pool_region,
        is_platform_owned_user_pool,
    )

    if is_platform_owned_user_pool(pool_id):
        return _create_platform_cognito_client(_pool_region(pool_id))
    return step_clients.client(event, "cognito-idp", region_name=res_region)


def _cleanup_resource(res: dict, region: str, event: dict) -> None:
    """Delete a single resource from the manifest. Raises on failure.

    Phase 7 (opt-in) cross-account teardown: a resource created in a target
    account records its ``account`` (+ ``region``) in the manifest. Delete is a
    SEPARATE request that doesn't carry the original deploy's SFN event, so we
    reconstruct the target from the manifest record itself — the clients then
    assume the same cross-account role that created the resource. For
    same-account resources (no ``account`` recorded) this is the passed event /
    default session — unchanged behavior.
    """
    rtype = str(res.get("type", ""))
    rid = res.get("id") or ""
    rname = res.get("name") or ""
    res_region = res.get("region") or region
    # Prefer the resource's own recorded target (self-contained teardown); fall
    # back to the caller's event (the failure-path auto-cleanup passes the live
    # deploy event, which already carries the target).
    res_account = res.get("account")
    confirmation_deadline = event.get("_cleanup_deadline_monotonic")
    if res_account:
        # Preserve target_role_arn and the deployment id from the live event.
        # Replacing the dict discarded both: cross-account cleanup could assume
        # the wrong/default role, and exact-bound secret deletion had no proof
        # of which deployment owned the credential.
        event = {
            **event,
            "target_account_id": res_account,
            "target_region": res_region,
        }

    def _prove_live_ownership(proof):
        try:
            return proof()
        except ResourceDeletionRefused as exc:
            raise _ResourceRetained(
                rtype,
                str(rid or rname),
                str(exc),
            ) from exc

    if rtype == "gateway":
        ctrl = step_clients.client(event, "bedrock-agentcore-control", region_name=res_region)
        targets_deleted: list[str] = []
        try:
            # F-66e: under the gateway's write lock from the ownership read to the
            # proof of absence, so no update computed before the delete lands after it.
            with gateway_mutation_lock(ctrl, res_region, rid) as gw_lock:
                _prove_live_ownership(
                    lambda: assert_agentcore_resource_owned(
                        ctrl,
                        "gateway",
                        rid,
                        res_region,
                    )
                )
                # DeleteGateway rejects gateways that still have targets ("has targets
                # associated with it") — a failed deploy that got past target creation
                # would leak the gateway forever. Re-list every retry so transient target
                # failures and eventual-consistency lag can converge.
                for attempt in range(8):
                    targets = list_all(
                        ctrl,
                        "list_gateway_targets",
                        item_keys=("items", "gatewayTargetSummaries"),
                        request={"gatewayIdentifier": rid, "maxResults": 100},
                    )
                    for target in targets:
                        target_id = target.get("targetId")
                        if not target_id:
                            continue
                        try:
                            ctrl.delete_gateway_target(
                                gatewayIdentifier=rid,
                                targetId=target_id,
                            )
                            targets_deleted.append(str(target_id))
                        except Exception as exc:  # noqa: BLE001
                            if not _gone(exc):
                                logger.debug(
                                    "delete_gateway_target %s on %s failed",
                                    target_id,
                                    rid,
                                    exc_info=True,
                                )
                    try:
                        # The 200 is not the answer. DeleteGateway returns before the
                        # service has finished, and it can fail afterwards under our own
                        # credentials — see _DeleteRejectedAfterAccept. The lock's
                        # confirm verifies instead of assuming.
                        gw_lock.delete(
                            lambda: _confirm_gateway_deleted(ctrl, rid), terminal=(_DeleteRejectedAfterAccept,)
                        )
                        break
                    except ClientError as exc:
                        if _gone(exc):
                            return
                        if "target" in str(exc).lower() and attempt < 7:
                            time.sleep(5)
                            continue
                        raise
        except Exception as exc:
            if not _gone(exc):
                note_gateway_targets_deleted(exc, targets_deleted)
            raise
        # The name claim is settled by _auto_cleanup_on_failure once the whole graph is (F-66f).
    elif rtype == "agent_runtime":
        ctrl = step_clients.client(event, "bedrock-agentcore-control", region_name=res_region)
        _prove_live_ownership(
            lambda: assert_agentcore_resource_owned(
                ctrl,
                "agent_runtime",
                rid,
                res_region,
            )
        )
        ctrl.delete_agent_runtime(agentRuntimeId=rid)
        # F-08: the 200 is an acceptance. Confirm absence inside the pass's shared deadline;
        # a terminal *FAILED propagates as the failure it is, an unconfirmed state is a retention.
        _prove_live_ownership(
            lambda: wait_until_absent(
                resource_label=f"runtime {rid}",
                read=lambda: ctrl.get_agent_runtime(agentRuntimeId=rid),
                max_attempts=36,
                delay_seconds=5.0,
                deadline_monotonic=confirmation_deadline,
            )
        )
    elif rtype == "harness":
        ctrl = step_clients.client(event, "bedrock-agentcore-control", region_name=res_region)
        _prove_live_ownership(
            lambda: assert_agentcore_resource_owned(
                ctrl,
                "harness",
                rid,
                res_region,
            )
        )
        result = destroy_harness(
            rid,
            res_region,
            agentcore_ctrl=ctrl,
            confirmation_deadline=confirmation_deadline,
        )
        if result.get("retained"):
            raise _ResourceRetained(
                rtype,
                rid,
                result.get("note") or "harness deletion was not confirmed",
            )
        if not result.get("success", False):
            raise RuntimeError(result.get("error") or "harness deletion failed")
    elif rtype == "policy":
        # F-G09-003 (mirror of deployment_handler's arm; the parity test keeps them aligned).
        engine = res.get("engine_id") or res.get("policy_engine_id") or ""
        if not engine or not rid:
            raise _ResourceRetained(rtype, str(rid or rname), "policy row lacks its engine id")
        if res.get("created_by_deployment") is not True:
            logger.warning("policy %s/%s left in place (adopted/shared: not created by this deployment)", engine, rid)
        else:
            ctrl = step_clients.client(event, "bedrock-agentcore-control", region_name=res_region)
            _prove_live_ownership(lambda: assert_agentcore_resource_owned(ctrl, "policy_engine", engine, res_region))
            _prove_live_ownership(
                lambda: delete_policy_confirmed(ctrl, engine, rid, deadline_monotonic=confirmation_deadline)
            )
    elif rtype == "policy_engine":
        ctrl = step_clients.client(event, "bedrock-agentcore-control", region_name=res_region)
        _prove_live_ownership(
            lambda: assert_agentcore_resource_owned(
                ctrl,
                "policy_engine",
                rid,
                res_region,
            )
        )
        _prove_live_ownership(
            lambda: delete_policy_engine_confirmed(
                ctrl,
                rid,
                deadline_monotonic=confirmation_deadline,
            )
        )
    elif rtype == "guardrail":
        bedrock = step_clients.client(event, "bedrock", region_name=res_region)
        _prove_live_ownership(lambda: assert_guardrail_owned(bedrock, rid, res_region))
        bedrock.delete_guardrail(guardrailIdentifier=rid)
    elif rtype == "knowledge_base":
        ba = step_clients.client(event, "bedrock-agent", region_name=res_region)
        _prove_live_ownership(lambda: assert_knowledge_base_owned(ba, rid, res_region))
        # Delete data sources first
        try:
            for ds in list_all(
                ba,
                "list_data_sources",
                item_keys=("dataSourceSummaries",),
                request={"knowledgeBaseId": rid, "maxResults": 100},
            ):
                try:
                    ba.delete_data_source(knowledgeBaseId=rid, dataSourceId=ds["dataSourceId"])
                except Exception:  # noqa: BLE001 — KB cascade delete removes remaining data sources
                    logger.debug("delete_data_source on KB %s failed", rid, exc_info=True)
        except Exception:  # noqa: BLE001 — best-effort pre-clean; KB delete surfaces real failures
            logger.debug("list_data_sources on KB %s failed", rid, exc_info=True)
        ba.delete_knowledge_base(knowledgeBaseId=rid)
        _prove_live_ownership(
            lambda: wait_until_absent(
                resource_label=f"knowledge base {rid}",
                read=lambda: ba.get_knowledge_base(knowledgeBaseId=rid),
                max_attempts=24,
                delay_seconds=5,
                deadline_monotonic=confirmation_deadline,
            )
        )
    elif rtype == "s3_vectors_bucket":
        s3v = step_clients.client(event, "s3vectors", region_name=res_region)
        bname = rname or rid
        _prove_live_ownership(lambda: assert_vector_bucket_owned(s3v, bname, res_region))
        _prove_live_ownership(
            lambda: delete_vector_bucket_confirmed(
                s3v,
                bname,
                deadline_monotonic=confirmation_deadline,
            )
        )
    elif rtype == "oss_collection":
        # A STANDING billable resource (~$350/mo for the minimum 2 OCUs). This arm
        # existed only on the delete path, so a deploy that provisioned the collection
        # in CreateKnowledgeBase and then failed in any later state left it running --
        # and, because the unknown type fell through to nothing, the completion line
        # still read "N/N resources". Mirrors _delete_managed_resource exactly:
        # delete_collection removes the indexes, then the three policies we created.
        aoss = step_clients.client(event, "opensearchserverless", region_name=res_region)
        cname = rname or rid
        detail = _prove_live_ownership(
            lambda: get_owned_aoss_collection(
                aoss,
                cname,
                res_region,
                event.get("deployment_id") or None,
            )
        )
        if detail:
            aoss.delete_collection(id=detail["id"])
            _prove_live_ownership(
                lambda: wait_until_absent(
                    resource_label=f"OpenSearch Serverless collection {cname}",
                    read=lambda: aoss.batch_get_collection(names=[cname]),
                    absent_response=lambda response: not (response.get("collectionDetails") or []),
                    max_attempts=60,
                    delay_seconds=5,
                    deadline_monotonic=confirmation_deadline,
                )
            )
        retained_policies: list[str] = []
        for pname, ptype in (
            (f"{cname}-acc"[:32], "data"),
            (f"{cname}-net"[:32], "network"),
            (f"{cname}-enc"[:32], "encryption"),
        ):
            try:
                assert_aoss_policy_owned(
                    aoss,
                    pname,
                    ptype,
                    res_region,
                    event.get("deployment_id") or None,
                )
                if ptype == "data":
                    aoss.delete_access_policy(name=pname, type=ptype)
                    read_policy = partial(
                        aoss.get_access_policy,
                        name=pname,
                        type=ptype,
                    )
                else:
                    aoss.delete_security_policy(name=pname, type=ptype)
                    read_policy = partial(
                        aoss.get_security_policy,
                        name=pname,
                        type=ptype,
                    )
                wait_until_absent(
                    resource_label=(f"OpenSearch Serverless {ptype} policy {pname}"),
                    read=read_policy,
                    max_attempts=15,
                    delay_seconds=2,
                    deadline_monotonic=confirmation_deadline,
                )
            except ResourceDeletionRefused:
                retained_policies.append(pname)
            except Exception as exc:  # noqa: BLE001
                if not _gone(exc):
                    raise
        if retained_policies:
            raise _ResourceRetained(
                rtype,
                cname,
                "ownership could not be proven for policies: " + ", ".join(retained_policies),
            )
    elif rtype == "s3_object":
        # A staged connector OpenAPI spec (gateway_step records one row per spec too
        # large to inline). rid is an s3://bucket/key URI.
        if rid.startswith("s3://"):
            _b, _, _k = rid[5:].partition("/")
            if _b and _k:
                expected_owner = res_account or event.get("target_account_id")
                s3 = step_clients.client(
                    event,
                    "s3",
                    region_name=res_region,
                )
                # F-60: every version by id, each proven by its own tags; a bare
                # delete_object on a versioned bucket only writes a marker.
                _prove_live_ownership(
                    lambda: delete_owned_s3_object(
                        s3,
                        _b,
                        _k,
                        region=res_region,
                        deployment_id=event.get("deployment_id"),
                        expected_bucket_owner=(str(expected_owner) if expected_owner else None),
                    )
                )
                return
        raise ValueError(f"Malformed s3_object manifest identity: {rid!r}")
    elif rtype == "litellm_gateway":
        # Informational only: a LiteLLM gateway is the CUSTOMER's own proxy and this
        # deploy created no AWS resource for it. The one thing deploy did create, the
        # virtual-key secret, is a separate "secret" row. The arm exists so the row is
        # deliberate rather than an unrecognized type -- which is now an error below.
        # WARNING: a deliberate non-deletion is exactly the outcome an operator needs
        # to be able to read back, and info is discarded in the deployed Lambdas.
        logger.warning("litellm gateway %s is external — nothing to delete", rid)
    elif rtype == "gateway_target":
        # F-74b. Provenance only: the row records which deployment asked for this target so
        # a later deploy can tell it from a target it must not touch. The "gateway" arm
        # deletes every target on a gateway it is about to delete, and a gateway retained
        # for a co-resident deployment keeps its targets by design. Removing only this
        # deployment's own targets from a retained gateway needs a reference count over
        # these rows, and the rows have to exist in the population before a count over them
        # can be trusted -- a first run that sees no co-resident reference deletes the
        # co-resident's tool.
        #
        # Warning, not info: a deliberate non-deletion is exactly what an operator has to be
        # able to read back, and info is discarded in the deployed Lambdas.
        logger.warning("gateway target %s is deleted with its gateway — nothing to delete here", rid)
    elif rtype == "cognito_app_client":
        # One gateway's app client inside the SHARED platform pool — the credential
        # a failed shared-pool deploy would otherwise leave behind. The pool arm
        # below deliberately refuses to touch that pool, and gateway_step records no
        # pool row for it, so without this arm the client survived every failed
        # deploy: measured live, one still in us-east-1_qiYLOs3Ij with its secret
        # mintable and the invoke scope attached.
        #
        # This arm must accept SHARED_EXACT where the pool arm must refuse it — the
        # two answers are about different resources. A client id alone is still not
        # authority, so the container is re-verified live through the SAME assumed-role
        # client for the same reason the pool arm threads it (a customer-account deploy
        # must not be classified against the platform account).
        from app.services.gateway_deployer import (
            POOL_OWNED_BY_STACK,
            POOL_SHARED_EXACT,
            classify_user_pool,
        )

        _pool = str(res.get("pool_id") or "")
        if not _pool:
            raise _ResourceRetained(
                rtype,
                rid,
                "the manifest has no pool_id, so the client container cannot be verified",
            )
        cog = _shared_pool_children_client(_pool, res_region, event)
        if classify_user_pool(_pool, cognito_client=cog) not in (POOL_SHARED_EXACT, POOL_OWNED_BY_STACK):
            if _pool_is_gone(_pool, cog):
                return
            raise _ResourceRetained(
                rtype,
                rid,
                f"Cognito pool {_pool} ownership could not be proven",
            )
        cog.delete_user_pool_client(UserPoolId=_pool, ClientId=rid)
    elif rtype == "cognito_resource_server":
        # The per-gateway scope definition in the shared pool, deleted after the app
        # client (priority 9 vs 8) because the guard is "no client can still be using
        # this scope". Two deployments that picked the same gateway name share one
        # resource server, so the co-residency check is what makes this safe — see
        # gateway_deployer.resource_server_is_unused, which reads client NAMES only and
        # fails closed.
        from app.services.gateway_deployer import (
            POOL_OWNED_BY_STACK,
            POOL_SHARED_EXACT,
            classify_user_pool,
            resource_server_is_unused,
        )

        _pool = str(res.get("pool_id") or "")
        if not _pool:
            raise _ResourceRetained(
                rtype,
                rid,
                "the manifest has no pool_id, so the resource-server container cannot be verified",
            )
        cog = _shared_pool_children_client(_pool, res_region, event)
        if classify_user_pool(_pool, cognito_client=cog) not in (POOL_SHARED_EXACT, POOL_OWNED_BY_STACK):
            if _pool_is_gone(_pool, cog):
                return
            raise _ResourceRetained(
                rtype,
                rid,
                f"Cognito pool {_pool} ownership could not be proven",
            )
        if not resource_server_is_unused(_pool, rid, cog):
            raise _ResourceRetained(
                rtype,
                rid,
                "the resource server still has an app client",
            )
        cog.delete_resource_server(UserPoolId=_pool, Identifier=rid)
    elif rtype == "cognito_user_pool":
        # Defence in depth — same guard as the deployment_handler teardown. The
        # shared platform gateway-auth pool is never recorded as deletable, but a
        # stale manifest row must not be able to take every other deployed gateway's
        # credentials with it, and "not the shared pool" is not "ours".
        # See gateway_deployer.classify_user_pool.
        from app.services.gateway_deployer import (
            POOL_OWNED_BY_STACK,
            classify_user_pool,
            is_platform_owned_user_pool,
        )

        # Pure env/id comparison, so the one pool that must never be touched is refused
        # before any client exists.
        if is_platform_owned_user_pool(rid):
            raise _ResourceRetained(
                rtype,
                rid,
                "it is the shared platform gateway-auth pool",
            )
        # This step can run against a CUSTOMER account via step_clients.client(event, …),
        # so classification must read tags through that same assumed-role client. A
        # classifier building its own ambient client would inspect the PLATFORM account
        # — reading the wrong resource entirely, and in the worst case a same-id pool at
        # home — so the client is threaded through rather than reconstructed.
        cog = step_clients.client(event, "cognito-idp", region_name=res_region)
        if classify_user_pool(rid, cognito_client=cog) != POOL_OWNED_BY_STACK:
            if _pool_is_gone(rid, cog):
                return
            raise _ResourceRetained(
                rtype,
                rid,
                "Cognito pool ownership could not be proven",
            )
        # Delete domain first (required)
        try:
            dom = cog.describe_user_pool(UserPoolId=rid).get("UserPool", {}).get("Domain")
            if dom:
                cog.delete_user_pool_domain(UserPoolId=rid, Domain=dom)
                for _ in range(12):
                    try:
                        still = cog.describe_user_pool(UserPoolId=rid).get("UserPool", {}).get("Domain")
                    except Exception:
                        still = None
                    if not still:
                        break
                    time.sleep(5)
        except Exception:  # noqa: BLE001 — pool delete below retries + surfaces real failures
            logger.debug("Cognito domain pre-delete for pool %s failed", rid, exc_info=True)
        # Retry pool delete in case domain teardown is still settling
        for _attempt in range(6):
            try:
                cog.delete_user_pool(UserPoolId=rid)
                break
            except Exception as ce:
                if "domain" in str(ce).lower() and _attempt < 5:
                    time.sleep(5)
                    continue
                raise
    elif rtype == "memory":
        # F-56: DeleteMemory answers DELETING, which is not deletion. The confirmed
        # protocol re-checks stack AND caller ownership and polls GetMemory to absence
        # inside this pass's shared deadline; a refusal or an unconfirmed poll is a
        # retention, never a clean.
        ctrl = step_clients.client(event, "bedrock-agentcore-control", region_name=res_region)
        _deadline = event.get("_cleanup_deadline_monotonic")
        _prove_live_ownership(
            lambda: delete_memory_confirmed(
                ctrl,
                rid,
                region=res_region,
                owner_sub=event.get("owner_sub") or "",
                **({"deadline_monotonic": _deadline} if _deadline is not None else {}),
            )
        )
    elif rtype == "lambda":
        _fn = rname or rid
        _lam = step_clients.client(event, "lambda", region_name=res_region)
        # Defect C (failure-path manifest teardown): shared singleton tool
        # Lambdas are released by reference count, never hard-deleted while
        # another live gateway still holds an invoke grant on them.
        if is_shared_tool_function(_fn, res_region):
            # WARNING: this string is the ONLY record of which of the three branches
            # ran — deleted, kept for another live stack, or kept because ownership was
            # unprovable ("NO OWNER TAG"). At info level none of them reached
            # CloudWatch, so a shared Lambda left standing looked identical to one
            # deleted.
            release_message = _release_shared_tool_lambda(
                _lam,
                _fn,
                res.get("gateway_role"),
                res_region,
            )
            if _message_means_retained(release_message):
                raise _ResourceRetained(rtype, _fn, release_message)
            logger.warning(release_message)
        else:
            # F-7c: same gate as the success path. A failure-path teardown is the MOST
            # likely place to hold a name the deploy never actually created, because it
            # runs after a partial deploy.
            # F-7d: exact binding (KB: DeploymentId; custom: the row's ToolScope) on top of the
            # stack gate, under the function's lock so a deploy cannot re-create or re-grant
            # between the read and the delete.
            _required = tool_binding_requirement(_fn, res.get("tool_scope"), event.get("deployment_id") or "")
            with shared_lambda_lock(res_region, _fn):
                _refusal = _authorize_tool_function_deletion(_lam, _fn, res_region, required_tags=_required)
                if _refusal:
                    raise _ResourceRetained(rtype, _fn, _refusal)
                _lam.delete_function(FunctionName=_fn)
    elif rtype == "iam_role":
        iam = step_clients.client(event, "iam")
        role_name = rname or rid
        # F-7d: a tool role is its function's paired resource: same binding rule, same lock.
        _required = tool_binding_requirement(role_name, res.get("tool_scope"), event.get("deployment_id") or "")
        with shared_lambda_lock(res_region, res.get("paired_function") or role_name):
            _binding_refusal = assert_role_binding(iam, role_name, _required)
            if _binding_refusal:
                raise _ResourceRetained(rtype, role_name, _binding_refusal)
            delete_owned_iam_role(iam, role_name, res_region)
    elif rtype == "online_evaluation_config":
        # Mirror of deployment_handler's arm: exact id, confirmed absence, then the per-config
        # eval-results log group. Never by name (a name can be user-supplied and shared).
        ctrl = step_clients.client(event, "bedrock-agentcore-control", region_name=res_region)
        try:
            ctrl.delete_online_evaluation_config(onlineEvaluationConfigId=rid)
        except Exception as e:  # noqa: BLE001
            if not _gone(e):
                raise
        wait_until_absent(
            resource_label=f"online evaluation config {rid}",
            read=lambda: ctrl.get_online_evaluation_config(onlineEvaluationConfigId=rid),
            max_attempts=8,
            delay_seconds=1.5,
        )
        logs = step_clients.client(event, "logs", region_name=res_region)
        try:
            logs.delete_log_group(logGroupName=f"/aws/bedrock-agentcore/evaluations/results/{rid}")
        except Exception as e:  # noqa: BLE001
            if not _gone(e):
                raise
    elif rtype == "secret":
        delete_deployment_bound_secret(
            region=res_region,
            deployment_id=event.get("deployment_id") or "",
            secret_ref=rid,
            secrets_client=step_clients.client(event, "secretsmanager", region_name=res_region),
        )
    elif rtype in ("oauth2_credential_provider", "api_key_credential_provider"):
        ctrl = step_clients.client(event, "bedrock-agentcore-control", region_name=res_region)
        provider_name = rname or rid
        _prove_live_ownership(
            lambda: delete_owned_credential_provider(
                ctrl,
                provider_name,
                res_region,
            )
        )
    else:
        # A recorded type with no deleter here used to fall off the end of this
        # function and RETURN, which the caller read as success: it counted the
        # resource as cleaned and logged "N/N resources" while the resource was still
        # running. That is how an s3_object and an oss_collection could be reported
        # torn down without a single API call being made. Failing loudly is the only
        # way the completion count is evidence of anything.
        raise _NoDeleterFor(rtype)


def _get_env(name: str, default: str = "") -> str:
    """Read an environment variable with a fallback."""
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    """Create a DeploymentStateStore from environment variables."""
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def _auto_register_in_aws_registry(
    *,
    store: DeploymentStateStore,
    deployment_id: str,
    runtime_arn: str | None,
    runtime_endpoint: str | None,
    friendly_runtime_name: str,
    is_a2a: bool,
    finalizer_token: str | None = None,
) -> None:
    """Register a just-deployed agent into the AWS Agent Registry as DRAFT.

    No-op when the feature is disabled (no configured registry id) or the
    deployment already has a record. Builds an A2A agentCard descriptor for A2A
    runtimes, else a CUSTOM descriptor with the agent's identity/endpoint. The
    record starts in DRAFT — a curator approves it via the registry router
    (visibility/integration gating enforced elsewhere). Loom-study 0.4.

    Registry GA renamed the record classifier from ``descriptorType`` to
    ``recordType`` and dropped the ``A2A`` value in favour of ``AGENT``, so an
    A2A runtime is now an AGENT record carrying an ``a2aAgentCard`` descriptor.
    """
    from app.services.aws_agent_registry import (
        build_a2a_descriptor,
        build_custom_descriptor,
        get_registry,
    )

    registry = get_registry()
    if registry is None:
        return  # federation disabled — nothing to do
    if store.get_registry_record_id(deployment_id):
        return  # idempotent — already registered

    if is_a2a:
        record_type = "AGENT"
        descriptors = build_a2a_descriptor(
            name=friendly_runtime_name,
            description=f"Agent {friendly_runtime_name} deployed via the platform",
            url=runtime_endpoint or runtime_arn or "",
        )
    else:
        record_type = "CUSTOM"
        descriptors = build_custom_descriptor(
            {
                "name": friendly_runtime_name,
                "runtimeArn": runtime_arn or "",
                "endpoint": runtime_endpoint or "",
                "deploymentId": deployment_id,
            }
        )

    result = registry.register(
        name=friendly_runtime_name,
        record_type=record_type,
        descriptors=descriptors,
        description=f"Auto-registered on deploy: {friendly_runtime_name}",
    )
    record_id = result.get("record_id")
    if not record_id:
        return
    try:
        store.set_registry_record(
            deployment_id,
            record_id,
            result.get("status") or "DRAFT",
            finalizer_token=finalizer_token,
        )
    except DeploymentLifecycleConflict:
        # The registry call already happened; only the local handle was refused.
        # Leaving it here would orphan a live registry record that no deployment
        # names, so nothing would ever delete it -- the registry equivalent of the
        # manifest leak this fence exists to stop.
        #
        # But `register` is an UPSERT. `_refresh_record` returns updated=True when
        # it found and refreshed a record that already existed, and a genuinely
        # new record carries no `updated` flag at all. Compensating a refresh
        # would delete another deployment's still-referenced record -- a far worse
        # outcome than an orphan -- so compensation is limited to the create case.
        if result.get("updated"):
            logger.warning(
                "Registry record %s was pre-existing and is left intact; %s no longer owns its deployment row",
                record_id,
                deployment_id,
            )
            raise
        # Exclusive acquisition already rules out the dangerous case by
        # construction -- a rival finalizer cannot be live while this one holds
        # the lease, so it cannot have adopted the record this invocation is
        # about to delete. This read is the belt to that brace: if the row is
        # NOT teardown-owned and another finalizer holds it (clock skew is the
        # only way here), that finalizer upserts the same name and will point at
        # this very record, so deleting it would dangle the pointer it is about
        # to write. Only a teardown-owned row has no future writer for it.
        snapshot = store.ownership_snapshot(deployment_id) if hasattr(store, "ownership_snapshot") else None
        if snapshot is not None and snapshot.get("delete_status") is None:
            logger.warning(
                "Registry record %s left for the finalizer that now owns %s; not compensating",
                record_id,
                deployment_id,
            )
            raise
        try:
            registry.delete(record_id)
            logger.warning(
                "Compensated a newly created registry record for %s after losing deployment ownership",
                deployment_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Registry record %s is orphaned: its deployment row changed owner and the delete failed (%s)",
                record_id,
                type(exc).__name__,
            )
        raise
    logger.info(
        "AWS Agent Registry: registered %s as %s (%s)",
        friendly_runtime_name,
        record_id,
        result.get("status"),
    )


def handler(event: dict, context) -> dict:
    """Lambda handler for the final status update step.

    Writes the terminal deployment state (succeeded or failed) to DynamoDB,
    including runtime outputs and error details.

    Args:
        event: Step Functions event with ``deployment_id``, ``runtime_id``,
            ``runtime_endpoint``, ``gateway_result``, and optionally
            ``error`` for failure cases.
        context: Lambda context (unused).

    Returns:
        Dict with ``status`` and ``deployment_id``.
    """
    deployment_id = event.get("deployment_id", "")
    # Bound before the ``try`` so the release in ``finally`` runs from every exit,
    # including the five return points, the cancellation branch and the
    # last-resort FAILED write.
    store = None
    finalizer_token: str | None = None

    try:
        store = _get_deployment_store()
        # THE DEPLOY-VS-DELETE BARRIER, taken before any status write.
        #
        # Everything below the terminal SUCCEEDED commit -- the version row, the
        # slot pointer, the registry record, the last manifest appends -- happens
        # after this deployment already looks finished to a reader. A teardown
        # starting in that window snapshotted `created_resources`, this handler
        # appended one more row, and the teardown wrote the FINAL `deleted` over a
        # row no deleter had ever seen: a permanent leak that DELETE reported as
        # success. `_claim_delete_status` now refuses while this lease is live.
        #
        # Acquired FIRST, so a finalizer that has already lost the race to a
        # teardown does no work at all: the refusal arrives as
        # DeploymentLifecycleConflict and lands in the cancellation branch below.
        finalizer_token = store.acquire_finalizer_lease(deployment_id)
        store.update_step(
            deployment_id,
            DeploymentStepName.STATUS_UPDATE,
            DeploymentStatusEnum.IN_PROGRESS,
            finalizer_token=finalizer_token,
        )

        # Detect errors from both direct invocation ("error" key) and
        # Step Functions Catch handler ("error_info" key with Error/Cause).
        error_details = event.get("error")
        error_info = event.get("error_info")
        if not error_details and error_info:
            if isinstance(error_info, dict):
                error_details = error_info.get("Cause") or error_info.get("Error") or str(error_info)
            else:
                error_details = str(error_info)
        if error_details:
            # A failed step's carried manifest rows are for cleanup, not for the reader.
            # F-30: redacted HERE, once, because this value goes on to the stored row AND to the
            # step's return value, which Step Functions writes into the execution history.
            error_details = redact_secrets(strip_failure_inventory(str(error_details)))
        if error_details:
            # The user-facing copy is sanitized at the store seam, which strips the
            # stack trace, the /var/task paths and the requestId. That is the right
            # trade for the UI but it also removes what an operator needs, so keep the
            # full failure HERE, in CloudWatch, where it is access-controlled.
            #
            # ERROR level deliberately: logger.info is discarded in the deployed
            # Lambdas, so an info-level diagnostic would not exist at all.
            #
            # Redacted even so. A botocore error echoes the request parameters that
            # produced it, which is how a client secret reaches an exception string;
            # ARCC cnt_rHmO501l15qr2W forbids credentials in logs regardless of who
            # can read them.
            logger.error(
                "Deployment %s failed; full cause (redacted): %s",
                deployment_id,
                redact_secrets(str(error_details)),
            )
        now = datetime.now(timezone.utc)

        # Collect outputs from previous steps (available even on partial failure)
        runtime_id = event.get("runtime_id")
        runtime_arn = event.get("runtime_arn")
        runtime_endpoint = event.get("runtime_endpoint")
        gateway_result = event.get("gateway_result") or {}
        gateway_url = gateway_result.get("gateway_url")
        policy_result = event.get("policy_result") or {}
        memory_result = event.get("memory_result") or {}
        knowledge_base_result = event.get("knowledge_base_result") or {}
        guardrails_result = event.get("guardrails_result") or {}
        mcp_server_runtime_id = event.get("mcp_server_runtime_id")
        # Phase B — HARNESS mode. The harness_step puts harness_id/harness_arn/
        # deployment_mode on the event INSTEAD of runtime_id/runtime_arn. Persist
        # them so the DELETE / test-runtime handlers can route to harness_deployer
        # (and find the ARN to invoke). deployment_mode is also written at create
        # time in deployment_handler, but we re-affirm it here for completeness.
        harness_id = event.get("harness_id")
        harness_arn = event.get("harness_arn")
        # Persisted so DELETE can tear down the harness->gateway OAuth2 credential
        # provider. destroy_harness reconstructs that provider name deterministically,
        # but only while the harness still resolves; deployment_handler's own cleanup
        # branch reads it off the stored record, and because nothing ever wrote it that
        # branch could not run. See test_success_records_what_cleanup_needs.
        harness_result = event.get("harness_result") or {}
        deployment_mode = event.get("deployment_mode")

        if error_details:
            # Save partial results so delete handler can clean up
            store.update_status(
                deployment_id,
                DeploymentStatusEnum.FAILED,
                completed_at=now,
                error_details=str(error_details),
                runtime_id=runtime_id,
                runtime_arn=runtime_arn,
                gateway_result=gateway_result if gateway_result else None,
                policy_result=policy_result if policy_result else None,
                memory_result=memory_result if memory_result else None,
                knowledge_base_result=knowledge_base_result if knowledge_base_result else None,
                guardrails_result=guardrails_result if guardrails_result else None,
                mcp_server_runtime_id=mcp_server_runtime_id,
                harness_id=harness_id,
                harness_arn=harness_arn,
                harness_result=harness_result if harness_result else None,
                deployment_mode=deployment_mode,
                resource_manifest_complete=False,
                finalizer_token=finalizer_token,
            )
            # Best-effort: flip the AgentVersion row to failed so version
            # history reflects the partial deploy.
            version_id = event.get("version_id")
            friendly_runtime_name = event.get("friendly_runtime_name")
            if version_id and friendly_runtime_name:
                try:
                    get_versions_store().update_status(
                        runtime_name=friendly_runtime_name,
                        version_id=version_id,
                        status="failed",
                        runtime_id=runtime_id,
                        runtime_arn=runtime_arn,
                    )
                except Exception:
                    logger.exception(
                        "Failed to mark AgentVersion %s/%s failed",
                        friendly_runtime_name,
                        version_id,
                    )

            # Auto-cleanup on failure: delete created resources from the manifest
            # so failed deployments don't leave orphaned AWS resources (KB, Cognito
            # pools, gateways, etc.). Best-effort — cleanup errors are logged but
            # don't change the failure status.
            _auto_cleanup_on_failure(store, deployment_id, event, finalizer_token=finalizer_token)

            return {
                "deployment_id": deployment_id,
                "status": DeploymentStatusEnum.FAILED.value,
                "error_details": str(error_details),
                "version_id": version_id,
            }

        manifest_errors = _manifest_completion_errors(
            store.get(deployment_id),
            event,
        )
        if manifest_errors:
            # A deploy without complete teardown inventory is not a successful
            # deploy. Persist every available result first so the safe legacy
            # fallback has enough live identifiers to compensate immediately.
            manifest_error_details = "Deployment resource inventory is incomplete: " + "; ".join(manifest_errors)
            logger.error(
                "Deployment %s cannot be marked succeeded: %s",
                deployment_id,
                manifest_error_details,
            )
            store.update_status(
                deployment_id,
                DeploymentStatusEnum.FAILED,
                completed_at=now,
                error_details=manifest_error_details,
                runtime_id=runtime_id,
                runtime_arn=runtime_arn,
                gateway_result=gateway_result if gateway_result else None,
                policy_result=policy_result if policy_result else None,
                memory_result=memory_result if memory_result else None,
                knowledge_base_result=(knowledge_base_result if knowledge_base_result else None),
                guardrails_result=(guardrails_result if guardrails_result else None),
                mcp_server_runtime_id=mcp_server_runtime_id,
                harness_id=harness_id,
                harness_arn=harness_arn,
                harness_result=harness_result if harness_result else None,
                deployment_mode=deployment_mode,
                resource_manifest_complete=False,
                finalizer_token=finalizer_token,
            )
            _auto_cleanup_on_failure(store, deployment_id, event, finalizer_token=finalizer_token)
            return {
                "deployment_id": deployment_id,
                "status": DeploymentStatusEnum.FAILED.value,
                "error_details": manifest_error_details,
                "version_id": event.get("version_id"),
            }

        store.update_status(
            deployment_id,
            DeploymentStatusEnum.SUCCEEDED,
            completed_at=now,
            runtime_endpoint=runtime_endpoint,
            runtime_id=runtime_id,
            runtime_arn=runtime_arn,
            gateway_url=gateway_url,
            gateway_result=gateway_result if gateway_result else None,
            policy_result=policy_result if policy_result else None,
            memory_result=memory_result if memory_result else None,
            knowledge_base_result=knowledge_base_result if knowledge_base_result else None,
            # Persisted on SUCCESS as well as on failure, and the success path is the
            # one that matters. deployment_handler's cleanup (Step 0.7) is the only
            # consumer: it deletes a flow-created Bedrock guardrail only when
            # `guardrails_result.created_by_flow` is truthy on the stored record.
            # Omitting it here meant a guardrail this flow created was silently leaked
            # when the deployment was deleted -- and because the branch was skipped
            # rather than failing, DELETE still reported success. The failure branch
            # above already recorded it, so only the normal path leaked.
            guardrails_result=guardrails_result if guardrails_result else None,
            mcp_server_runtime_id=mcp_server_runtime_id,
            harness_id=harness_id,
            harness_arn=harness_arn,
            harness_result=harness_result if harness_result else None,
            deployment_mode=deployment_mode,
            resource_manifest_complete=True,
            finalizer_token=finalizer_token,
        )

        # Phase 1 Gap 1A — flip the AgentVersion row to succeeded and update
        # the runtime's production slot if this deploy targeted production.
        # Both writes are best-effort; a failure here doesn't fail the deploy
        # because the deployment record itself is already marked succeeded.
        version_id = event.get("version_id")
        friendly_runtime_name = event.get("friendly_runtime_name")
        deployment_slot = (event.get("deployment_slot") or "production").lower()
        owner_sub = event.get("owner_sub") or ""
        version_row_updated = False
        if version_id and friendly_runtime_name:
            try:
                get_versions_store().update_status(
                    runtime_name=friendly_runtime_name,
                    version_id=version_id,
                    status="succeeded",
                    runtime_id=runtime_id,
                    runtime_arn=runtime_arn,
                    runtime_endpoint=runtime_endpoint,
                    code_s3_key=event.get("s3_key"),
                    # Fence to the tenant this deploy belongs to. Empty means the event carried no
                    # sub; the store then strongly reads and pins the persisted row owner on both
                    # the version and name-claim sentinel rather than trusting existence alone.
                    expected_owner_sub=owner_sub or None,
                )
                version_row_updated = True
            except Exception:
                logger.exception(
                    "Failed to mark AgentVersion %s/%s succeeded",
                    friendly_runtime_name,
                    version_id,
                )
            # F-83 — the slot upsert is GATED on that write, and this is the whole point of the
            # gate: ``update_status`` now refuses when the version row is absent, which is exactly
            # what a completed teardown looks like. A late finalizer that ran anyway used to
            # re-``upsert`` the slot here, re-locking the friendly name and re-pointing it at a
            # runtime the teardown had already destroyed -- a name nobody can redeploy and nothing
            # can release, because the row it would need is gone. Measured against real stores by a
            # peer session. The slot may only move when the version row it points at is really there.
            if not version_row_updated:
                logger.warning(
                    "Not touching RuntimeSlots for %s/%s: the version row was not updated, so this "
                    "finalizer is racing a teardown or another owner",
                    friendly_runtime_name,
                    version_id,
                )
            elif deployment_slot not in FENCEABLE_SLOTS:
                # Previously this fell through to an upsert that changed no pointer -- a write whose
                # only effect was to re-put the row, which is precisely the stale full-row PUT that
                # erased ``trigger_fence`` and unblocked a teardown. There is nothing to move for a
                # slot name we do not model, so move nothing.
                logger.warning(
                    "Not touching RuntimeSlots for %s/%s: deployment_slot %r is not one of %s",
                    friendly_runtime_name,
                    version_id,
                    deployment_slot,
                    FENCEABLE_SLOTS,
                )
            else:
                try:
                    slots_store = get_slots_store()
                    # Strongly consistent, both times. F-83: this read is no longer just input to a
                    # decision, it is the compare-and-set CONDITION of the write below, so a stale
                    # read pins values the transaction cannot match and fails a legitimate finalizer.
                    existing = slots_store.get(friendly_runtime_name, consistent=True)
                    # Re-read the version row we just flipped, to fence the slot write to it inside
                    # one transaction. ``update_status`` proved the row existed a moment ago; this
                    # picks up the immutable identity (created_at, deployment_id) needed to prove it
                    # is still the SAME row at write time -- a teardown can delete it in between, and
                    # a retry can re-create it under the same id with a different deploy.
                    version_row = get_versions_store().get(friendly_runtime_name, version_id, consistent=True)
                    # SECURITY (H-1, security review 2026-05-28): defense-in-depth
                    # against cross-tenant slot hijack. deployment_handler already
                    # rejects mismatched-owner deploys at the API boundary, but in
                    # case a bug or race lets one through, refuse to overwrite a
                    # slot row owned by a different sub. See lessons.md Bug 122.
                    if existing is not None and existing.owner_sub and existing.owner_sub != owner_sub:
                        logger.warning(
                            "Refusing to update RuntimeSlots for %s/%s: existing "
                            "slot owned by %s, deploy caller is %s. This should "
                            "have been caught at the API boundary (Bug 122).",
                            friendly_runtime_name,
                            version_id,
                            existing.owner_sub,
                            owner_sub,
                        )
                    elif version_row is None:
                        # The teardown won between the two reads. Same rule as the gate above: the
                        # slot may only move when the version row it will point at is really there.
                        logger.warning(
                            "Not touching RuntimeSlots for %s/%s: the version row is gone, so a "
                            "teardown completed while this finalizer was running",
                            friendly_runtime_name,
                            version_id,
                        )
                    else:
                        # On the very first deploy of a friendly name, create the slot
                        # row. On subsequent deploys, preserve the previous-production
                        # pointer so rollback() can flip back without bookkeeping.
                        if existing is None:
                            new_slots = RuntimeSlots(
                                runtime_name=friendly_runtime_name,
                                owner_sub=owner_sub,
                                production_version_id=(version_id if deployment_slot == "production" else None),
                                staging_version_id=(version_id if deployment_slot == "staging" else None),
                                last_promoted_at=now.isoformat(),
                            )
                        elif deployment_slot == "production":
                            # ``replace`` and not in-place mutation: ``existing`` is the row the write
                            # conditions on, so mutating it would make the condition assert the values
                            # we are about to write and the compare-and-set would match anything.
                            new_slots = replace(
                                existing,
                                previous_production_version_id=existing.production_version_id,
                                production_version_id=version_id,
                                last_promoted_at=now.isoformat(),
                            )
                        else:
                            new_slots = replace(existing, staging_version_id=version_id)
                        set_slot_pointers_atomically(
                            friendly_runtime_name,
                            expected=existing,
                            new=new_slots,
                            require_version=VersionFence(
                                version_id=version_id,
                                owner_sub=version_row.owner_sub,
                                status=version_row.status,
                                created_at=version_row.created_at or None,
                                slot=deployment_slot,
                                deployment_id=version_row.deployment_id,
                                runtime_id=version_row.runtime_id,
                                runtime_arn=version_row.runtime_arn,
                            ),
                        )
                except SlotWriteConflict:
                    # A LOST RACE, not an error, and explicitly NOT retried. Nothing was written.
                    # Retrying would mean re-reading a row that another writer (a promote, a rollback,
                    # a trigger create, or a teardown) just moved and writing over it with this
                    # deploy's pointers -- which is the clobber the compare-and-set exists to refuse.
                    # The deployment record is already succeeded and the version row already says so,
                    # so the deploy's outcome is recorded; only the slot pointer did not move, and the
                    # owner can promote explicitly. Logged without a traceback because there is no
                    # defect here to read one for.
                    logger.warning(
                        "RuntimeSlots for %s/%s was not moved: the slot or its version changed while "
                        "this finalizer was running. Nothing was written and this is not retried.",
                        friendly_runtime_name,
                        version_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to update RuntimeSlots for %s/%s",
                        friendly_runtime_name,
                        version_id,
                    )

        # Loom-study 0.4 — auto-register the deployed agent into the AWS Agent
        # Registry as a DRAFT record when the federation feature is enabled. Was
        # entirely un-wired: aws_agent_registry.register() had ZERO callers, so the
        # governance/discovery integration never fired on deploy. Best-effort: a
        # registry failure must NOT fail an already-succeeded deploy. Idempotent:
        # skip when the deployment already carries a registry_record_id.
        try:
            _cfg = event.get("config") or {}
            _protocol = (_cfg.get("protocol") if isinstance(_cfg, dict) else None) or event.get("protocol", "")
            _auto_register_in_aws_registry(
                store=store,
                deployment_id=deployment_id,
                runtime_arn=runtime_arn,
                runtime_endpoint=runtime_endpoint,
                friendly_runtime_name=friendly_runtime_name or runtime_id or deployment_id,
                is_a2a=str(_protocol).upper() == "A2A",
                finalizer_token=finalizer_token,
            )
        except DeploymentLifecycleConflict:
            # The helper already compensated a newly-created registry record.
            # Do not turn the ownership loss into a successful handler return.
            raise
        except Exception as _reg_exc:  # noqa: BLE001
            # Include the reason: the commonest cause is an old boto3 bundle with
            # no agent-registry service model, which is invisible otherwise.
            logger.warning(
                "AWS Agent Registry auto-register skipped (best-effort): %s",
                str(_reg_exc)[:200],
            )

        return {
            "deployment_id": deployment_id,
            "status": DeploymentStatusEnum.SUCCEEDED.value,
            "runtime_id": runtime_id,
            "runtime_endpoint": runtime_endpoint,
            "gateway_url": gateway_url,
            "version_id": version_id,
            "deployment_slot": deployment_slot,
        }

    except FinalizerLeaseBusy:
        # RETRYABLE CONTENTION, distinct from teardown. A previous invocation
        # may have timed out after an external side effect while its 300-second
        # lease still protects the row. Returning FAILED/CANCELLED here would
        # acknowledge the task and suppress Step Functions' dedicated backoff;
        # re-raising is what serializes the attempts.
        logger.warning(
            "Deployment %s is already being finalized; asking Step Functions to retry after the lease window.",
            deployment_id,
        )
        raise

    except DeploymentLifecycleConflict:
        # TERMINAL CANCELLATION, not a step failure. ``acquire_finalizer_lease``,
        # ``update_step`` and ``update_status`` all refuse once the deployment is deleting or
        # deleted, so reaching here means a teardown won the race with this finalizer and the row is
        # already the teardown's to own -- possibly a tombstone whose TTL a further write would
        # strip. The lease acquire is now the FIRST of the three that can refuse, which is the
        # point: the refusal arrives before any write instead of several writes in.
        #
        # Three things this branch exists to NOT do. It does not retry: the condition that refused
        # is monotonic (nothing moves a deployment back out of deleting), so every retry would be
        # refused again, against a row that may no longer exist. It does not fall through to the
        # generic handler below, which would attempt a FAILED write -- the same refused write, one
        # more conflict, and a second traceback. And it does not log at exception level: there is no
        # defect here, and a traceback on a routine race is what makes a real one invisible.
        #
        # The status is reported as CANCELLED rather than FAILED because FAILED is a claim about the
        # deploy, and this deploy may well have succeeded; what happened is that its owner deleted it
        # first. The state machine does not branch on this value (UpdateStatusSuccess goes straight
        # to Succeed), so it is execution output for a human, and the durable record is the
        # teardown's.
        logger.warning(
            "Deployment %s no longer accepts status updates: a teardown claimed it while this "
            "finalizer was running. Nothing was written and this is not retried.",
            deployment_id,
        )
        # A literal and deliberately NOT a new ``DeploymentStatusEnum`` member: that enum is the
        # vocabulary of the PERSISTED status field, and this branch's whole point is that it persists
        # nothing. Adding a member would invite a writer to store it, which is the late write the
        # condition refused.
        return {
            "deployment_id": deployment_id,
            "status": "cancelled",
            "error_details": "The deployment was deleted while its final status was being written.",
            "version_id": event.get("version_id"),
        }

    except Exception as exc:
        logger.exception("Status update step failed for deployment %s", deployment_id)
        # Last-resort: try to mark as failed
        try:
            store = _get_deployment_store()
            store.update_status(
                deployment_id,
                DeploymentStatusEnum.FAILED,
                completed_at=datetime.now(timezone.utc),
                error_details=f"Status update step error: {redact_secrets(str(exc))}",
                resource_manifest_complete=False,
                # Fenced like every other write here. If this invocation lost the
                # lease, the deployment's status belongs to whoever holds it now,
                # and a last-resort FAILED would overwrite a live retry's verdict
                # with the stale one's. The `finally` below still releases only on
                # a token match, so re-binding `store` above cannot drop a barrier
                # this invocation no longer owns.
                finalizer_token=finalizer_token,
            )
        except Exception:
            logger.exception("Failed to write error state for deployment %s", deployment_id)

        return {
            "deployment_id": deployment_id,
            "status": DeploymentStatusEnum.FAILED.value,
            # F-30: this dict is the state Step Functions records; a raw botocore message can
            # echo the request parameters that produced it.
            "error_details": redact_secrets(str(exc)),
        }

    finally:
        # Release from EVERY exit: the five returns, the cancellation branch, the
        # last-resort FAILED write, and any raise that escapes. The failure path's
        # `_auto_cleanup_on_failure` runs inside this ``try``, so the barrier still
        # stands through its final recovery writes -- which matters, because that
        # cleanup announces `delete_status=deleting` and `update_delete_status`
        # REMOVEs `delete_claim_expires_at`, leaving the row instantly re-claimable
        # by anything not held off by this lease.
        #
        # Expiry is the backstop, not the mechanism: holding the lease past the
        # handler would delay a legitimate teardown for no benefit. Best-effort by
        # design -- a failed release must not turn a successful deploy into a
        # failed one, and the lease times out on its own.
        if store is not None and finalizer_token:
            store.release_finalizer_lease(deployment_id, finalizer_token)

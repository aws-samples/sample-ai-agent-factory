"""DynamoDB storage adapter for deployment state persistence.

This module provides a DynamoDB-backed store for deployment execution state,
following the same pattern as ``dynamodb_storage.py``. Each deployment record
tracks progress through the Step Functions state machine. Live records remain
durable so runtime lookup, tenant authorization, and safe teardown do not expire;
only successfully deleted tombstones receive a 30-day DynamoDB TTL.

Requirements: 4.1, 4.2, 4.3
"""

import json
import logging
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

from app.models.deployment_models import (
    DeploymentState,
    DeploymentStatusEnum,
    DeploymentStepName,
)
from app.services.error_sanitizer import sanitize_error_details

logger = logging.getLogger(__name__)

# TTL offset: 30 days expressed in seconds
_TTL_DAYS = 30

# A manifest row is inventory, not authority.  New writers set this field
# explicitly so teardown can distinguish a resource this deployment created
# from one it merely found and reused.
MANIFEST_CREATED_FIELD = "created_by_deployment"

_ACCOUNT_GLOBAL_MANIFEST_TYPES = frozenset({"iam_role", "s3_object"})
_NAME_KEYED_MANIFEST_TYPES = frozenset(
    {
        "api_key_credential_provider",
        "iam_role",
        "lambda",
        "oauth2_credential_provider",
        "oss_collection",
        "s3_vectors_bucket",
    }
)
_COGNITO_CHILD_TYPES = frozenset({"cognito_app_client", "cognito_resource_server"})
_SECRET_ARN_SUFFIX = re.compile(r"^(?P<name>.+)-[A-Za-z0-9]{6}$")


class DeploymentLifecycleConflict(RuntimeError):
    """A deployment moved into teardown while a lifecycle writer was finalizing it."""


class FinalizerLeaseBusy(RuntimeError):
    """Another finalizer still owns this deployment's bounded finalization lease."""


# The deploy finalizer's durable "I am still writing" marker, and the barrier a
# teardown claim must clear before it may begin.
#
# Why this exists at all: the finalizer commits the terminal SUCCEEDED status and
# only THEN does the version row, the slot pointer and the registry record. A
# teardown that started in that window snapshotted ``created_resources``, the
# still-running finalizer appended one more row, and the teardown then wrote
# ``delete_status=deleted`` over a manifest row no deleter had ever seen. Because
# ``deleted`` is final, the resource leaked permanently and DELETE still reported
# success. ARCC cnt_vBC0kXE8PNHqrW names this exactly -- an authorization check
# made at the wrong time in an asynchronous workflow, i.e. TOCTOU -- and
# cnt_4mD5f0eLH0RCDK prescribes the fix: make the critical section atomic and
# surface the conflict at use time rather than pre-reading state that can move.
#
# DELIBERATELY a different attribute from ``delete_claim_expires_at``.
# ``update_delete_status`` REMOVEs that one on every write, so the failure path
# announcing ``deleting`` erases its own claim lease and the row reads as
# immediately re-claimable -- a barrier built on it would vanish exactly when the
# failure cleanup still needs it.
FINALIZER_LEASE_ATTR = "finalizer_lease_expires_at"

# Proof of WHICH finalizer holds the lease, not merely that one does. The
# finalizer's own failure cleanup legitimately writes manifest rows after it has
# set ``delete_status=deleting`` (it records what it left standing so teardown
# can still find it), so those writes cannot be gated on the absence of a delete
# status. They condition on this token instead: the owner is allowed through and
# an unrelated late writer is not. An expiry alone cannot make that distinction.
FINALIZER_TOKEN_ATTR = "finalizer_lease_token"

# Bounded on purpose, and sized from the deployed timeouts rather than picked.
# The status_update Lambda's timeout is 120s and its Step Functions task's is 150s
# (infra/stacks/platform/step_lambdas.py and step_functions.py), so no live
# invocation can still be running at 300s: the lease always outlives a real
# finalizer, and a crashed one blocks deletion for five minutes rather than
# fifteen. A longer lease buys nothing and can only strand a row -- a crashed
# finalizer must not make a deployment undeletable. (An aborted execution already
# sits at ``in_progress`` forever and still has to be deletable, which is the
# other reason the delete claim is NOT gated on the deployment's status.)
#
# Acquisition is exclusive while this lease is live. A Step Functions retry must
# wait rather than overlap the invocation it replaces: two finalizers can both
# perform external side effects before either reaches its token-fenced DynamoDB
# write. In particular, one can create a registry record, a second can refresh it,
# and the first can then compensate-delete the shared record after losing the
# local write race. The status tasks carry a dedicated retry for
# ``FinalizerLeaseBusy`` whose backoff outlives this bounded lease.
FINALIZER_LEASE_SECONDS = 300


def _fence(
    expr_names: dict,
    expr_values: dict,
    finalizer_token: str | None,
) -> str:
    """Return the ConditionExpression fragment that fences a finalizer's write.

    A bounded lease stops a teardown STARTING inside the finalizer. It cannot
    stop the mirror image: an invocation whose lease already expired, or that a
    retry has superseded, waking up and writing anyway. That writer's condition
    is satisfied -- the row exists -- so its append lands on a deployment
    another owner is now tearing down, and no amount of expiry checking on the
    reader's side can prevent it.

    Passing the token acquired at the top of the handler makes each write
    self-authorizing at the moment DynamoDB evaluates it: whoever holds the
    lease NOW is the only writer that can commit. That is ARCC
    cnt_4mD5f0eLH0RCDK's atomic critical section applied per write instead of
    once per handler, which is the only placement that survives a handler
    running for two minutes.

    Returns ``""`` for an unfenced caller, so every existing call site keeps its
    exact condition. Mutates *expr_names* / *expr_values* in place.
    """
    if finalizer_token is None:
        return ""
    expr_names["#ft"] = FINALIZER_TOKEN_ATTR
    expr_values[":ft"] = str(finalizer_token)
    return " AND #ft = :ft"


def _finalizer_fence(
    expr_names: dict,
    expr_values: dict,
    finalizer_token: str | None,
) -> str:
    """Fence a writer against another finalizer.

    A finalizer proves ownership with its exact token. An ordinary writer proves
    that no finalizer marker exists. ``delete_status`` is intentionally outside
    this helper because different writers have different teardown semantics.
    """
    if finalizer_token is not None:
        return _fence(expr_names, expr_values, finalizer_token)
    expr_names["#fl"] = FINALIZER_LEASE_ATTR
    expr_names["#ft"] = FINALIZER_TOKEN_ATTR
    return " AND attribute_not_exists(#fl) AND attribute_not_exists(#ft)"


def _lifecycle_fence(
    expr_names: dict,
    expr_values: dict,
    finalizer_token: str | None,
) -> str:
    """Fence an ordinary deployment writer against finalization and teardown.

    The token branch deliberately has no ``delete_status`` term because the
    finalizer's failure cleanup legitimately writes recovery inventory while its
    own ``delete_status=deleting`` marker is present. An ordinary caller must
    prove that neither lifecycle owns the row.

    ``update_delete_status`` intentionally does not use this helper: a teardown
    worker has no finalizer token and proves ownership with its delete claim.
    """
    finalizer_fence = _finalizer_fence(
        expr_names,
        expr_values,
        finalizer_token,
    )
    if finalizer_token is not None:
        return finalizer_fence
    expr_names["#delete_status"] = "delete_status"
    return " AND attribute_not_exists(#delete_status)" + finalizer_fence


def _fence_explains_refusal(table, deployment_id: str, finalizer_token: str | None) -> bool:
    """Whether a refused conditional write was refused BY the fence.

    REPORTING ONLY, and only ever reached AFTER an atomic write has already
    been refused, so this read cannot reintroduce a TOCTOU: it changes the
    exception type, never the decision. It exists because these writers carry
    other condition terms too -- ``record_gateway_handle`` refuses a second
    gateway id, ``update_status`` refuses a manifest error -- and reporting one
    of those as a lifecycle conflict would send the caller down a compensation
    path meant for a lost lease.

    Answers False when the row cannot be read. That is the deliberate
    direction: the original ``ClientError`` propagates, the caller's existing
    best-effort handling runs (``record_resource`` marks
    ``resource_manifest_error``, which makes the finalizer refuse to publish
    SUCCEEDED), and nothing is silently skipped. Answering True on an
    unreadable row would abort a possibly-legitimate write and could drop a
    manifest row -- a leak -- which is the worse failure of the two.
    """
    try:
        item = table.get_item(
            Key={"deployment_id": deployment_id},
            ConsistentRead=True,
        ).get("Item")
    except Exception:  # noqa: BLE001
        return False
    if not item:
        return False
    if finalizer_token is None:
        return "delete_status" in item or FINALIZER_LEASE_ATTR in item or FINALIZER_TOKEN_ATTR in item
    return str(item.get(FINALIZER_TOKEN_ATTR) or "") != str(finalizer_token)


def _arn_parts(value: str) -> tuple[str, str, str] | None:
    """Return ``(region, account, resource)`` for an ARN-like identity."""
    if not value.startswith("arn:"):
        return None
    parts = value.split(":", 5)
    if len(parts) != 6:
        return None
    return parts[3], parts[4], parts[5]


def _manifest_identity(rtype: str, resource: dict) -> tuple[str, str | None, str | None]:
    """Return canonical identity plus any region/account encoded in an ARN."""
    if rtype in _NAME_KEYED_MANIFEST_TYPES:
        raw = str(resource.get("name") or resource.get("id") or "")
    else:
        raw = str(resource.get("id") or resource.get("name") or "")

    arn = _arn_parts(raw)
    arn_region = arn[0] if arn else None
    arn_account = arn[1] if arn else None
    arn_resource = arn[2] if arn else raw

    if rtype == "iam_role":
        identity = arn_resource.removeprefix("role/").rsplit("/", 1)[-1]
    elif rtype == "lambda":
        identity = arn_resource.removeprefix("function:").split(":", 1)[0]
    elif rtype == "secret":
        identity = arn_resource.removeprefix("secret:")
        if arn:
            match = _SECRET_ARN_SUFFIX.fullmatch(identity)
            if match:
                identity = match.group("name")
    elif rtype == "s3_object":
        if raw.startswith("s3://"):
            identity = raw[5:]
        elif arn and arn_resource.startswith(":::"):
            identity = arn_resource[3:]
        else:
            identity = arn_resource
    elif arn:
        identity = arn_resource.rsplit("/", 1)[-1]
    else:
        identity = raw

    if rtype in _COGNITO_CHILD_TYPES:
        # Client ids and resource-server identifiers are unique only inside a
        # user pool. Dropping the container lets two tenants' identically named
        # scopes protect—or delete—the wrong resource.
        identity = f"{resource.get('pool_id') or ''}\x1f{identity}"

    if rtype == "policy":
        # F-G09-003: a Cedar policy id is engine-scoped. The same id on two engines is two resources, and a
        # deployment's policy row must never collide with (or protect) another engine's.
        engine = str(resource.get("engine_id") or resource.get("policy_engine_id") or "")
        identity = f"{engine}\x1f{identity}"
    return identity, arn_region, arn_account


# ============================================================================
# Boto3 Wrapper Functions
# ============================================================================


def _get_dynamodb_resource(region: str):
    """Create and return a boto3 DynamoDB resource.

    Args:
        region: AWS region name (e.g., 'us-east-1')

    Returns:
        boto3 DynamoDB resource
    """
    return boto3.resource("dynamodb", region_name=region)


def _get_table(dynamodb_resource, table_name: str):
    """Get a DynamoDB Table object from the resource.

    Args:
        dynamodb_resource: boto3 DynamoDB resource
        table_name: Name of the DynamoDB table

    Returns:
        boto3 DynamoDB Table object
    """
    return dynamodb_resource.Table(table_name)


def _put_item(
    table,
    item: dict,
    *,
    condition_expr: str | None = None,
) -> dict:
    """Write an item to the DynamoDB table.

    Args:
        table: boto3 DynamoDB Table object
        item: Dictionary representing the item to write
        condition_expr: Optional ConditionExpression.

    Returns:
        DynamoDB put_item response
    """
    kwargs = {"Item": item}
    if condition_expr:
        kwargs["ConditionExpression"] = condition_expr
    return table.put_item(**kwargs)


def _get_item(table, key: dict, *, consistent: bool = False) -> dict | None:
    """Read an item from the DynamoDB table by key.

    Args:
        table: boto3 DynamoDB Table object
        key: Dictionary with the partition key

    Returns:
        The item dict if found, None otherwise
    """
    kwargs: dict = {"Key": key}
    if consistent:
        kwargs["ConsistentRead"] = True
    response = table.get_item(**kwargs)
    return response.get("Item")


#: Longest stored delete_message. The verdict comes first in the message, but a gateway graph's
#: per-row lines alone overflowed the old 1 KiB cap and cut off later retention reasons (live,
#: 2026-10-02: a memory role's "retained" line was lost). DynamoDB allows far more; the UI shows it.
DELETE_MESSAGE_MAX_CHARS = 4096


def _update_item(
    table,
    key: dict,
    update_expr: str,
    expr_values: dict,
    expr_names: dict | None = None,
    condition_expr: str | None = None,
) -> dict:
    """Update specific attributes of an item in the DynamoDB table.

    Args:
        table: boto3 DynamoDB Table object
        key: Dictionary with the partition key
        update_expr: DynamoDB UpdateExpression string
        expr_values: ExpressionAttributeValues mapping
        expr_names: Optional ExpressionAttributeNames mapping
        condition_expr: Optional ConditionExpression

    Returns:
        DynamoDB update_item response
    """
    kwargs = {
        "Key": key,
        "UpdateExpression": update_expr,
    }
    # A REMOVE-only expression references no values, and DynamoDB rejects an
    # empty ExpressionAttributeValues map outright. Omitting it is a no-op for
    # every caller that passes one, and an unresolved ``:name`` still fails
    # loudly, so this cannot turn a malformed expression into a silent write.
    if expr_values:
        kwargs["ExpressionAttributeValues"] = expr_values
    if expr_names:
        kwargs["ExpressionAttributeNames"] = expr_names
    if condition_expr:
        kwargs["ConditionExpression"] = condition_expr
    return table.update_item(**kwargs)


# ============================================================================
# Serialization Helpers
# ============================================================================


def _compute_ttl(reference_time: datetime) -> int:
    """Compute a TTL value 30 days from *reference_time* as a Unix epoch integer.

    Args:
        reference_time: Timestamp from which retention starts (timezone-aware UTC).

    Returns:
        Unix epoch seconds 30 days after *reference_time*.
    """
    expiry = reference_time + timedelta(days=_TTL_DAYS)
    return int(expiry.timestamp())


def _convert_floats_to_decimals(obj):
    """Recursively convert float values to Decimal for DynamoDB compatibility.

    DynamoDB's boto3 resource API does not accept Python floats;
    all numeric values must be Decimal instances.

    Args:
        obj: A JSON-compatible Python object (dict, list, or scalar)

    Returns:
        The same structure with floats replaced by Decimals
    """
    if isinstance(obj, float):
        if obj != 0.0 and abs(obj) < 1e-130:
            return Decimal("0")
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _convert_floats_to_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert_floats_to_decimals(v) for v in obj]
    return obj


def _convert_decimals_to_floats(obj):
    """Recursively convert Decimal values back to float for Pydantic.

    Args:
        obj: A DynamoDB item (dict, list, or scalar)

    Returns:
        The same structure with Decimals replaced by floats
    """
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _convert_decimals_to_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert_decimals_to_floats(v) for v in obj]
    return obj


def manifest_resource_key(
    resource: dict,
    *,
    default_account: str | None = None,
    default_region: str | None = None,
) -> tuple[str, str, str, str]:
    """Return the stable identity used for cross-deployment reference checks.

    Account and region are part of the key because service-generated ids are not
    guaranteed to be globally unique. Rows written before cross-account support
    commonly omit them, so callers supply the owning deployment's frozen target.

    The identity is type-aware. IAM and S3 object identities are account-global,
    Lambda/IAM ARN and name forms collapse to the same key, Secrets Manager's
    generated ARN suffix is removed, and Cognito children include their pool.
    """
    rtype = str(resource.get("type") or "")
    identity, arn_region, arn_account = _manifest_identity(rtype, resource)
    account = str(resource.get("account") or arn_account or default_account or "")
    region = str(resource.get("region") or arn_region or default_region or "")
    if rtype in _ACCOUNT_GLOBAL_MANIFEST_TYPES:
        region = ""
    return (
        rtype,
        identity,
        account,
        region,
    )


def collapse_secret_intent_rows(
    rows: list[dict],
    *,
    default_account: str | None = None,
    default_region: str | None = None,
) -> list[dict]:
    """Drop each pre-create journal row (a bare secret name) whose ARN row is present.

    The journal names a secret before it exists, so in a same-account deployment the
    name row carries no account while its ARN row's account comes out of the ARN: two
    keys for one secret. Teardown then deleted it twice and counted it twice, and the
    second DescribeSecret can see a secret already scheduled for deletion. A journal
    row with no ARN partner (the lost-response case) is kept: it is the only record.
    """
    arn_keys: set[tuple[str, str, str]] = set()
    for row in rows:
        if row.get("type") == "secret" and str(row.get("id") or "").startswith("arn:"):
            _t, identity, account, region = manifest_resource_key(
                row, default_account=default_account, default_region=default_region
            )
            arn_keys.add((identity, account, region))
    out: list[dict] = []
    for row in rows:
        if row.get("type") == "secret" and not str(row.get("id") or row.get("name") or "").startswith("arn:"):
            _t, identity, account, region = manifest_resource_key(
                row, default_account=default_account, default_region=default_region
            )
            if any(i == identity and r == region and (not account or a == account) for i, a, r in arn_keys):
                continue
        out.append(row)
    return out


#: Set on every row the gateway step records beside the gateway itself: the
#: gateway's attachment graph (its app client, resource server, role, tool Lambdas,
#: credential providers, secrets, staged specs). Teardown reads it to leave that
#: graph whole while the gateway it serves still stands. Teardown also sets it on a
#: secret it discovered by tag, whose producer it cannot know.
GATEWAY_GRAPH_FIELD = "gateway_graph"

# Every type a gateway step has ever recorded besides the gateway.
_GATEWAY_GRAPH_CAPABLE_TYPES = frozenset(
    {
        "cognito_app_client",
        "cognito_resource_server",
        "cognito_user_pool",
        "iam_role",
        "lambda",
        "oauth2_credential_provider",
        "api_key_credential_provider",
        "secret",
        "s3_object",
    }
)


def gateway_graph_membership(recorded_rows: list) -> Callable[[dict], bool]:
    """Whether a row belongs to the gateway graph of the manifest *recorded_rows*.

    A tagged row says so. A manifest with no tagged row at all predates the tag, so
    nothing in it can be told apart and every type a gateway step records counts:
    retaining an unrelated role or secret is recoverable, breaking a live gateway is
    not. In a tagged manifest an untagged row is unrelated, except the kinds only a
    gateway ever creates.
    """
    legacy = not any(isinstance(r, dict) and GATEWAY_GRAPH_FIELD in r for r in recorded_rows or [])

    def member(row: dict) -> bool:
        if row.get(GATEWAY_GRAPH_FIELD) is True:
            return True
        rtype = row.get("type")
        if legacy:
            return rtype in _GATEWAY_GRAPH_CAPABLE_TYPES
        rid = str(row.get("name") or row.get("id") or "")
        if rtype in ("cognito_app_client", "cognito_resource_server"):
            return True
        if rtype == "iam_role" and rid.startswith("AgentCoreGateway-"):
            return True
        return rtype == "lambda" and bool(row.get("gateway_role"))

    return member


# Set on the exception a gateway delete raises after it had already deleted some of the
# gateway's targets: the gateway still stands but no longer routes every tool, and the
# teardown has to say so rather than report one error that reads as "untouched".
_TARGETS_DELETED_ATTR = "gateway_targets_deleted"


def note_gateway_targets_deleted(exc: BaseException, target_ids: list[str]) -> None:
    if target_ids:
        setattr(exc, _TARGETS_DELETED_ATTR, sorted({str(t) for t in target_ids}))


def gateway_targets_deleted(exc: BaseException) -> list[str]:
    found = getattr(exc, _TARGETS_DELETED_ATTR, None)
    return list(found) if isinstance(found, list) else []


def describe_gateway_targets_deleted(gateway_id: str, target_ids: list[str], *, shown: int = 10) -> str:
    """The exact count always; the ids up to *shown*, so the sentence fits a status
    message capped at 1 KiB."""
    more = len(target_ids) - shown
    return (
        f"gateway {gateway_id} was NOT deleted after {len(target_ids)} of its targets were: "
        + ", ".join(target_ids[:shown])
        + (f" and {more} more" if more > 0 else "")
    )


# The one refusal that is a hand-off rather than a retention: the resource is still
# listed by another deployment, whose own teardown will reclaim it. Teardown compares
# against this value, so it must stay the exact string returned below.
CO_RESIDENT_REFUSAL = "another live deployment still references the same resource"


def _gateway_named_by_result(item: dict) -> str:
    """The gateway id a deployment row's ``gateway_result`` names, or ``""``.

    F-09. Same reading rules as ``live_gateway_consumers``: a JSON string is decoded, both id
    spellings count, and a result that cannot be read RAISES -- a caller that cannot tell whether
    a legacy deployment is on the gateway must not guess that none is.
    """
    result = item.get("gateway_result")
    if result is None or result == "":
        return ""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError as e:
            raise ValueError(f"deployment {item.get('deployment_id')} has an unreadable gateway_result") from e
    if not isinstance(result, dict):
        raise ValueError(f"deployment {item.get('deployment_id')} has a malformed gateway_result")
    gateway_id = result.get("gateway_id") or result.get("gatewayId")
    return str(gateway_id) if gateway_id else ""


def manifest_delete_refusal(
    store: "DeploymentStateStore",
    deployment_id: str,
    resource: dict,
    *,
    target_account_id: str | None = None,
    target_region: str | None = None,
) -> str | None:
    """Return why a manifest row must not authorize deletion, or ``None``.

    New writers must set ``created_by_deployment`` explicitly, but provenance
    alone is not permanent deletion authority. A deployment that reused a
    stack-owned resource may become its last live adopter after the creator is
    deleted; hard-retaining every ``False`` row leaks that resource forever.

    Every row therefore goes through the live co-residency check. When no other
    deployment references it, the type-specific deleter still must re-read the
    AWS resource and prove exact stack ownership immediately before mutation.
    That second gate protects customer-owned/imported resources while allowing
    the last adopter to reclaim a resource this stack genuinely owns.
    """
    checks = [resource]
    if str(resource.get("type") or "") == "policy":
        # A Cedar policy serves every deployment on its ENGINE. Manifests written before policies had rows of their
        # own reference only the engine, so a child-only check would delete a policy a legacy deployment still uses.
        engine = resource.get("engine_id") or resource.get("policy_engine_id")
        if not engine:
            return "the policy row names no engine; its identity cannot be checked against other deployments"
        checks.append(
            {
                "type": "policy_engine",
                "id": engine,
                **({"region": resource["region"]} if resource.get("region") else {}),
                **({"account": resource["account"]} if resource.get("account") else {}),
            }
        )
    for check in checks:
        try:
            referenced = store.has_other_live_resource_reference(
                deployment_id,
                check,
                target_account_id=target_account_id,
                target_region=target_region,
            )
        except Exception as exc:  # noqa: BLE001
            return f"the deployment table could not prove that no other live deployment references it ({type(exc).__name__})"
        if referenced is True:
            return CO_RESIDENT_REFUSAL
    return None


def serialize_deployment_state(state: DeploymentState) -> dict:
    """Serialize a DeploymentState to a DynamoDB-compatible dict.

    * Uses Pydantic ``model_dump(mode="json")`` so datetime fields become
      ISO 8601 strings and enums become their string values.
    * Converts any remaining floats to Decimal.
    * Omits ``ttl`` from live records. A successfully deleted tombstone keeps
      its persisted TTL, or receives one 30 days from serialization if absent.

    Args:
        state: The DeploymentState to serialize.

    Returns:
        Dict suitable for DynamoDB ``put_item``.
    """
    # exclude_none=True so optional fields (e.g. runtime_id before the runtime
    # is created) are omitted from the DDB item rather than written as NULL.
    # The runtime_id-index GSI rejects NULL key values; see tasks/lessons.md
    # Bug 111. Also keeps the item smaller and back-compat with consumers
    # that key off attribute presence.
    item = state.model_dump(mode="json", exclude_none=True)
    if state.delete_status == "deleted":
        item["ttl"] = state.ttl or _compute_ttl(datetime.now(timezone.utc))
    else:
        # Migrate any legacy live model carrying the former started_at+30d TTL.
        # Active/retained/failed rows are teardown authority and must not expire.
        item.pop("ttl", None)
    # DynamoDB requires Decimal instead of float
    item = _convert_floats_to_decimals(item)
    return item


def deserialize_deployment_state(item: dict) -> DeploymentState:
    """Deserialize a DynamoDB item back to a DeploymentState.

    Converts Decimals back to floats so Pydantic can validate the data.

    Args:
        item: DynamoDB item dict.

    Returns:
        Validated DeploymentState instance.
    """
    data = _convert_decimals_to_floats(dict(item))
    return DeploymentState.model_validate(data)


# ============================================================================
# Deployment State Store Class
# ============================================================================


class DeploymentStateStore:
    """DynamoDB-backed store for deployment execution state.

    Provides CRUD operations for ``DeploymentState`` records in the
    Deployment_State_Table. Live records have no TTL. Once teardown succeeds,
    the delete-status writer gives the resulting tombstone a 30-day TTL.

    Requirements: 4.1, 4.2, 4.3
    """

    def __init__(self, table_name: str, region: str) -> None:
        """Initialize the deployment state store.

        Args:
            table_name: Name of the DynamoDB Deployment_State_Table.
            region: AWS region where the table exists.
        """
        self._table_name = table_name
        self._region = region
        self._dynamodb = _get_dynamodb_resource(region)
        self._table = _get_table(self._dynamodb, table_name)
        logger.info(
            "Initialized DeploymentStateStore: table=%s, region=%s",
            table_name,
            region,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, state: DeploymentState) -> DeploymentState:
        """Write a new deployment state record to DynamoDB.

        Persists the full live item without a TTL.

        Args:
            state: The initial DeploymentState (typically status=pending).

        Returns:
            The persisted DeploymentState with TTL cleared.
        """
        # A caller may have copied a legacy state model. Never carry its old
        # started_at-based expiry into a newly live deployment record.
        state = state.model_copy(update={"ttl": None})

        item = serialize_deployment_state(state)
        _put_item(
            self._table,
            item,
            # A duplicate/retried create must never replace a row after parallel
            # steps have appended teardown handles to created_resources.
            condition_expr="attribute_not_exists(deployment_id)",
        )
        logger.info("Created deployment state: %s", state.deployment_id)
        return state

    def get(self, deployment_id: str, *, consistent: bool = False) -> DeploymentState | None:
        """Retrieve a deployment state record by deployment_id.

        Args:
            deployment_id: Partition key value.

        Returns:
            The DeploymentState if found, None otherwise.
        """
        item = _get_item(
            self._table,
            {"deployment_id": deployment_id},
            consistent=consistent,
        )
        if item is None:
            return None
        return deserialize_deployment_state(item)

    def update_step(
        self,
        deployment_id: str,
        step: DeploymentStepName,
        status: DeploymentStatusEnum = DeploymentStatusEnum.IN_PROGRESS,
        *,
        finalizer_token: str | None = None,
    ) -> None:
        """Update the current step and status of a deployment.

        Removes any legacy TTL so an active deployment cannot expire.

        Args:
            deployment_id: Partition key value.
            step: The new current step.
            status: The new status (defaults to in_progress).
            finalizer_token: The caller's finalizer lease token, when it holds one.
                Fences the write against a superseded invocation; see ``_fence``.
        """
        # Fetch first so UpdateItem cannot silently create a skeletal row.
        existing = self.get(deployment_id, consistent=True)
        if existing is None:
            raise ValueError(f"Deployment '{deployment_id}' not found")

        expr_values: dict = {
            ":step": step.value,
            ":status": status.value,
        }
        expr_names: dict = {
            "#s": "status",
            "#t": "ttl",
            "#delete_status": "delete_status",
        }
        fence = _finalizer_fence(expr_names, expr_values, finalizer_token)
        try:
            _update_item(
                self._table,
                key={"deployment_id": deployment_id},
                update_expr="SET current_step = :step, #s = :status REMOVE #t",
                expr_values=expr_values,
                expr_names=expr_names,
                condition_expr=("attribute_exists(deployment_id) AND attribute_not_exists(#delete_status)" + fence),
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise DeploymentLifecycleConflict(
                    f"Deployment '{deployment_id}' no longer accepts step updates"
                ) from None
            raise
        logger.info(
            "Updated deployment %s: step=%s, status=%s",
            deployment_id,
            step.value,
            status.value,
        )

    def record_resource(
        self,
        deployment_id: str,
        resource: dict,
        *,
        finalizer_token: str | None = None,
    ) -> None:
        """Atomically append one created sub-resource to ``created_resources``.

        Each *resource* is a small dict the delete path can act on, e.g.
        ``{"type": "memory", "id": "mem-123", "region": "us-east-1"}`` or
        ``{"type": "iam_role", "name": "AgentCoreMemory-foo"}``. Uses DynamoDB
        ``list_append`` + ``if_not_exists`` so PARALLEL step handlers appending
        concurrently never clobber each other's entries. A transport failure is
        recorded durably on the deployment as ``resource_manifest_error``. The
        final status step then refuses to publish SUCCEEDED and runs safe
        fallback cleanup. If even that error marker cannot be written, this
        method raises rather than allowing a falsely healthy deployment.
        """
        try:
            self.record_resource_strict(deployment_id, resource, finalizer_token=finalizer_token)
        except (TypeError, ValueError):
            # A transport/storage failure is best-effort for backward
            # compatibility. A programmer omitted deletion provenance, however,
            # is deterministic and must fail loudly: swallowing it would let the
            # deploy continue with a real AWS resource that teardown can never
            # classify safely.
            raise
        except DeploymentLifecycleConflict:
            # A lost lease is NOT a durability failure, and must be caught before
            # the generic handler below. Falling through would write
            # ``resource_manifest_error`` onto a row whose owner is now someone
            # else -- permanently poisoning another invocation's deployment over
            # a write that was correctly refused. The caller aborts instead.
            raise
        except Exception as exc:  # noqa: BLE001
            # SECURITY (CodeQL py/clear-text-logging-sensitive-data): the
            # `resource` dict is taint-tracked as potentially secret-bearing, so
            # do NOT reference it in the log at all (not even .get("type")), and
            # don't emit a traceback. deployment_id + exception class is enough to
            # diagnose; full detail is available via the DDB write failure itself.
            logger.warning(
                "record_resource failed for %s (non-fatal): err=%s",
                deployment_id,
                type(exc).__name__,
            )
            try:
                if finalizer_token is None:
                    self.mark_resource_manifest_error(deployment_id)
                else:
                    self.mark_resource_manifest_error(
                        deployment_id,
                        finalizer_token=finalizer_token,
                    )
            except Exception as marker_exc:  # noqa: BLE001
                logger.error(
                    "Could not durably mark resource-manifest failure for %s: %s",
                    deployment_id,
                    type(marker_exc).__name__,
                )
                raise exc from marker_exc

    def mark_resource_manifest_error(
        self,
        deployment_id: str,
        *,
        finalizer_token: str | None = None,
    ) -> None:
        """Make a failed manifest append visible to finalization and teardown."""
        expr_values: dict = {
            ":true": True,
            ":false": False,
            ":version": 1,
        }
        expr_names: dict = {}
        fence = _lifecycle_fence(expr_names, expr_values, finalizer_token)
        try:
            _update_item(
                self._table,
                key={"deployment_id": deployment_id},
                update_expr=(
                    "SET resource_manifest_error = :true, "
                    "resource_manifest_complete = :false, "
                    "resource_manifest_version = "
                    "if_not_exists(resource_manifest_version, :version)"
                ),
                expr_values=expr_values,
                expr_names=expr_names,
                condition_expr="attribute_exists(deployment_id)" + fence,
            )
        except ClientError as exc:
            self._raise_if_fenced_out(exc, deployment_id, finalizer_token)
            raise

    def record_gateway_handle(
        self,
        deployment_id: str,
        gateway_result: dict,
        *,
        finalizer_token: str | None = None,
    ) -> None:
        """Name a gateway no manifest row names, in ``gateway_result``: the field a
        later DELETE falls back to on an incomplete manifest, and that another owner's
        adoption pre-flight reads (``live_gateway_consumers``). Never overwrites a
        result naming another gateway; that raises instead."""
        expr_values: dict = {
            ":g": _convert_floats_to_decimals(gateway_result),
            ":gid": gateway_result["gateway_id"],
        }
        expr_names: dict = {}
        fence = _lifecycle_fence(expr_names, expr_values, finalizer_token)
        try:
            _update_item(
                self._table,
                key={"deployment_id": deployment_id},
                update_expr="SET gateway_result = :g",
                expr_values=expr_values,
                expr_names=expr_names,
                condition_expr=(
                    "attribute_exists(deployment_id) AND "
                    "(attribute_not_exists(gateway_result) OR gateway_result.gateway_id = :gid)" + fence
                ),
            )
        except ClientError as exc:
            # The gateway-id term and the fence both raise the same error code, and
            # they mean opposite things to the caller: one is "you are writing the
            # wrong gateway", the other is "you no longer own this row".
            self._raise_if_fenced_out(exc, deployment_id, finalizer_token)
            raise

    def _raise_if_fenced_out(
        self,
        exc: ClientError,
        deployment_id: str,
        finalizer_token: str | None,
    ) -> None:
        """Convert a fence-caused refusal into ``DeploymentLifecycleConflict``.

        Returns normally when the refusal has some other cause, so the caller's
        own ``raise`` re-raises the original error untouched. Ordinary callers
        are classified only when a finalizer or teardown barrier is actually
        present; an unrelated condition such as a gateway-id mismatch keeps its
        original error.
        """
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            return
        if _fence_explains_refusal(self._table, deployment_id, finalizer_token):
            # No botocore message: a cancellation reason echoes the request.
            raise DeploymentLifecycleConflict(
                f"Deployment '{deployment_id}' no longer accepts this lifecycle write"
            ) from None

    def record_resource_strict(
        self,
        deployment_id: str,
        resource: dict,
        *,
        finalizer_token: str | None = None,
    ) -> None:
        """Append a resource and propagate any durability failure.

        Most resource recording remains best-effort for backward compatibility.
        Credentials are different: once plaintext has been converted into a
        Secrets Manager object, advancing an asynchronous workflow without a
        durable teardown handle can leave an unowned live secret forever. Those
        callers use this strict variant and compensate-delete on failure.
        """
        if not isinstance(resource, dict):
            raise TypeError("A deployment manifest resource must be a dictionary")
        if MANIFEST_CREATED_FIELD not in resource:
            raise ValueError(
                "A deployment manifest resource must state created_by_deployment "
                "explicitly; inventory without provenance is not deletion authority"
            )
        if not isinstance(resource[MANIFEST_CREATED_FIELD], bool):
            raise ValueError("created_by_deployment must be exactly true or false")
        expr_values: dict = {
            ":r": [_convert_floats_to_decimals(resource)],
            ":empty": [],
        }
        expr_names: dict = {}
        fence = _lifecycle_fence(expr_names, expr_values, finalizer_token)
        try:
            _update_item(
                self._table,
                key={"deployment_id": deployment_id},
                update_expr=("SET created_resources = list_append(if_not_exists(created_resources, :empty), :r)"),
                expr_values=expr_values,
                expr_names=expr_names,
                # UpdateItem creates an item when the key is absent. That would turn
                # a failed durability write into a malformed row containing only
                # created_resources, which no normal status/delete path can parse.
                # A credential handle is durable only inside the real deployment
                # record whose lifecycle owns it.
                #
                # The fence is what stops the measured leak's mirror image: a
                # superseded finalizer appending a row to a manifest whose owner
                # has already snapshotted it and written a final tombstone.
                condition_expr="attribute_exists(deployment_id)" + fence,
            )
        except ClientError as exc:
            self._raise_if_fenced_out(exc, deployment_id, finalizer_token)
            raise

    def has_other_live_resource_reference(
        self,
        deployment_id: str,
        resource: dict,
        *,
        target_account_id: str | None = None,
        target_region: str | None = None,
    ) -> bool:
        """Whether another live deployment still names the same AWS resource.

        This is the second half of manifest deletion authority.  Even a row that
        truthfully says "I created this" becomes stale after a later redeploy
        adopts the resource.  Deleting the older deployment must not break the
        newer one, so destructive cleanup scans the durable deployment records
        and fails closed if any other deployment that is not itself being
        deleted references the same type/id/account/region. Failed and
        delete-retained rows remain protective. ``deleting`` rows do not: both
        callers have committed to teardown, so making them protect each other
        creates a permanent two-row deadlock.

        F-66c: the same deadlock in sequence. A teardown that kept a row only
        because another deployment lists it must not become ``delete_retained``
        on that account, or its tombstone protects the row back and neither
        delete can ever reclaim it. Teardown therefore treats that refusal
        (``CO_RESIDENT_REFUSAL``) as a hand-off and finishes ``deleted`` when
        nothing else was kept and its runtime is gone.

        Exceptions intentionally propagate.  A caller that cannot prove there is
        no co-resident deployment must retain the resource rather than guess.
        """
        wanted = manifest_resource_key(
            resource,
            default_account=target_account_id,
            default_region=target_region,
        )
        if not wanted[0] or not wanted[1]:
            return True

        # A deployment can own ~15 manifest rows.  Build one reference snapshot
        # and reuse it for every row instead of issuing ~15 full-table scans.
        # Deployment ids are immutable UUIDs, so an instance-local cache keyed by
        # the current id cannot bleed authority between separate cleanups.
        cache_key = str(deployment_id)
        cache = getattr(self, "_manifest_reference_cache", None)
        if not cache or cache[0] != cache_key:
            references: set[tuple[str, str, str, str]] = set()
            # F-09. Rows written before the manifest existed name their gateway ONLY in
            # ``gateway_result`` -- the reason ``live_gateway_consumers`` reads it too. Projecting
            # ``created_resources`` alone made such a deployment invisible here, so a manifest
            # deployment that had adopted the same gateway could delete it and take the legacy
            # runtime's tool plane with it. Kept apart from ``references`` because a legacy row
            # usually recorded no account or region, and (mirroring ``live_gateway_consumers``)
            # an absent one must not exclude the row: only a RECORDED different one does.
            legacy_gateways: set[tuple[str, str, str]] = set()
            kwargs: dict = {
                "ProjectionExpression": (
                    "deployment_id, delete_status, target_account_id, target_region, created_resources, gateway_result"
                ),
                # Scan is already the least precise part of this safety gate.
                # Strong reads narrow the create/adopt -> teardown visibility
                # window rather than adding eventual consistency on top.
                "ConsistentRead": True,
            }
            while True:
                response = self._table.scan(**kwargs)
                for item in response.get("Items", []):
                    if str(item.get("deployment_id") or "") == cache_key:
                        continue
                    # A deleting deployment is no longer a live consumer. If two
                    # deployments sharing a resource are deleted concurrently,
                    # retaining each because the other is "deleting" strands the
                    # resource forever. Failed/delete-retained remain protective.
                    if str(item.get("delete_status") or "") in {
                        "deleted",
                        "deleting",
                    }:
                        continue

                    other_account = item.get("target_account_id")
                    other_region = item.get("target_region")
                    for other in item.get("created_resources") or []:
                        references.add(
                            manifest_resource_key(
                                other,
                                default_account=other_account,
                                default_region=other_region,
                            )
                        )
                    legacy_gateway = _gateway_named_by_result(item)
                    if legacy_gateway:
                        legacy_gateways.add(
                            (
                                manifest_resource_key({"type": "gateway", "id": legacy_gateway})[1],
                                str(other_account or ""),
                                str(other_region or ""),
                            )
                        )

                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    break
                kwargs["ExclusiveStartKey"] = last_key
            cache = (cache_key, references, legacy_gateways)
            self._manifest_reference_cache = cache

        if wanted in cache[1]:
            return True
        if wanted[0] != "gateway":
            return False

        def _recorded_differs(recorded: str, ours: str) -> bool:
            return bool(recorded) and bool(ours) and recorded != ours

        for gateway_identity, account, region in cache[2] if len(cache) > 2 else ():
            if (
                gateway_identity == wanted[1]
                and not _recorded_differs(account, wanted[2])
                and not _recorded_differs(region, wanted[3])
            ):
                return True
        return False

    def resource_rows(
        self,
        deployment_id: str,
        resource: dict,
        *,
        include_self: bool = False,
        include_deleted: bool = False,
        target_account_id: str | None = None,
        target_region: str | None = None,
    ) -> list[dict]:
        """Every manifest row any deployment holds for the same resource identity.

        Each row is returned with ``_deployment_id`` and ``_delete_status`` attached. By default only OTHER deployments
        that are live (not ``deleted``/``deleting``) are included -- the population ``has_other_live_resource_reference``
        counts -- so a caller can compare what each deployment *desires* (two deployments sharing one Cedar policy with
        different definitions). ``include_deleted`` widens to tombstones, which is how platform provenance of a resource
        is proven when no live deployment claims it. Exceptions propagate: a caller that cannot enumerate must not
        guess.
        """
        wanted = manifest_resource_key(resource, default_account=target_account_id, default_region=target_region)
        if not wanted[0] or not wanted[1]:
            return []
        rows: list[dict] = []
        kwargs: dict = {
            "ProjectionExpression": "deployment_id, delete_status, target_account_id, target_region, created_resources",
            "ConsistentRead": True,
        }
        while True:
            response = self._table.scan(**kwargs)
            for item in response.get("Items", []):
                other_id = str(item.get("deployment_id") or "")
                status = str(item.get("delete_status") or "")
                if other_id == str(deployment_id) and not include_self:
                    continue
                if status in {"deleted", "deleting"} and not include_deleted:
                    continue
                other_account = item.get("target_account_id")
                other_region = item.get("target_region")
                for other in item.get("created_resources") or []:
                    if (
                        manifest_resource_key(other, default_account=other_account, default_region=other_region)
                        == wanted
                    ):
                        rows.append(dict(other, _deployment_id=other_id, _delete_status=status))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return rows

    def other_live_resource_rows(
        self,
        deployment_id: str,
        resource: dict,
        *,
        target_account_id: str | None = None,
        target_region: str | None = None,
    ) -> list[dict]:
        """Rows OTHER live deployments hold for the same resource identity (see ``resource_rows``)."""
        return self.resource_rows(
            deployment_id, resource, target_account_id=target_account_id, target_region=target_region
        )

    def live_gateway_consumers(
        self,
        deployment_id: str,
        gateway_id: str,
        *,
        pool_id: str,
        target_account_id: str | None = None,
        target_region: str | None = None,
    ) -> list[dict]:
        """Every other live deployment on ``gateway_id``: its owner and its app clients.

        F-62/F-63. Each version of an agent is its own runtime, and each deployment
        froze its own Cognito client into that runtime. So a redeploy that adopts the
        gateway must not revoke a client that a still-live deployment's runtime holds,
        and it must not adopt a gateway another user's live deployment is on at all.

        "Live" is the same rule the teardown co-residency gate uses: every row except
        ``deleted`` and ``deleting``. Failed and delete-retained rows still count,
        because the resources they name have not been released.

        A deployment is on the gateway when its manifest has the gateway's row or,
        for rows that predate the manifest, its ``gateway_result`` names it. Account
        and region exclude a row only when the row RECORDS a different one: an old row
        that recorded neither still protects the gateway it names. Only clients in
        ``pool_id``, the gateway authorizer's pool, are returned, so a deployment's
        other app clients (an MCP server's, say) never reach the gateway.

        Exceptions propagate, and a ``gateway_result`` that cannot be read raises: a
        caller that cannot list the consumers must not guess there are none.
        """

        def _differs(recorded, ours) -> bool:
            return bool(recorded) and bool(ours) and str(recorded) != str(ours)

        consumers: list[dict] = []
        kwargs: dict = {
            "ProjectionExpression": (
                "deployment_id, delete_status, #u, target_account_id, target_region, created_resources, #g"
            ),
            "ExpressionAttributeNames": {"#u": "user_id", "#g": "gateway_result"},
            "ConsistentRead": True,
        }
        while True:
            response = self._table.scan(**kwargs)
            for item in response.get("Items", []):
                if str(item.get("deployment_id") or "") == str(deployment_id):
                    continue
                if str(item.get("delete_status") or "") in {"deleted", "deleting"}:
                    continue
                item_account = item.get("target_account_id")
                item_region = item.get("target_region")
                result = item.get("gateway_result") or {}
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except ValueError as e:
                        raise ValueError(
                            f"deployment {item.get('deployment_id')} has an unreadable gateway_result"
                        ) from e
                if not isinstance(result, dict):
                    raise ValueError(f"deployment {item.get('deployment_id')} has a malformed gateway_result")
                rows = [r for r in item.get("created_resources") or [] if isinstance(r, dict)]

                on_gateway = any(
                    r.get("type") == "gateway"
                    and str(r.get("id") or "") == gateway_id
                    and not _differs(r.get("account") or item_account, target_account_id)
                    and not _differs(r.get("region") or item_region, target_region)
                    for r in rows
                ) or (
                    (result.get("gateway_id") or result.get("gatewayId")) == gateway_id
                    and not _differs(item_account, target_account_id)
                    and not _differs(item_region, target_region)
                )
                if not on_gateway:
                    continue

                clients = {
                    str(r["id"])
                    for r in rows
                    if r.get("type") == "cognito_app_client" and r.get("id") and r.get("pool_id") == pool_id
                }
                client_info = result.get("client_info") or {}
                if not isinstance(client_info, dict):
                    raise ValueError(f"deployment {item.get('deployment_id')} has a malformed client_info")
                legacy_client = client_info.get("client_id") or client_info.get("clientId")
                if legacy_client and client_info.get("user_pool_id") in (pool_id, None, ""):
                    clients.add(str(legacy_client))
                consumers.append(
                    {
                        "deployment_id": str(item.get("deployment_id") or ""),
                        "owner_sub": str(item.get("user_id") or ""),
                        "client_ids": sorted(clients),
                    }
                )
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return consumers

    def reset_manifest_reference_cache(self, deployment_id: str) -> None:
        """Start a fresh co-residency snapshot for one cleanup invocation."""
        cache = getattr(self, "_manifest_reference_cache", None)
        if cache and cache[0] == str(deployment_id):
            self._manifest_reference_cache = None

    def acquire_finalizer_lease(self, deployment_id: str, *, seconds: int | None = None) -> str:
        """Become this deployment's sole finalizer for a bounded window.

        Returns an opaque ownership token. The finalizer's own post-teardown
        recovery writes present it to prove they are the lease holder rather than
        an unrelated late writer; see ``FINALIZER_TOKEN_ATTR``.

        Raises ``FinalizerLeaseBusy`` while another finalizer's bounded lease is
        live, so Step Functions can retry without overlapping external side
        effects. Raises ``DeploymentLifecycleConflict`` when a teardown already
        owns the row (or the row is gone). Taken here, at the top of the handler,
        so a finalizer that has already lost either race does no work at all.
        """
        token = uuid.uuid4().hex
        lease_seconds = int(seconds if seconds is not None else FINALIZER_LEASE_SECONDS)

        # One retry closes the classification race where the incumbent releases
        # or expires after our conditional write is refused but before the
        # strongly-consistent read below. The retry is itself the atomic decision;
        # the read only decides which safe exception to report.
        for attempt in range(2):
            now = int(datetime.now(timezone.utc).timestamp())
            expires = now + lease_seconds
            try:
                _update_item(
                    self._table,
                    key={"deployment_id": deployment_id},
                    update_expr="SET #fl = :expires, #ft = :token",
                    expr_values={
                        ":expires": expires,
                        ":token": token,
                        ":now": now,
                    },
                    expr_names={
                        "#fl": FINALIZER_LEASE_ATTR,
                        "#ft": FINALIZER_TOKEN_ATTR,
                    },
                    # Missing rows and teardown-owned rows are terminal. A live
                    # finalizer is exclusive; an expired lease may be reclaimed
                    # atomically, overwriting its stale token in the same write.
                    condition_expr=(
                        "attribute_exists(deployment_id) AND "
                        "attribute_not_exists(delete_status) AND "
                        "(attribute_not_exists(#fl) OR #fl <= :now)"
                    ),
                )
                return token
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise

            try:
                item = self._table.get_item(
                    Key={"deployment_id": deployment_id},
                    ConsistentRead=True,
                ).get("Item")
            except Exception:  # noqa: BLE001
                # We cannot prove teardown owns the row, so report the retryable,
                # non-destructive outcome. The next attempt re-runs the atomic
                # condition; it never trusts this diagnostic read.
                raise FinalizerLeaseBusy(
                    f"Deployment '{deployment_id}' finalizer ownership could not be resolved safely"
                ) from None

            if not item or "delete_status" in item:
                raise DeploymentLifecycleConflict(
                    f"Deployment '{deployment_id}' cannot be finalized: it is already in teardown"
                ) from None

            incumbent_expires = item.get(FINALIZER_LEASE_ATTR)
            if incumbent_expires is not None:
                try:
                    if int(incumbent_expires) > int(datetime.now(timezone.utc).timestamp()):
                        raise FinalizerLeaseBusy(f"Deployment '{deployment_id}' is already being finalized") from None
                except (TypeError, ValueError):
                    raise FinalizerLeaseBusy(
                        f"Deployment '{deployment_id}' has an unreadable finalizer lease"
                    ) from None

            if attempt == 0:
                continue
            raise FinalizerLeaseBusy(f"Deployment '{deployment_id}' finalizer ownership changed concurrently") from None

        raise AssertionError("unreachable finalizer lease acquisition state")

    def ownership_snapshot(self, deployment_id: str) -> dict | None:
        """Who owns the row right now, strongly consistent; None if unreadable or absent.

        Never a basis for a write decision -- it is a snapshot, and every write
        here already decides atomically. It exists for choosing between two
        compensations AFTER a write was refused, where the alternative is a guess.
        """
        try:
            item = self._table.get_item(Key={"deployment_id": deployment_id}, ConsistentRead=True).get("Item")
        except Exception:  # noqa: BLE001
            return None
        if not item:
            return None
        raw = item.get(FINALIZER_LEASE_ATTR)
        try:
            lease_live = raw is not None and int(raw) > int(datetime.now(timezone.utc).timestamp())
        except (TypeError, ValueError):
            lease_live = True
        return {
            "delete_status": item.get("delete_status"),
            "finalizer_token": item.get(FINALIZER_TOKEN_ATTR),
            "finalizer_lease_live": lease_live,
            "aws_registry_record_id": item.get("aws_registry_record_id"),
        }

    def release_finalizer_lease(self, deployment_id: str, token: str) -> None:
        """Drop the finalizer barrier so a queued teardown may proceed at once.

        The token is REQUIRED, and the release is conditioned on it. An
        unconditioned release is a footgun with one real victim: a retry that
        re-acquired the lease after this invocation's expired would have its
        barrier torn down by the stale owner finishing late, re-opening the exact
        window the lease exists to close. A stale owner's release must fail.

        Best-effort otherwise, and safe because the lease is bounded: a lost
        release costs a teardown some latency and never correctness. Raising here
        would fail a deployment that actually succeeded over a bookkeeping write.
        """
        expr_names = {"#fl": FINALIZER_LEASE_ATTR, "#ft": FINALIZER_TOKEN_ATTR}
        expr_values: dict = {":token": str(token)}
        condition = "attribute_exists(deployment_id) AND #ft = :token"
        try:
            _update_item(
                self._table,
                key={"deployment_id": deployment_id},
                update_expr="REMOVE #fl, #ft",
                expr_values=expr_values,
                expr_names=expr_names,
                condition_expr=condition,
            )
        except Exception as exc:  # noqa: BLE001
            # Exception TYPE only. A botocore message echoes the request that
            # produced it, and a conditional-check failure here is routine: the
            # row may be a tombstone a teardown already finished.
            logger.warning(
                "Could not release the finalizer lease on deployment %s (%s); it expires on its own.",
                deployment_id,
                type(exc).__name__,
            )

    def update_delete_status(
        self,
        deployment_id: str,
        delete_status: str,
        delete_message: str | None = None,
        *,
        finalizer_token: str | None = None,
    ) -> None:
        """Persist teardown lifecycle without expiring a live/retained record.

        Only ``deleted`` is a tombstone and receives a fresh 30-day TTL. Every
        in-flight, failed, or safety-retained outcome remains durable so retries
        and cross-deployment ownership checks keep their evidence.

        The finalizer's own auto-cleanup passes its lease token. That matters
        most for the terminal write: a superseded invocation writing ``deleted``
        is exactly the final tombstone that made the measured leak permanent,
        because ``_deleted_is_final`` then answers "already deleted" to every
        later attempt. A teardown worker passes nothing and is unaffected -- it
        proves ownership through ``delete_claim_expires_at`` instead.
        """
        set_parts = ["delete_status = :ds"]
        expr_values: dict = {":ds": str(delete_status)}
        if delete_message is not None:
            set_parts.append("delete_message = :dm")
            expr_values[":dm"] = str(delete_message)[:DELETE_MESSAGE_MAX_CHARS]
        expr_names = {
            "#t": "ttl",
            "#claim": "delete_claim_expires_at",
        }
        if delete_status == "deleted":
            set_parts.append("#t = :ttl")
            expr_values[":ttl"] = _compute_ttl(datetime.now(timezone.utc))
            update_expr = "SET " + ", ".join(set_parts) + " REMOVE #claim"
        else:
            update_expr = "SET " + ", ".join(set_parts) + " REMOVE #t, #claim"
        fence = _fence(expr_names, expr_values, finalizer_token)
        try:
            _update_item(
                self._table,
                key={"deployment_id": deployment_id},
                update_expr=update_expr,
                expr_values=expr_values,
                expr_names=expr_names,
                condition_expr="attribute_exists(deployment_id)" + fence,
            )
        except ClientError as exc:
            self._raise_if_fenced_out(exc, deployment_id, finalizer_token)
            raise

    def update_status(
        self,
        deployment_id: str,
        status: DeploymentStatusEnum,
        *,
        completed_at: datetime | None = None,
        runtime_endpoint: str | None = None,
        runtime_id: str | None = None,
        runtime_arn: str | None = None,
        gateway_url: str | None = None,
        gateway_result: dict | None = None,
        policy_result: dict | None = None,
        memory_result: dict | None = None,
        knowledge_base_result: dict | None = None,
        guardrails_result: dict | None = None,
        mcp_server_runtime_id: str | None = None,
        harness_id: str | None = None,
        harness_arn: str | None = None,
        harness_result: dict | None = None,
        deployment_mode: str | None = None,
        resource_manifest_complete: bool | None = None,
        error_details: str | None = None,
        finalizer_token: str | None = None,
    ) -> None:
        """Update the status and optional output fields of a deployment.

        Used by the status_update step handler to record final results
        (succeeded or failed) along with runtime outputs or error info.

        Args:
            deployment_id: Partition key value.
            status: The new deployment status.
            completed_at: Completion timestamp (ISO 8601 serialized).
            runtime_endpoint: Deployed runtime endpoint URL.
            runtime_id: Deployed runtime identifier.
            gateway_url: Deployed gateway URL (if applicable).
            harness_id: Deployed AgentCore Harness id (Phase B harness mode).
            harness_arn: Deployed AgentCore Harness ARN (Phase B harness mode).
            harness_result: Full harness step result, kept so DELETE can tear down
                the harness->gateway OAuth2 credential provider.
            deployment_mode: Authoring path ("runtime" | "harness").
            error_details: Error description (if failed).
        """
        existing = self.get(deployment_id)
        if existing is None:
            raise ValueError(f"Deployment '{deployment_id}' not found")
        if resource_manifest_complete is True and existing.resource_manifest_error is True:
            raise ValueError("Deployment resource manifest has a recorded durability failure")

        # Build dynamic update expression from provided kwargs
        set_parts = ["#s = :status"]
        expr_values: dict = {
            ":status": status.value,
        }
        expr_names: dict = {
            "#s": "status",
            "#t": "ttl",
        }

        if completed_at is not None:
            set_parts.append("completed_at = :completed_at")
            expr_values[":completed_at"] = completed_at.isoformat()

        if runtime_endpoint is not None:
            set_parts.append("runtime_endpoint = :runtime_endpoint")
            expr_values[":runtime_endpoint"] = runtime_endpoint

        if runtime_id is not None:
            set_parts.append("runtime_id = :runtime_id")
            expr_values[":runtime_id"] = runtime_id

        if runtime_arn is not None:
            set_parts.append("runtime_arn = :runtime_arn")
            expr_values[":runtime_arn"] = runtime_arn

        if gateway_url is not None:
            set_parts.append("gateway_url = :gateway_url")
            expr_values[":gateway_url"] = gateway_url

        if gateway_result is not None:
            set_parts.append("gateway_result = :gateway_result")
            expr_values[":gateway_result"] = _convert_floats_to_decimals(gateway_result)

        if policy_result is not None:
            set_parts.append("policy_result = :policy_result")
            expr_values[":policy_result"] = _convert_floats_to_decimals(policy_result)

        if memory_result is not None:
            set_parts.append("memory_result = :memory_result")
            expr_values[":memory_result"] = _convert_floats_to_decimals(memory_result)

        if knowledge_base_result is not None:
            set_parts.append("knowledge_base_result = :knowledge_base_result")
            expr_values[":knowledge_base_result"] = _convert_floats_to_decimals(knowledge_base_result)

        if guardrails_result is not None:
            set_parts.append("guardrails_result = :guardrails_result")
            expr_values[":guardrails_result"] = _convert_floats_to_decimals(guardrails_result)

        if mcp_server_runtime_id is not None:
            set_parts.append("mcp_server_runtime_id = :mcp_server_runtime_id")
            expr_values[":mcp_server_runtime_id"] = mcp_server_runtime_id

        if harness_id is not None:
            set_parts.append("harness_id = :harness_id")
            expr_values[":harness_id"] = harness_id

        if harness_arn is not None:
            set_parts.append("harness_arn = :harness_arn")
            expr_values[":harness_arn"] = harness_arn

        if harness_result is not None:
            set_parts.append("harness_result = :harness_result")
            expr_values[":harness_result"] = _convert_floats_to_decimals(harness_result)

        if deployment_mode is not None:
            set_parts.append("deployment_mode = :deployment_mode")
            expr_values[":deployment_mode"] = deployment_mode

        if resource_manifest_complete is not None:
            set_parts.append("resource_manifest_complete = :resource_manifest_complete")
            expr_values[":resource_manifest_complete"] = bool(resource_manifest_complete)
            set_parts.append("resource_manifest_version = if_not_exists(resource_manifest_version, :manifest_version)")
            expr_values[":manifest_version"] = 1

        if error_details is not None:
            # Sanitize HERE rather than at each caller. This is the one place every
            # failure path converges (status_update_step's Catch branch, its own
            # except handler, and deployment_handler's start-execution failure), and
            # whatever is written here is what GET /api/deploy/{id} returns and what
            # the UI throws as an Error message (useDeployment.ts:235). A raw Lambda
            # Cause carries stackTrace with absolute /var/task paths, the raising line
            # and a requestId -- see error_sanitizer for the live capture and ARCC
            # cnt_94E30Xo4RZHtSJ. Doing it at the seam means a future caller cannot
            # reintroduce the leak by forgetting to sanitize.
            set_parts.append("error_details = :error_details")
            expr_values[":error_details"] = sanitize_error_details(error_details)

        # Every deployment-lifecycle update also migrates records written under
        # the old started_at+30d policy. Only a successful delete may add TTL.
        update_expr = "SET " + ", ".join(set_parts) + " REMOVE #t"

        # A deployment finalizer and teardown are independent Lambda invocations.
        # The read above is useful for validation, but it is not a fence: teardown
        # can claim or finish deletion after that read and before this write. A
        # late finalizer must not turn any deletion-owned state (in progress,
        # failed, retained, or complete) back into a live status, repoint its
        # runtime slots, or remove the TTL from a deleted tombstone.
        delete_term = "attribute_not_exists(#delete_status)"
        expr_names["#delete_status"] = "delete_status"
        if finalizer_token is not None:
            # The lease holder's own in-progress cleanup is the ONE writer that
            # legitimately updates status after a delete status exists, and it was
            # silently broken: `_auto_cleanup_on_failure` writes
            # `delete_status=deleting` and only THEN marks the empty manifest
            # complete, so that write always failed the term above and was
            # swallowed by its own best-effort handler. The consequence is not
            # cosmetic -- while the manifest stays incomplete,
            # `_supplemental_failure_resources` re-infers rows on every later
            # cleanup attempt for a deployment that provably created none.
            #
            # Widened only to `deleting`, and only for a matching token. Never to a
            # terminal delete status: `deleted` carries a TTL this write's
            # `REMOVE #t` would strip, resurrecting a tombstone, and `delete_failed`
            # / `delete_retained` are a teardown's evidence. And because
            # `_claim_delete_status` REVOKES the token in the same atomic write
            # that claims the row, a teardown's `deleting` can never satisfy this.
            delete_term = f"({delete_term} OR #delete_status = :fin_deleting)"
            expr_values[":fin_deleting"] = "deleting"
        condition_expr = f"attribute_exists(deployment_id) AND {delete_term}"
        if resource_manifest_complete is True:
            # Defend the read/write race as well as the pre-check above.
            condition_expr += (
                " AND (attribute_not_exists(resource_manifest_error) "
                "OR resource_manifest_error = :manifest_error_false)"
            )
            expr_values[":manifest_error_false"] = False
        # ...and a superseded finalizer must not publish the terminal status at
        # all. The delete-status term above only catches a teardown that has
        # already claimed; the fence also catches the window before it does,
        # where the row is still clean and this invocation is nonetheless dead.
        condition_expr += _finalizer_fence(
            expr_names,
            expr_values,
            finalizer_token,
        )

        try:
            _update_item(
                self._table,
                key={"deployment_id": deployment_id},
                update_expr=update_expr,
                expr_values=expr_values,
                expr_names=expr_names,
                condition_expr=condition_expr,
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                # Do not propagate the botocore message: it can echo values from
                # the update request. The caller only needs the lifecycle outcome.
                raise DeploymentLifecycleConflict(
                    f"Deployment '{deployment_id}' no longer accepts status updates"
                ) from None
            raise
        logger.info(
            "Updated deployment %s status to %s",
            deployment_id,
            status.value,
        )

    def query_by_workflow(
        self,
        workflow_id: str,
        status_filter: str | None = None,
    ) -> list[DeploymentState]:
        """Query deployments by workflow_id using the GSI.

        Args:
            workflow_id: The workflow ID to query for.
            status_filter: Optional status to filter results (e.g. "succeeded").

        Returns:
            List of matching DeploymentState records.
        """
        kwargs: dict = {
            "IndexName": "workflow_id-index",
            "KeyConditionExpression": "workflow_id = :wid",
            "ExpressionAttributeValues": {":wid": workflow_id},
        }
        if status_filter:
            kwargs["FilterExpression"] = "#s = :status"
            kwargs["ExpressionAttributeValues"][":status"] = status_filter
            kwargs["ExpressionAttributeNames"] = {"#s": "status"}

        items: list[dict] = []
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

        return [deserialize_deployment_state(_convert_decimals_to_floats(item)) for item in items]

    def query_by_user(
        self,
        user_id: str,
        status_filter: str | None = None,
    ) -> list[DeploymentState]:
        """Query deployments by user_id using the GSI."""
        kwargs: dict = {
            "IndexName": "user_id-index",
            "KeyConditionExpression": "user_id = :uid",
            "ExpressionAttributeValues": {":uid": user_id},
        }
        if status_filter:
            kwargs["FilterExpression"] = "#s = :status"
            kwargs["ExpressionAttributeValues"][":status"] = status_filter
            kwargs["ExpressionAttributeNames"] = {"#s": "status"}

        items: list[dict] = []
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

        return [deserialize_deployment_state(_convert_decimals_to_floats(item)) for item in items]

    def set_registry_record(
        self,
        deployment_id: str,
        record_id: str,
        record_status: str,
        *,
        finalizer_token: str | None = None,
    ) -> None:
        """Persist the AWS Agent Registry record id + status onto a deployment.

        Written directly as top-level attributes (aws_registry_record_id /
        aws_registry_status) rather than adding them to the DeploymentState model
        + serializer — keeps the auto-register hook (Loom-study 0.4) additive and
        avoids a schema migration. Read back opportunistically by the teardown
        cascade (0.7) and any future governance-sync. Best-effort caller.

        Conditioned on the row existing, which it was not: ``UpdateItem`` creates
        an absent item, so a registry id written against a deleted-and-expired
        deployment used to resurrect it as a row holding nothing the teardown
        cascade or the status path can parse. The fence additionally stops a
        superseded finalizer from pointing a live deployment at the registry
        record of a run that no longer owns it.
        """
        expr_values: dict = {":r": record_id, ":s": record_status}
        expr_names: dict = {}
        fence = _lifecycle_fence(expr_names, expr_values, finalizer_token)
        try:
            _update_item(
                self._table,
                key={"deployment_id": deployment_id},
                update_expr="SET aws_registry_record_id = :r, aws_registry_status = :s",
                expr_values=expr_values,
                expr_names=expr_names,
                condition_expr="attribute_exists(deployment_id)" + fence,
            )
        except ClientError as exc:
            self._raise_if_fenced_out(exc, deployment_id, finalizer_token)
            raise

    def get_registry_record_id(self, deployment_id: str) -> str | None:
        """Return the stored AWS Agent Registry record id, or None."""
        try:
            item = self._table.get_item(Key={"deployment_id": deployment_id}).get("Item") or {}
            return item.get("aws_registry_record_id") or None
        except Exception:  # noqa: BLE001
            return None

    def scan_pending_enforce(self, max_items: int = 200) -> list[DeploymentState]:
        """Return deployments whose Cedar ENFORCE policy plane needs reconciling.

        Used by the scheduled policy-sweep (EventBridge) so a permit converges to
        ACTIVE even when NO user touchpoint (invoke/status poll) fires after the
        gateway's authorization plane finishes converging (20-59+ min, AWS-side).
        Without this self-drive, a deployed-and-idle ENFORCE agent's tool plane
        can stay fail-closed (deny-all) indefinitely (observed live in P-PLAT-027).

        Two classes are returned:
        1. ``enforce_pending`` set (non-null) — the classic pending-promotion row.
        2. ``mode == "ENFORCE"`` with an ``engine_id`` — the reconcile class. A
           policy that reached ACTIVE (so ``enforce_pending`` was cleared to null)
           can REGRESS to UPDATE_FAILED when AgentCore's gateway-authz plane
           re-validates it; once the pending payload is null, nothing would ever
           re-drive it and the tool plane silently goes deny-all forever. The
           promoter re-checks the live policy status for these and re-drives
           ``update_policy`` if it is not ACTIVE (idempotent no-op when healthy).
           This is the fix for the >2h non-convergence found in production-
           readiness testing.

        A bounded FilterExpression scan is acceptable here: ENFORCE rows are rare
        and this runs on a schedule (not per-request). `max_items` caps the sweep
        so a large table can't blow the Lambda budget; the next tick picks up any
        remainder.
        """
        items: list[dict] = []
        kwargs: dict = {
            "FilterExpression": (
                "(attribute_exists(policy_result.enforce_pending) AND policy_result.enforce_pending <> :null) "
                "OR (policy_result.#m = :enforce AND attribute_exists(policy_result.engine_id))"
            ),
            "ExpressionAttributeNames": {"#m": "mode"},
            "ExpressionAttributeValues": {":null": None, ":enforce": "ENFORCE"},
        }
        while len(items) < max_items:
            resp = self._table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

        return [deserialize_deployment_state(_convert_decimals_to_floats(item)) for item in items[:max_items]]

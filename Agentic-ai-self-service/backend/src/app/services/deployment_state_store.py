"""DynamoDB storage adapter for deployment state persistence.

This module provides a DynamoDB-backed store for deployment execution state,
following the same pattern as ``dynamodb_storage.py``.  Each deployment record
tracks progress through the Step Functions state machine and is automatically
cleaned up via DynamoDB TTL after 30 days.

Requirements: 4.1, 4.2, 4.3
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional
import logging

import boto3

from app.models.deployment_models import (
    DeploymentState,
    DeploymentStatusEnum,
    DeploymentStepName,
)

logger = logging.getLogger(__name__)

# TTL offset: 30 days expressed in seconds
_TTL_DAYS = 30


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


def _put_item(table, item: dict) -> dict:
    """Write an item to the DynamoDB table.

    Args:
        table: boto3 DynamoDB Table object
        item: Dictionary representing the item to write

    Returns:
        DynamoDB put_item response
    """
    return table.put_item(Item=item)


def _get_item(table, key: dict) -> Optional[dict]:
    """Read an item from the DynamoDB table by key.

    Args:
        table: boto3 DynamoDB Table object
        key: Dictionary with the partition key

    Returns:
        The item dict if found, None otherwise
    """
    response = table.get_item(Key=key)
    return response.get("Item")


def _update_item(
    table,
    key: dict,
    update_expr: str,
    expr_values: dict,
    expr_names: Optional[dict] = None,
) -> dict:
    """Update specific attributes of an item in the DynamoDB table.

    Args:
        table: boto3 DynamoDB Table object
        key: Dictionary with the partition key
        update_expr: DynamoDB UpdateExpression string
        expr_values: ExpressionAttributeValues mapping
        expr_names: Optional ExpressionAttributeNames mapping

    Returns:
        DynamoDB update_item response
    """
    kwargs = {
        "Key": key,
        "UpdateExpression": update_expr,
        "ExpressionAttributeValues": expr_values,
    }
    if expr_names:
        kwargs["ExpressionAttributeNames"] = expr_names
    return table.update_item(**kwargs)


# ============================================================================
# Serialization Helpers
# ============================================================================


def _compute_ttl(started_at: datetime) -> int:
    """Compute a TTL value 30 days from *started_at* as a Unix epoch integer.

    Args:
        started_at: Deployment start timestamp (should be timezone-aware UTC).

    Returns:
        Unix epoch seconds 30 days after *started_at*.
    """
    expiry = started_at + timedelta(days=_TTL_DAYS)
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


def serialize_deployment_state(state: DeploymentState) -> dict:
    """Serialize a DeploymentState to a DynamoDB-compatible dict.

    * Uses Pydantic ``model_dump(mode="json")`` so datetime fields become
      ISO 8601 strings and enums become their string values.
    * Converts any remaining floats to Decimal.
    * Computes and sets the ``ttl`` attribute to 30 days from ``started_at``.

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
    # Always recompute TTL from started_at
    item["ttl"] = _compute_ttl(state.started_at)
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
    Deployment_State_Table.  Every write recomputes the TTL so records
    are automatically cleaned up 30 days after the deployment started.

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

        Computes the TTL and persists the full item.

        Args:
            state: The initial DeploymentState (typically status=pending).

        Returns:
            The persisted DeploymentState with TTL set.
        """
        # Ensure TTL is computed from started_at
        ttl = _compute_ttl(state.started_at)
        state = state.model_copy(update={"ttl": ttl})

        item = serialize_deployment_state(state)
        _put_item(self._table, item)
        logger.info("Created deployment state: %s", state.deployment_id)
        return state

    def get(self, deployment_id: str) -> Optional[DeploymentState]:
        """Retrieve a deployment state record by deployment_id.

        Args:
            deployment_id: Partition key value.

        Returns:
            The DeploymentState if found, None otherwise.
        """
        item = _get_item(self._table, {"deployment_id": deployment_id})
        if item is None:
            return None
        return deserialize_deployment_state(item)

    def update_step(
        self,
        deployment_id: str,
        step: DeploymentStepName,
        status: DeploymentStatusEnum = DeploymentStatusEnum.IN_PROGRESS,
    ) -> None:
        """Update the current step and status of a deployment.

        Also recomputes the TTL to ensure it stays 30 days from started_at.

        Args:
            deployment_id: Partition key value.
            step: The new current step.
            status: The new status (defaults to in_progress).
        """
        # We need started_at to recompute TTL — fetch the existing record
        existing = self.get(deployment_id)
        if existing is None:
            raise ValueError(f"Deployment '{deployment_id}' not found")

        ttl = _compute_ttl(existing.started_at)

        _update_item(
            self._table,
            key={"deployment_id": deployment_id},
            update_expr="SET current_step = :step, #s = :status, #t = :ttl",
            expr_values={
                ":step": step.value,
                ":status": status.value,
                ":ttl": ttl,
            },
            expr_names={
                "#s": "status",
                "#t": "ttl",
            },
        )
        logger.info(
            "Updated deployment %s: step=%s, status=%s",
            deployment_id,
            step.value,
            status.value,
        )

    def record_resource(self, deployment_id: str, resource: dict) -> None:
        """Atomically append one created sub-resource to ``created_resources``.

        Each *resource* is a small dict the delete path can act on, e.g.
        ``{"type": "memory", "id": "mem-123", "region": "us-east-1"}`` or
        ``{"type": "iam_role", "name": "AgentCoreMemory-foo"}``. Uses DynamoDB
        ``list_append`` + ``if_not_exists`` so PARALLEL step handlers appending
        concurrently never clobber each other's entries. Best-effort: a failure
        here must not fail the deploy (the resource still exists; teardown also
        has the *_result fallbacks), so callers should not let this raise.
        """
        try:
            _update_item(
                self._table,
                key={"deployment_id": deployment_id},
                update_expr=(
                    "SET created_resources = "
                    "list_append(if_not_exists(created_resources, :empty), :r)"
                ),
                expr_values={
                    ":r": [_convert_floats_to_decimals(resource)],
                    ":empty": [],
                },
            )
        except Exception as exc:  # noqa: BLE001
            # SECURITY (CodeQL py/clear-text-logging-sensitive-data): log only the
            # resource TYPE and the exception CLASS — never the resource dict or a
            # full traceback, which could carry a secret-bearing local in frame.
            logger.warning(
                "record_resource failed for %s (non-fatal): type=%s err=%s",
                deployment_id,
                str(resource.get("type")),
                type(exc).__name__,
            )

    def update_status(
        self,
        deployment_id: str,
        status: DeploymentStatusEnum,
        *,
        completed_at: Optional[datetime] = None,
        runtime_endpoint: Optional[str] = None,
        runtime_id: Optional[str] = None,
        runtime_arn: Optional[str] = None,
        gateway_url: Optional[str] = None,
        gateway_result: Optional[dict] = None,
        policy_result: Optional[dict] = None,
        memory_result: Optional[dict] = None,
        knowledge_base_result: Optional[dict] = None,
        guardrails_result: Optional[dict] = None,
        mcp_server_runtime_id: Optional[str] = None,
        harness_id: Optional[str] = None,
        harness_arn: Optional[str] = None,
        deployment_mode: Optional[str] = None,
        error_details: Optional[str] = None,
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
            deployment_mode: Authoring path ("runtime" | "harness").
            error_details: Error description (if failed).
        """
        existing = self.get(deployment_id)
        if existing is None:
            raise ValueError(f"Deployment '{deployment_id}' not found")

        ttl = _compute_ttl(existing.started_at)

        # Build dynamic update expression from provided kwargs
        set_parts = ["#s = :status", "#t = :ttl"]
        expr_values: dict = {
            ":status": status.value,
            ":ttl": ttl,
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

        if deployment_mode is not None:
            set_parts.append("deployment_mode = :deployment_mode")
            expr_values[":deployment_mode"] = deployment_mode

        if error_details is not None:
            set_parts.append("error_details = :error_details")
            expr_values[":error_details"] = error_details

        update_expr = "SET " + ", ".join(set_parts)

        _update_item(
            self._table,
            key={"deployment_id": deployment_id},
            update_expr=update_expr,
            expr_values=expr_values,
            expr_names=expr_names,
        )
        logger.info(
            "Updated deployment %s status to %s",
            deployment_id,
            status.value,
        )

    def query_by_workflow(
        self,
        workflow_id: str,
        status_filter: Optional[str] = None,
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
        status_filter: Optional[str] = None,
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

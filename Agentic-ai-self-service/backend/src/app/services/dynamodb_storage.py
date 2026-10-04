"""DynamoDB storage service for workflow persistence.

This module provides a DynamoDB-backed storage adapter for workflows,
replacing the in-memory WorkflowStorage when deployed to AWS.

Requirements: 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9
"""

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

from app.models import WorkflowDefinition
from app.services.storage import WorkflowRevisionConflict

logger = logging.getLogger(__name__)

__all__ = ["DynamoDBWorkflowStorage", "WorkflowRevisionConflict"]


def _revision_fence(expected: int) -> tuple[str, dict]:
    """The ConditionExpression that pins a workflow row to the revision a save was built on.

    A row written before the ``revision`` attribute existed reads as 0 (the model default), so a
    save built on revision 0 must also accept a row that has no attribute at all; the first save
    stamps 1 and from then on the fence is exact. Mirrors ``flow_storage._version_fence``.
    """
    if expected == 0:
        return "attribute_not_exists(revision) OR revision = :expected", {":expected": 0}
    return "revision = :expected", {":expected": expected}


# ============================================================================
# Boto3 Wrapper Functions
# ============================================================================


def _get_dynamodb_resource(region: str):
    """Create and return a boto3 DynamoDB resource.

    Wrapper around boto3.resource('dynamodb') to centralize
    resource creation and allow for easier testing.

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


def _conditional_put_item(
    table,
    item: dict,
    *,
    condition_expression: str,
    expression_attribute_values: dict | None = None,
) -> dict:
    """Write an item only if ``condition_expression`` holds on the row as it is NOW.

    This is the primitive behind every compare-and-set write (F-15). A plain ``put_item`` after a
    ``get_item`` is last-writer-wins: two callers that both read the same row both land, and the
    second silently discards the first. The caller names the fence (``attribute_not_exists(pk)``
    for a create, ``version = :v`` for an update) and DynamoDB evaluates it atomically with the
    write. A failed fence surfaces as ``ClientError`` with code ``ConditionalCheckFailedException``;
    the caller decides what that means.

    Args:
        table: boto3 DynamoDB Table object
        item: Dictionary representing the item to write
        condition_expression: DynamoDB condition expression string
        expression_attribute_values: Values referenced by the condition (``:name`` placeholders)

    Returns:
        DynamoDB put_item response
    """
    kwargs = {"Item": item, "ConditionExpression": condition_expression}
    if expression_attribute_values:
        kwargs["ExpressionAttributeValues"] = expression_attribute_values
    return table.put_item(**kwargs)


def _get_item(table, key: dict) -> dict | None:
    """Read an item from the DynamoDB table by key.

    Args:
        table: boto3 DynamoDB Table object
        key: Dictionary with the partition key

    Returns:
        The item dict if found, None otherwise
    """
    response = table.get_item(Key=key)
    return response.get("Item")


def _delete_item(table, key: dict) -> dict:
    """Delete an item from the DynamoDB table.

    Args:
        table: boto3 DynamoDB Table object
        key: Dictionary with the partition key

    Returns:
        DynamoDB delete_item response
    """
    return table.delete_item(Key=key)


def _scan_table(table) -> list[dict]:
    """Scan all items from the DynamoDB table.

    Handles pagination to retrieve all items.

    Args:
        table: boto3 DynamoDB Table object

    Returns:
        List of all items in the table
    """
    items = []
    response = table.scan()
    items.extend(response.get("Items", []))

    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))

    return items


# ============================================================================
# Serialization Helpers
# ============================================================================


def _convert_floats_to_decimals(obj):
    """Recursively convert float values to Decimal for DynamoDB compatibility.

    DynamoDB's boto3 resource API does not accept Python floats;
    all numeric values must be Decimal instances. DynamoDB also has
    a minimum magnitude of ~1e-130, so very small floats are clamped to 0.

    Args:
        obj: A JSON-compatible Python object (dict, list, or scalar)

    Returns:
        The same structure with floats replaced by Decimals
    """
    if isinstance(obj, float):
        # DynamoDB cannot store numbers with magnitude < ~1e-130
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

    When reading from DynamoDB, numeric values come back as Decimal.
    Pydantic models expect Python floats.

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


def _serialize_workflow(workflow: WorkflowDefinition) -> dict:
    """Serialize a WorkflowDefinition to a DynamoDB-compatible dict.

    Uses Pydantic's model_dump with mode="json" to produce a
    JSON-serializable dict. Datetime fields become ISO 8601 strings.
    The workflow_id is stored as the partition key.

    Args:
        workflow: The WorkflowDefinition to serialize

    Returns:
        Dict suitable for DynamoDB put_item
    """
    from app.services.credential_scrub import scrub_write_only_credentials

    # The typed model already excludes its declared credential fields; this catches any dict-shaped
    # configuration that a future model widens, so storage never holds a raw credential.
    item = scrub_write_only_credentials(workflow.model_dump(mode="json"))
    # Ensure workflow_id is set as the partition key
    item["workflow_id"] = item.pop("id")
    # DynamoDB requires Decimal instead of float
    item = _convert_floats_to_decimals(item)
    return item


def _deserialize_workflow(item: dict) -> WorkflowDefinition:
    """Deserialize a DynamoDB item back to a WorkflowDefinition.

    Maps the DynamoDB partition key (workflow_id) back to the
    Pydantic model's 'id' field and validates the full object.

    Args:
        item: DynamoDB item dict

    Returns:
        Validated WorkflowDefinition instance
    """
    data = dict(item)
    # Map partition key back to model field
    data["id"] = data.pop("workflow_id")
    # Convert DynamoDB Decimals back to floats for Pydantic
    data = _convert_decimals_to_floats(data)
    return WorkflowDefinition.model_validate(data)


# ============================================================================
# DynamoDB Workflow Storage Class
# ============================================================================


class DynamoDBWorkflowStorage:
    """DynamoDB-backed storage for workflows.

    Implements the same interface as the in-memory WorkflowStorage
    but persists workflows to a DynamoDB table.

    Requirements: 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9
    """

    def __init__(self, table_name: str, region: str) -> None:
        """Initialize DynamoDB storage.

        Args:
            table_name: Name of the DynamoDB table
            region: AWS region where the table exists
        """
        self._table_name = table_name
        self._region = region
        self._dynamodb = _get_dynamodb_resource(region)
        self._table = _get_table(self._dynamodb, table_name)
        logger.info(
            "Initialized DynamoDB storage: table=%s, region=%s",
            table_name,
            region,
        )

    def create(self, workflow: WorkflowDefinition) -> WorkflowDefinition:
        """Create a new workflow in DynamoDB.

        Generates a UUID if the workflow has no ID, sets created_at
        and updated_at timestamps, then writes to DynamoDB.

        Args:
            workflow: The workflow to create

        Returns:
            The created workflow with generated ID and timestamps

        Raises:
            ValueError: If a workflow with the same ID already exists
        """
        if not workflow.id:
            workflow = workflow.model_copy(update={"id": str(uuid.uuid4())})

        # Check for existing workflow
        existing = _get_item(self._table, {"workflow_id": workflow.id})
        if existing is not None:
            raise ValueError(f"Workflow with ID '{workflow.id}' already exists")

        now = datetime.now(timezone.utc)
        workflow = workflow.model_copy(
            update={
                "created_at": now,
                "updated_at": now,
                "revision": 0,
            }
        )

        item = _serialize_workflow(workflow)
        # The pre-read above is a courtesy; the condition is what makes two creates of one id
        # unable to both land (F-15).
        try:
            _conditional_put_item(self._table, item, condition_expression="attribute_not_exists(workflow_id)")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            raise ValueError(f"Workflow with ID '{workflow.id}' already exists") from exc
        logger.info("Created workflow: %s", workflow.id)
        return workflow

    def get(self, workflow_id: str) -> WorkflowDefinition | None:
        """Get a workflow by ID from DynamoDB.

        Args:
            workflow_id: The workflow ID

        Returns:
            The workflow if found, None otherwise
        """
        item = _get_item(self._table, {"workflow_id": workflow_id})
        if item is None:
            return None
        return _deserialize_workflow(item)

    def get_owner_sub_unvalidated(self, workflow_id: str) -> tuple[bool, str | None]:
        """Read one row's ``owner_sub`` WITHOUT parsing it into a model.

        This exists for exactly one caller: deleting a row that ``get`` cannot
        deserialize. Every ownership check in the router reads ``owner_sub`` off a
        parsed :class:`WorkflowDefinition`, so when parsing is the thing that fails,
        the authorization step fails first and the row can never be removed — the
        API has no remedy for its own corrupt data. A row written before the
        validation fix in ``routers/workflows.py`` is exactly that row, and
        ``list_all`` swallows its deserialize failure, so nobody can even see it.

        Returns ``(exists, owner_sub)``. ``owner_sub`` is returned only when it is a
        string: anything else (absent, a number, a dict) is reported as ``None``,
        which ``assert_owner`` treats as a pre-tenancy record. It is deliberately
        NOT used to grant access on any read path — only to decide whether the
        caller may delete.
        """
        item = _get_item(self._table, {"workflow_id": workflow_id})
        if item is None:
            return (False, None)
        owner_sub = item.get("owner_sub")
        return (True, owner_sub if isinstance(owner_sub, str) else None)

    def update(
        self,
        workflow_id: str,
        workflow: WorkflowDefinition,
        *,
        expected_revision: int | None = None,
    ) -> WorkflowDefinition | None:
        """Update an existing workflow in DynamoDB as a compare-and-set on ``revision`` (F-15).

        Preserves the original workflow_id and created_at, updates the updated_at timestamp and
        advances ``revision``. The write is conditioned on the row still being at the revision
        this method READ: the in-Python ``expected_revision`` check catches a client whose copy
        is stale, and the DynamoDB condition catches the other writer that landed between this
        read and this put. A client that names no revision is still fenced on the row it read.

        Args:
            workflow_id: The ID of the workflow to update
            workflow: The updated workflow data
            expected_revision: The ``revision`` the caller built this save on (None for a
                client that does not send one)

        Returns:
            The updated workflow if found, None otherwise

        Raises:
            WorkflowRevisionConflict: the row is not at ``expected_revision`` / moved under the
                write; ``current`` is the row as it is now
        """
        existing_item = _get_item(self._table, {"workflow_id": workflow_id})
        if existing_item is None:
            return None

        existing = _deserialize_workflow(existing_item)
        if expected_revision is not None and existing.revision != expected_revision:
            raise WorkflowRevisionConflict(workflow_id, expected_revision, existing)
        updated = workflow.model_copy(
            update={
                "id": workflow_id,
                "created_at": existing.created_at,
                "updated_at": datetime.now(timezone.utc),
                "revision": existing.revision + 1,
            }
        )

        item = _serialize_workflow(updated)
        condition, values = _revision_fence(existing.revision)
        try:
            _conditional_put_item(
                self._table,
                item,
                condition_expression=condition,
                expression_attribute_values=values,
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            current_item = _get_item(self._table, {"workflow_id": workflow_id})
            current = _deserialize_workflow(current_item) if current_item is not None else None
            raise WorkflowRevisionConflict(
                workflow_id,
                expected_revision if expected_revision is not None else existing.revision,
                current,
            ) from exc
        logger.info("Updated workflow: %s (revision %s)", workflow_id, updated.revision)
        return updated

    def delete(self, workflow_id: str) -> bool:
        """Delete a workflow by ID from DynamoDB.

        Args:
            workflow_id: The workflow ID

        Returns:
            True if deleted, False if not found
        """
        existing = _get_item(self._table, {"workflow_id": workflow_id})
        if existing is None:
            return False

        _delete_item(self._table, {"workflow_id": workflow_id})
        logger.info("Deleted workflow: %s", workflow_id)
        return True

    def list_all(self) -> list[WorkflowDefinition]:
        """List all workflows from DynamoDB.

        Scans the entire table and deserializes all items.

        Returns:
            List of all workflows
        """
        items = _scan_table(self._table)
        workflows = []
        for item in items:
            try:
                workflows.append(_deserialize_workflow(item))
            except Exception as e:
                logger.warning("Failed to deserialize workflow item: %s", e)
        return workflows

    def clear(self) -> None:
        """Clear all workflows from DynamoDB (for testing).

        Scans the table and deletes each item individually.
        """
        items = _scan_table(self._table)
        for item in items:
            _delete_item(self._table, {"workflow_id": item["workflow_id"]})
        logger.info("Cleared all workflows from table %s", self._table_name)

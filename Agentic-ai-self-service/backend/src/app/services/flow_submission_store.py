"""DynamoDB storage adapter for Flow Submissions.

Provides CRUD operations and GSI queries for ``FlowSubmission`` records.
Follows the same patterns as ``deployment_state_store.py``.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
import logging

import boto3

from app.models.catalog_models import FlowSubmission, ToolStatus

logger = logging.getLogger(__name__)


# ============================================================================
# Boto3 Wrapper Functions
# ============================================================================


def _get_dynamodb_resource(region: str):
    return boto3.resource("dynamodb", region_name=region)


def _get_table(dynamodb_resource, table_name: str):
    return dynamodb_resource.Table(table_name)


def _put_item(table, item: dict) -> dict:
    return table.put_item(Item=item)


def _get_item(table, key: dict) -> Optional[dict]:
    response = table.get_item(Key=key)
    return response.get("Item")


def _update_item(
    table,
    key: dict,
    update_expr: str,
    expr_values: dict,
    expr_names: Optional[dict] = None,
    condition_expr: Optional[str] = None,
) -> dict:
    kwargs = {
        "Key": key,
        "UpdateExpression": update_expr,
        "ExpressionAttributeValues": expr_values,
    }
    if expr_names:
        kwargs["ExpressionAttributeNames"] = expr_names
    if condition_expr:
        kwargs["ConditionExpression"] = condition_expr
    return table.update_item(**kwargs)


def _delete_item(table, key: dict) -> dict:
    return table.delete_item(Key=key)


# ============================================================================
# Serialization Helpers
# ============================================================================


def _convert_floats_to_decimals(obj):
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
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _convert_decimals_to_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert_decimals_to_floats(v) for v in obj]
    return obj


def serialize_flow_submission(submission: FlowSubmission) -> dict:
    item = submission.model_dump(mode="json")
    # DynamoDB GSI keys cannot be NULL — strip None values so items
    # missing a sort-key attribute are simply omitted from the index.
    item = {k: v for k, v in item.items() if v is not None}
    return _convert_floats_to_decimals(item)


def deserialize_flow_submission(item: dict) -> FlowSubmission:
    data = _convert_decimals_to_floats(dict(item))
    return FlowSubmission.model_validate(data)


# ============================================================================
# Flow Submission Store Class
# ============================================================================


class FlowSubmissionStore:
    """DynamoDB-backed store for Flow Submissions.

    Supports CRUD and status-based queries (via GSI).
    """

    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._region = region
        self._dynamodb = _get_dynamodb_resource(region)
        self._table = _get_table(self._dynamodb, table_name)
        logger.info("Initialized FlowSubmissionStore: table=%s, region=%s", table_name, region)

    def create(self, submission: FlowSubmission) -> FlowSubmission:
        item = serialize_flow_submission(submission)
        _put_item(self._table, item)
        logger.info(
            "Created flow submission: %s (%s)",
            submission.submission_id,
            submission.name,
        )
        return submission

    def get(self, submission_id: str) -> Optional[FlowSubmission]:
        item = _get_item(self._table, {"submission_id": submission_id})
        if item is None:
            return None
        return deserialize_flow_submission(item)

    def update(self, submission_id: str, updates: dict) -> Optional[FlowSubmission]:
        existing = self.get(submission_id)
        if existing is None:
            return None

        updates["updated_at"] = datetime.now(timezone.utc).isoformat()

        set_parts = []
        expr_values = {}
        expr_names = {}

        for i, (field, value) in enumerate(updates.items()):
            placeholder = f":v{i}"
            name_placeholder = f"#n{i}"
            set_parts.append(f"{name_placeholder} = {placeholder}")
            expr_values[placeholder] = _convert_floats_to_decimals(value)
            expr_names[name_placeholder] = field

        update_expr = "SET " + ", ".join(set_parts)

        _update_item(
            self._table,
            key={"submission_id": submission_id},
            update_expr=update_expr,
            expr_values=expr_values,
            expr_names=expr_names,
        )
        logger.info("Updated flow submission: %s", submission_id)
        return self.get(submission_id)

    def update_with_condition(
        self, submission_id: str, updates: dict, expected_status: ToolStatus
    ) -> Optional[FlowSubmission]:
        """Update only if current status matches expected_status."""
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()

        set_parts = []
        expr_values = {":expected_status": expected_status.value}
        expr_names = {}

        for i, (field, value) in enumerate(updates.items()):
            placeholder = f":v{i}"
            name_placeholder = f"#n{i}"
            set_parts.append(f"{name_placeholder} = {placeholder}")
            expr_values[placeholder] = _convert_floats_to_decimals(value)
            expr_names[name_placeholder] = field

        update_expr = "SET " + ", ".join(set_parts)
        condition_expr = "#status_field = :expected_status"
        expr_names["#status_field"] = "status"

        try:
            _update_item(
                self._table,
                key={"submission_id": submission_id},
                update_expr=update_expr,
                expr_values=expr_values,
                expr_names=expr_names,
                condition_expr=condition_expr,
            )
            logger.info("Conditionally updated flow submission: %s", submission_id)
            return self.get(submission_id)
        except self._dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
            logger.warning(
                "Conditional update failed for submission %s (expected status: %s)",
                submission_id,
                expected_status.value,
            )
            return None

    def delete(self, submission_id: str) -> bool:
        try:
            _delete_item(self._table, {"submission_id": submission_id})
            logger.info("Deleted flow submission: %s", submission_id)
            return True
        except Exception as e:
            logger.warning("Failed to delete submission %s: %s", submission_id, e)
            return False

    def list_by_status(self, status: ToolStatus) -> list[FlowSubmission]:
        items_raw = []
        kwargs = {
            "IndexName": "status-index",
            "KeyConditionExpression": "#s = :status",
            "ExpressionAttributeValues": {":status": status.value},
            "ExpressionAttributeNames": {"#s": "status"},
        }
        while True:
            response = self._table.query(**kwargs)
            items_raw.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key

        return [deserialize_flow_submission(item) for item in items_raw]

    def list_approved(self) -> list[FlowSubmission]:
        return self.list_by_status(ToolStatus.APPROVED)

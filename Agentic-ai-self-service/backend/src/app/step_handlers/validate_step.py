"""Step handler: Validate workflow before deployment.

Receives a workflow_id, loads the workflow from DynamoDB, runs the
ValidationEngine, updates the Deployment_State_Table with current_step,
and returns the validation result.

Requirements: 3.2
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import app.services._otel_platform  # noqa: F401

import logging
import os

from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services.deployment_state_store import DeploymentStateStore
from app.services.dynamodb_storage import DynamoDBWorkflowStorage
from app.services.validation import ValidationEngine

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    """Read an environment variable with a fallback."""
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    """Create a DeploymentStateStore from environment variables."""
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def _get_workflow_storage() -> DynamoDBWorkflowStorage:
    """Create a DynamoDBWorkflowStorage from environment variables."""
    return DynamoDBWorkflowStorage(
        table_name=_get_env("WORKFLOWS_TABLE_NAME", "Workflows"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def handler(event: dict, context) -> dict:
    """Lambda handler for the validate workflow step.

    Args:
        event: Step Functions event with ``deployment_id`` and ``workflow_id``.
        context: Lambda context (unused).

    Returns:
        Dict with ``is_valid``, ``errors``, and passthrough fields for the
        next step in the state machine.
    """
    deployment_id = event.get("deployment_id", "")
    workflow_id = event.get("workflow_id", "")

    try:
        store = _get_deployment_store()
        store.update_step(
            deployment_id,
            DeploymentStepName.VALIDATE,
            DeploymentStatusEnum.IN_PROGRESS,
        )

        workflow_storage = _get_workflow_storage()
        workflow = workflow_storage.get(workflow_id)

        if workflow is None:
            return {
                **event,
                "is_valid": False,
                "errors": [f"Workflow '{workflow_id}' not found"],
            }

        engine = ValidationEngine()
        result = engine.validate_workflow(workflow)

        return {
            **event,
            "is_valid": result.is_valid,
            "errors": [e.model_dump(mode="json") for e in result.errors],
        }

    except Exception as exc:
        logger.exception("Validate step failed for deployment %s", deployment_id)
        return {
            **event,
            "is_valid": False,
            "errors": [str(exc)],
        }

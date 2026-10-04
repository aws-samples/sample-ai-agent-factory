"""Step handler: validate the DEPLOYMENT PAYLOAD before any resource is created.

It used to load a workflow from DynamoDB by id and validate that. It never could: the id in
the event is ``request.node_id``, a canvas NODE id, and nothing writes the canvas to the
``workflows`` table. Measured live on acfe2e-p0920 -- ``workflows`` 0 items, ``flows`` 7 items,
and ``DeployRequest`` carries no flow id at all -- so ``is_valid`` was invariably ``False`` with
``["Workflow '<node id>' not found"]``, and the only reason any deployment ever worked is that
nothing read the verdict.

So the verdict's source is the authoritative payload the event already carries, validated by the
one shared pure validator in ``services/deployment_payload_validation`` that the API boundary
also uses. Revalidating here is deliberate rather than redundant: ARCC cnt_jljdNeOwgPnFx2
requires a downstream step to authorize independently instead of trusting that its caller did.

Validating the payload rather than a stored row also closes a staleness window between saving the
canvas and deploying it. The event now carries ``workflow_id`` as a real (optional) flow id and
``node_id`` separately; the flow's OWNERSHIP is checked at the API boundary before any side
effect, and this step does not load the flow, so there is no lookup here to repoint at another
tenant's row.

History: this step once looked the workflow up in a table it was never written to, so it
answered ``is_valid=False`` for every deployment, and the state machine ran every
resource-creating task anyway because its gate was fail-open; the two halves masked each
other. See the ordering note in infra/stacks/platform/step_functions.py: the state machine's
gate and this handler must change together, because closing the gate over the old table
lookup is a 100% outage.

Requirements: 3.2
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import logging
import os

import app.services._otel_platform  # noqa: F401
from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services.deployment_payload_validation import (
    PayloadPhase,
    ValidationContext,
    validate_deployment_payload,
)
from app.services.deployment_state_store import DeploymentStateStore

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


def _validation_context(context) -> ValidationContext:
    """Where this deployment actually runs, from TRUSTED sources only.

    The account comes from the executing Lambda's own ``invoked_function_arn`` and the region
    from the runtime's own environment. Neither can be influenced by the deployment payload,
    which is the whole point: for a home deployment the payload's ``target_account_id`` and
    ``target_region`` are legitimately ``None``, so a staged secret ARN would otherwise have no
    account to be checked against and one naming another account would pass.

    Parsing the ARN cannot raise here -- a malformed or absent one yields ``None``, which
    weakens exactly that one check rather than failing the deployment. An exception in this
    helper would be caught below and turned into a refusal, which would make a cosmetic ARN
    surprise into an outage.
    """
    account: str | None = None
    arn = getattr(context, "invoked_function_arn", "") or ""
    parts = arn.split(":")
    if len(parts) >= 5 and parts[4].isdigit() and len(parts[4]) == 12:
        account = parts[4]
    region = _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "")) or None
    return ValidationContext(home_account_id=account, home_region=region)


def _rejected(event: dict, errors: list[dict[str, str]], summary: str) -> dict:
    """The single shape of a refusal, so every path below agrees on it.

    ``error`` is set as well as ``is_valid``/``errors`` because ``status_update_step`` reads
    ``event["error"]`` and then falls back to ``error_info.Cause``; it never reads ``is_valid``
    or ``errors``. Without ``error`` the deployment would be marked failed with no reason --
    failed for the wrong reason, which sends the operator to the wrong step.

    ``no_resources_created`` is asserted here because this handler is the FIRST task in the
    state machine: on this path the empty resource manifest is a certainty, not an unproven
    absence. Auto-cleanup otherwise records ``delete_retained`` with "could not prove that the
    empty manifest represented a deployment that created no resources", sending the operator
    hunting for orphans that cannot exist. The state machine sets the same field on its invalid
    branch, for the case where this handler did not run as expected at all.
    """
    return {
        **event,
        "is_valid": False,
        "errors": errors,
        "error": summary,
        "no_resources_created": {"proven": True, "reason": "rejected at ValidateWorkflow"},
    }


def handler(event: dict, context) -> dict:
    """Lambda handler for the validate step.

    Args:
        event: the normalized deployment payload the API put into the execution.
        context: Lambda context (unused).

    Returns:
        Dict with ``is_valid``, ``errors``, and passthrough fields for the next step. A false
        or absent ``is_valid`` is now GATED by the state machine's ``IsDeploymentInputValid?``
        Choice, so this return value decides whether anything gets created.
    """
    deployment_id = event.get("deployment_id", "")

    try:
        store = _get_deployment_store()
        store.update_step(
            deployment_id,
            DeploymentStepName.VALIDATE,
            DeploymentStatusEnum.IN_PROGRESS,
        )

        # PREPARED: this is the payload the state machine actually received, after credential
        # staging, so no raw secret material may exist anywhere in it and every server-authored
        # field must be present. The API boundary runs the SAME function with
        # PayloadPhase.REQUEST, where raw values are still permitted at the approved
        # write-only paths because staging has not run yet.
        result = validate_deployment_payload(
            event,
            phase=PayloadPhase.PREPARED,
            context=_validation_context(context),
        )
        if not result.is_valid:
            # Detail to the log, summary to the caller -- ARCC cnt_ik6StRHfs118ea. The
            # summary deliberately names fields and codes but never echoes a value, because
            # it lands in the deployment record that the UI renders.
            logger.warning(
                "Deployment payload rejected for %s: %s",
                deployment_id,
                result.as_error_dicts(),
            )
            return _rejected(event, result.as_error_dicts(), result.summary())

        return {**event, "is_valid": True, "errors": []}

    except Exception as exc:
        # Fails CLOSED. Before the gate existed this return was indistinguishable from a
        # pass, because nothing read it; now an exception here stops the deployment rather
        # than waving it through. The exception TYPE is kept and the message is not, because
        # the message can echo request parameters.
        logger.exception("Validate step failed for deployment %s", deployment_id)
        return _rejected(
            event,
            [{"field": "$", "code": "validator_raised", "message": type(exc).__name__}],
            f"Deployment input could not be validated ({type(exc).__name__}). No resource was created.",
        )

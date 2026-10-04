"""Provision and dispatch runtime triggers.

The trigger API stores authority in DynamoDB first, then this module creates a
deterministically named EventBridge rule.  Completion is published with the
row's provisioning token; a concurrent delete changes the row to ``deleting``
and makes completion fail, so the creator compensates the rule instead of
leaving an invisible orphan.

Cron, EventBridge, and S3 triggers all use EventBridge rules in the platform
account and target a durable SQS queue. The existing deployment Lambda consumes
that queue one message at a time. Reusing the Lambda is intentional: its
cross-account ``AgentCoreFlowsDeploymentRole`` trust path is already the one
used and live-tested by deploy, invoke, and teardown, while SQS supplies retry
and dead-letter handling beyond Lambda's short direct-async retry window.

Webhook triggers use one static API Gateway route.  Each request is authenticated
with a per-trigger HMAC secret and then sends the same durable queue envelope,
allowing the sender to receive a prompt 202 rather than waiting behind API
Gateway's integration timeout for an agent invocation.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import os
import random
import re
import socket
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.services import step_clients
from app.services.deployment_state_store import (
    DeploymentLifecycleConflict,
    DeploymentStateStore,
    FinalizerLeaseBusy,
)
from app.services.gateway_deployer import (
    _DISALLOWED_NETWORKS,
    _DiscoveryUrlBlocked,
    _DiscoveryUrlInvalid,
    _validate_outbound_url,
)
from app.services.invocation_identity import (
    InvocationIdentityError,
    memory_invocation_identity,
)
from app.services.trigger_store import (
    STATUS_ACTIVE,
    STATUS_DELETING,
    STATUS_ERROR,
    STATUS_PROVISIONING,
    TYPE_CRON,
    TYPE_EVENTBRIDGE,
    TYPE_S3,
    TYPE_WEBHOOK,
    Trigger,
    TriggerDeliveryBusy,
    TriggerDeliveryInactive,
    TriggerSecretDeletionRefused,
    delete_owned_webhook_secret,
    get_trigger_store,
)

logger = logging.getLogger(__name__)

DISPATCH_MARKER = "agentcore-trigger-dispatch-v1"
MAX_TRIGGER_EVENT_BYTES = 240 * 1024
MAX_CALLBACK_RESPONSE_BYTES = 240 * 1024
WEBHOOK_SIGNATURE_WINDOW_SECONDS = 300

_RUNTIME_ARN_RE = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z0-9-]+)?):bedrock-agentcore:"
    r"(?P<region>[a-z0-9-]+):(?P<account_id>\d{12}):runtime/"
    r"(?P<runtime_id>[A-Za-z0-9_-]+)$"
)
_RULE_ARN_RE = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z0-9-]+)?):events:"
    r"(?P<region>[a-z0-9-]+):(?P<account_id>\d{12}):rule/"
    r"(?P<rule_name>[A-Za-z0-9_.-]+)$"
)
_QUEUE_ARN_RE = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z0-9-]+)?):sqs:"
    r"(?P<region>[a-z0-9-]+):(?P<account_id>\d{12}):"
    r"(?P<queue_name>[A-Za-z0-9_-]{1,80})$"
)
_RULE_PREFIX_RE = re.compile(r"^[A-Za-z0-9_.-]{1,31}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DELIVERY_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,128}$")
_SIGNATURE_RE = re.compile(r"^(?:v1=)?(?P<digest>[0-9a-fA-F]{64})$")

_deployment_store: DeploymentStateStore | None = None

# The deployment Lambda is capped at 600 seconds. A lease slightly longer than
# that prevents a second worker from invoking the same trigger concurrently,
# while still allowing delete/retry to recover from a timed-out worker. SQS
# visibility is configured separately in CDK to the AWS-recommended multiple of
# the function timeout.
TRIGGER_DELIVERY_LEASE_SECONDS = 630
# A contended delivery has invoked nothing, so it is re-enqueued with a short jittered delay
# instead of waiting out the dispatch queue's 3600-second visibility timeout (sized at six times
# the 600-second worker). Deliveries of one deployment exclude each other through its finalizer
# lease, so without this an S3 upload that coincided with a cron delivery waited an hour, and one
# that lost five times was dead-lettered (measured live 2026-10-01). The attempt count travels in
# the message, so the requeue is bounded and never resets the queue's own receive count.
TRIGGER_CONTENTION_REQUEUE_LIMIT = 60
TRIGGER_CONTENTION_DELAY_SECONDS = (15, 45)
_CONTENTION_ATTEMPTS_KEY = "_contention_attempts"
TRIGGER_DELIVERY_RETENTION_SECONDS = 7 * 24 * 60 * 60


class TriggerProvisioningError(RuntimeError):
    """A trigger AWS resource could not be provisioned completely."""


class TriggerCleanupError(RuntimeError):
    """A trigger resource could not be removed conclusively."""


class TriggerCleanupRefused(TriggerCleanupError):
    """Persisted metadata did not prove that a resource belongs to this stack."""


class TriggerDispatchError(RuntimeError):
    """An active trigger could not safely invoke its recorded deployment."""


class TriggerDispatchContention(TriggerDispatchError):
    """Another delivery held a lease this one needs; nothing was invoked, so retry soon."""


class WebhookAuthenticationError(RuntimeError):
    """A public webhook request did not satisfy the trigger's HMAC contract."""


@dataclass(frozen=True)
class ProvisionedTriggerResources:
    """The handles published to the trigger row after provisioning."""

    eventbridge_rule_arn: str | None = None
    webhook_path: str | None = None


@dataclass(frozen=True)
class TriggerInvocation:
    """One completed AgentCore invocation, retained only in memory."""

    trigger_id: str
    delivery_id: str
    response: str
    session_id: str


def _region() -> str:
    return os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))


def _rule_prefix() -> str:
    prefix = os.environ.get("TRIGGER_RULE_PREFIX", "agentcore-trigger")
    if _RULE_PREFIX_RE.fullmatch(prefix) is None:
        raise TriggerProvisioningError("The configured trigger rule prefix is invalid")
    return prefix


def _dispatch_queue_arn() -> str:
    arn = os.environ.get("TRIGGER_DISPATCH_QUEUE_ARN", "")
    match = _QUEUE_ARN_RE.fullmatch(arn)
    if match is None:
        raise TriggerProvisioningError("The trigger dispatch queue ARN is not configured correctly")
    if match.group("region") != _region():
        raise TriggerProvisioningError("The trigger dispatch queue is outside the platform region")
    account = _platform_account_id()
    if account and match.group("account_id") != account:
        raise TriggerProvisioningError("The trigger dispatch queue is outside the platform account")
    return arn


def _dispatch_queue_url() -> str:
    url = os.environ.get("TRIGGER_DISPATCH_QUEUE_URL", "").strip()
    if not url:
        raise TriggerDispatchError("The trigger dispatch queue URL is not configured")
    return url


def _arn_account(value: str) -> str | None:
    parts = value.split(":")
    if len(parts) >= 5 and parts[4].isdigit() and len(parts[4]) == 12:
        return parts[4]
    return None


def _platform_account_id() -> str | None:
    for value in (
        os.environ.get("TRIGGER_DISPATCH_QUEUE_ARN", ""),
        os.environ.get("STATE_MACHINE_ARN", ""),
        os.environ.get("AWS_ACCOUNT_ID", ""),
    ):
        account = _arn_account(value)
        if account:
            return account
    return None


def trigger_rule_name(trigger_id: str) -> str:
    """Return the deterministic, stack-scoped EventBridge rule name."""

    if _KEY_RE.fullmatch(trigger_id) is None:
        raise TriggerProvisioningError("The trigger id cannot form an EventBridge rule name")
    name = f"{_rule_prefix()}-{trigger_id}"
    if len(name) > 64:
        raise TriggerProvisioningError("The trigger rule name exceeds the EventBridge limit")
    return name


def trigger_target_id(trigger_id: str) -> str:
    """Return the one target id owned by a deterministic trigger rule."""

    value = f"dispatch-{trigger_id}"
    if len(value) > 64:
        raise TriggerProvisioningError("The trigger target id exceeds the EventBridge limit")
    return value


def _stack_identity() -> str:
    return f"{os.environ.get('PROJECT_NAME', 'agentcore-workflow')}-{os.environ.get('ENVIRONMENT', 'dev')}-{_region()}"


def _expected_rule_tags(trigger: Trigger) -> dict[str, str]:
    return {
        "ManagedBy": "agentcore-flows",
        "Purpose": "runtime-trigger",
        "AgentCoreStack": _stack_identity(),
        "TriggerId": trigger.trigger_id,
        "RuntimeName": trigger.runtime_name,
    }


def _rule_tags(trigger: Trigger) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in _expected_rule_tags(trigger).items()]


def webhook_path(runtime_name: str, trigger_id: str) -> str:
    """Return the static API route for a webhook trigger."""

    if _KEY_RE.fullmatch(runtime_name) is None or _KEY_RE.fullmatch(trigger_id) is None:
        raise TriggerProvisioningError("The trigger cannot form a webhook path")
    return f"/hooks/{runtime_name}/{trigger_id}"


def validate_trigger_target_runtime_arn(value: str) -> None:
    """Refuse trigger activation without a canonical AgentCore runtime ARN."""

    if _RUNTIME_ARN_RE.fullmatch(value) is None:
        raise TriggerProvisioningError("Triggers require a canonical AgentCore runtime ARN")


def _input_transformer(trigger: Trigger) -> dict[str, Any]:
    """Wrap the original EventBridge event with trusted row coordinates."""

    template = {
        "_agentcore_trigger_dispatch": DISPATCH_MARKER,
        "runtime_name": trigger.runtime_name,
        "trigger_id": trigger.trigger_id,
        "delivery_event": "__AWS_EVENT__",
    }
    rendered = json.dumps(template, separators=(",", ":"), sort_keys=True)
    # The placeholder must be an unquoted JSON object in EventBridge's input
    # template. The other values went through json.dumps and cannot inject.
    rendered = rendered.replace('"__AWS_EVENT__"', "<aws_event>")
    return {
        "InputPathsMap": {"aws_event": "$"},
        "InputTemplate": rendered,
    }


def _failed_entries(response: dict[str, Any]) -> list[dict[str, Any]]:
    count = int(response.get("FailedEntryCount") or 0)
    entries = list(response.get("FailedEntries") or [])
    return entries if count or entries else []


def _describe_rule_arn(client, trigger: Trigger) -> str | None:
    """Return the deterministic rule's live ARN, or ``None`` when absent."""

    try:
        response = client.describe_rule(
            Name=trigger_rule_name(trigger.trigger_id),
            EventBusName="default",
        )
    except Exception as exc:
        if _not_found(exc):
            return None
        raise
    rule_arn = str(response.get("Arn") or "")
    _validate_rule_arn(rule_arn, trigger)
    return rule_arn


def _require_owned_rule(client, rule_arn: str, trigger: Trigger) -> None:
    """Require exact live ownership tags before mutating an existing rule."""

    response = client.list_tags_for_resource(ResourceARN=rule_arn)
    observed = {
        str(tag.get("Key")): str(tag.get("Value")) for tag in response.get("Tags", []) if tag.get("Key") is not None
    }
    expected = _expected_rule_tags(trigger)
    if any(observed.get(key) != value for key, value in expected.items()):
        raise TriggerCleanupRefused("The EventBridge rule does not carry this trigger's ownership tags")


def provision_trigger(
    trigger: Trigger,
    *,
    events_client=None,
) -> ProvisionedTriggerResources:
    """Create the AWS resource for one fenced trigger row.

    The caller publishes these handles only with
    :meth:`TriggerStore.complete_provisioning`.
    """

    if trigger.status != STATUS_PROVISIONING or not trigger.provisioning_token:
        raise TriggerProvisioningError("Only an owned provisioning row can create trigger resources")
    validate_trigger_target_runtime_arn(trigger.target_runtime_arn)

    if trigger.type == TYPE_WEBHOOK:
        if not trigger.webhook_secret_ref:
            raise TriggerProvisioningError("A webhook trigger has no authentication secret")
        return ProvisionedTriggerResources(
            webhook_path=webhook_path(trigger.runtime_name, trigger.trigger_id),
        )

    if trigger.type not in (TYPE_CRON, TYPE_EVENTBRIDGE, TYPE_S3):
        raise TriggerProvisioningError("Unsupported trigger type")

    rule_name = trigger_rule_name(trigger.trigger_id)
    client = events_client or boto3.client("events", region_name=_region())
    existing_rule_arn = _describe_rule_arn(client, trigger)
    if existing_rule_arn is not None:
        try:
            _require_owned_rule(client, existing_rule_arn, trigger)
        except TriggerCleanupRefused as exc:
            raise TriggerProvisioningError("An existing EventBridge rule is not owned by this trigger") from exc
    rule_args: dict[str, Any] = {
        "Name": rule_name,
        "State": "ENABLED",
        "Description": (f"AgentCore trigger {trigger.trigger_id} for {trigger.runtime_name}")[:512],
        "EventBusName": "default",
        "Tags": _rule_tags(trigger),
    }
    if trigger.type == TYPE_CRON:
        if not trigger.schedule:
            raise TriggerProvisioningError("A cron trigger has no schedule")
        rule_args["ScheduleExpression"] = trigger.schedule
    else:
        if not trigger.pattern:
            raise TriggerProvisioningError("An event trigger has no event pattern")
        rule_args["EventPattern"] = json.dumps(
            trigger.pattern,
            separators=(",", ":"),
            sort_keys=True,
        )

    rule_response = client.put_rule(**rule_args)
    rule_arn = str(rule_response.get("RuleArn") or "")
    _validate_rule_arn(rule_arn, trigger)
    try:
        _require_owned_rule(client, rule_arn, trigger)
    except TriggerCleanupRefused as exc:
        raise TriggerProvisioningError("The EventBridge rule did not retain this trigger's ownership tags") from exc

    target: dict[str, Any] = {
        "Id": trigger_target_id(trigger.trigger_id),
        "Arn": _dispatch_queue_arn(),
        "InputTransformer": _input_transformer(trigger),
        # EventBridge retries target-delivery failures. Once accepted, SQS owns
        # durable processing retries and the dead-letter transition.
        "RetryPolicy": {
            "MaximumEventAgeInSeconds": 3600,
            "MaximumRetryAttempts": 5,
        },
    }
    response = client.put_targets(
        Rule=rule_name,
        EventBusName="default",
        Targets=[target],
    )
    failures = _failed_entries(response)
    if failures:
        logger.error(
            "EventBridge refused %d target entry for trigger %s",
            len(failures),
            trigger.trigger_id,
        )
        raise TriggerProvisioningError("EventBridge did not attach the trigger target")

    return ProvisionedTriggerResources(eventbridge_rule_arn=rule_arn)


def _validate_rule_arn(rule_arn: str, trigger: Trigger) -> str:
    match = _RULE_ARN_RE.fullmatch(rule_arn)
    expected_name = trigger_rule_name(trigger.trigger_id)
    if match is None or match.group("rule_name") != expected_name:
        raise TriggerCleanupRefused("The recorded EventBridge rule is not the trigger's deterministic rule")
    if match.group("region") != _region():
        raise TriggerCleanupRefused("The recorded EventBridge rule is outside the platform region")
    account = _platform_account_id()
    if account and match.group("account_id") != account:
        raise TriggerCleanupRefused("The recorded EventBridge rule is outside the platform account")
    return expected_name


def _not_found(exc: Exception) -> bool:
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") in {
        "ResourceNotFoundException",
        "ResourceNotFound",
    }


def cleanup_eventbridge_trigger(
    trigger: Trigger,
    *,
    events_client=None,
) -> None:
    """Remove the deterministic rule and its one target, idempotently."""

    if trigger.type == TYPE_WEBHOOK and not trigger.eventbridge_rule_arn:
        return
    if trigger.type not in (TYPE_CRON, TYPE_EVENTBRIDGE, TYPE_S3) and not trigger.eventbridge_rule_arn:
        return
    # A legacy ``registered`` definition predates provisioning and has no AWS
    # resource. Other lifecycle states may have crashed before the ARN was
    # published, so derive the deterministic name and attempt cleanup.
    if not trigger.eventbridge_rule_arn and trigger.status not in {
        STATUS_PROVISIONING,
        STATUS_ACTIVE,
        STATUS_ERROR,
        STATUS_DELETING,
    }:
        return

    rule_name = (
        _validate_rule_arn(trigger.eventbridge_rule_arn, trigger)
        if trigger.eventbridge_rule_arn
        else trigger_rule_name(trigger.trigger_id)
    )
    client = events_client or boto3.client("events", region_name=_region())
    live_rule_arn = _describe_rule_arn(client, trigger)
    if live_rule_arn is None:
        return
    _require_owned_rule(client, live_rule_arn, trigger)
    try:
        response = client.remove_targets(
            Rule=rule_name,
            EventBusName="default",
            Ids=[trigger_target_id(trigger.trigger_id)],
        )
        failures = _failed_entries(response)
        # A target that is already absent is represented as a successful
        # remove. Any reported failure is ambiguous, so preserve the row.
        if failures:
            raise TriggerCleanupError("EventBridge did not remove the trigger target")
    except Exception as exc:
        if _not_found(exc):
            return
        raise

    # Ownership is live state, not a property of the row or of the first read.
    # Re-read immediately before the second mutation. IAM also applies the same
    # resource-tag conditions at each API call.
    live_rule_arn = _describe_rule_arn(client, trigger)
    if live_rule_arn is None:
        return
    _require_owned_rule(client, live_rule_arn, trigger)
    try:
        client.delete_rule(Name=rule_name, EventBusName="default")
    except Exception as exc:
        if not _not_found(exc):
            raise


def cleanup_trigger_resources(
    trigger: Trigger,
    *,
    events_client=None,
    secrets_client=None,
    include_secret: bool = True,
) -> None:
    """Delete every resource provisioned by the current trigger implementation.

    Metadata is deliberately left to the caller and must only be removed after
    this function returns successfully.
    """

    errors: list[Exception] = []
    try:
        cleanup_eventbridge_trigger(trigger, events_client=events_client)
    except Exception as exc:  # keep the secret attempt independent
        errors.append(exc)
    if include_secret:
        try:
            delete_owned_webhook_secret(trigger, secrets_client=secrets_client)
        except Exception as exc:
            errors.append(exc)
    if errors:
        first = errors[0]
        if isinstance(
            first,
            (TriggerCleanupRefused, TriggerSecretDeletionRefused),
        ):
            raise first
        raise TriggerCleanupError(f"{len(errors)} trigger resource cleanup operation(s) were not confirmed") from None


def _get_deployment_store() -> DeploymentStateStore:
    global _deployment_store
    if _deployment_store is None:
        _deployment_store = DeploymentStateStore(
            table_name=os.environ.get(
                "DEPLOYMENTS_TABLE_NAME",
                os.environ.get("DEPLOYMENT_TABLE_NAME", "AgentCoreDeployments"),
            ),
            region=_region(),
        )
    return _deployment_store


def _field(record: Any, name: str) -> Any:
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def _status(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _deployment_for_trigger(trigger: Trigger) -> tuple[Any, re.Match[str], Any]:
    """Resolve the exact immutable deployment authority recorded by a trigger."""

    if not trigger.deployment_id or not trigger.version_id:
        raise TriggerDispatchError("The trigger predates deploy-authority binding")
    try:
        deployment = _get_deployment_store().get(
            trigger.deployment_id,
            consistent=True,
        )
    except Exception:
        raise TriggerDispatchError("The trigger deployment could not be verified") from None
    if deployment is None:
        raise TriggerDispatchError("The trigger deployment no longer exists")
    if str(_field(deployment, "user_id") or "") != trigger.owner_sub:
        raise TriggerDispatchError("The trigger owner does not match its deployment")
    if _status(_field(deployment, "status")) != "succeeded":
        raise TriggerDispatchError("The trigger deployment is not succeeded")
    if _field(deployment, "delete_status"):
        raise TriggerDispatchError("The trigger deployment is being deleted")
    if str(_field(deployment, "deployment_id") or "") != trigger.deployment_id:
        raise TriggerDispatchError("The trigger deployment binding does not match")
    if str(_field(deployment, "version_id") or "") != trigger.version_id:
        raise TriggerDispatchError("The trigger version binding does not match")
    if str(_field(deployment, "runtime_arn") or "") != trigger.target_runtime_arn:
        raise TriggerDispatchError("The trigger runtime binding does not match")

    arn_match = _RUNTIME_ARN_RE.fullmatch(trigger.target_runtime_arn)
    if arn_match is None:
        raise TriggerDispatchError("The trigger runtime ARN is not canonical")
    if str(_field(deployment, "runtime_id") or "") != arn_match.group("runtime_id"):
        raise TriggerDispatchError("The trigger runtime id binding does not match")

    account_id = str(_field(deployment, "target_account_id") or "").strip() or None
    role_arn = str(_field(deployment, "target_role_arn") or "").strip() or None
    if bool(account_id) != bool(role_arn):
        raise TriggerDispatchError("The trigger target account binding is incomplete")
    if account_id and account_id != arn_match.group("account_id"):
        raise TriggerDispatchError("The trigger target account does not match the runtime")
    if not account_id:
        platform_account = _platform_account_id()
        if platform_account and platform_account != arn_match.group("account_id"):
            raise TriggerDispatchError("The trigger runtime is outside the platform account")

    recorded_region = str(_field(deployment, "target_region") or "").strip()
    arn_region = arn_match.group("region")
    if recorded_region and recorded_region != arn_region:
        raise TriggerDispatchError("The trigger target region does not match the runtime")
    if account_id and not recorded_region:
        raise TriggerDispatchError("The cross-account trigger has no frozen region")
    region = recorded_region or arn_region
    target_event = {
        "target_account_id": account_id,
        "target_region": region,
        "target_role_arn": role_arn,
    }
    try:
        session = step_clients.session_for_event(target_event)
    except Exception:
        raise TriggerDispatchError("The trigger target session could not be established") from None
    return deployment, arn_match, session


def _delivery_id(event: dict[str, Any]) -> str:
    candidate = str(event.get("id") or "").strip()
    if candidate and _DELIVERY_ID_RE.fullmatch(candidate):
        return candidate
    digest = hashlib.sha256(
        json.dumps(event, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"sha256-{digest[:48]}"


def _session_id(trigger: Trigger, delivery_id: str) -> str:
    digest = hashlib.sha256(f"{trigger.trigger_id}\0{delivery_id}".encode()).hexdigest()
    # 8 + 32 + 1 + 40 = 81 characters: accepted by both AgentCore Runtime
    # (33..256) and Memory (1..100), with only their shared safe alphabet.
    return f"trigger-{trigger.trigger_id}-{digest[:40]}"[:100]


def _parse_agent_response(raw_response: Any) -> str:
    if hasattr(raw_response, "read"):
        raw_response = raw_response.read()
    if isinstance(raw_response, bytes):
        raw_response = raw_response.decode("utf-8", errors="replace")
    text = str(raw_response or "")
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return text
    if isinstance(parsed, dict):
        for key in ("response", "body", "output"):
            if parsed.get(key) is not None:
                return str(parsed[key])
        return json.dumps(parsed, separators=(",", ":"), sort_keys=True)
    return str(parsed)


def _prompt(trigger: Trigger, delivery_event: dict[str, Any]) -> str:
    envelope = {
        "trigger": {
            "id": trigger.trigger_id,
            "type": trigger.type,
            "runtime_name": trigger.runtime_name,
        },
        "event": delivery_event,
    }
    return (
        "An automated trigger fired. Process the following event and perform the "
        "agent task that is appropriate for it.\n"
        + json.dumps(envelope, separators=(",", ":"), sort_keys=True, default=str)
    )


def invoke_trigger(
    trigger: Trigger,
    delivery_event: dict[str, Any],
) -> TriggerInvocation:
    """Invoke the exact deployment pinned by an active trigger."""

    deployment, arn_match, session = _deployment_for_trigger(trigger)
    delivery_id = _delivery_id(delivery_event)
    session_id = _session_id(trigger, delivery_id)
    prompt = _prompt(trigger, delivery_event)
    try:
        memory_identity = memory_invocation_identity(
            deployment if isinstance(deployment, dict) else vars(deployment),
            session_id,
            trigger.owner_sub,
        )
    except (InvocationIdentityError, TypeError):
        raise TriggerDispatchError("The trigger invocation identity is invalid") from None

    payload: dict[str, str] = {
        "prompt": prompt,
        "session_id": session_id,
    }
    if memory_identity:
        payload["actor_id"] = memory_identity.actor_id

    try:
        client = session.client(
            "bedrock-agentcore",
            region_name=arn_match.group("region"),
            config=BotoConfig(
                read_timeout=540,
                connect_timeout=10,
                retries={"max_attempts": 0},
            ),
        )
        response = client.invoke_agent_runtime(
            agentRuntimeArn=trigger.target_runtime_arn,
            payload=json.dumps(payload, separators=(",", ":")),
            runtimeSessionId=session_id,
        )
        text = _parse_agent_response(response.get("response", "") or response.get("body", b""))
    except Exception as exc:
        logger.error(
            "Trigger %s AgentCore invocation failed (%s)",
            trigger.trigger_id,
            type(exc).__name__,
        )
        raise TriggerDispatchError("The trigger invocation failed") from None

    return TriggerInvocation(
        trigger_id=trigger.trigger_id,
        delivery_id=delivery_id,
        response=text,
        session_id=session_id,
    )


@dataclass(frozen=True)
class _CallbackTarget:
    host: str
    port: int
    pinned_ip: str
    request_target: str


def _resolve_callback_target(url: str) -> _CallbackTarget:
    """Validate and resolve a callback once, returning the IP to connect to.

    The shared URL validator rejects private/link-local/etc. answers and applies
    the outbound host allowlist. We then resolve again for the actual port,
    validate *that exact answer set*, and pin the connection to one of those
    addresses. This closes the validate-then-re-resolve DNS-rebinding gap while
    preserving the original hostname for TLS SNI and certificate validation.
    """

    validated = _validate_outbound_url(
        url,
        label="Trigger result callback URL",
    )
    parsed = urllib.parse.urlsplit(validated)
    host = parsed.hostname
    if not host:
        raise _DiscoveryUrlInvalid("Trigger result callback URL has no host")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise _DiscoveryUrlInvalid("Trigger result callback URL has an invalid port") from exc

    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(5)
    try:
        try:
            infos = socket.getaddrinfo(
                host,
                port,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
        except (TimeoutError, socket.gaierror, OSError) as exc:
            raise _DiscoveryUrlBlocked(f"Trigger result callback URL host '{host}' could not be resolved") from exc
    finally:
        socket.setdefaulttimeout(previous_timeout)

    if not infos:
        raise _DiscoveryUrlBlocked(f"Trigger result callback URL host '{host}' returned no DNS records")

    pinned_ip: str | None = None
    for info in infos:
        ip_text = str(info[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(ip_text)
        except ValueError as exc:
            raise _DiscoveryUrlBlocked("Trigger result callback URL resolved to an invalid address") from exc
        if not address.is_global or any(
            address.version == network.version and address in network for network in _DISALLOWED_NETWORKS
        ):
            raise _DiscoveryUrlBlocked("Trigger result callback URL resolved to a disallowed address")
        if pinned_ip is None:
            pinned_ip = ip_text

    assert pinned_ip is not None
    request_target = parsed.path or "/"
    if parsed.query:
        request_target += f"?{parsed.query}"
    return _CallbackTarget(
        host=host,
        port=port,
        pinned_ip=pinned_ip,
        request_target=request_target,
    )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection whose TCP destination is a pre-validated IP.

    ``self.host`` remains the user-visible hostname, so http.client emits the
    correct Host header and the base HTTPS implementation performs SNI and
    certificate checks against that hostname.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        pinned_ip: str,
        timeout: float,
    ) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_ip = pinned_ip
        self._create_connection = self._create_pinned_connection

    def _create_pinned_connection(
        self,
        _address,
        timeout,
        source_address,
    ):
        return socket.create_connection(
            (self._pinned_ip, self.port),
            timeout,
            source_address,
        )


def _post_callback(trigger: Trigger, invocation: TriggerInvocation) -> None:
    """Best-effort delivery of an optional result callback.

    Agent invocation remains the primary side effect. A callback outage is
    logged without re-invoking the agent and duplicating its tools.
    """

    if not trigger.webhook_out_url:
        return
    try:
        target = _resolve_callback_target(trigger.webhook_out_url)
    except (_DiscoveryUrlInvalid, _DiscoveryUrlBlocked):
        logger.error(
            "Trigger %s callback URL failed its dispatch-time SSRF check",
            trigger.trigger_id,
        )
        return

    body = json.dumps(
        {
            "trigger_id": trigger.trigger_id,
            "delivery_id": invocation.delivery_id,
            "session_id": invocation.session_id,
            "response": invocation.response,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > MAX_CALLBACK_RESPONSE_BYTES:
        logger.error(
            "Trigger %s callback body exceeded the safe size bound",
            trigger.trigger_id,
        )
        return
    connection: _PinnedHTTPSConnection | None = None
    try:
        connection = _PinnedHTTPSConnection(
            target.host,
            target.port,
            pinned_ip=target.pinned_ip,
            timeout=10,
        )
        connection.request(
            "POST",
            target.request_target,
            body=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "agentcore-flows-trigger/1",
            },
        )
        response = connection.getresponse()
        try:
            status = int(response.status or 0)
            if status < 200 or status >= 300:
                raise OSError("non-success callback status")
        finally:
            response.close()
    except Exception as exc:
        logger.warning(
            "Trigger %s callback delivery failed (%s)",
            trigger.trigger_id,
            type(exc).__name__,
        )
    finally:
        if connection is not None:
            connection.close()


def dispatch_trigger_event(event: dict[str, Any]) -> dict[str, Any]:
    """Process one durable EventBridge/webhook delivery from SQS."""

    if event.get("_agentcore_trigger_dispatch") != DISPATCH_MARKER:
        raise TriggerDispatchError("The event is not a trigger dispatch")
    runtime_name = str(event.get("runtime_name") or "")
    trigger_id = str(event.get("trigger_id") or "")
    if _KEY_RE.fullmatch(runtime_name) is None or _KEY_RE.fullmatch(trigger_id) is None:
        raise TriggerDispatchError("The trigger coordinates are invalid")
    delivery_event = event.get("delivery_event")
    if not isinstance(delivery_event, dict):
        raise TriggerDispatchError("The trigger delivery is not a JSON object")
    encoded = json.dumps(
        delivery_event,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    if len(encoded) > MAX_TRIGGER_EVENT_BYTES:
        raise TriggerDispatchError("The trigger delivery exceeds the size bound")

    trigger = get_trigger_store().get(
        runtime_name,
        trigger_id,
        consistent=True,
    )
    if trigger is None:
        # A delayed EventBridge delivery after a successful delete is a normal
        # no-op, not an error that should retry for hours.
        return {"status": "gone", "trigger_id": trigger_id}
    if trigger.status != STATUS_ACTIVE:
        return {
            "status": "inactive",
            "trigger_id": trigger_id,
            "trigger_status": trigger.status,
        }

    delivery_id = _delivery_id(delivery_event)
    deployment_lease_token: str | None = None
    try:
        claim = get_trigger_store().acquire_delivery(
            trigger=trigger,
            delivery_id=delivery_id,
            lease_seconds=TRIGGER_DELIVERY_LEASE_SECONDS,
            retention_seconds=TRIGGER_DELIVERY_RETENTION_SECONDS,
        )
    except TriggerDeliveryInactive:
        return {
            "status": "inactive",
            "trigger_id": trigger_id,
        }
    except TriggerDeliveryBusy:
        # Returning success here would delete the SQS message while the prior
        # worker may still fail. Retry it (dispatch_sqs_batch re-enqueues a
        # contended delivery); the completed-delivery row suppresses a duplicate later.
        raise TriggerDispatchContention("Another worker still owns the trigger delivery") from None

    if claim is None:
        return {
            "status": "duplicate",
            "trigger_id": trigger_id,
            "delivery_id": delivery_id,
        }

    try:
        # The trigger row protects trigger deletion; the deployment row protects
        # runtime teardown. Acquire in that order and perform no external side
        # effect until both leases are held. If teardown already owns the
        # deployment, release the trigger claim and no-op. The existing
        # finalizer lease is the exact row/condition _claim_delete_status gates
        # on, so this adds no new delete-side code path.
        deployment_lease_token = _get_deployment_store().acquire_finalizer_lease(
            trigger.deployment_id,
            seconds=TRIGGER_DELIVERY_LEASE_SECONDS,
        )
        invocation = invoke_trigger(trigger, delivery_event)
    except DeploymentLifecycleConflict:
        try:
            get_trigger_store().release_delivery(claim)
        except Exception:
            logger.exception(
                "Could not release trigger delivery after teardown won %s/%s",
                trigger.trigger_id,
                delivery_id,
            )
        return {
            "status": "inactive",
            "trigger_id": trigger_id,
            "delivery_id": delivery_id,
        }
    except FinalizerLeaseBusy:
        try:
            get_trigger_store().release_delivery(claim)
        except Exception:
            logger.exception(
                "Could not release trigger delivery after deployment lease conflict %s/%s",
                trigger.trigger_id,
                delivery_id,
            )
        raise TriggerDispatchContention("The trigger deployment is still finalizing") from None
    except Exception:
        try:
            get_trigger_store().release_delivery(claim)
        except Exception:
            logger.exception(
                "Could not release failed trigger delivery %s/%s",
                trigger.trigger_id,
                delivery_id,
            )
        if deployment_lease_token:
            _get_deployment_store().release_finalizer_lease(
                trigger.deployment_id,
                deployment_lease_token,
            )
        raise

    completion_confirmed = False
    try:
        completion_confirmed = get_trigger_store().complete_delivery(
            claim,
            retention_seconds=TRIGGER_DELIVERY_RETENTION_SECONDS,
        )
    except Exception:
        # The agent already ran. Retrying now could duplicate its external tool
        # side effects, so acknowledge the queue message and retain the lease
        # until expiry rather than invoking twice.
        logger.exception(
            "Could not publish completion for trigger delivery %s/%s",
            trigger.trigger_id,
            delivery_id,
        )
    finally:
        if deployment_lease_token:
            _get_deployment_store().release_finalizer_lease(
                trigger.deployment_id,
                deployment_lease_token,
            )
    _post_callback(trigger, invocation)
    logger.info(
        "Dispatched trigger %s delivery %s",
        trigger.trigger_id,
        invocation.delivery_id,
    )
    return {
        "status": "invoked" if completion_confirmed else "invoked_unconfirmed",
        "trigger_id": trigger.trigger_id,
        "delivery_id": invocation.delivery_id,
        "session_id": invocation.session_id,
    }


def _requeue_contended_delivery(body: dict[str, Any], *, sqs_client=None) -> bool:
    """Re-enqueue a delivery that lost a lease race, with a short delay; False when it cannot."""

    attempts = body.get(_CONTENTION_ATTEMPTS_KEY, 0)
    if type(attempts) is not int or not 0 <= attempts < TRIGGER_CONTENTION_REQUEUE_LIMIT:
        return False
    requeued = {**body, _CONTENTION_ATTEMPTS_KEY: attempts + 1}
    client = sqs_client or boto3.client("sqs", region_name=_region())
    try:
        response = client.send_message(
            QueueUrl=_dispatch_queue_url(),
            MessageBody=json.dumps(requeued, separators=(",", ":"), default=str),
            DelaySeconds=random.randint(*TRIGGER_CONTENTION_DELAY_SECONDS),
        )
    except Exception as exc:  # noqa: BLE001 - the caller falls back to the queue's own retry
        logger.warning("Could not re-enqueue a contended trigger delivery (%s)", type(exc).__name__)
        return False
    return bool(response.get("MessageId"))


def dispatch_sqs_batch(event: dict[str, Any], *, sqs_client=None) -> dict[str, list[dict[str, str]]]:
    """Process an SQS event with Lambda partial-batch failure semantics."""

    records = event.get("Records")
    if not isinstance(records, list) or not records:
        raise TriggerDispatchError("The trigger queue event has no records")

    failures: list[dict[str, str]] = []
    for record in records:
        if not isinstance(record, dict):
            raise TriggerDispatchError("The trigger queue record is malformed")
        message_id = str(record.get("messageId") or "")
        if not message_id:
            raise TriggerDispatchError("The trigger queue record has no message id")
        try:
            if record.get("eventSource") != "aws:sqs":
                raise TriggerDispatchError("The trigger queue record has an invalid source")
            body = json.loads(str(record.get("body") or ""))
            if not isinstance(body, dict):
                raise TriggerDispatchError("The trigger queue body is not a JSON object")
            dispatch_trigger_event(body)
        except TriggerDispatchContention as exc:
            if _requeue_contended_delivery(body, sqs_client=sqs_client):
                logger.warning(
                    "Trigger queue message %s re-enqueued after contention (%s)",
                    message_id,
                    exc,
                )
                continue
            logger.error("Trigger queue message %s failed (%s: %s)", message_id, type(exc).__name__, exc)
            failures.append({"itemIdentifier": message_id})
        except Exception as exc:
            # A TriggerDispatchError carries only a fixed sentence, so it is logged; any other
            # exception is reported by type alone, since its text may echo the delivery.
            logger.error(
                "Trigger queue message %s failed (%s%s)",
                message_id,
                type(exc).__name__,
                f": {exc}" if isinstance(exc, TriggerDispatchError) else "",
            )
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


def _secret_tags(metadata: dict[str, Any]) -> dict[str, str]:
    return {
        str(tag.get("Key")): str(tag.get("Value")) for tag in metadata.get("Tags", []) if tag.get("Key") is not None
    }


def _load_webhook_secret(
    trigger: Trigger,
    *,
    secrets_client=None,
) -> str:
    secret_ref = trigger.webhook_secret_ref
    if trigger.type != TYPE_WEBHOOK or not secret_ref:
        raise WebhookAuthenticationError("Webhook authentication failed")
    client = secrets_client or boto3.client("secretsmanager", region_name=_region())
    try:
        metadata = client.describe_secret(SecretId=secret_ref)
        tags = _secret_tags(metadata)
        if (
            not str(metadata.get("Name") or "").startswith("agentcore-trigger/")
            or tags.get("ManagedBy") != "agentcore-flows"
            or tags.get("Purpose") != "trigger-webhook-hmac"
            or tags.get("owner_sub") != trigger.owner_sub
        ):
            raise WebhookAuthenticationError("Webhook authentication failed")
        value = client.get_secret_value(SecretId=secret_ref).get("SecretString")
    except WebhookAuthenticationError:
        raise
    except Exception:
        raise WebhookAuthenticationError("Webhook authentication failed") from None
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise WebhookAuthenticationError("Webhook authentication failed")
    return value


def webhook_signature(
    secret: str,
    *,
    timestamp: str,
    delivery_id: str,
    body: bytes,
) -> str:
    """Return the documented v1 signature for a webhook request."""

    message = timestamp.encode("ascii") + b"." + delivery_id.encode("utf-8") + b"." + body
    return "v1=" + hmac.new(secret.encode("ascii"), message, hashlib.sha256).hexdigest()


def authenticate_webhook(
    trigger: Trigger,
    *,
    timestamp: str | None,
    delivery_id: str | None,
    signature: str | None,
    body: bytes,
    secrets_client=None,
    now: float | None = None,
) -> str:
    """Verify timestamp, delivery id, and body HMAC; return the delivery id."""

    if trigger.status != STATUS_ACTIVE or trigger.type != TYPE_WEBHOOK:
        raise WebhookAuthenticationError("Webhook authentication failed")
    if len(body) > MAX_TRIGGER_EVENT_BYTES:
        raise WebhookAuthenticationError("Webhook authentication failed")
    try:
        timestamp_int = int(timestamp or "")
    except (TypeError, ValueError):
        raise WebhookAuthenticationError("Webhook authentication failed") from None
    current = int(time.time() if now is None else now)
    if abs(current - timestamp_int) > WEBHOOK_SIGNATURE_WINDOW_SECONDS:
        raise WebhookAuthenticationError("Webhook authentication failed")
    if not delivery_id or _DELIVERY_ID_RE.fullmatch(delivery_id) is None:
        raise WebhookAuthenticationError("Webhook authentication failed")
    match = _SIGNATURE_RE.fullmatch(signature or "")
    if match is None:
        raise WebhookAuthenticationError("Webhook authentication failed")

    secret = _load_webhook_secret(trigger, secrets_client=secrets_client)
    expected = webhook_signature(
        secret,
        timestamp=str(timestamp_int),
        delivery_id=delivery_id,
        body=body,
    )
    actual = "v1=" + match.group("digest").lower()
    if not hmac.compare_digest(expected, actual):
        raise WebhookAuthenticationError("Webhook authentication failed")
    return delivery_id


def webhook_delivery_event(
    *,
    body: bytes,
    delivery_id: str,
    content_type: str,
) -> dict[str, Any]:
    """Build the bounded event envelope delivered to the agent."""

    text = body.decode("utf-8", errors="replace")
    if "json" in content_type.lower():
        try:
            detail: Any = json.loads(text)
        except (TypeError, ValueError):
            detail = text
    else:
        detail = text
    return {
        "version": "0",
        "id": delivery_id,
        "detail-type": "AgentCore webhook",
        "source": "agentcore.webhook",
        "time": datetime.now(timezone.utc).isoformat(),
        "detail": detail,
    }


def enqueue_webhook_dispatch(
    *,
    trigger: Trigger,
    delivery_event: dict[str, Any],
    sqs_client=None,
) -> None:
    """Durably enqueue a webhook after HMAC succeeds."""

    event = {
        "_agentcore_trigger_dispatch": DISPATCH_MARKER,
        "runtime_name": trigger.runtime_name,
        "trigger_id": trigger.trigger_id,
        "delivery_event": delivery_event,
    }
    encoded = json.dumps(
        event,
        separators=(",", ":"),
        default=str,
    )
    client = sqs_client or boto3.client("sqs", region_name=_region())
    try:
        response = client.send_message(
            QueueUrl=_dispatch_queue_url(),
            MessageBody=encoded,
        )
    except Exception:
        raise TriggerDispatchError("The webhook delivery could not be queued") from None
    if not response.get("MessageId"):
        raise TriggerDispatchError("The webhook delivery was not accepted")

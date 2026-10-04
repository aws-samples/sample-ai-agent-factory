"""Shared invocation logic for already-authorized runtime targets.

Target selection and invocation are deliberately separate.  Callers must first
prove ownership and resolve an exact deployment/runtime target, then pass that
frozen target here.  This module never scans deployments and never accepts an
account, role, region, runtime ARN, or runtime id from an HTTP request.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from botocore.config import Config as BotoConfig
from fastapi import HTTPException

from app.models.deployment_models import DeploymentStatusEnum, TestRequest, TestResponse
from app.services import step_clients
from app.services.invocation_identity import InvocationIdentity, InvocationIdentityError

logger = logging.getLogger(__name__)


def _close_response_body(body: Any) -> None:
    close = getattr(body, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:  # noqa: BLE001
        logger.debug("Could not close the runtime response body", exc_info=True)


def runtime_payload(
    prompt: str,
    session_id: str | None,
    memory_identity: InvocationIdentity | None,
    warmup: bool = False,
) -> dict[str, str | bool]:
    """Build the exact payload consumed by generated HTTP agents."""

    payload: dict[str, str | bool] = {"prompt": prompt}
    if session_id:
        payload["session_id"] = session_id
    if memory_identity:
        payload["actor_id"] = memory_identity.actor_id
    if warmup:
        payload["warmup"] = True
    return payload


def _body_text(body: Any) -> str:
    if body is None:
        return ""
    if isinstance(body, memoryview):
        return body.tobytes().decode("utf-8", errors="replace")
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8", errors="replace")
    if isinstance(body, str):
        return body
    try:
        return json.dumps(body)
    except (TypeError, ValueError):
        return str(body)


_RECEIPT_LIMIT = 64
_RECEIPT_ARGUMENT_LIMIT = 32
_RECEIPT_KEYS = frozenset({"name", "status", "input_sha256", "argument_sha256"})
_RECEIPT_STATUSES = frozenset({"success", "error", "missing"})
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def _printable(value: Any, limit: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    )


def _validated_receipt(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict) or set(item) != _RECEIPT_KEYS:
        return None
    arguments = item["argument_sha256"]
    if (
        not _printable(item["name"], 256)
        or item["status"] not in _RECEIPT_STATUSES
        or not isinstance(item["input_sha256"], str)
        or not _SHA256_HEX.fullmatch(item["input_sha256"])
        or not isinstance(arguments, dict)
        or len(arguments) > _RECEIPT_ARGUMENT_LIMIT
    ):
        return None
    for key, digest in arguments.items():
        if not _printable(key, 128) or not isinstance(digest, str) or not _SHA256_HEX.fullmatch(digest):
            return None
    return {
        "name": item["name"],
        "status": item["status"],
        "input_sha256": item["input_sha256"],
        "argument_sha256": dict(arguments),
    }


def parse_tool_receipts(body: Any) -> list[dict[str, Any]] | None:
    """The tool-use receipts a generated agent returned beside its reply, validated.

    A receipt names one tool call the agent's own loop executed during this invocation,
    with its status and SHA-256 digests of the arguments (codegen_templates.tool_receipts),
    so a caller can tell a tool the runtime ran from a tool result the model invented.
    ``None`` when the body carries none. A malformed list is dropped whole, never passed
    on in part: a caller must not mistake a partial list for the complete one.
    """

    try:
        data = json.loads(_body_text(body))
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or "tool_receipts" not in data:
        return None
    raw = data["tool_receipts"]
    if not isinstance(raw, list) or len(raw) > _RECEIPT_LIMIT:
        logger.warning("Runtime returned malformed tool receipts (%s); dropped", type(raw).__name__)
        return None
    receipts: list[dict[str, Any]] = []
    for item in raw:
        receipt = _validated_receipt(item)
        if receipt is None:
            logger.warning("Runtime returned a malformed tool receipt; every receipt of the turn was dropped")
            return None
        receipts.append(receipt)
    return receipts


def parse_response_body(body: Any) -> str:
    """Normalize every response shape currently returned by AgentCore agents."""

    body = _body_text(body)

    try:
        data = json.loads(body)
        if isinstance(data, dict):
            for key in ("response", "body", "output"):
                val = data.get(key)
                if val is not None:
                    return str(val) if not isinstance(val, str) else val
            return json.dumps(data)
        return str(data)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    chunks = [line[6:] for line in body.split("\n") if line.startswith("data: ")]
    if chunks:
        try:
            last = json.loads(chunks[-1])
            if isinstance(last, dict):
                value = last.get("response")
                if value is not None:
                    return value if isinstance(value, str) else str(value)
                return " ".join(chunks)
            return str(last)
        except (json.JSONDecodeError, TypeError, ValueError):
            return " ".join(chunks)
    return body


def promote_pending_policy(
    deployment_state: dict,
    fallback_region: str,
    *,
    target_event: dict,
    state_store: Any,
) -> bool:
    """Best-effort promotion of one deployment's pending Cedar policy."""

    policy_result = deployment_state.get("policy_result") or {}
    if not policy_result.get("enforce_pending") and policy_result.get("mode") != "ENFORCE":
        return False
    try:
        from app.services.policy_promoter import try_promote_to_enforce

        target_region = target_event.get("target_region") or fallback_region
        control_client = step_clients.client(
            target_event,
            "bedrock-agentcore-control",
            region_name=target_region,
        )
        outcome = try_promote_to_enforce(
            deployment_state,
            target_region,
            control_client=control_client,
            # F-G09-003: lazy promotion creates/adopts policies AFTER finalization; without the store the promoter
            # cannot record them durably and therefore does not create missing ones.
            store=state_store,
        )
        logger.info("policy promote outcome: %s", outcome)
        fresh = [c for c in ((outcome or {}).get("policy_children") or []) if isinstance(c, dict)]
        promoted = bool(outcome and outcome.get("promoted"))
        if fresh or promoted:
            updated = dict(deployment_state.get("policy_result") or {})
            if fresh:
                # F-G09-003: receipts of policies the lazy path created/adopted, bound to the deployment result
                # (separate from created_resources and from desired_policies), deduped by exact identity.
                def _key(c: dict) -> tuple:
                    return (c.get("engine_id"), c.get("id") or c.get("policy_id"), c.get("account"), c.get("region"))

                merged = list(updated.get("policy_children") or [])
                known = {_key(c) for c in merged}
                for c in fresh:
                    if _key(c) not in known:
                        known.add(_key(c))
                        merged.append(c)
                updated["policy_children"] = merged
            if promoted:
                updated["mode"] = "ENFORCE"
                updated["downgraded_to_log_only"] = False
                updated["enforce_validation_pending"] = False
                updated["enforce_pending"] = None
                updated["promoted_at_first_use"] = True
            try:
                state_store.update_status(
                    deployment_state.get("deployment_id"),
                    DeploymentStatusEnum.SUCCEEDED,
                    policy_result=updated,
                )
            except Exception:  # noqa: BLE001
                # No durable receipt/mode = no success: the next touchpoint re-drives (the manifest rows already
                # exist, so nothing leaks; only the deployment-bound receipt is missing).
                logger.error(
                    "policy promote: could not persist the policy result (receipts/ENFORCE mode); not reporting success"
                )
                return False
            deployment_state["policy_result"] = updated
            if promoted:
                logger.info("policy promote: gateway now in ENFORCE mode")
                return True
    except Exception:  # noqa: BLE001
        logger.warning("policy promote: skipped (will retry next touchpoint)")
    return False


def invoke_verified_http_runtime(
    request: TestRequest,
    *,
    caller_sub: str,
    deployment_state: dict,
    runtime_id: str,
    runtime_arn: str,
    region: str,
    target_session: Any,
    promote_policy: Callable[[dict, str], bool],
    invoke_harness: Callable[..., dict],
    resolve_memory_identity: Callable[[dict, str | None, str], InvocationIdentity | None],
    gateway_session: Callable[[Any], AbstractContextManager],
    get_gateway_token: Callable[[dict], str],
    start_harness_warmup: Callable[[dict, str, str], None] | None = None,
) -> TestResponse:
    """Invoke one exact, owner-checked target without re-resolving authority."""

    protocol = str(deployment_state.get("runtime_protocol") or "HTTP").upper()
    if protocol != "HTTP":
        raise HTTPException(
            status_code=409,
            detail=(
                f"This deployment uses the {protocol} runtime protocol. The HTTP runtime invocation path requires HTTP."
            ),
        )

    promote_policy(deployment_state, region)

    prompt = request.input
    if request.history:
        history_text = "\n".join(
            f"{'User' if message['role'] == 'user' else 'Assistant'}: {message['content']}"
            for message in request.history[-6:]
        )
        prompt = f"Previous conversation:\n{history_text}\n\nUser: {request.input}"

    if deployment_state.get("deployment_mode") == "harness":
        harness_arn = deployment_state.get("harness_arn", "")
        if not harness_arn:
            return TestResponse(success=False, error="Harness ARN not found for this deployment")
        if request.warmup and start_harness_warmup is not None:
            # The deploy-time ping (DeployPanel.warmupRuntime) is fire-and-forget. A cold harness's
            # first turn runs past the HTTP API's 30 s integration limit (measured 2026-10-02: over
            # 30 s once after deploy, then 3.5-5 s for fresh and continuing sessions), so the browser
            # got a 504 while this Lambda finished the turn, and the ping was stored as a conversation
            # turn in the session named after the harness. It now runs in the background on a session
            # of its own, and the route answers at once.
            try:
                start_harness_warmup(deployment_state, region, harness_arn)
            except Exception as exc:  # noqa: BLE001 -- a warm-up that cannot start is reported, not raised
                logger.warning("Harness warm-up could not be started (%s)", type(exc).__name__)
                return TestResponse(success=False, error="Harness warm-up could not be started", arn=harness_arn)
            return TestResponse(success=True, response="", arn=harness_arn)
        session_id = request.session_id or runtime_id
        result = invoke_harness(
            region,
            harness_arn,
            prompt,
            session_id,
            agentcore_data_client=target_session.client(
                "bedrock-agentcore",
                region_name=region,
            ),
        )
        harness_ok = bool(result.get("success"))
        if not harness_ok:
            logger.warning("Harness invoke failed: %s", result.get("error"))
        return TestResponse(
            success=harness_ok,
            response=result.get("output", ""),
            error=None if harness_ok else "Harness invocation failed",
            session_id=session_id,
            arn=harness_arn,
            trace_id=result.get("trace_id"),
        )

    try:
        memory_identity = resolve_memory_identity(
            deployment_state,
            request.session_id,
            caller_sub,
        )
    except InvocationIdentityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session_id = memory_identity.session_id if memory_identity else request.session_id

    if not runtime_arn:
        raise HTTPException(
            status_code=503,
            detail="Could not verify this runtime right now. Try again shortly.",
        )

    gateway_result = deployment_state.get("gateway_result") or {}
    client_info = gateway_result.get("client_info")
    if client_info:
        try:
            with gateway_session(target_session):
                get_gateway_token(client_info)
        except Exception:  # noqa: BLE001
            logger.warning("Could not get Cognito token for gateway auth validation")

    try:
        agentcore_client = target_session.client(
            "bedrock-agentcore",
            region_name=region,
            config=BotoConfig(
                read_timeout=25,
                connect_timeout=5,
                retries={"max_attempts": 0},
            ),
        )
        invoke_params: dict[str, Any] = {
            "agentRuntimeArn": runtime_arn,
            "payload": json.dumps(
                runtime_payload(
                    prompt,
                    session_id,
                    memory_identity,
                    warmup=request.warmup,
                )
            ),
        }
        if session_id:
            invoke_params["runtimeSessionId"] = session_id

        response = agentcore_client.invoke_agent_runtime(**invoke_params)
        status_code = response.get("statusCode")
        status_is_valid = status_code is None or (
            isinstance(status_code, int) and not isinstance(status_code, bool) and 200 <= status_code < 300
        )
        if not status_is_valid:
            _close_response_body(response.get("response") or response.get("body"))
            if isinstance(status_code, int) and not isinstance(status_code, bool):
                logger.warning(
                    "Runtime invocation returned non-success statusCode=%d",
                    status_code,
                )
            else:
                logger.warning(
                    "Runtime invocation returned malformed statusCode type=%s",
                    type(status_code).__name__,
                )
            return TestResponse(success=False, error="Runtime invocation failed.")

        raw_response = response.get("response", "") or response.get("body", b"")
        if hasattr(raw_response, "read"):
            response_stream = raw_response
            try:
                raw_response = response_stream.read()
            finally:
                _close_response_body(response_stream)

        if not memory_identity:
            session_id = response.get("runtimeSessionId") or response.get("sessionId")

        return TestResponse(
            success=True,
            response=parse_response_body(raw_response),
            session_id=session_id,
            arn=runtime_arn,
            tool_receipts=parse_tool_receipts(raw_response),
        )
    except Exception as exc:  # noqa: BLE001
        error_msg = str(exc)
        if "ResourceNotFound" in error_msg:
            return TestResponse(success=False, error="Runtime not found. It may have been deleted.")
        error_type = type(exc).__name__
        if (
            "ReadTimeout" in error_type
            or "ConnectTimeout" in error_type
            or "Read timeout" in error_msg
            or "timed out" in error_msg.lower()
        ):
            return TestResponse(
                success=False,
                error=(
                    "Agent still running; exceeded 30s sync test limit — "
                    "use the streaming endpoint for tool-heavy agents that "
                    "take longer than 30 seconds to respond."
                ),
            )
        logger.warning("Runtime invocation error: %s", error_msg)
        return TestResponse(success=False, error="Runtime invocation failed.")

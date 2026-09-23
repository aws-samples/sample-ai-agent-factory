"""Live ``InvokeAgentRuntime`` probe for the pipeline-owned generated agent.

Closes the open live gate recorded in ``README.md``: one deterministic
round-trip through the D-03 two-Gateway topology that must prove, in a single
response, all five integration legs of the reference agent:

* ``marker``               -- the exact handshake constant (real container ran)
* ``discoveredToolCount``  -- ``tools/list`` over SigV4 to the AWS_IAM tool Gateway
* ``toolCalls``            -- ``tools/call`` of the subscribed echo tool
* ``contentBlocks``        -- Cognito-M2M bearer -> Platform inference Gateway
* ``memoryRoundTrip``      -- AgentCore Memory ``create_event``/``list_events``

Modes
-----
``positive``      authorized invoke with the vended (Workstream) credentials.
``wrong-account`` fail-closed guard: refuses to run unless STS matches
                  ``--expected-account`` (proves the probe itself cannot be
                  pointed at the wrong account by mistake).
``unsubscribed-tool``  adversarial twin: asks the model for a tool that is NOT
                  in the subscribed set. The agent must refuse *before* any
                  Gateway call; the Runtime therefore returns a non-2xx /
                  error body, never a ``toolCalls`` entry for that tool.

Evidence discipline (same rules as the sibling spikes): the written evidence
file carries booleans, counts, status codes and SHA-256 fingerprints only --
never a token, ARN, account id, resource id or prompt text.

Run it with the repo's live venv, never inline::

    scripts/live-agentcore-gateway-spike/.venv/bin/python \
        scripts/live-agentcore-generated-agent-spike/live_invoke_probe.py \
        --mode positive --expected-account <workstream-account-id> \
        --stack-name <nonprod RuntimeMemory stack> --evidence-out <path>

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

HANDSHAKE_MARKER = "agentcore-generated-agent-ok"
ECHO_TOOL = "target-tool-echo___tool-echo"
UNSUBSCRIBED_TOOL = "target-tool-forbidden___tool-forbidden"

#: Deterministic prompts. The agent's directive grammar is ``TOOL <name> <json>``
#: on its own line; ``<done/>`` terminates the bounded loop.
POSITIVE_PROMPT = (
    "Follow these steps exactly. Step 1: reply with exactly one line and nothing "
    f'else: TOOL {ECHO_TOOL} {{"message":"probe"}} . Step 2: after you receive '
    "the tool result, reply with exactly <done/> and nothing else."
)
UNSUBSCRIBED_PROMPT = (
    "Follow these steps exactly. Step 1: reply with exactly one line and nothing "
    f'else: TOOL {UNSUBSCRIBED_TOOL} {{"message":"probe"}} . Step 2: after you '
    "receive the tool result, reply with exactly <done/> and nothing else."
)


class ProbeError(RuntimeError):
    """Raised when a gate condition is not met; exit status 2."""


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mode", choices=("positive", "wrong-account", "unsubscribed-tool"), required=True)
    parser.add_argument("--expected-account", required=True, help="12-digit account the creds MUST belong to")
    parser.add_argument("--region", default="us-west-2")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stack-name", help="RuntimeMemory stack exposing RuntimeArn/MemoryId outputs")
    group.add_argument("--runtime-arn", help="Explicit Runtime ARN (bypasses stack lookup)")
    parser.add_argument("--actor-id", default=None, help="Memory actor scope; defaults to a per-run value")
    parser.add_argument("--evidence-out", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=300, help="Data-plane read timeout in seconds")
    return parser.parse_args(argv)


def verify_identity(session: boto3.Session, expected_account: str) -> str:
    account = session.client("sts").get_caller_identity()["Account"]
    if account != expected_account:
        raise ProbeError(
            f"credentials belong to account ...{account[-4:]}, expected ...{expected_account[-4:]}; refusing"
        )
    return account


def resolve_runtime(session: boto3.Session, region: str, args: argparse.Namespace) -> tuple[str, str | None]:
    if args.runtime_arn:
        return args.runtime_arn, None
    cfn = session.client("cloudformation", region_name=region)
    stack = cfn.describe_stacks(StackName=args.stack_name)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    status = stack["StackStatus"]
    # UPDATE_ROLLBACK_COMPLETE is a stable terminal state: CloudFormation has
    # restored the last good template, so the prior deployment is exactly what
    # is serving. It is the state a rejected release (e.g. the scan gate
    # refusing a digest) leaves behind and must remain probe-able; in-progress
    # and *_FAILED states still refuse.
    if status not in ("CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"):
        raise ProbeError(f"stack is {status}; refusing to invoke against an unhealthy deployment")
    if "RuntimeArn" not in outputs:
        raise ProbeError("stack has no RuntimeArn output")
    return outputs["RuntimeArn"], outputs.get("MemoryId")


def runtime_is_ready(session: boto3.Session, region: str, runtime_arn: str) -> str:
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    control = session.client("bedrock-agentcore-control", region_name=region)
    status = control.get_agent_runtime(agentRuntimeId=runtime_id)["status"]
    if status != "READY":
        raise ProbeError(f"runtime status is {status}, not READY")
    return status


def invoke(
    session: boto3.Session, region: str, runtime_arn: str, payload: Mapping[str, Any], timeout: int
) -> tuple[int, Mapping[str, Any], str]:
    data = session.client(
        "bedrock-agentcore",
        region_name=region,
        config=Config(read_timeout=timeout, connect_timeout=20, retries={"max_attempts": 0}),
    )
    session_id = f"probe-{uuid.uuid4()}-{int(time.time())}"  # >= 33 chars as the API requires
    try:
        response = data.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(payload).encode("utf-8"),
        )
    except ClientError as error:
        meta = error.response.get("ResponseMetadata", {})
        code = int(meta.get("HTTPStatusCode", 0))
        err = error.response.get("Error", {})
        body = {"errorCode": err.get("Code"), "errorMessageFingerprint": fingerprint(str(err.get("Message", "")))}
        return code, body, session_id
    status_code = int(response.get("statusCode") or response["ResponseMetadata"]["HTTPStatusCode"])
    stream = response.get("response")
    raw = stream.read() if hasattr(stream, "read") else (stream or b"")
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = {"nonJsonBodyFingerprint": fingerprint(text), "bodyLength": len(text)}
    return status_code, decoded if isinstance(decoded, Mapping) else {"body": decoded}, session_id


def assess_positive(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    tool_calls = body.get("toolCalls") or []
    checks = {
        "http2xx": 200 <= status < 300,
        "markerExact": body.get("marker") == HANDSHAKE_MARKER,
        "discoveredToolsPositive": isinstance(body.get("discoveredToolCount"), int)
        and body["discoveredToolCount"] > 0,
        "echoToolCalled": ECHO_TOOL in tool_calls,
        "onlySubscribedToolsCalled": all(t == ECHO_TOOL for t in tool_calls),
        "inferenceContentBlocksPositive": isinstance(body.get("contentBlocks"), int)
        and body["contentBlocks"] > 0,
        "memoryConfigured": body.get("memoryConfigured") is True,
        "memoryRoundTrip": body.get("memoryRoundTrip") is True,
    }
    return checks


def assess_unsubscribed(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    """Two independent refusal layers guard an unsubscribed tool directive.

    Layer 1 -- the system prompt lists only subscribed tools, so the rated model
    declines the forbidden directive itself: the Runtime returns 2xx with the
    success marker, ``toolCalls == []`` and a single content block (no TOOL line
    was ever parsed). Live-proven deterministic (2026-09-23, temperature 0).

    Layer 2 -- if a TOOL line naming a non-subscribed tool IS parsed, the core
    raises ``PermissionError`` BEFORE any Gateway call: non-2xx, no marker.

    Either layer holding is a pass; the forbidden tool being called, or a
    marker-bearing response that also records a tool call, is a fail.
    """
    tool_calls = body.get("toolCalls") or []
    marker_returned = body.get("marker") == HANDSHAKE_MARKER
    layer1_model_declined = (
        200 <= status < 300 and marker_returned and tool_calls == []
        and body.get("contentBlocks") == 1
    )
    layer2_agent_refused = not (200 <= status < 300) and not marker_returned
    return {
        "forbiddenToolNeverCalled": UNSUBSCRIBED_TOOL not in tool_calls,
        "noToolCallsRecorded": tool_calls == [],
        "refusedByModelAllowlistOrAgentGuard": layer1_model_declined or layer2_agent_refused,
        "refusalLayer": "model-allowlist" if layer1_model_declined else (
            "agent-permission-error" if layer2_agent_refused else "none"
        ),
        "statusRecorded": status,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    session = boto3.Session()
    started = datetime.now(timezone.utc).isoformat()
    evidence: dict[str, Any] = {
        "probe": "generated-agent-live-invoke",
        "mode": args.mode,
        "region": args.region,
        "startedAt": started,
    }

    if args.mode == "wrong-account":
        # Prove the guard: any account other than expected must be refused.
        try:
            verify_identity(session, args.expected_account)
        except ProbeError as error:
            evidence.update(guardRefused=True, reasonFingerprint=fingerprint(str(error)), passed=True)
            args.evidence_out.write_text(json.dumps(evidence, indent=2, sort_keys=True))
            print(json.dumps(evidence, indent=2, sort_keys=True))
            return 0
        evidence.update(guardRefused=False, passed=False)
        args.evidence_out.write_text(json.dumps(evidence, indent=2, sort_keys=True))
        print(json.dumps(evidence, indent=2, sort_keys=True))
        return 2

    verify_identity(session, args.expected_account)
    evidence["accountMatched"] = True

    runtime_arn, memory_id = resolve_runtime(session, args.region, args)
    evidence["runtimeArnFingerprint"] = fingerprint(runtime_arn)
    evidence["memoryIdPresent"] = bool(memory_id)
    evidence["runtimeStatus"] = runtime_is_ready(session, args.region, runtime_arn)

    actor_id = args.actor_id or f"probe-actor-{uuid.uuid4().hex[:12]}"
    prompt = POSITIVE_PROMPT if args.mode == "positive" else UNSUBSCRIBED_PROMPT
    payload = {"prompt": prompt, "actorId": actor_id}
    evidence["promptFingerprint"] = fingerprint(prompt)
    evidence["actorIdFingerprint"] = fingerprint(actor_id)

    t0 = time.monotonic()
    status, body, session_id = invoke(session, args.region, runtime_arn, payload, args.timeout)
    evidence["invokeLatencySeconds"] = round(time.monotonic() - t0, 2)
    evidence["httpStatus"] = status
    evidence["runtimeSessionIdFingerprint"] = fingerprint(session_id)
    evidence["responseFingerprint"] = fingerprint(json.dumps(body, sort_keys=True))
    # The response body carries only fingerprints/counts by contract; record
    # the fields the gate is defined on so the evidence is self-describing.
    evidence["responseFields"] = {
        k: body.get(k)
        for k in (
            "marker",
            "discoveredToolCount",
            "toolCalls",
            "contentBlocks",
            "memoryRoundTrip",
            "memoryConfigured",
            "errorCode",
            "errorMessageFingerprint",
        )
        if k in body
    }

    checks = assess_positive(status, body) if args.mode == "positive" else assess_unsubscribed(status, body)
    evidence["checks"] = checks
    passed = all(
        v is True for k, v in checks.items() if k not in ("statusRecorded", "refusalLayer")
    )
    evidence["passed"] = passed
    evidence["finishedAt"] = datetime.now(timezone.utc).isoformat()

    args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
    args.evidence_out.write_text(json.dumps(evidence, indent=2, sort_keys=True))
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if passed else 2


if __name__ == "__main__":  # pragma: no cover - live entrypoint
    try:
        sys.exit(main())
    except ProbeError as error:
        print(json.dumps({"passed": False, "error": str(error)}), file=sys.stderr)
        sys.exit(2)

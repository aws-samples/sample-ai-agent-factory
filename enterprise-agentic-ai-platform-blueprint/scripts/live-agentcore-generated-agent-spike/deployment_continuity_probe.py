"""Deployment-continuity sampler for the pipeline-owned generated agent.

Closes the *upgrade* and *interrupted-deployment* release gates recorded in
``README.md``. While a RuntimeMemory deployment is in flight -- an in-place
upgrade, a deliberately interrupted one, or a no-op redeploy -- this probe
invokes the governed positive session back to back for a bounded window and
records, per sample:

* the invocation status and latency and whether the eight positive checks
  from ``live_invoke_probe.assess_positive`` all hold;
* the ``agentVersion`` the serving revision reports (``null`` for revisions
  that predate the field);
* the Runtime control-plane status (``READY`` / ``UPDATING`` / ...) and the
  RuntimeMemory stack status observed alongside the sample.

Unlike the single-shot probe it deliberately keeps sampling through
``UPDATE_IN_PROGRESS`` and ``UPDATE_ROLLBACK_IN_PROGRESS``: the whole point is
to observe what a caller sees *during* the change. The gate is availability
(``--min-availability``, default 1.0 = zero failed samples) plus, optionally,
the exact version transition the campaign expects (``--expect-transition
1.0:1.1.0`` -- ``none`` stands for a revision without the field) or a single
final version (``--expect-final-version``) or no transition at all
(``--expect-stable-version``, the no-op redeploy gate).

Evidence discipline (same as the sibling probes): booleans, counts, status
codes, seconds and SHA-256 fingerprints only -- never a token, ARN, account id,
resource id or prompt text. A JSONL stream (``--stream-out``) receives one
line per sample as it happens so an interrupted sampler still leaves evidence.

Run it with the repo's live venv, never inline::

    scripts/live-agentcore-generated-agent-spike/.venv/bin/python \
        scripts/live-agentcore-generated-agent-spike/deployment_continuity_probe.py \
        --expected-account <workstream-account-id> \
        --stack-name <RuntimeMemory stack> --duration-seconds 900 \
        --expect-transition none:1.1.0 --evidence-out <path>

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import boto3
from botocore.exceptions import ClientError

from live_invoke_probe import (
    POSITIVE_PROMPT,
    ProbeError,
    assess_positive,
    fingerprint,
    invoke,
    verify_identity,
)

#: Marker for "the serving revision predates the agentVersion field".
NO_VERSION = "none"

#: Stack states in which the deployment is settled and sampling may stop early.
TERMINAL_STACK_STATES = frozenset(
    {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE", "UPDATE_ROLLBACK_FAILED", "UPDATE_FAILED"}
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--expected-account", required=True, help="12-digit account the creds MUST belong to")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--stack-name", required=True, help="RuntimeMemory stack exposing the RuntimeArn output")
    parser.add_argument("--duration-seconds", type=int, default=900, help="Sampling window (wall clock)")
    parser.add_argument(
        "--interval-seconds", type=float, default=15.0, help="Minimum spacing between sample starts"
    )
    parser.add_argument("--timeout", type=int, default=120, help="Data-plane read timeout per invoke")
    parser.add_argument("--min-availability", type=float, default=1.0, help="Fraction of samples that must pass")
    parser.add_argument(
        "--min-samples", type=int, default=8, help="Fewer samples than this fails the gate (window too short)"
    )
    parser.add_argument(
        "--stop-when-settled",
        action="store_true",
        help="Stop early once a stack change has been observed AND the stack is terminal again, "
        "after --settle-samples further passing samples",
    )
    parser.add_argument("--settle-samples", type=int, default=6)
    gate = parser.add_mutually_exclusive_group()
    gate.add_argument(
        "--expect-transition",
        help=f"FROM:TO agent versions that must both be observed, in that order (use '{NO_VERSION}' for a "
        "revision without the field)",
    )
    gate.add_argument("--expect-final-version", help="The last sample must report exactly this version")
    gate.add_argument(
        "--expect-stable-version",
        action="store_true",
        help="Every sample must report the same version and no stack change may be observed (no-op gate)",
    )
    parser.add_argument("--evidence-out", required=True, type=Path)
    parser.add_argument("--stream-out", type=Path, default=None, help="JSONL, one line per sample")
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- #
# Live lookups (read-only)
# --------------------------------------------------------------------------- #
def stack_state(cfn: Any, stack_name: str) -> tuple[str, str | None]:
    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    return stack["StackStatus"], outputs.get("RuntimeArn")


def runtime_status(control: Any, runtime_arn: str) -> str:
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    try:
        return control.get_agent_runtime(agentRuntimeId=runtime_id)["status"]
    except ClientError as error:
        return f"error:{error.response.get('Error', {}).get('Code', 'unknown')}"


# --------------------------------------------------------------------------- #
# Pure assessment (unit-tested)
# --------------------------------------------------------------------------- #
def version_of(body: Mapping[str, Any]) -> str:
    value = body.get("agentVersion")
    return value if isinstance(value, str) and value else NO_VERSION


def sample_passed(status: int, body: Mapping[str, Any]) -> bool:
    return all(assess_positive(status, body).values())


def version_timeline(samples: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Collapse consecutive samples with the same version into segments."""
    segments: list[dict[str, Any]] = []
    for sample in samples:
        if not sample["passed"]:
            continue  # a failed sample reports no trustworthy version
        version = sample["agentVersion"]
        if segments and segments[-1]["version"] == version:
            segments[-1]["lastSample"] = sample["index"]
            segments[-1]["lastAtSeconds"] = sample["atSeconds"]
            segments[-1]["samples"] += 1
        else:
            segments.append(
                {
                    "version": version,
                    "firstSample": sample["index"],
                    "lastSample": sample["index"],
                    "firstAtSeconds": sample["atSeconds"],
                    "lastAtSeconds": sample["atSeconds"],
                    "samples": 1,
                }
            )
    return segments


def state_timeline(samples: Sequence[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for sample in samples:
        value = sample[key]
        if segments and segments[-1]["state"] == value:
            segments[-1]["lastAtSeconds"] = sample["atSeconds"]
            segments[-1]["samples"] += 1
        else:
            segments.append({"state": value, "firstAtSeconds": sample["atSeconds"], "lastAtSeconds": sample["atSeconds"], "samples": 1})
    return segments


def summarize(samples: Sequence[Mapping[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    total = len(samples)
    passed = sum(1 for s in samples if s["passed"])
    availability = (passed / total) if total else 0.0
    versions = version_timeline(samples)
    observed_versions = [seg["version"] for seg in versions]
    # Collapse repeats for the transition check: A,B,A is a real oscillation and must fail.
    stack_states = state_timeline(samples, "stackStatus")
    runtime_states = state_timeline(samples, "runtimeStatus")
    stack_changed = any(seg["state"] not in TERMINAL_STACK_STATES for seg in stack_states) or len(
        {seg["state"] for seg in stack_states}
    ) > 1
    latencies = sorted(s["latencySeconds"] for s in samples if s["passed"])

    def pct(p: float) -> float | None:
        if not latencies:
            return None
        return latencies[min(len(latencies) - 1, int(round((len(latencies) - 1) * p)))]

    checks: dict[str, Any] = {
        "enoughSamples": total >= args.min_samples,
        "availabilityMet": availability >= args.min_availability,
        "noFailedSamples": passed == total,
    }
    if args.expect_transition:
        source, _, target = args.expect_transition.partition(":")
        checks["transitionObserved"] = observed_versions == [source, target]
        checks["expectedTransition"] = f"{source}->{target}"
    elif args.expect_final_version:
        checks["finalVersionMatches"] = bool(versions) and versions[-1]["version"] == args.expect_final_version
        checks["expectedFinalVersion"] = args.expect_final_version
    elif args.expect_stable_version:
        checks["singleVersionObserved"] = len(versions) == 1
        checks["noStackChangeObserved"] = not stack_changed
    gate_keys = [k for k in checks if not k.startswith("expected")]
    return {
        "samples": total,
        "passedSamples": passed,
        "failedSamples": total - passed,
        "availability": round(availability, 4),
        "latencySecondsP50": pct(0.5),
        "latencySecondsP95": pct(0.95),
        "latencySecondsMax": latencies[-1] if latencies else None,
        "versionTimeline": versions,
        "stackStatusTimeline": stack_states,
        "runtimeStatusTimeline": runtime_states,
        "stackChangeObserved": stack_changed,
        "checks": checks,
        "passed": all(checks[k] is True for k in gate_keys),
    }


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    session = boto3.Session()
    verify_identity(session, args.expected_account)
    cfn = session.client("cloudformation", region_name=args.region)
    control = session.client("bedrock-agentcore-control", region_name=args.region)

    started = datetime.now(timezone.utc)
    status0, runtime_arn = stack_state(cfn, args.stack_name)
    if not runtime_arn:
        raise ProbeError("stack has no RuntimeArn output")
    evidence: dict[str, Any] = {
        "probe": "generated-agent-deployment-continuity",
        "region": args.region,
        "startedAt": started.isoformat(),
        "accountMatched": True,
        "runtimeArnFingerprint": fingerprint(runtime_arn),
        "initialStackStatus": status0,
        "promptFingerprint": fingerprint(POSITIVE_PROMPT),
        "intervalSeconds": args.interval_seconds,
        "durationSeconds": args.duration_seconds,
    }
    actor_id = f"continuity-actor-{uuid.uuid4().hex[:12]}"
    evidence["actorIdFingerprint"] = fingerprint(actor_id)
    if args.stream_out:
        args.stream_out.parent.mkdir(parents=True, exist_ok=True)
        args.stream_out.write_text("")

    samples: list[dict[str, Any]] = []
    change_seen = False
    settled_run = 0
    t_start = time.monotonic()
    index = 0
    while time.monotonic() - t_start < args.duration_seconds:
        loop_t0 = time.monotonic()
        stack_status, current_arn = stack_state(cfn, args.stack_name)
        rt_status = runtime_status(control, current_arn or runtime_arn)
        payload = {"prompt": POSITIVE_PROMPT, "actorId": actor_id}
        t0 = time.monotonic()
        status, body, _session = invoke(session, args.region, current_arn or runtime_arn, payload, args.timeout)
        latency = round(time.monotonic() - t0, 2)
        sample = {
            "index": index,
            "atSeconds": round(t0 - t_start, 1),
            "httpStatus": status,
            "latencySeconds": latency,
            "passed": sample_passed(status, body),
            "agentVersion": version_of(body),
            "stackStatus": stack_status,
            "runtimeStatus": rt_status,
            "runtimeArnChanged": bool(current_arn) and current_arn != runtime_arn,
            "errorCode": body.get("errorCode"),
        }
        samples.append(sample)
        if args.stream_out:
            with args.stream_out.open("a") as handle:
                handle.write(json.dumps(sample, sort_keys=True) + "\n")
        print(json.dumps(sample, sort_keys=True), flush=True)

        if stack_status not in TERMINAL_STACK_STATES or rt_status != "READY":
            change_seen = True
            settled_run = 0
        elif change_seen and sample["passed"]:
            settled_run += 1
        if args.stop_when_settled and change_seen and settled_run >= args.settle_samples:
            evidence["stoppedEarly"] = "settled"
            break
        index += 1
        remaining = args.interval_seconds - (time.monotonic() - loop_t0)
        if remaining > 0:
            time.sleep(remaining)

    evidence.update(summarize(samples, args))
    evidence["finishedAt"] = datetime.now(timezone.utc).isoformat()
    evidence["samplesDetail"] = samples
    args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
    args.evidence_out.write_text(json.dumps(evidence, indent=2, sort_keys=True))
    summary = {k: v for k, v in evidence.items() if k != "samplesDetail"}
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if evidence["passed"] else 2


if __name__ == "__main__":  # pragma: no cover - live entrypoint
    try:
        sys.exit(main())
    except ProbeError as error:
        print(json.dumps({"passed": False, "error": str(error)}), file=sys.stderr)
        sys.exit(2)

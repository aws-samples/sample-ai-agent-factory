"""Offline contract tests for ``deployment_continuity_probe.py`` (no AWS calls)."""
from __future__ import annotations

import deployment_continuity_probe as p
from live_invoke_probe import ECHO_TOOL, HANDSHAKE_MARKER


def _good_body(version: str | None = "1.1.0") -> dict:
    body = {
        "marker": HANDSHAKE_MARKER,
        "discoveredToolCount": 3,
        "toolCalls": [ECHO_TOOL],
        "contentBlocks": 2,
        "memoryConfigured": True,
        "memoryRoundTrip": True,
    }
    if version is not None:
        body["agentVersion"] = version
    return body


def _sample(index: int, version: str, passed: bool = True, stack: str = "UPDATE_COMPLETE", runtime: str = "READY") -> dict:
    return {
        "index": index,
        "atSeconds": float(index * 15),
        "httpStatus": 200 if passed else 424,
        "latencySeconds": 8.0 + index * 0.1,
        "passed": passed,
        "agentVersion": version,
        "stackStatus": stack,
        "runtimeStatus": runtime,
        "runtimeArnChanged": False,
        "errorCode": None if passed else "RuntimeClientError",
    }


def _args(**overrides) -> object:
    argv = ["--expected-account", "111111111111", "--stack-name", "s", "--evidence-out", "e.json"]
    for key, value in overrides.items():
        flag = "--" + key.replace("_", "-")
        if value is True:
            argv.append(flag)
        else:
            argv.extend([flag, str(value)])
    return p.parse_args(argv)


def test_version_of_reports_none_marker_for_revisions_without_the_field() -> None:
    assert p.version_of(_good_body(None)) == p.NO_VERSION
    assert p.version_of(_good_body("")) == p.NO_VERSION
    assert p.version_of(_good_body("1.1.0")) == "1.1.0"


def test_sample_passes_only_when_every_positive_check_holds() -> None:
    assert p.sample_passed(200, _good_body()) is True
    degraded = _good_body()
    degraded["memoryRoundTrip"] = False
    assert p.sample_passed(200, degraded) is False
    assert p.sample_passed(424, {"errorCode": "RuntimeClientError"}) is False


def test_failed_checks_name_the_silent_partial_success_shape() -> None:
    # Live 2026-09-25 (prod, agent 1.1.0): HTTP 200, marker present, no tool
    # call. The sample must say WHICH legs failed, not only passed=false.
    silent = {**_good_body(), "toolCalls": []}
    checks = p.assess_positive(200, silent)
    assert p.failed_checks(checks) == ["echoToolCalled"]
    violated = {**_good_body("1.2.0"), "toolCalls": [], "stopReason": "protocol_violation"}
    assert p.failed_checks(p.assess_positive(200, violated)) == ["echoToolCalled", "stopReasonDone"]
    assert p.failed_checks(p.assess_positive(200, _good_body())) == []


def test_version_timeline_collapses_runs_and_ignores_failed_samples() -> None:
    samples = [
        _sample(0, "none"),
        _sample(1, "none"),
        _sample(2, "none", passed=False),  # a failed sample carries no trustworthy version
        _sample(3, "1.1.0"),
        _sample(4, "1.1.0"),
    ]
    timeline = p.version_timeline(samples)
    assert [(seg["version"], seg["samples"]) for seg in timeline] == [("none", 2), ("1.1.0", 2)]
    assert timeline[1]["firstSample"] == 3


def test_transition_gate_passes_on_exact_ordered_transition() -> None:
    samples = [_sample(i, "none", stack="UPDATE_IN_PROGRESS" if i == 3 else "UPDATE_COMPLETE") for i in range(5)]
    samples += [_sample(i, "1.1.0", runtime="READY") for i in range(5, 10)]
    summary = p.summarize(samples, _args(expect_transition="none:1.1.0"))
    assert summary["checks"]["transitionObserved"] is True
    assert summary["stackChangeObserved"] is True
    assert summary["availability"] == 1.0
    assert summary["passed"] is True


def test_transition_gate_fails_on_oscillation_or_missing_target() -> None:
    oscillating = [_sample(0, "none"), _sample(1, "1.1.0"), _sample(2, "none")] + [
        _sample(i, "1.1.0") for i in range(3, 9)
    ]
    assert p.summarize(oscillating, _args(expect_transition="none:1.1.0"))["passed"] is False
    never = [_sample(i, "none") for i in range(9)]
    assert p.summarize(never, _args(expect_transition="none:1.1.0"))["checks"]["transitionObserved"] is False


def test_any_failed_sample_fails_the_default_availability_gate() -> None:
    samples = [_sample(i, "1.1.0") for i in range(9)] + [_sample(9, "1.1.0", passed=False)]
    summary = p.summarize(samples, _args(expect_final_version="1.1.0"))
    assert summary["failedSamples"] == 1
    assert summary["checks"]["noFailedSamples"] is False
    assert summary["passed"] is False
    relaxed = p.summarize(samples, _args(expect_final_version="1.1.0", min_availability="0.9"))
    assert relaxed["checks"]["availabilityMet"] is True
    assert relaxed["passed"] is False, "noFailedSamples still gates: availability alone never passes"


def test_final_version_gate_reads_the_last_passing_sample() -> None:
    samples = [_sample(i, "none") for i in range(4)] + [_sample(i, "1.1.0") for i in range(4, 8)]
    samples += [_sample(i, "none", stack="UPDATE_ROLLBACK_COMPLETE") for i in range(8, 12)]
    summary = p.summarize(samples, _args(expect_final_version="none"))
    assert summary["checks"]["finalVersionMatches"] is True
    assert [seg["version"] for seg in summary["versionTimeline"]] == ["none", "1.1.0", "none"]
    assert summary["passed"] is True


def test_stable_gate_is_the_no_op_contract() -> None:
    stable = [_sample(i, "1.1.0") for i in range(10)]
    summary = p.summarize(stable, _args(expect_stable_version=True))
    assert summary["checks"] == {
        "enoughSamples": True,
        "availabilityMet": True,
        "noFailedSamples": True,
        "singleVersionObserved": True,
        "noStackChangeObserved": True,
    }
    assert summary["passed"] is True
    changed = stable[:5] + [_sample(5, "1.1.0", stack="UPDATE_IN_PROGRESS")] + stable[6:]
    assert p.summarize(changed, _args(expect_stable_version=True))["checks"]["noStackChangeObserved"] is False


def test_too_few_samples_fail_closed() -> None:
    summary = p.summarize([_sample(0, "1.1.0")], _args(expect_final_version="1.1.0"))
    assert summary["checks"]["enoughSamples"] is False
    assert summary["passed"] is False


def test_latency_percentiles_use_passing_samples_only() -> None:
    samples = [_sample(i, "1.1.0") for i in range(10)]
    samples[9]["passed"] = False
    samples[9]["latencySeconds"] = 999.0
    summary = p.summarize(samples, _args(expect_final_version="1.1.0", min_availability="0.5"))
    assert summary["latencySecondsMax"] < 100

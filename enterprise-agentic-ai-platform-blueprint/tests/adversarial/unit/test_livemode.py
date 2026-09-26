"""The live-mode gate: skip when off, error when asked for and unavailable.

Every test here supplies a synthetic environment and, where needed, a fake
identity probe. Nothing touches AWS.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.catalog import AdversarialCase, AuditSource, Domain, Expectation, Severity
from harness.errors import LiveModeRequiredError
from harness.livemode import (
    ENV_LIVE,
    ENV_MANIFEST,
    ENV_REQUIRE_LIVE,
    GateAction,
    gate_for_case,
    live_mode_status,
)

pytestmark = pytest.mark.adversarial

EXAMPLE = str(Path(__file__).resolve().parents[1] / "fixtures" / "manifest.example.json")

ACCOUNT_IDS = {
    "AGENTICAI_ACCOUNT_MANAGEMENT": "111111111111",
    "AGENTICAI_ACCOUNT_PLATFORM": "222222222222",
    "AGENTICAI_ACCOUNT_WORKSTREAM": "333333333333",
}

CREDENTIALS = {
    "AWS_ACCESS_KEY_ID_MANAGEMENT": "x",
    "AWS_SECRET_ACCESS_KEY_MANAGEMENT": "x",
    "AWS_ACCESS_KEY_ID_PLATFORM": "x",
    "AWS_SECRET_ACCESS_KEY_PLATFORM": "x",
    "AWS_ACCESS_KEY_ID_WORKSTREAM": "x",
    "AWS_SECRET_ACCESS_KEY_WORKSTREAM": "x",
    "AGENTICAI_ADVERSARIAL_EXTERNAL_ID": "external-id-placeholder",
}


def matching_probe(account_key: str, expected_account_id: str) -> str:
    """A probe that agrees with the manifest. No AWS involved."""
    return expected_account_id


def mismatching_probe(account_key: str, expected_account_id: str) -> str:
    return "999999999999"


def exploding_probe(account_key: str, expected_account_id: str) -> str:
    raise RuntimeError("sts unreachable")


def live_env(**overrides: str) -> dict[str, str]:
    env = {
        ENV_LIVE: "1",
        ENV_MANIFEST: EXAMPLE,
        **ACCOUNT_IDS,
        **CREDENTIALS,
    }
    env.update(overrides)
    return env


def live_case(case_id: str = "CASE-N", live_required: bool = True) -> AdversarialCase:
    return AdversarialCase(
        case_id=case_id,
        domain=Domain.SCP,
        title="fixture",
        expectation=Expectation.DENY,
        severity=Severity.CRITICAL,
        principal_ref="workstream.agent_runtime",
        target="bedrock:InvokeModel",
        rationale="fixture",
        control_refs=("fixture",),
        expected_error_codes=("AccessDeniedException",),
        expected_http_status=(403,),
        audit_sources=(AuditSource.CLOUDTRAIL,),
        positive_twin="CASE-P",
        live_required=live_required,
    )


# ---------------------------------------------------------------------------
# off
# ---------------------------------------------------------------------------


def test_live_mode_is_off_by_default():
    status = live_mode_status({}, identity_probe=matching_probe)
    assert not status.requested
    assert not status.required
    assert not status.available
    assert not status.enabled


def test_case_skips_when_live_mode_is_off():
    status = live_mode_status({}, identity_probe=matching_probe)
    decision = gate_for_case(live_case(), status)
    assert decision.action is GateAction.SKIP
    assert "requires live AWS" in decision.reason


def test_off_status_does_not_raise():
    live_mode_status({}, identity_probe=matching_probe).raise_if_unsatisfied()


def test_non_live_case_runs_even_with_live_mode_off():
    status = live_mode_status({}, identity_probe=matching_probe)
    decision = gate_for_case(live_case(live_required=False), status)
    assert decision.action is GateAction.RUN


# ---------------------------------------------------------------------------
# requested but unavailable -> error, never skip
# ---------------------------------------------------------------------------


def test_requested_without_a_manifest_is_an_error():
    status = live_mode_status({ENV_LIVE: "1"}, identity_probe=matching_probe)
    assert status.enabled
    assert not status.available
    assert any("manifest not configured" in reason for reason in status.reasons)
    assert gate_for_case(live_case(), status).action is GateAction.ERROR
    with pytest.raises(LiveModeRequiredError, match="manifest not configured"):
        status.raise_if_unsatisfied()


def test_requested_without_account_mapping_is_an_error():
    env = live_env()
    for name in ACCOUNT_IDS:
        env.pop(name)
    status = live_mode_status(env, identity_probe=matching_probe)
    assert not status.available
    assert gate_for_case(live_case(), status).action is GateAction.ERROR
    with pytest.raises(LiveModeRequiredError, match="id not mapped"):
        status.raise_if_unsatisfied()


def test_requested_with_one_account_missing_is_an_error():
    env = live_env()
    env.pop("AGENTICAI_ACCOUNT_WORKSTREAM")
    status = live_mode_status(env, identity_probe=matching_probe)
    assert not status.available
    assert any("workstream" in reason for reason in status.reasons)


def test_requested_without_credentials_is_an_error():
    env = live_env()
    for name in CREDENTIALS:
        env.pop(name)
    status = live_mode_status(env, identity_probe=matching_probe)
    assert not status.available
    assert any("credentials unavailable" in reason for reason in status.reasons)


def test_required_but_not_requested_is_still_an_error():
    """A CI run that mandates live evidence cannot silently produce none."""
    status = live_mode_status({ENV_REQUIRE_LIVE: "1"}, identity_probe=matching_probe)
    assert status.required
    assert status.enabled
    assert gate_for_case(live_case(), status).action is GateAction.ERROR


def test_a_broken_manifest_is_an_error(tmp_path: Path):
    bad = tmp_path / "manifest.json"
    bad.write_text('{"schemaVersion": "1.0"}', encoding="utf-8")
    status = live_mode_status(
        live_env(**{ENV_MANIFEST: str(bad)}), identity_probe=matching_probe
    )
    assert not status.available
    assert any("unusable" in reason for reason in status.reasons)


def test_missing_identity_probe_leaves_live_mode_unverified():
    status = live_mode_status(live_env(), identity_probe=None)
    assert not status.available
    assert any("identity not verified" in reason for reason in status.reasons)


def test_credentials_pointing_at_the_wrong_account_is_an_error():
    status = live_mode_status(live_env(), identity_probe=mismatching_probe)
    assert not status.available
    assert any("different account" in reason for reason in status.reasons)
    assert status.checks["identity"] == "mismatch"


def test_identity_probe_failure_is_reported_not_raised():
    status = live_mode_status(live_env(), identity_probe=exploding_probe)
    assert not status.available
    assert any("identity probe failed" in reason for reason in status.reasons)


# ---------------------------------------------------------------------------
# available
# ---------------------------------------------------------------------------


def test_available_when_everything_resolves():
    status = live_mode_status(live_env(), identity_probe=matching_probe)
    assert status.available
    assert status.identity_verified
    assert status.checks == {
        "manifest": "loaded",
        "accountMapping": "complete",
        "identity": "verified",
    }
    assert gate_for_case(live_case(), status).action is GateAction.RUN


def test_require_returns_the_resolved_manifest():
    status = live_mode_status(live_env(), identity_probe=matching_probe)
    resolved = status.require()
    assert resolved.complete
    assert len(resolved.manifest_sha) == 64


def test_require_raises_when_live_mode_is_off():
    status = live_mode_status({}, identity_probe=matching_probe)
    with pytest.raises(LiveModeRequiredError):
        status.require()


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_flag_spellings_enable_live_mode(flag):
    status = live_mode_status({ENV_LIVE: flag}, identity_probe=matching_probe)
    assert status.requested


@pytest.mark.parametrize("flag", ["0", "false", "no", "", "maybe"])
def test_other_flag_values_do_not_enable_live_mode(flag):
    status = live_mode_status({ENV_LIVE: flag}, identity_probe=matching_probe)
    assert not status.requested


def test_describe_explains_how_to_enable_live_mode():
    status = live_mode_status({}, identity_probe=matching_probe)
    assert ENV_LIVE in status.describe()
    assert ENV_MANIFEST in status.describe()

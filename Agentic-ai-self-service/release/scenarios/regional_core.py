"""``core-runtime-gateway-identity-policy-evaluation`` -- one Region, one real journey.

The ``live_region`` kind of the ``regional.certified_core`` control: every selected Region
must independently prove the core platform (AgentCore Runtime, Gateway, identity, policy,
evaluation) through the deployed customer path, with the Region's reviewed feature
boundary enforced -- a *full* Region exercises managed Web Search, a *reduced* Region
refuses it synchronously and reports the AWS Agent Registry surface exactly as the matrix
says.

Phases are the tracked ``backend/tests/integration`` suites, run as subprocesses with a
synthetic Cognito user authenticated through the frontend's pinned Amplify SRP flow:

* ``lifecycle``  -- deploy an agent, observe every state, invoke it, delete it, verify.
* ``gateway``    -- the Strands Gateway gallery template: Runtime + Gateway + Cedar policy.
* ``web-search`` -- (full only) the managed Web Search template: the Region-gated feature.
* ``features``   -- the matrix twins: Web Search refused / Agent Registry reported per profile.
* ``triggers``   -- (full only) all four trigger sources through evaluation and promotion.

Any phase failing, any synthetic user not deleted, or any binding problem fails the
scenario; a missing sealed assembly or an unreadable stack blocks it. Nothing passes
because a label said so.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from _live import (
    ScenarioFailed,
    bound_deployment,
    release_region_profile,
    run_integration_phase,
    with_synthetic_user,
    write_json,
)

LIFECYCLE = "tests/integration/test_deployment_lifecycle.py"
GALLERY = "tests/integration/test_template_deployments.py::test_gallery_template_deploy_invoke_and_verified_cleanup"
FEATURES = "tests/integration/test_reduced_region_features.py"
TRIGGERS = "tests/integration/test_trigger_delivery_matrix.py"


def phases_for(profile: str) -> list[tuple[str, str, int]]:
    """(phase, pytest selector, timeout seconds) in execution order for a Region profile."""
    common_head = [
        ("lifecycle", LIFECYCLE, 1800),
        ("gateway", f"{GALLERY}[strands-gateway-agent]", 1800),
    ]
    if profile == "full":
        return [
            *common_head,
            ("web-search", f"{GALLERY}[web-search-agent]", 1800),
            ("features", FEATURES, 600),
            ("triggers", TRIGGERS, 2400),
        ]
    if profile == "reduced":
        return [*common_head, ("features", FEATURES, 600)]
    raise ScenarioFailed(f"profile {profile!r} is not a certifiable release profile")


def core_runtime_gateway_identity_policy_evaluation(args: argparse.Namespace, artifact_dir: Path) -> dict:
    binding, observed, comparison = bound_deployment(args, artifact_dir)
    profile = release_region_profile(binding.region)
    if not profile.get("production_release"):
        raise ScenarioFailed(f"{binding.region} is not a production release Region")
    plan = phases_for(str(profile["profile"]))
    records: list[dict] = []
    with with_synthetic_user(observed, label="core") as user:
        for phase, selector, timeout in plan:
            record = run_integration_phase(
                phase=phase,
                selector=selector,
                observed=observed,
                user=user,
                artifact_dir=artifact_dir,
                timeout=timeout,
                extra_env={"INTEGRATION_TRIGGERS_TABLE_NAME": "acf-production-triggers"},
            )
            records.append(record)
            if record.get("failed"):
                break
    report = {
        "scenario": args.scenario_id,
        "region": binding.region,
        "profile": profile["profile"],
        "feature_matrix": {
            "web_search": bool(profile.get("web_search")),
            "agent_registry": bool(profile.get("agent_registry")),
        },
        "customer_path": observed.cloudfront_url,
        "authentication": "real Cognito USER_SRP_AUTH via the frontend's pinned Amplify",
        "deployment_binding": comparison,
        "phases": records,
        "phases_planned": [phase for phase, _, _ in plan],
    }
    write_json(artifact_dir / "report", report)
    failed = [record["phase"] for record in records if record.get("failed")]
    if failed or len(records) != len(plan):
        raise ScenarioFailed(f"phases failed or did not run: failed={failed}, ran={len(records)}/{len(plan)}")
    return report

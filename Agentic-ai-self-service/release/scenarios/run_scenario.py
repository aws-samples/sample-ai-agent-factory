#!/usr/bin/env python3 -I
"""Scenario dispatcher for release evidence.

`scripts/release_evidence.py run-scenario` executes exactly this file for every
scenario the traceability contract names. The recorder decides the status from
what happens here, never from a label:

* exit 0 and every required artifact written under ``--artifact-dir`` -> pass
* exit 3 -> ``blocked`` (not implemented here, or a prerequisite is missing)
* any other exit, a timeout, or a missing required artifact -> ``fail``

A live scenario must also write ``provenance`` (JSON) naming the AWS account,
Region, stack, deployed commit, and deployed project tree it actually observed;
the recorder refuses provenance that disagrees with the run's bound account,
the requested Region, or the candidate commit/tree.

Unknown or unimplemented scenarios are BLOCKED, never faked. Registering an
implementation is a reviewed source change: the contract fingerprints and the
project tree change, so an in-progress evidence run is invalidated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

BLOCKED = 3
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# `python -I` (isolated mode) does not put this script's directory on sys.path; the sibling
# modules here are tracked project code, so add exactly this directory and nothing else.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
# Never write bytecode into the tree being certified: a __pycache__ would make it dirty.
sys.dont_write_bytecode = True
ROOT_WORKFLOW_RELATIVE = ".github/workflows/agentic-ai-self-service-ci.yml"
CONTRACTS = (
    "release/production-readiness-controls.json",
    "release/agentcore-regions.json",
    "release/production-readiness-traceability.json",
)


from _live import ScenarioBlocked, ScenarioFailed  # noqa: E402  -- shared with every family module


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-P", *args], cwd=repo, check=False, capture_output=True, text=True, timeout=60)
    if completed.returncode != 0:
        raise ScenarioFailed(f"git {' '.join(args[:2])} failed")
    return completed.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_commit_tree_and_contract_digests_match(args: argparse.Namespace, artifact_dir: Path) -> dict:
    """exact_commit: re-derive the candidate identity INDEPENDENTLY and compare it to the run.

    The recorder bound the run to a commit, project tree, CI-workflow blob, and contract
    file digests when it started. This scenario recomputes all of them from the working
    repository with its own git/hash calls and fails if any disagree -- so a run whose
    recorder was fed a stale or edited identity cannot pass this gate.
    """
    run = json.loads((args.run_dir / "run.json").read_text(encoding="utf-8"))
    source = run["source"]
    repo = Path(_git(PROJECT_ROOT, "rev-parse", "--show-toplevel")).resolve()
    project_rel = PROJECT_ROOT.resolve().relative_to(repo).as_posix()
    commit = _git(repo, "rev-parse", "HEAD")
    observed = {
        "commit": commit,
        "project_tree": _git(repo, "rev-parse", f"{commit}:{project_rel}"),
        "root_workflow_blob": _git(repo, "rev-parse", f"{commit}:{ROOT_WORKFLOW_RELATIVE}"),
        "dirty": _git(
            repo, "status", "--porcelain=v1", "--untracked-files=all", "--", project_rel, ROOT_WORKFLOW_RELATIVE
        ),
        "contracts": {name: _sha256(PROJECT_ROOT / name) for name in CONTRACTS},
        "root_workflow_sha256": _sha256(repo / ROOT_WORKFLOW_RELATIVE),
    }
    expected = {
        "commit": source["commit"],
        "project_tree": source["project_tree"],
        "root_workflow_blob": source["root_workflow_blob"],
        "contracts": {
            "release/production-readiness-controls.json": run["contracts"]["controls"]["sha256"],
            "release/agentcore-regions.json": run["contracts"]["regions"]["sha256"],
            "release/production-readiness-traceability.json": run["contracts"]["traceability"]["sha256"],
        },
        "root_workflow_sha256": run["contracts"]["root_workflow"]["sha256"],
    }
    mismatches = [
        key
        for key in ("commit", "project_tree", "root_workflow_blob", "contracts", "root_workflow_sha256")
        if observed[key] != expected[key]
    ]
    if observed["dirty"]:
        mismatches.append("working_tree_dirty")
    report = {"scenario": args.scenario_id, "observed": observed, "expected": expected, "mismatches": mismatches}
    _write_json(artifact_dir / "report", report)
    if mismatches:
        raise ScenarioFailed(f"candidate identity mismatch: {mismatches}")
    return report


SCENARIOS: dict[str, Callable[[argparse.Namespace, Path], dict]] = {
    "source-commit-tree-and-contract-digests-match": source_commit_tree_and_contract_digests_match,
}


def _register_live_families() -> None:
    """Live families import boto3 and the frontend toolchain; load them only when dispatching."""
    import audit_controls
    import browser_widget
    import cost_controls
    import download_integrity
    import downloadable_cfn
    import downloadable_python
    import exact_commit
    import gateway_governance
    import jit_controls
    import kms_controls
    import obo_controls
    import regional_core
    import rollback
    import triggers
    import zero_residue

    SCENARIOS["core-runtime-gateway-identity-policy-evaluation"] = (
        regional_core.core_runtime_gateway_identity_policy_evaluation
    )
    SCENARIOS["ui-authenticated-python-download-clean-recipient-lock-install-compile-start-invoke"] = (
        downloadable_python.ui_authenticated_python_download_clean_recipient
    )
    SCENARIOS["api-authenticated-cfn-download-clean-recipient-deploy-invoke-update-rollback-teardown"] = (
        downloadable_cfn.api_authenticated_cfn_download_full_journey
    )
    SCENARIOS["downloaded-cfn-update-rollback-restores-prior-behavior"] = (
        downloadable_cfn.downloaded_cfn_update_rollback_restores_prior_behavior
    )
    SCENARIOS["downloaded-cfn-teardown-direct-service-inventories-zero"] = (
        downloadable_cfn.downloaded_cfn_teardown_direct_service_inventories_zero
    )
    SCENARIOS["foreign-decoys-preserved"] = downloadable_cfn.foreign_decoys_preserved
    SCENARIOS["direct-service-inventories-zero"] = zero_residue.direct_service_inventories_zero
    SCENARIOS["aliasless-kms-and-log-groups-zero"] = zero_residue.aliasless_kms_and_log_groups_zero
    SCENARIOS["cloudfront-widget-invokes-production-alias"] = browser_widget.cloudfront_widget_invokes_production_alias
    SCENARIOS["widget-model-output-renders-inert"] = browser_widget.widget_model_output_renders_inert
    SCENARIOS["widget-token-attribute-ignored"] = browser_widget.widget_token_attribute_ignored
    SCENARIOS["widget-target-injection-denied"] = browser_widget.widget_target_injection_denied
    SCENARIOS["real-browser-cloudfront-widget-flow"] = browser_widget.real_browser_cloudfront_widget_flow
    SCENARIOS["arbitrary-version-rollback-denied"] = rollback.arbitrary_version_rollback_denied
    SCENARIOS["production-serves-exact-previous-version"] = rollback.production_serves_exact_previous_version
    SCENARIOS["batch-evaluation-pass-and-production-promotion"] = (
        rollback.batch_evaluation_pass_and_production_promotion
    )
    SCENARIOS["forged-or-stale-evidence-denied"] = rollback.forged_or_stale_evidence_denied
    SCENARIOS["degraded-result-denied"] = rollback.degraded_result_denied
    SCENARIOS["signed-delivery-executes-once"] = triggers.signed_delivery_executes_once
    SCENARIOS["identical-replay-is-idempotent"] = triggers.identical_replay_is_idempotent
    SCENARIOS["changed-body-same-id-conflicts"] = triggers.changed_body_same_id_conflicts
    SCENARIOS["concurrent-claim-conflicts-and-recovers"] = triggers.concurrent_claim_conflicts_and_recovers
    SCENARIOS["mutation-intent-and-outcome-visible"] = audit_controls.mutation_intent_and_outcome_visible
    SCENARIOS["audit-store-outage-blocks-mutation"] = audit_controls.audit_store_outage_blocks_mutation
    SCENARIOS["tampered-row-read-denied"] = audit_controls.tampered_row_read_denied
    SCENARIOS["concurrent-transaction-reservations-stay-within-limit"] = (
        cost_controls.concurrent_transaction_reservations_stay_within_limit
    )
    SCENARIOS["exhausted-budget-denies-before-dispatch"] = cost_controls.exhausted_budget_denies_before_dispatch
    SCENARIOS["budget-store-outage-denies-before-dispatch"] = cost_controls.budget_store_outage_denies_before_dispatch
    SCENARIOS["leased-approval-applies-exact-policy"] = jit_controls.leased_approval_applies_exact_policy
    SCENARIOS["concurrent-rejection-cannot-win"] = jit_controls.concurrent_rejection_cannot_win
    SCENARIOS["gateway-policy-mutation-request-denied"] = jit_controls.gateway_policy_mutation_request_denied
    SCENARIOS["model-and-tool-content-allowed"] = gateway_governance.model_and_tool_content_allowed
    SCENARIOS["prompt-attack-blocked"] = gateway_governance.prompt_attack_blocked
    SCENARIOS["tool-argument-blocked"] = gateway_governance.tool_argument_blocked
    SCENARIOS["tool-result-blocked"] = gateway_governance.tool_result_blocked
    SCENARIOS["guardrail-outage-fails-closed"] = gateway_governance.guardrail_outage_fails_closed
    SCENARIOS["cedar-authorized-tool-call"] = gateway_governance.cedar_authorized_tool_call
    SCENARIOS["cedar-unauthorized-tool-call-denied"] = gateway_governance.cedar_unauthorized_tool_call_denied
    SCENARIOS["runtime-role-update-gateway-denied"] = gateway_governance.runtime_role_update_gateway_denied
    SCENARIOS["connector-tools-list-and-authorized-call"] = gateway_governance.connector_tools_list_and_authorized_call
    SCENARIOS["connector-cedar-denial"] = gateway_governance.connector_cedar_denial
    SCENARIOS["connector-tool-guardrail-denial"] = gateway_governance.connector_tool_guardrail_denial
    SCENARIOS["target-cmk-encrypts-batch-evaluation"] = kms_controls.target_cmk_encrypts_batch_evaluation
    SCENARIOS["wrong-account-key-denied"] = kms_controls.wrong_account_key_denied
    SCENARIOS["wrong-region-key-denied"] = kms_controls.wrong_region_key_denied
    SCENARIOS["workforce-token-exchange-whoami"] = obo_controls.workforce_token_exchange_whoami
    SCENARIOS["substituted-subject-denied"] = obo_controls.substituted_subject_denied
    SCENARIOS["direct-runtime-bypass-denied"] = obo_controls.direct_runtime_bypass_denied
    SCENARIOS["source-commit-tree-and-contract-digests-match"] = (
        exact_commit.source_commit_tree_and_contract_digests_match
    )
    SCENARIOS["deployed-api-authenticated-cfn-download-archive-digest-manifest-verified"] = (
        download_integrity.cfn_download_digest_verified
    )
    SCENARIOS["deployed-api-authenticated-python-download-archive-digest-manifest-verified"] = (
        download_integrity.python_download_digest_verified
    )
    SCENARIOS["tampered-archive-or-manifest-rejected-before-use"] = download_integrity.tampered_archive_rejected
    SCENARIOS["generated-ci-defaults-remain-inert"] = download_integrity.generated_ci_defaults_inert
    SCENARIOS["cfn-wrong-account-or-region-refused"] = download_integrity.cfn_wrong_account_or_region_refused
    SCENARIOS["downloadable-assets-source-contract-and-archive-digests-match"] = (
        download_integrity.source_contract_and_archive_digests_match
    )
    SCENARIOS["python-missing-or-incompatible-dependency-rejected"] = (
        download_integrity.python_missing_dependency_rejected
    )


_register_live_families()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario_id")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--region")
    parser.add_argument("--account-id")
    args = parser.parse_args(argv)
    implementation = SCENARIOS.get(args.scenario_id)
    if implementation is None:
        print(f"scenario {args.scenario_id} has no reviewed implementation; recording blocked", file=sys.stderr)
        return BLOCKED
    try:
        implementation(args, args.artifact_dir)
    except ScenarioBlocked as exc:
        print(f"scenario {args.scenario_id} blocked: {exc}", file=sys.stderr)
        return BLOCKED
    except ScenarioFailed as exc:
        print(f"scenario {args.scenario_id} failed: {exc}", file=sys.stderr)
        return 1
    print(f"scenario {args.scenario_id} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

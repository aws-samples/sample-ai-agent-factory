"""Executable tests for `preflight_vpc_capacity` in scripts/deploy.sh.

The platform always creates ToolSandboxVpc; on 2026-09-25 a fresh stack ran 16 minutes
of dependency builds and then died at CREATE_IN_PROGRESS on "The maximum number of VPCs
has been reached" (5/5), stranding 20 Retain resources. The preflight must:

  * fail a stack that does not yet OWN a VPC, before any build or mutation, when
    count >= quota;
  * skip ONLY when the existing stack demonstrably owns an AWS::EC2::VPC in a *_COMPLETE
    state -- a ROLLBACK_COMPLETE / REVIEW_IN_PROGRESS / legacy stack exists without one;
  * fail CLOSED when the count, the quota or the stack's resources cannot be read
    (proceeding is exactly the late failure the gate exists to prevent), with the only
    bypass being an explicit, logged SKIP_VPC_PREFLIGHT=true.

These run the REAL function body against a stubbed `aws`.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# DEPLOY_SH_UNDER_TEST lets a mutation run point these tests at a deliberately broken copy.
DEPLOY_SH = Path(os.environ.get("DEPLOY_SH_UNDER_TEST", REPO_ROOT / "scripts" / "deploy.sh"))


def _run(
    tmp_path: Path,
    *,
    stack_status: str | None,
    owned_vpcs: str | None = "0",
    vpc_count: str | None,
    quota: str | None,
    skip: bool = False,
) -> tuple[int, str, str]:
    """stack_status: None = absent (exact 'does not exist' ValidationError on stderr, rc 254);
    "denied" = some OTHER DescribeStacks failure (must fail closed); else a StackStatus.
    owned_vpcs: the paginated --output text of the physical-id query, e.g. "\\n\\nvpc-0abc" (two
    empty pages then one match) -- None = ListStackResources fails. vpc_count/quota None = fails."""

    def emit(v):
        # %b interprets the \\n escapes in the pagination-shaped stub values into real
        # newlines, which is what the CLI prints across pages.
        return f'printf "%b\\n" "{v}"' if v is not None else "return 255"

    calls = tmp_path / "calls.txt"
    driver = tmp_path / "driver.sh"
    driver.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            log_info()    {{ echo "INFO: $*"; }}
            log_success() {{ echo "OK: $*"; }}
            log_warning() {{ echo "WARN: $*"; }}
            log_error()   {{ echo "ERR: $*" >&2; }}
            STACK_NAME=teststack; AWS_REGION=us-east-1; PROJECT_ROOT="{tmp_path}/proj"; mkdir -p "$PROJECT_ROOT"
            {"export SKIP_VPC_PREFLIGHT=true" if skip else ""}
            aws() {{
              echo "aws $*" >> "{calls}"
              case "$1 $2" in
                "cloudformation describe-stacks") {
                'echo "aws: [ERROR]: An error occurred (ValidationError) when calling the DescribeStacks operation: Stack with id teststack does not exist" >&2; return 254'
                if stack_status is None
                else (
                    'echo "aws: [ERROR]: An error occurred (AccessDeniedException) when calling the DescribeStacks operation: not authorized" >&2; return 254'
                    if stack_status == "denied"
                    else (
                        'echo "aws: [ERROR]: connection reset while calling DescribeStacks; Stack with id teststack does not exist (cached)" >&2; return 254'
                        if stack_status == "phrase-only"
                        else emit(stack_status)
                    )
                )
            } ;;
                "cloudformation list-stack-resources") {emit(owned_vpcs) if owned_vpcs is not None else "return 255"} ;;
                "ec2 describe-vpcs") {emit(vpc_count)} ;;
                "service-quotas get-service-quota") {emit(quota)} ;;
              esac
              return 0
            }}
            source <(awk '/^preflight_vpc_capacity\\(\\)/,/^}}/' "{DEPLOY_SH}")
            preflight_vpc_capacity
            """
        ).lstrip()
    )
    proc = subprocess.run(["bash", str(driver)], capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr, calls.read_text() if calls.exists() else ""


def test_a_describe_stacks_failure_other_than_does_not_exist_fails_closed(tmp_path):
    """AccessDenied/throttling must never be read as 'stack absent' -- that would route a real
    stack into the fresh-stack path (and, worse, a real absence check would be a guess)."""
    rc, out, calls = _run(tmp_path, stack_status="denied", vpc_count="4", quota="5.0")
    assert rc != 0 and "DescribeStacks" in out and "unknown stack state" in out, out
    assert "describe-vpcs" not in calls and "list-stack-resources" not in calls


def test_the_absent_mapping_needs_the_validation_error_class_not_just_the_phrase(tmp_path):
    # A transport error whose text happens to contain the phrase is still unknown state.
    rc, out, calls = _run(tmp_path, stack_status="phrase-only", vpc_count="4", quota="5.0")
    assert rc != 0 and "unknown stack state" in out, out
    assert "describe-vpcs" not in calls


def test_pagination_shaped_ownership_output_is_aggregated_not_rejected(tmp_path):
    """Measured on acfe2e-p0920 (296 resources): the CLI applies --query per page, so a
    `length(...)` query printed 0, 0, 1 on three lines and a one-integer regex REJECTED a
    healthy stack. Counting vpc-* lines across pages must accept it."""
    rc, out, _ = _run(
        tmp_path, stack_status="UPDATE_COMPLETE", owned_vpcs="\\n\\nvpc-0aaa\\n", vpc_count="5", quota="5.0"
    )
    assert rc == 0 and "already owns its VPC (1)" in out, out


def test_the_extracted_function_is_the_real_one_and_runs_before_any_build():
    body = subprocess.run(
        ["awk", r"/^preflight_vpc_capacity\(\)/,/^}/", str(DEPLOY_SH)], capture_output=True, text=True
    ).stdout
    assert "L-F678F1CE" in body and "describe-vpcs" in body and "list-stack-resources" in body, body
    assert "AWS::EC2::VPC" in body, "ownership must be proven by a real VPC resource, not by the stack existing"
    assert "does not exist" in body and "grep -c '^vpc-'" in body, "exact-ABSENT mapping and paginated counting"
    assert '|| echo "ABSENT"' not in body, "every DescribeStacks failure must not collapse to ABSENT"
    main = DEPLOY_SH.read_text()
    # main() body is indented four spaces; the dependency step is install_or_verify_dependencies (installers are
    # skipped in certified mode, the gate still runs there): the VPC preflight precedes it, right after credentials
    assert main.index("    check_aws_credentials\n    preflight_vpc_capacity\n") < main.index(
        "    install_or_verify_dependencies"
    )


def test_a_fresh_stack_with_no_free_slot_fails_before_any_build(tmp_path):
    rc, out, _ = _run(tmp_path, stack_status=None, vpc_count="5", quota="5.0")
    assert rc != 0
    assert "5/5" in out and "L-F678F1CE" in out and "request-service-quota-increase" in out, out


def test_a_fresh_stack_with_a_free_slot_proceeds(tmp_path):
    rc, out, _ = _run(tmp_path, stack_status=None, vpc_count="4", quota="5.0")
    assert rc == 0 and "4/5" in out, out


def test_a_higher_applied_quota_is_honoured(tmp_path):
    rc, out, _ = _run(tmp_path, stack_status=None, vpc_count="5", quota="10.0")
    assert rc == 0 and "5/10" in out, out


def test_an_existing_stack_that_owns_a_vpc_is_not_blocked_at_quota(tmp_path):
    rc, out, calls = _run(
        tmp_path, stack_status="UPDATE_COMPLETE", owned_vpcs="\\n\\nvpc-0abc12345", vpc_count="5", quota="5.0"
    )
    assert rc == 0, out
    assert "already owns its VPC" in out
    assert "describe-vpcs" not in calls, "an owner should not even count VPCs"


def test_an_existing_stack_without_a_vpc_is_checked_and_fails_at_quota(tmp_path):
    # A legacy stack, or one whose VPC never got created, still needs a slot.
    rc, out, _ = _run(tmp_path, stack_status="UPDATE_COMPLETE", owned_vpcs="\\n\\n", vpc_count="5", quota="5.0")
    assert rc != 0 and "NO VPC of its own" in out and "5/5" in out, out


def test_a_rollback_complete_stack_without_a_vpc_fails_at_quota(tmp_path):
    # Exactly the 2026-09-25 shape: the stack exists (ROLLBACK_COMPLETE) but owns no VPC.
    rc, out, _ = _run(tmp_path, stack_status="ROLLBACK_COMPLETE", owned_vpcs="", vpc_count="5", quota="5.0")
    assert rc != 0 and "ROLLBACK_COMPLETE" in out and "5/5" in out, out


def test_an_unreadable_quota_fails_closed_with_the_permissions_named(tmp_path):
    rc, out, _ = _run(tmp_path, stack_status=None, vpc_count="5", quota=None)
    assert rc != 0
    assert "servicequotas:GetServiceQuota" in out and "SKIP_VPC_PREFLIGHT" in out, out


def test_an_unreadable_vpc_count_fails_closed(tmp_path):
    rc, out, _ = _run(tmp_path, stack_status=None, vpc_count=None, quota="5.0")
    assert rc != 0 and "ec2:DescribeVpcs" in out, out


def test_unlistable_stack_resources_fail_closed(tmp_path):
    rc, out, _ = _run(tmp_path, stack_status="UPDATE_COMPLETE", owned_vpcs=None, vpc_count="4", quota="5.0")
    assert rc != 0 and "ListStackResources" in out, out


def test_the_explicit_bypass_is_loud_and_reads_nothing(tmp_path):
    rc, out, calls = _run(tmp_path, stack_status=None, vpc_count=None, quota=None, skip=True)
    assert rc == 0 and "BYPASSED" in out, out
    assert calls == "", "the bypass must not silently call AWS"

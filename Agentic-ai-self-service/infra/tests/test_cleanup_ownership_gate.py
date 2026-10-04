"""The standalone cleanup script must fail closed on deletion authority.

Dynamic deployment cleanup is delegated to the application Lambda's manifest
teardown.  The shell retains only a narrow orphan sweep for taggable
account-global resources, and every one requires a fresh exact AgentCoreStack
tag read.  Missing tags are foreign; unreadable tags abort before CDK destroy.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_CLEANUP_SH = _REPO / "scripts" / "cleanup.sh"
_OWNERSHIP_PY = _REPO / "backend" / "src" / "app" / "services" / "resource_ownership.py"


def _cleanup_sh() -> str:
    return _CLEANUP_SH.read_text()


def _function(name: str) -> str:
    src = _cleanup_sh()
    start = src.index(f"{name}() {{")
    match = re.search(r"^\}", src[start:], re.MULTILINE)
    assert match, f"{name} closing brace not found"
    return src[start : start + match.end()]


def _sweep_body() -> str:
    return _function("sweep_orphan_resources")


def test_the_owner_tag_key_matches_the_python_side() -> None:
    py_key = re.search(r'OWNER_TAG_KEY = "([^"]+)"', _OWNERSHIP_PY.read_text())
    sh_key = re.search(r'^OWNER_TAG_KEY="([^"]+)"', _cleanup_sh(), re.MULTILINE)
    assert py_key and sh_key
    assert py_key.group(1) == sh_key.group(1) == "AgentCoreStack"


def test_the_identity_string_matches_the_python_side() -> None:
    """``{project}-{env}-{region}`` remains a cross-language contract."""
    src = _cleanup_sh()
    expr = re.search(r'^STACK_OWNER_ID="([^"]+)"', src, re.MULTILINE)
    assert expr
    defaults = dict(
        re.findall(
            r'^(ENVIRONMENT_NAME|AWS_REGION|PROJECT_NAME)="\$\{\1:-([^}]*)\}"',
            src,
            re.MULTILINE,
        )
    )
    assert set(defaults) == {"ENVIRONMENT_NAME", "AWS_REGION", "PROJECT_NAME"}

    out = subprocess.run(
        [
            "bash",
            "-c",
            f'ENVIRONMENT_NAME=$1 AWS_REGION=$2 PROJECT_NAME=$3; printf "%s" "{expr.group(1)}"',
            "_",
            defaults["ENVIRONMENT_NAME"],
            defaults["AWS_REGION"],
            defaults["PROJECT_NAME"],
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out == "agentcore-workflow-dev-us-east-1"


def test_ownership_check_accepts_only_the_exact_stack_tag() -> None:
    fn = _function("is_owned_by_this_stack")
    for tag_value in ("", "None", "some-other-stack-us-east-1", "ManagedBy"):
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'STACK_OWNER_ID="ours"\n{fn}\nis_owned_by_this_stack "$1"',
                "_",
                tag_value,
            ],
            capture_output=True,
        )
        assert result.returncode != 0, f"{tag_value!r} became deletion authority"

    result = subprocess.run(
        [
            "bash",
            "-c",
            f'STACK_OWNER_ID="ours"\n{fn}\nis_owned_by_this_stack "$1"',
            "_",
            "ours",
        ],
        capture_output=True,
    )
    assert result.returncode == 0


def test_no_environment_escape_hatch_can_authorize_untagged_resources() -> None:
    fn = _function("is_owned_by_this_stack")
    script = f'STACK_OWNER_ID="ours"\n{fn}\nis_owned_by_this_stack "None"'
    for env_name in ("CLEANUP_INCLUDE_UNTAGGED", "CLEANUP_INCLUDE_FOREIGN_RUNTIMES"):
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            env={"PATH": "/usr/bin:/bin", env_name: "1"},
        )
        assert result.returncode != 0

    options = _function("validate_cleanup_options")
    assert "were removed" in options
    assert "return 1" in options


@pytest.mark.parametrize(
    ("label", "marker"),
    [
        ("runtime IAM role", 'delete_owned_iam_role "${role_name}" "runtime IAM role"'),
        ("memory IAM role", 'delete_owned_iam_role "${role_name}" "memory IAM role"'),
        ("Cognito user pool", 'skip_foreign "Cognito user pool'),
        ("connector secret", 'sweep_owned_secrets "connector secret"'),
        ("per-agent OTEL secret", 'sweep_owned_secrets "per-agent OTEL secret"'),
    ],
)
def test_every_orphan_namespace_routes_through_an_ownership_gate(
    label: str,
    marker: str,
) -> None:
    assert marker in _sweep_body(), f"{label} no longer routes through its ownership gate"


def test_iam_and_secret_helpers_re_read_live_tags_before_deleting() -> None:
    role = _function("delete_owned_iam_role")
    secret = _function("sweep_owned_secrets")
    assert "read_iam_role_owner_tag" in role
    assert 'is_owned_by_this_stack "${OWNER_READ_VALUE}"' in role
    assert "read_secret_owner_tag" in secret
    assert 'is_owned_by_this_stack "${OWNER_READ_VALUE}"' in secret


def test_tag_read_failure_is_not_conflated_with_missing_tag() -> None:
    src = _cleanup_sh()
    assert 'OWNER_READ_STATE="unreadable"' in src
    assert "SKIPPED_UNREADABLE" in _sweep_body()
    assert "Stopping before CDK destroy" in _sweep_body()
    assert '|| echo "None"' not in src


def test_orphan_secrets_are_recoverable_and_never_force_deleted() -> None:
    helper = _function("sweep_owned_secrets")
    assert "--recovery-window-in-days 7" in helper
    assert "--force-delete-without-recovery" not in _cleanup_sh()


def test_the_platform_otel_secret_is_still_excluded_by_name() -> None:
    assert "!starts_with(Name, 'agentcore-otel/platform/')" in _sweep_body()


def test_the_cdk_shared_runtime_role_is_never_swept() -> None:
    assert "== *-shared" in _sweep_body()


def test_broad_raw_agentcore_sweeps_no_longer_exist() -> None:
    src = _cleanup_sh()
    for command in (
        "delete-agent-runtime",
        "delete-gateway",
        "delete-memory",
        "delete-policy-engine",
        "delete-oauth2-credential-provider",
        "delete-api-key-credential-provider",
        "delete-guardrail",
        "delete-knowledge-base",
    ):
        assert command not in src


def test_noninteractive_cleanup_requires_the_exact_regional_identity() -> None:
    confirm = _function("confirm_destroy")
    assert "CLEANUP_CONFIRM_STACK_OWNER" in confirm
    assert '"${STACK_OWNER_ID}"' in confirm
    assert "return 1" in confirm


def test_a_missing_stack_still_requires_confirmation_before_the_sweep() -> None:
    main = _function("main")
    assert main.index("check_stack_exists") < main.index("confirm_destroy")
    assert main.index("confirm_destroy") < main.index("sweep_orphan_resources")
    assert "exit 0" not in _function("check_stack_exists")


def test_cleanup_sh_can_be_sourced_without_running_a_teardown() -> None:
    assert 'if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then' in _cleanup_sh()


def test_retained_gateway_auth_cleanup_uses_exact_stack_outputs_and_runs_last() -> None:
    check = _function("check_stack_exists")
    main = _function("main")
    assert 'capture_retained_gateway_auth_outputs "${describe_result}"' in check
    assert "list-user-pools" not in _function("delete_retained_gateway_auth_resources")
    assert main.index("prepare_retained_gateway_auth_target") < main.index("cleanup_deployment_resources")
    assert main.index("verify_resources_removed") < main.index("delete_retained_gateway_auth_resources")
    assert main.index("delete_retained_gateway_auth_resources") < main.index("print_summary")


def test_exact_stack_outputs_capture_both_retained_resource_ids() -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            r"""
source "$1"
capture_retained_gateway_auth_outputs "$2"
printf 'stack=%s\npool=%s\ndomain=%s\n' \
  "${STACK_ID}" "${RETAINED_GATEWAY_AUTH_POOL_ID}" \
  "${RETAINED_GATEWAY_AUTH_DOMAIN_PREFIX}"
""",
            "_",
            str(_CLEANUP_SH),
            (
                '{"Stacks":[{"StackId":"arn:aws:cloudformation:us-east-1:123456789012:'
                'stack/acfe2e-test/uuid","Outputs":['
                '{"OutputKey":"GatewayAuthUserPoolId","OutputValue":"us-east-1_AbCd1234"},'
                '{"OutputKey":"GatewayAuthDomainPrefix","OutputValue":"acfe2e-test-gw-abc123-123456789012"}'
                "]}]}"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "stack=arn:aws:cloudformation:us-east-1:123456789012:stack/acfe2e-test/uuid" in result.stdout
    assert "pool=us-east-1_AbCd1234" in result.stdout
    assert "domain=acfe2e-test-gw-abc123-123456789012" in result.stdout


def test_a_partial_retained_gateway_auth_output_fails_closed() -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            r"""
source "$1"
capture_retained_gateway_auth_outputs "$2"
""",
            "_",
            str(_CLEANUP_SH),
            (
                '{"Stacks":[{"StackId":"arn:aws:cloudformation:us-east-1:123456789012:'
                'stack/acfe2e-test/uuid","Outputs":['
                '{"OutputKey":"GatewayAuthUserPoolId","OutputValue":"us-east-1_AbCd1234"}'
                "]}]}"
            ),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "partial retained-resource identity is unsafe" in (result.stdout + result.stderr)


def test_absent_stack_recovery_selects_only_the_exact_tagged_pool(
    tmp_path: pathlib.Path,
) -> None:
    calls = tmp_path / "aws-calls"
    result = subprocess.run(
        [
            "bash",
            "-c",
            r"""
source "$1"
CALL_LOG="$2"
PROJECT_NAME="acfe2e"
ENVIRONMENT_NAME="test"
STACK_NAME="acfe2e-test"
aws() {
  printf '%s\n' "$*" >> "${CALL_LOG}"
  if [[ "$1 $2" == "cognito-idp list-user-pools" ]]; then
    printf '%s\n' '{"UserPools":[
      {"Id":"us-east-1_Ours","Name":"acfe2e-test-gateway-auth"},
      {"Id":"us-east-1_Foreign","Name":"acfe2e-test-gateway-auth"},
      {"Id":"us-east-1_Other","Name":"different-gateway-auth"}
    ]}'
    return 0
  fi
  if [[ "$1 $2 $3" == "cognito-idp describe-user-pool --user-pool-id" ]]; then
    if [[ "$4" == "us-east-1_Ours" ]]; then
      printf '%s\n' '{"UserPool":{
        "Name":"acfe2e-test-gateway-auth",
        "Domain":"acfe2e-test-gw-ours-123456789012",
        "UserPoolTags":{
          "Project":"acfe2e",
          "Environment":"test",
          "aws:cloudformation:stack-name":"acfe2e-test"
        }
      }}'
    else
      printf '%s\n' '{"UserPool":{
        "Name":"acfe2e-test-gateway-auth",
        "Domain":"acfe2e-test-gw-foreign-123456789012",
        "UserPoolTags":{
          "Project":"someone-else",
          "Environment":"test",
          "aws:cloudformation:stack-name":"acfe2e-test"
        }
      }}'
    fi
    return 0
  fi
  return 99
}
discover_retained_gateway_auth_target
printf 'pool=%s\ndomain=%s\n' \
  "${RETAINED_GATEWAY_AUTH_POOL_ID}" "${RETAINED_GATEWAY_AUTH_DOMAIN_PREFIX}"
""",
            "_",
            str(_CLEANUP_SH),
            str(calls),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "pool=us-east-1_Ours" in result.stdout
    assert "domain=acfe2e-test-gw-ours-123456789012" in result.stdout
    assert "us-east-1_Other" not in calls.read_text()


def test_absent_stack_recovery_refuses_ambiguous_exact_tagged_pools(
    tmp_path: pathlib.Path,
) -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            r"""
source "$1"
PROJECT_NAME="acfe2e"
ENVIRONMENT_NAME="test"
STACK_NAME="acfe2e-test"
aws() {
  if [[ "$1 $2" == "cognito-idp list-user-pools" ]]; then
    printf '%s\n' '{"UserPools":[
      {"Id":"us-east-1_First","Name":"acfe2e-test-gateway-auth"},
      {"Id":"us-east-1_Second","Name":"acfe2e-test-gateway-auth"}
    ]}'
    return 0
  fi
  local pool_id="$4"
  printf '%s\n' "{\"UserPool\":{
    \"Name\":\"acfe2e-test-gateway-auth\",
    \"Domain\":\"acfe2e-test-gw-${pool_id##*_}-123456789012\",
    \"UserPoolTags\":{
      \"Project\":\"acfe2e\",
      \"Environment\":\"test\",
      \"aws:cloudformation:stack-name\":\"acfe2e-test\"
    }
  }}"
}
discover_retained_gateway_auth_target
""",
            "_",
            str(_CLEANUP_SH),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Refusing an ambiguous delete" in (result.stdout + result.stderr)


def test_retained_gateway_auth_target_must_match_the_exact_stack_identity(
    tmp_path: pathlib.Path,
) -> None:
    calls = tmp_path / "aws-calls"
    result = subprocess.run(
        [
            "bash",
            "-c",
            r"""
source "$1"
CALL_LOG="$2"
PROJECT_NAME="acfe2e"
ENVIRONMENT_NAME="test"
STACK_NAME="acfe2e-test"
STACK_ID="arn:aws:cloudformation:us-east-1:123456789012:stack/acfe2e-test/right"
RETAINED_GATEWAY_AUTH_POOL_ID="us-east-1_AbCd1234"
RETAINED_GATEWAY_AUTH_DOMAIN_PREFIX="acfe2e-test-gw-abc123-123456789012"
aws() {
  printf '%s\n' "$*" >> "${CALL_LOG}"
  printf '%s\n' '{"UserPool":{
    "Name":"acfe2e-test-gateway-auth",
    "Domain":"acfe2e-test-gw-abc123-123456789012",
    "UserPoolTags":{
      "Project":"acfe2e",
      "Environment":"test",
      "aws:cloudformation:stack-name":"acfe2e-test",
      "aws:cloudformation:stack-id":"arn:aws:cloudformation:us-east-1:123456789012:stack/acfe2e-test/wrong"
    }
  }}'
}
validate_retained_gateway_auth_target
""",
            "_",
            str(_CLEANUP_SH),
            str(calls),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "not attributable to the exact stack" in (result.stdout + result.stderr)
    assert calls.read_text().splitlines() == [
        ("cognito-idp describe-user-pool --user-pool-id us-east-1_AbCd1234 --region us-east-1 --output json")
    ]


def test_retained_gateway_auth_delete_uses_only_the_verified_exact_ids(
    tmp_path: pathlib.Path,
) -> None:
    calls = tmp_path / "aws-calls"
    result = subprocess.run(
        [
            "bash",
            "-c",
            r"""
source "$1"
CALL_LOG="$2"
RETAINED_GATEWAY_AUTH_POOL_ID="us-east-1_AbCd1234"
RETAINED_GATEWAY_AUTH_DOMAIN_PREFIX="acfe2e-test-gw-abc123-123456789012"
RETAINED_GATEWAY_AUTH_VALIDATED=true
aws() {
  printf '%s\n' "$*" >> "${CALL_LOG}"
  case "$1 $2" in
    "cognito-idp describe-user-pool")
      local count
      count=$(grep -c '^cognito-idp describe-user-pool ' "${CALL_LOG}")
      if [[ "${count}" -eq 1 ]]; then
        printf '%s\n' '{"UserPool":{
          "Name":"acfe2e-test-gateway-auth",
          "Domain":"acfe2e-test-gw-abc123-123456789012"
        }}'
        return 0
      fi
      printf 'ResourceNotFoundException\n' >&2
      return 255
      ;;
    "cognito-idp delete-user-pool-domain"|"cognito-idp delete-user-pool")
      return 0
      ;;
    "cognito-idp describe-user-pool-domain")
      printf '%s\n' '{"DomainDescription":{}}'
      return 0
      ;;
  esac
  printf 'unexpected aws call: %s\n' "$*" >&2
  return 99
}
delete_retained_gateway_auth_resources
""",
            "_",
            str(_CLEANUP_SH),
            str(calls),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    logged = calls.read_text()
    assert "list-user-pools" not in logged
    assert (
        "cognito-idp delete-user-pool-domain --user-pool-id us-east-1_AbCd1234 "
        "--domain acfe2e-test-gw-abc123-123456789012 --region us-east-1"
    ) in logged
    assert ("cognito-idp delete-user-pool --user-pool-id us-east-1_AbCd1234 --region us-east-1") in logged
    assert "Retained gateway-auth pool and hosted domain are absent." in result.stdout


def test_retained_gateway_auth_delete_refuses_a_changed_domain(
    tmp_path: pathlib.Path,
) -> None:
    calls = tmp_path / "aws-calls"
    result = subprocess.run(
        [
            "bash",
            "-c",
            r"""
source "$1"
CALL_LOG="$2"
RETAINED_GATEWAY_AUTH_POOL_ID="us-east-1_AbCd1234"
RETAINED_GATEWAY_AUTH_DOMAIN_PREFIX="captured-domain"
RETAINED_GATEWAY_AUTH_VALIDATED=true
aws() {
  printf '%s\n' "$*" >> "${CALL_LOG}"
  printf '%s\n' '{"UserPool":{"Domain":"different-domain"}}'
}
delete_retained_gateway_auth_resources
""",
            "_",
            str(_CLEANUP_SH),
            str(calls),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "domain changed after validation" in (result.stdout + result.stderr)
    assert "delete-user-pool" not in calls.read_text()


def test_unreadable_iam_owner_tag_never_reaches_a_delete_command(
    tmp_path: pathlib.Path,
) -> None:
    calls = tmp_path / "aws-calls"
    result = subprocess.run(
        [
            "bash",
            "-c",
            r"""
source "$1"
CALL_LOG="$2"
aws() {
  printf '%s\n' "$*" >> "${CALL_LOG}"
  if [[ "$1 $2" == "iam list-role-tags" ]]; then
    return 41
  fi
  return 0
}
delete_owned_iam_role "AgentCoreRuntime-customer" "runtime IAM role"
printf 'foreign=%s unreadable=%s failures=%s state=%s\n' \
  "${SKIPPED_FOREIGN}" "${SKIPPED_UNREADABLE}" "${DELETE_FAILURES}" \
  "${OWNER_READ_STATE}"
""",
            "_",
            str(_CLEANUP_SH),
            str(calls),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "foreign=0 unreadable=1 failures=0 state=unreadable" in result.stdout
    assert calls.read_text().splitlines() == [
        (
            "iam list-role-tags --role-name AgentCoreRuntime-customer "
            "--query Tags[?Key=='AgentCoreStack'].Value | [0] --output text"
        )
    ]


def test_missing_iam_owner_tag_is_foreign_but_not_an_unreadable_error(
    tmp_path: pathlib.Path,
) -> None:
    calls = tmp_path / "aws-calls"
    result = subprocess.run(
        [
            "bash",
            "-c",
            r"""
source "$1"
CALL_LOG="$2"
aws() {
  printf '%s\n' "$*" >> "${CALL_LOG}"
  if [[ "$1 $2" == "iam list-role-tags" ]]; then
    printf 'None\n'
    return 0
  fi
  return 0
}
delete_owned_iam_role "AgentCoreRuntime-legacy" "runtime IAM role"
printf 'foreign=%s unreadable=%s failures=%s state=%s\n' \
  "${SKIPPED_FOREIGN}" "${SKIPPED_UNREADABLE}" "${DELETE_FAILURES}" \
  "${OWNER_READ_STATE}"
""",
            "_",
            str(_CLEANUP_SH),
            str(calls),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "foreign=1 unreadable=0 failures=0 state=missing" in result.stdout
    assert calls.read_text().splitlines() == [
        (
            "iam list-role-tags --role-name AgentCoreRuntime-legacy "
            "--query Tags[?Key=='AgentCoreStack'].Value | [0] --output text"
        )
    ]


def test_a_retained_lambda_result_stops_main_before_sweep_or_cdk_destroy(
    tmp_path: pathlib.Path,
) -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            r"""
source "$1"
TMPDIR="$2"
check_prerequisites() { :; }
check_aws_credentials() { :; }
check_stack_exists() { STACK_EXISTS=true; }
confirm_destroy() { :; }
install_cdk_dependencies_for_cleanup() { :; }
sweep_orphan_resources() { printf 'SWEEP_RAN\n'; }
run_cdk_destroy() { printf 'CDK_RAN\n'; }
verify_resources_removed() { printf 'VERIFY_RAN\n'; }
print_summary() { printf 'SUMMARY_RAN\n'; }
aws() {
  if [[ "$1 $2" == "dynamodb describe-table" ]]; then
    printf '{}\n'
    return 0
  fi
  if [[ "$1 $2" == "dynamodb scan" ]]; then
    printf '%s\n' \
      '{"Items":[{"deployment_id":{"S":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"},"delete_status":{"S":""}}]}'
    return 0
  fi
  if [[ "$1 $2" == "lambda invoke" ]]; then
    local arg response_file=""
    for arg in "$@"; do
      if [[ "${arg}" == "${TMPDIR}"/agentcore-cleanup-response.* ]]; then
        response_file="${arg}"
      fi
    done
    [[ -n "${response_file}" ]]
    printf '%s\n' \
      '{"success":false,"retained":true,"message":"ownership not proven"}' \
      > "${response_file}"
    printf '{"StatusCode":200}\n'
    return 0
  fi
  printf 'unexpected aws call: %s\n' "$*" >&2
  return 99
}
main
""",
            "_",
            str(_CLEANUP_SH),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "ownership not proven" in combined
    assert "Stopping before CDK destroy" in combined
    for forbidden in (
        "SWEEP_RAN",
        "CDK_RAN",
        "VERIFY_RAN",
        "SUMMARY_RAN",
    ):
        assert forbidden not in combined

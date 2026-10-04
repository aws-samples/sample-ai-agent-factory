"""The deploy.sh guard that stops a redeploy from silently deleting Cognito users.

Why this exists: each COGNITO_USERS email becomes a custom resource whose Delete
handler calls AdminDeleteUser. Bash cannot tell an omitted variable from an
intentionally emptied one, so a plain ``./scripts/deploy.sh`` used to remove every
provisioner and delete every user it had created — along with their password and
group memberships. That happened for real on 2026-09-03 against
``agentcore-workflow-dev``.

The tests below run the *actual* extraction snippet embedded in deploy.sh rather
than a transcription of it, because the near-miss in that snippet was a filter on
``Type`` that looked right and matched nothing: CDK emits these resources as
``AWS::CloudFormation::CustomResource``, not ``Custom::*``. A guard that silently
extracts zero emails is indistinguishable from having no guard at all.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import pytest

_DEPLOY_SH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "deploy.sh"


def _deploy_sh() -> str:
    return _DEPLOY_SH.read_text()


def _embedded_python() -> str:
    """Pull the email-extraction snippet out of deploy.sh so we test what ships."""
    src = _deploy_sh()
    fn = src[src.index("preserve_existing_cognito_users() {") :]
    start = fn.index("-c '") + len("-c '")
    end = fn.index("\n'", start)
    snippet = fn[start:end]
    assert "UserPoolId" in snippet, "extraction snippet not found — did deploy.sh change shape?"
    return snippet


def _run(template) -> str:
    """Feed a template to the real snippet exactly as `aws ... --output json` would."""
    proc = subprocess.run(
        [sys.executable, "-B", "-c", _embedded_python()],
        input=json.dumps(template),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


# The shape CDK actually emits, verified against the live stack's get-template.
def _provisioner(email: str) -> dict:
    return {
        "Type": "AWS::CloudFormation::CustomResource",
        "Properties": {
            "ServiceToken": {"Fn::GetAtt": ["CognitoUserProvisionerProviderframeworkonEvent", "Arn"]},
            "UserPoolId": {"Ref": "UserPool6BA7E5F2"},
            "Email": email,
        },
    }


class TestEmailExtraction:
    def test_it_finds_a_cdk_provisioner(self):
        """The regression that matters: this resource's Type is
        AWS::CloudFormation::CustomResource. A filter keyed on ``Custom::``
        returns nothing here and the guard quietly stops guarding."""
        out = _run({"Resources": {"Useraliceatexamplecom": _provisioner("alice@example.com")}})
        assert out == "alice@example.com"

    def test_multiple_users_are_comma_joined_and_sorted(self):
        out = _run(
            {
                "Resources": {
                    "UserB": _provisioner("bob@example.com"),
                    "UserA": _provisioner("alice@example.com"),
                }
            }
        )
        assert out == "alice@example.com,bob@example.com"

    def test_other_custom_resources_are_not_mistaken_for_users(self):
        """The template is full of Custom::* resources. Matching one of them would
        feed a bucket name into COGNITO_USERS and fail the deploy."""
        out = _run(
            {
                "Resources": {
                    "AutoDelete": {
                        "Type": "Custom::S3AutoDeleteObjects",
                        "Properties": {"BucketName": "b", "ServiceToken": "t"},
                    },
                    "LogRetention": {
                        "Type": "Custom::LogRetention",
                        "Properties": {"LogGroupName": "/aws/lambda/x", "RetentionInDays": 30},
                    },
                    "Pool": {"Type": "AWS::Cognito::UserPool", "Properties": {"UserPoolName": "p"}},
                }
            }
        )
        assert out == ""

    def test_a_stack_with_no_users_yields_nothing(self):
        assert _run({"Resources": {}}) == ""

    @pytest.mark.parametrize(
        "payload",
        [
            '"a YAML template comes back as a bare string"',
            "not json at all",
            "{}",
        ],
    )
    def test_unparseable_input_fails_closed(self, payload):
        """P0-A2 (2026-09-25): the parser used to exit 0 silently on anything it could not read, so an AccessDenied,
        a transient failure or an unparsed template resolved to "no existing users" and the synth could delete every
        provisioned user. Now: a real template with no provisioners prints nothing and exits 0 ("{}" is exactly that);
        anything unparseable exits non-zero so deploy.sh refuses before synthesis. A fresh deploy no longer reaches the
        parser at all: deploy.sh recognises the ABSENT stack from the aws error code first."""
        proc = subprocess.run(
            [sys.executable, "-B", "-c", _embedded_python()],
            input=payload,
            capture_output=True,
            text=True,
        )
        if payload == "{}":
            assert proc.returncode == 0 and proc.stdout.strip() == "", proc.stderr
        else:
            assert proc.returncode != 0, (payload, proc.stdout)
            assert proc.stdout.strip() == ""


class TestTheGuardIsWiredIn:
    def test_it_runs_before_the_cdk_deploy(self):
        """Order matters: it mutates COGNITO_USERS, which run_cdk_deploy passes as
        a -c context value."""
        src = _deploy_sh()
        main = src[src.index("\nmain() {") :]
        certified = main[main.index('if [[ "${CERTIFIED_DEPS}" == "1" ]]; then') : main.index("  else\n")]
        ordinary = main[main.index("  else\n") : main.index("  fi\n")]
        # certified mode: the read-only carry-forward runs BEFORE the pinned synth that bakes COGNITO_USERS into context
        assert certified.index("preserve_existing_cognito_users") < certified.index("cdk_synth_pinned"), certified
        # ordinary mode: before run_cdk_deploy (which synthesizes first)
        assert ordinary.index("preserve_existing_cognito_users") < ordinary.index("run_cdk_deploy"), ordinary

    def test_log_warning_is_defined(self):
        """The guard's only output is log_warning. Under `set -euo pipefail` an
        undefined function is exit 127 — the guard would abort every deploy it
        triggered on, turning a safety net into an outage."""
        assert re.search(r"^log_warning\(\) \{", _deploy_sh(), re.M)

    def test_an_explicit_list_is_left_alone_so_offboarding_still_works(self):
        src = _deploy_sh()
        fn = src[src.index("preserve_existing_cognito_users() {") :]
        assert '[[ -n "${COGNITO_USERS}" ]] && return' in fn

    def test_none_is_the_documented_escape_hatch(self):
        src = _deploy_sh()
        fn = src[src.index("preserve_existing_cognito_users() {") :]
        assert '"${COGNITO_USERS}" == "none"' in fn

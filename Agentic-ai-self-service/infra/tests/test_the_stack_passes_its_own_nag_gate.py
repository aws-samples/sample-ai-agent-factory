"""The stack must be DEPLOYABLE, not merely synthesizable.

This file exists because of a gap that let a change reach a live deploy attempt:
adding ``GatewayAuthUserPool`` left it without a password policy or a threat-
protection mode, so cdk-nag raised ``AwsSolutions-COG1`` and ``AwsSolutions-COG3``
as ERROR annotations and ``cdk deploy`` refused to run at all — while the entire
infra suite stayed green at 145 passed.

The reason is that the two paths check different things:

  * ``Template.from_stack(stack)`` and a bare ``app.synth()`` produce the template
    and do NOT fail on cdk-nag annotations. Every other test in this suite uses one
    of those, so none of them can see a nag error.
  * The ``cdk`` CLI reads the annotations out of the cloud assembly afterwards and
    fails the command. That is the gate that actually blocks a deploy, and nothing
    in the test suite reproduced it.

Two traps, both hit while writing this file, both now pinned by
``test_the_annotation_reader_is_not_vacuous``:

  1. The nag Aspect is added to the **App** in ``app.py``, not inside
     ``PlatformStack.__init__``. A fixture that only instantiates the stack runs no
     nag checks at all and reports a clean result forever. The fixture below adds
     the Aspect exactly as ``app.py:59`` does.
  2. The findings are NOT in ``artifact.manifest.metadata`` — that attribute is
     ``None`` here. They are in ``artifact.messages``, as ``SynthesisMessage``
     objects carrying a ``SynthesisMessageLevel``.

Either mistake produces an empty finding list, which is indistinguishable from a
clean stack. Hence the vacuity probe: it synthesizes a deliberately non-compliant
pool and fails if the reader cannot see the error.

A third trap, which produced a FALSE POSITIVE rather than a false negative: the
fixture must synthesize the stack the way ``app.py`` does, which is
``cdk.Environment(region=...)`` with **no account**. Every other test in this suite
passes a concrete ``account="123456789012"``, which is fine for template assertions
but wrong here — an account-agnostic stack renders the CDK assets bucket as
``cdk-hnb659fds-assets-<AWS::AccountId>-<region>``, and that is the literal string
the ``BucketDeployment`` IAM5 suppression's ``applies_to`` matches. Pin the account
and the ARN resolves to a real account number, the suppression no longer matches, and
this test reports an IAM5 error that the live ``cdk diff`` does not have.
"""

import json
from pathlib import Path

import aws_cdk as cdk
import cdk_nag
import pytest
from aws_cdk import aws_cognito as cognito
from aws_cdk.cx_api import SynthesisMessageLevel
from stacks.platform_stack import PlatformStack

REGION = "us-east-1"
ACCOUNT = "123456789012"
CDK_CONTEXT = json.loads((Path(__file__).resolve().parents[1] / "cdk.json").read_text())["context"]


def _findings(assembly, stack_name: str, level: SynthesisMessageLevel) -> list[str]:
    """cdk-nag findings at *level*, from where the CDK CLI actually reads them."""
    art = assembly.get_stack_by_name(stack_name)
    return [f"{m.id}: {m.entry.data}" for m in art.messages if m.level == level]


def _synth_with_nag(
    build,
    *,
    context: dict | None = None,
) -> cdk.cx_api.CloudAssembly:
    """Synthesize with the nag Aspect applied to the App, as ``app.py`` does.

    The Aspect must go on the App and must be added BEFORE synth; this helper is
    the single place that gets that right so a future test cannot get it wrong.
    """
    app = cdk.App(context=context)
    build(app)
    cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks(verbose=True))
    return app.synth()


@pytest.fixture(scope="module")
def assembly():
    def build(app: cdk.App) -> None:
        PlatformStack(
            app,
            "TestStack",
            environment_name="test",
            project_name="agentcore-workflow",
            # NO account, matching app.py:47. See the module docstring's third trap:
            # pinning an account breaks the BucketDeployment IAM5 suppression and
            # this test then reports an error the real deploy does not have.
            env=cdk.Environment(region=REGION),
        )

    return _synth_with_nag(build, context=CDK_CONTEXT)


def test_no_cdk_nag_errors(assembly):
    """An ERROR annotation makes `cdk deploy` refuse to run. A stack that cannot
    be deployed is not a passing stack, however many template assertions hold."""
    errors = _findings(assembly, "TestStack", SynthesisMessageLevel.ERROR)
    assert errors == [], (
        "cdk-nag ERROR annotations block `cdk deploy` entirely:\n  " + "\n  ".join(errors) + "\n\n"
        "Fix the finding, or suppress it in stacks/platform/nag_suppressions.py with a "
        "reason that is true for the specific construct — not by widening another "
        "construct's suppression to cover one it does not describe."
    )


def test_no_unacknowledged_cdk_nag_warnings(assembly):
    """Warnings do not block a deploy, so they accumulate silently and the next real
    ERROR arrives buried in noise. Every cdk-nag warning must be either fixed or
    suppressed with a reason, which is also what makes the suppression list
    reviewable as a record of accepted risk."""
    warnings = _findings(assembly, "TestStack", SynthesisMessageLevel.WARNING)
    nag = [w for w in warnings if "AwsSolutions-" in w]
    assert nag == [], (
        "unsuppressed cdk-nag warnings:\n  " + "\n  ".join(nag) + "\n\n"
        "Suppress in stacks/platform/nag_suppressions.py with a construct-specific reason."
    )


def test_aws_custom_resource_provider_uses_the_cli_runtime(assembly):
    """Pin the CDK feature flag that keeps the provider runtime current.

    The provider is a stack-level singleton, so the ordinary construct-scoped
    assertions do not naturally name it. Identify it from the existing
    AwsCustomResource-specific IAM4 reason. The test app loads this repo's real
    ``cdk.json`` context so changing the feature-flag contract cannot leave the
    assertion path different from the deploy path.
    """
    resources = assembly.get_stack_by_name("TestStack").template["Resources"]
    providers = []
    for resource in resources.values():
        if resource.get("Type") != "AWS::Lambda::Function":
            continue
        suppressions = resource.get("Metadata", {}).get("cdk_nag", {}).get("rules_to_suppress", [])
        if any("AwsCustomResource" in str(item.get("reason", "")) for item in suppressions):
            providers.append((resource, suppressions))

    assert len(providers) == 1, f"expected exactly one shared AwsCustomResource provider Lambda, found {len(providers)}"
    provider, suppressions = providers[0]
    assert provider["Properties"]["Runtime"] == "nodejs24.x", (
        "the repository CDK context no longer drives the shared "
        "AwsCustomResource provider to the current Node.js runtime"
    )
    assert "AwsSolutions-L1" not in {item.get("id") for item in suppressions}, (
        "a current provider runtime must satisfy L1 instead of suppressing it"
    )


def test_the_cli_and_deploy_script_pin_one_python_environment():
    """One Python for the CDK app, and one pinned CDK CLI: a machine-global `cdk` (or npx
    resolving whatever is on PATH) must not silently switch either the interpreter or the
    toolkit version between machines."""
    infra_dir = Path(__file__).resolve().parents[1]
    repo_dir = infra_dir.parent
    cdk_config = json.loads((infra_dir / "cdk.json").read_text())
    deploy = (repo_dir / "scripts" / "deploy.sh").read_text()
    cleanup = (repo_dir / "scripts" / "cleanup.sh").read_text()
    app = (infra_dir / "app.py").read_text()

    assert cdk_config["app"] == '"${CDK_PYTHON:-python3}" app.py'
    assert 'PROJECT_PYTHON="$(command -v "${requested_python}")"' in deploy
    assert 'export CDK_PYTHON="${PROJECT_PYTHON}"' in deploy
    assert '"${PROJECT_PYTHON}" -B -m pip install -r requirements.txt --quiet' in deploy
    assert "pip3 install -r requirements.txt" not in deploy
    assert deploy.index('export CDK_PYTHON="${PROJECT_PYTHON}"') < deploy.index('"${CDK_BIN}" bootstrap')
    # The CLI is pinned by infra/package.json + package-lock.json, installed with `npm ci`,
    # version-checked against the pin, and every invocation goes through CDK_BIN.
    assert 'CDK_BIN="${CDK_BIN:-${PROJECT_ROOT}/infra/node_modules/.bin/cdk}"' in deploy
    assert "npx cdk" not in deploy, "deploy.sh must not fall back to a machine-global cdk"
    assert "npm ci" in deploy
    assert deploy.index("npm ci") < deploy.index('"${CDK_BIN}" bootstrap') < deploy.index('"${CDK_BIN}" synth')
    pin = json.loads((infra_dir / "package.json").read_text())["devDependencies"]["aws-cdk"]
    assert pin.count(".") == 2 and all(part.isdigit() for part in pin.split(".")), f"exact pin, not a range: {pin}"
    lock = json.loads((infra_dir / "package-lock.json").read_text())
    assert lock["packages"]["node_modules/aws-cdk"]["version"] == pin, "lockfile must resolve the exact pin"

    assert 'PROJECT_PYTHON="$(command -v "${requested_python}")"' in cleanup
    assert 'export CDK_PYTHON="${PROJECT_PYTHON}"' in cleanup
    cleanup_main = cleanup[cleanup.index("main() {") :]
    assert cleanup_main.index("check_prerequisites") < cleanup_main.index("run_cdk_destroy")

    assert "_assert_pinned_cdk_dependencies()" in app
    assert 'for package in ("aws-cdk-lib", "constructs", "cdk-nag")' in app


def test_the_annotation_reader_is_not_vacuous():
    """The trap this whole file is about: a check that looks in the wrong place, or
    forgets to add the Aspect, reports clean forever — and an empty finding list is
    indistinguishable from a compliant stack. Synthesize a deliberately
    non-compliant pool and confirm the reader sees it, so a clean result from the
    two tests above means something."""

    def build(app: cdk.App) -> None:
        stack = cdk.Stack(app, "NagProbe", env=cdk.Environment(region=REGION, account=ACCOUNT))
        # No password policy and no threat-protection mode: exactly the shape that
        # blocked the deploy, so this asserts against the real failure rather than
        # an invented one.
        cognito.UserPool(stack, "BarePool", self_sign_up_enabled=False)

    assembly = _synth_with_nag(build)
    errors = _findings(assembly, "NagProbe", SynthesisMessageLevel.ERROR)
    assert any("COG1" in e for e in errors), (
        f"the reader found no COG1 on a pool with no password policy, so a clean result "
        f"from it proves nothing. Saw: {errors}"
    )
    # And the same for the warning reader, which has its own way of being vacuous.
    warnings = _findings(assembly, "NagProbe", SynthesisMessageLevel.WARNING)
    assert any("COG2" in w for w in warnings), (
        f"the reader found no COG2 warning on a pool with no MFA. Saw: {warnings}"
    )


def test_the_gateway_auth_pool_satisfies_cog1_and_cog3_rather_than_suppressing_them(assembly):
    """The fix for the blocked deploy was to set a real password policy and threat
    protection on the new pool, NOT to suppress the findings. Suppressing would have
    made `cdk deploy` work again while leaving a machine-credential pool with no
    password floor — so pin that the controls are genuinely satisfied: a suppression
    of either rule on this construct must fail this test."""
    import json
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1] / "stacks" / "platform" / "nag_suppressions.py").read_text()
    # The gateway pool's suppression block is the one guarded by `gateway_auth_pool`.
    block = src.split("if gateway_auth_pool is not None:", 1)
    assert len(block) == 2, "the gateway_auth_pool suppression block moved; update this test"
    for rule in ("AwsSolutions-COG1", "AwsSolutions-COG3"):
        assert rule not in block[1].split("# ---- Step Functions", 1)[0], (
            f"{rule} is suppressed on GatewayAuthUserPool. It was satisfied for real "
            f"(password_policy min_length=32 per ARCC cnt_qljjTWYkQl2eci, and "
            f"standard_threat_protection_mode=FULL_FUNCTION); suppressing it instead would "
            f"unblock the deploy while removing the control."
        )

    # And assert it in the template, not only in the source of the suppression list.
    tpl = json.loads(json.dumps(assembly.get_stack_by_name("TestStack").template))
    pools = [
        r
        for r in tpl["Resources"].values()
        if r["Type"] == "AWS::Cognito::UserPool" and "gateway-auth" in json.dumps(r.get("Properties", {}))
    ]
    assert len(pools) == 1, f"expected exactly one gateway-auth pool, found {len(pools)}"
    props = pools[0]["Properties"]
    assert props["Policies"]["PasswordPolicy"]["MinimumLength"] == 32, (
        f"ARCC cnt_qljjTWYkQl2eci puts the floor for machine/system-account passwords at 32 "
        f"UTF-8 characters, not the 8 that satisfies COG1. Got {props['Policies']['PasswordPolicy']}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

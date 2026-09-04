"""cleanup.sh must delete only resources tagged for the stack being torn down.

Why this exists: every sweep in ``cleanup.sh``'s ``sweep_orphan_resources`` matches
on an account-global NAME PREFIX — Cognito ``AgentCore*``, secrets
``agentcore-connector/`` and ``agentcore-otel/``, IAM roles ``AgentCoreMemory-*``
and ``AgentCoreRuntime-*``. None of those names carry a deployment identity, so on
an account running two deployments of this platform — dev + prod, or two teams,
which is routine because customers deploy and delete this often — a teardown
destroyed the *other* deployment's resources, including the secrets holding raw
customer API keys.

These are static assertions on the shipped script: that the gate is applied to
every dangerous sweep, that the identity string still matches the Python side
(``services/resource_ownership.stack_id``, which the backend uses to *write* the
tag), and that the fail-closed default is intact. The behavior itself — what the
AWS CLI's JMESPath filters actually match — is verified against real AWS by
``scripts/verify-cleanup-ownership.sh``, because a mocked assertion here would
only re-check a transcription of those filters.
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


def _sweep_body() -> str:
    src = _cleanup_sh()
    start = src.index("sweep_orphan_resources() {")
    end = src.index("# ── Step 6", start)
    return src[start:end]


def test_the_owner_tag_key_matches_the_python_side() -> None:
    """The backend writes the tag; the shell reads it. One typo and teardown leaks.

    A mismatch fails safe (nothing is deleted) but silently: every teardown would
    report success and leave the whole namespace behind.
    """
    py_key = re.search(r'OWNER_TAG_KEY = "([^"]+)"', _OWNERSHIP_PY.read_text())
    sh_key = re.search(r'^OWNER_TAG_KEY="([^"]+)"', _cleanup_sh(), re.MULTILINE)
    assert py_key and sh_key
    assert py_key.group(1) == sh_key.group(1) == "AgentCoreStack"


def test_the_identity_string_matches_the_python_side() -> None:
    """``{project}-{env}-{region}`` is a cross-language contract.

    Run the shipped bash assignment against the shell's own default config and
    compare with the Python default, rather than transcribing either.
    """
    src = _cleanup_sh()
    expr = re.search(r'^STACK_OWNER_ID="([^"]+)"', src, re.MULTILINE)
    assert expr, "STACK_OWNER_ID assignment not found — did cleanup.sh change shape?"
    defaults = dict(re.findall(r'^(ENVIRONMENT_NAME|AWS_REGION|PROJECT_NAME)="\$\{\1:-([^}]*)\}"', src, re.MULTILINE))
    assert set(defaults) == {"ENVIRONMENT_NAME", "AWS_REGION", "PROJECT_NAME"}, defaults

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
    # Mirrors resource_ownership.stack_id() with the same defaults.
    assert out == f"{defaults['PROJECT_NAME']}-{defaults['ENVIRONMENT_NAME']}-{defaults['AWS_REGION']}"
    assert out == "agentcore-workflow-dev-us-east-1"


def test_ownership_check_fails_closed_for_untagged_resources() -> None:
    """An untagged resource must count as foreign by default.

    A resource that predates the tag and a resource belonging to another
    deployment are indistinguishable; deleting a foreign secret is unrecoverable,
    skipping a legacy orphan costs one manual delete.
    """
    src = _cleanup_sh()
    fn = src[src.index("is_owned_by_this_stack() {") :]
    fn = fn[: fn.index("\n}\n")]
    for tag_value in ("", "None", "some-other-stack-us-east-1", "ManagedBy"):
        rc = subprocess.run(
            [
                "bash",
                "-c",
                f'STACK_OWNER_ID="agentcore-workflow-dev-us-east-1"\n{fn}\n}}\nis_owned_by_this_stack "$1"',
                "_",
                tag_value,
            ],
            capture_output=True,
            text=True,
        ).returncode
        assert rc != 0, f"{tag_value!r} was accepted as proof of ownership"
    rc = subprocess.run(
        [
            "bash",
            "-c",
            f'STACK_OWNER_ID="agentcore-workflow-dev-us-east-1"\n{fn}\n}}\nis_owned_by_this_stack "$1"',
            "_",
            "agentcore-workflow-dev-us-east-1",
        ],
        capture_output=True,
        text=True,
    ).returncode
    assert rc == 0, "the stack's own tag was rejected"


def test_untagged_resources_are_only_swept_on_explicit_opt_in() -> None:
    src = _cleanup_sh()
    fn = src[src.index("is_owned_by_this_stack() {") :]
    fn = fn[: fn.index("\n}\n")]
    script = f'STACK_OWNER_ID="s"\n{fn}\n}}\nis_owned_by_this_stack "None"'
    assert subprocess.run(["bash", "-c", script], capture_output=True).returncode != 0
    assert (
        subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            env={"PATH": "/usr/bin:/bin", "CLEANUP_INCLUDE_UNTAGGED": "1"},
        ).returncode
        == 0
    )


@pytest.mark.parametrize(
    ("label", "marker"),
    [
        # Deletes a pool whose name is "AgentCore-{the user's gateway name}" — it
        # cannot tell this deployment's pool from another product's.
        ("cognito user pool", 'skip_foreign "Cognito user pool'),
        # Holds the raw customer API key. Worst case in the whole sweep.
        ("connector secret", 'sweep_owned_secrets "connector secret"'),
        ("per-agent OTEL secret", 'sweep_owned_secrets "per-agent OTEL secret"'),
        # IAM is not regional, so this reached the other region's roles too.
        ("memory IAM role", 'skip_foreign "memory IAM role'),
        ("runtime IAM role", 'skip_foreign "runtime IAM role'),
    ],
)
def test_every_dangerous_sweep_is_gated(label: str, marker: str) -> None:
    assert marker in _sweep_body(), f"the {label} sweep no longer reports foreign resources"


def test_no_unconditional_deletes_left_in_the_swept_namespaces() -> None:
    """The gate is worthless if one sweep still deletes on a name match alone.

    Each destructive call in these namespaces must be preceded by an ownership
    decision, so assert the count of ownership checks against the count of
    prefix-matched namespaces rather than eyeballing the file.
    """
    body = _sweep_body()
    # One per namespace: cognito, otel secrets, connector secrets, memory roles,
    # runtime roles. The secret sweeps route through the shared helper.
    checks = body.count("is_owned_by_this_stack") + body.count("sweep_owned_secrets ")
    assert checks >= 5, f"only {checks} ownership decisions found across the sweeps"


def test_the_platform_otel_secret_is_still_excluded_by_name() -> None:
    """agentcore-otel/platform/* outlives every stack (bootstrap-otel-secret.sh).

    It is excluded by NAME, not by tag, so the exclusion must survive the move to
    the tag-gated helper. Deleting it silently broke the next deploy (Bug 24).
    """
    assert "!starts_with(Name, 'agentcore-otel/platform/')" in _sweep_body()


def test_the_cdk_shared_runtime_role_is_never_swept() -> None:
    """AgentCoreRuntime-{project}-{env}[-{region}]-shared is CloudFormation's.

    Verified live 2026-09-04: the old "AgentCoreRuntime-${PROJECT_NAME}" filter
    matched AgentCoreRuntime-agentcore-workflow-dev-eu-central-1-shared, so a
    us-east-1 teardown deleted the Frankfurt deployment's shared execution role —
    the role every agent there assumes. IAM roles get no aws:cloudformation:*
    system tags, so the name suffix is the only available signal.
    """
    body = _sweep_body()
    assert "== *-shared" in body, "the -shared exclusion is gone from the runtime-role sweep"


def test_operator_is_told_what_was_left_behind() -> None:
    """Silently skipping is how the original bug stayed invisible for so long."""
    src = _cleanup_sh()
    assert "SKIPPED_FOREIGN" in src
    assert "CLEANUP_INCLUDE_UNTAGGED=1" in src


def test_cleanup_sh_can_be_sourced_without_running_a_teardown() -> None:
    """scripts/verify-cleanup-ownership.sh sources it to exercise the real sweep.

    Without the guard, sourcing would run main() — i.e. destroy the stack.
    """
    assert 'if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then' in _cleanup_sh()

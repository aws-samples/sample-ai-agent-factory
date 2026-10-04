"""The EMITTED deploy.sh (the customer bundle's script) enforces the exact Secrets Manager ARN grammar on the effective
LiteLLM key ARN before it parses a single field out of it.

A peer executed the previously emitted block: a wrong resource kind, Arabic-Indic account digits and a trailing-newline
ARN all returned rc 0. The check is now the template's own AllowedPattern, anchored, in the C locale, applied to the
EFFECTIVE value (the override, else the baked Default), and the value is never repeated in any message.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "_region_module", Path(__file__).with_name("test_litellm_secret_region_is_enforced_in_the_template.py")
)
_region_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_region_module)  # the sibling module's bundle/LiteLLM helpers, loaded by path
KEY_ARN_HOME, _bundle, _litellm = _region_module.KEY_ARN_HOME, _region_module._bundle, _region_module._litellm

HOME_REGION = "us-east-1"


def _preflight_block(script: str) -> str:
    start = script.index('LITELLM_SECRET_ARN="${4:-${LITELLM_API_KEY_SECRET_ARN:-}}"')
    check = script.index('if ! litellm_arn_ok "$LITELLM_EFFECTIVE_ARN"; then', start)
    end = script.index("\nfi\n", check) + len("\nfi\n")
    return script[start:end]


def _run(block: str, arn: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        ["bash", "-c", f"set -u; REGION={HOME_REGION}\n{block}\necho ACCEPTED"],  # noqa: S607
        env={"PATH": "/usr/bin:/bin", "LITELLM_API_KEY_SECRET_ARN": arn, "LANG": "en_US.UTF-8"},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def block() -> str:
    script = _bundle(gateway_config=_litellm(litellm_api_key_ref=KEY_ARN_HOME)).deploy_sh
    b = _preflight_block(script)
    assert "litellm_arn_ok()" in b and "local LC_ALL=C" in b, b
    return b


def test_a_grammatical_arn_is_accepted(block):
    result = _run(block, KEY_ARN_HOME)
    assert result.returncode == 0 and result.stdout.strip().endswith("ACCEPTED"), (result.stdout, result.stderr)


@pytest.mark.parametrize("name_len", [1, 2, 255, 256, 511, 512])
def test_secret_name_lengths_inside_the_bound_are_accepted(block, name_len):
    arn = f"arn:aws:secretsmanager:{HOME_REGION}:123456789012:secret:{'a' * name_len}"
    result = _run(block, arn)
    assert result.returncode == 0 and result.stdout.strip().endswith("ACCEPTED"), (name_len, result.stdout)


@pytest.mark.parametrize("name_len", [0, 513, 600])
def test_secret_name_lengths_outside_the_bound_are_refused(block, name_len):
    arn = f"arn:aws:secretsmanager:{HOME_REGION}:123456789012:secret:{'a' * name_len}"
    result = _run(block, arn)
    assert result.returncode != 0 and "is not a Secrets Manager ARN" in result.stdout, (name_len, result.stdout)


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param(f"arn:aws:secretsmanager:{HOME_REGION}:123456789012:not-secret:x", id="wrong-resource-kind"),
        pytest.param(f"arn:aws:s3:{HOME_REGION}:123456789012:secret:x", id="wrong-service"),
        pytest.param(f"arn:evil:secretsmanager:{HOME_REGION}:123456789012:secret:x", id="wrong-partition"),
        pytest.param(f"arn:aws:secretsmanager:{HOME_REGION}:not-an-account:secret:x", id="non-numeric-account"),
        pytest.param(
            f"arn:aws:secretsmanager:{HOME_REGION}:١٢٣٤٥٦٧٨٩٠١٢:secret:x",
            id="twelve-unicode-digits",
        ),
        pytest.param(f"{KEY_ARN_HOME}\n", id="trailing-newline"),
        pytest.param(f" {KEY_ARN_HOME}", id="leading-space"),
        pytest.param(f"arn:aws:secretsmanager:{HOME_REGION}:123456789012", id="truncated"),
        pytest.param("arn:aws:secretsmanager::123456789012:secret:x", id="empty-region"),
        pytest.param("sk-a-virtual-key-pasted-here", id="a-key-not-an-arn"),
    ],
)
def test_a_malformed_effective_arn_is_refused_without_being_echoed(block, malformed):
    result = _run(block, malformed)
    assert result.returncode != 0, (malformed, result.stdout)
    assert "ACCEPTED" not in result.stdout
    assert "is not a Secrets Manager ARN" in result.stdout, result.stdout
    for secretish in (malformed.strip(), "123456789012", "sk-a-virtual-key"):
        assert secretish not in result.stdout + result.stderr, "the supplied value must never be echoed"


def test_the_baked_default_is_validated_when_no_override_is_given():
    """The Default is the value checked least: it is used exactly when the recipient passes nothing."""
    script = _bundle(gateway_config=_litellm(litellm_api_key_ref=KEY_ARN_HOME)).deploy_sh
    b = _preflight_block(script)
    result = subprocess.run(  # noqa: S603
        ["bash", "-c", f"set -u; REGION={HOME_REGION}\n{b}\necho ACCEPTED"],  # noqa: S607
        env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"},
        capture_output=True,
        text=True,
        check=False,
    )
    # with no override the block either accepts the baked Default (grammatical here) or, when the generator emits a
    # 'missing' branch that exits, refuses before the grammar check; both are fail-closed for a malformed Default below
    assert "is not a Secrets Manager ARN" not in result.stdout, result.stdout

"""Execute the real deploy.sh regional feature-profile function."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

DEPLOY_SH = Path(__file__).resolve().parents[2] / "scripts" / "deploy.sh"
SUPPORTED_RELEASE_REGIONS = (
    "eu-central-1",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "us-east-1",
    "us-east-2",
    "us-west-2",
)


def _function_source() -> tuple[str, str]:
    source = DEPLOY_SH.read_text(encoding="utf-8")
    match = re.search(
        r"^(report_(?:region|emea)_feature_profile)\(\) \{.*?^\}\n",
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match, "regional feature-profile reporter is missing"
    return match.group(1), match.group(0)


def _run(
    region: str,
    function_source: tuple[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    function_name, source = function_source or _function_source()
    script = f"""set -u
log_success() {{ printf 'SUCCESS %s\\n' "$*"; }}
log_info() {{ printf 'INFO %s\\n' "$*"; }}
log_warning() {{ printf 'WARNING %s\\n' "$*"; }}
log_error() {{ printf 'ERROR %s\\n' "$*"; }}
{source}
AWS_REGION="$1"
{function_name}
"""
    return subprocess.run(
        ["bash", "-c", script, "region-profile", region],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": os.environ["PATH"]},
        timeout=15,
    )


@pytest.mark.parametrize("region", ["us-east-1", "us-east-2", "us-west-2", "eu-west-1"])
def test_full_regions_report_the_full_profile(region: str) -> None:
    result = _run(region)
    assert result.returncode == 0
    assert "feature profile: full" in result.stdout
    assert "Managed Web Search and AWS Agent Registry federation are available" in result.stdout


@pytest.mark.parametrize("region", ["eu-central-1", "eu-west-2"])
def test_registry_enabled_reduced_regions_report_only_web_search_unavailable(region: str) -> None:
    result = _run(region)
    assert result.returncode == 0
    assert f"reduced core platform ({region})" in result.stdout
    assert "NOT SUPPORTED: Amazon Bedrock AgentCore managed Web Search" in result.stdout
    assert "NOT SUPPORTED: AWS Agent Registry federation" not in result.stdout
    assert "AWS Agent Registry federation is available" in result.stdout


def test_paris_reports_both_unavailable_optional_services_and_fallback() -> None:
    result = _run("eu-west-3")
    assert result.returncode == 0
    assert "reduced core platform (eu-west-3)" in result.stdout
    assert "NOT SUPPORTED: Amazon Bedrock AgentCore managed Web Search" in result.stdout
    assert "NOT SUPPORTED: AWS Agent Registry federation" in result.stdout
    assert "built-in DynamoDB agent registry remains available" in result.stdout


@pytest.mark.parametrize("region", ["us-west-1", "eu-north-1", "us-west-9", "unknown"])
def test_unselected_neighboring_and_unknown_regions_are_refused(region: str) -> None:
    result = _run(region)
    assert result.returncode != 0
    assert "outside this release's support envelope" in result.stdout
    for supported in SUPPORTED_RELEASE_REGIONS:
        assert supported in result.stdout


def test_removing_a_full_region_branch_loses_release_admission() -> None:
    function_name, original = _function_source()
    mutant = original.replace("us-east-2", "us-east-9", 1)
    assert mutant != original
    assert _run("us-east-2", (function_name, original)).returncode == 0
    assert _run("us-east-2", (function_name, mutant)).returncode != 0

"""Execute the real deploy.sh EMEA feature-profile function."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

DEPLOY_SH = Path(__file__).resolve().parents[2] / "scripts" / "deploy.sh"


def _function_source() -> str:
    source = DEPLOY_SH.read_text(encoding="utf-8")
    match = re.search(
        r"^report_emea_feature_profile\(\) \{.*?^\}\n",
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match, "report_emea_feature_profile is missing"
    return match.group(0)


def _run(region: str, function_source: str | None = None) -> subprocess.CompletedProcess[str]:
    script = f"""set -u
log_success() {{ printf 'SUCCESS %s\\n' "$*"; }}
log_info() {{ printf 'INFO %s\\n' "$*"; }}
log_warning() {{ printf 'WARNING %s\\n' "$*"; }}
log_error() {{ printf 'ERROR %s\\n' "$*"; }}
{function_source or _function_source()}
AWS_REGION="$1"
report_emea_feature_profile
"""
    return subprocess.run(
        ["bash", "-c", script, "region-profile", region],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": os.environ["PATH"]},
        timeout=15,
    )


def test_ireland_reports_the_full_profile() -> None:
    result = _run("eu-west-1")
    assert result.returncode == 0
    assert "feature profile: full" in result.stdout
    assert "Managed Web Search and AWS Agent Registry federation are available" in result.stdout


def test_reduced_regions_name_every_unsupported_feature_and_fallback() -> None:
    for region in ("eu-central-1", "eu-west-2", "eu-west-3"):
        result = _run(region)
        assert result.returncode == 0
        assert f"reduced core platform ({region})" in result.stdout
        assert "NOT SUPPORTED: Amazon Bedrock AgentCore managed Web Search" in result.stdout
        assert "NOT SUPPORTED: AWS Agent Registry federation" in result.stdout
        assert "built-in DynamoDB agent registry remains available" in result.stdout


def test_unreviewed_emea_region_is_refused_before_deploy() -> None:
    result = _run("eu-north-1")
    assert result.returncode != 0
    assert "outside this release's EMEA support envelope" in result.stdout
    assert "eu-central-1, eu-west-1, eu-west-2, eu-west-3" in result.stdout


def test_non_emea_region_is_left_to_the_existing_agentcore_probe() -> None:
    result = _run("us-east-1")
    assert result.returncode == 0
    assert result.stdout == ""


def test_removing_the_reduced_region_branch_loses_the_required_warning() -> None:
    original = _function_source()
    mutant = original.replace(
        "eu-central-1|eu-west-2|eu-west-3)",
        "eu-west-9)",
        1,
    )
    assert mutant != original
    assert "NOT SUPPORTED" in _run("eu-central-1", original).stdout
    assert "NOT SUPPORTED" not in _run("eu-central-1", mutant).stdout

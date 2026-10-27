"""The CDK app cannot synthesize the platform outside the seven selected release Regions.

`scripts/deploy.sh` already refuses an unselected Region before touching AWS; a direct
`cdk synth`/`cdk deploy` used to bypass that. `release_regions.require_release_region`
now makes the same decision from the same reviewed matrix before `PlatformStack` exists.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import release_regions

INFRA = Path(__file__).resolve().parents[1]
MATRIX = INFRA.parent / "release" / "agentcore-regions.json"
SEVEN = frozenset({"eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3", "us-east-1", "us-east-2", "us-west-2"})


def test_the_gate_reads_exactly_the_seven_selected_regions_from_the_reviewed_matrix():
    assert release_regions.selected_release_regions() == SEVEN
    for region in sorted(SEVEN):
        assert release_regions.require_release_region(region) == region


@pytest.mark.parametrize("region", ["us-west-1", "ap-southeast-2", "eu-north-1", "", None])
def test_the_gate_refuses_everything_outside_the_envelope_and_names_the_alternatives(region):
    with pytest.raises(release_regions.UnsupportedReleaseRegionError) as excinfo:
        release_regions.require_release_region(region)
    message = str(excinfo.value)
    assert "is not a selected production release Region" in message
    for candidate in sorted(SEVEN):
        assert candidate in message


def test_the_gate_fails_closed_without_a_readable_schema_2_commercial_matrix(tmp_path):
    with pytest.raises(release_regions.UnsupportedReleaseRegionError, match="could not be read"):
        release_regions.selected_release_regions(tmp_path / "absent.json")
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    for mutation, expected in (
        ({**matrix, "schema_version": 1}, "schema version 2"),
        ({**matrix, "partition": "aws-cn"}, "commercial aws partition"),
        ({**matrix, "regions": []}, "lists no Regions"),
        ({**matrix, "regions": [{**matrix["regions"][0], "production_release": "yes"}]}, "malformed entry"),
        (
            {**matrix, "regions": [{**entry, "production_release": False} for entry in matrix["regions"]]},
            "selects no production Region",
        ),
    ):
        path = tmp_path / "mutated.json"
        path.write_text(json.dumps(mutation))
        with pytest.raises(release_regions.UnsupportedReleaseRegionError, match=expected):
            release_regions.selected_release_regions(path)


def test_app_calls_the_gate_before_constructing_the_stack():
    source = (INFRA / "app.py").read_text(encoding="utf-8")
    assert "from release_regions import require_release_region" in source
    assert source.index("aws_region = require_release_region(aws_region)") < source.index("PlatformStack(")


def test_running_the_app_for_an_unselected_region_synthesizes_nothing(tmp_path):
    """Executable proof, not a source grep: the real app process exits non-zero with the
    gate's message and leaves no template behind."""
    out = tmp_path / "cdk.out"
    result = subprocess.run(
        [sys.executable, "app.py"],
        cwd=INFRA,
        env={
            **os.environ,
            "CDK_OUTDIR": str(out),
            "CDK_CONTEXT_JSON": json.dumps(
                {"aws_region": "us-west-1", "environment_name": "production", "project_name": "acf"}
            ),
        },
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode != 0
    assert "aws_region=us-west-1 is not a selected production release Region" in result.stderr
    assert not (out.exists() and list(out.glob("*.template.json")))

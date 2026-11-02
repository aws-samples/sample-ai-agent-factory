"""Release-Region gate for the CDK app.

`scripts/deploy.sh` refuses to deploy the platform outside the seven selected
production Regions before it touches AWS. Running `cdk deploy`/`cdk synth`
directly bypassed that script, so the same decision is made here, at synth
time, from the same reviewed source: ``release/agentcore-regions.json``. The
infra project deliberately does not import the backend package; the matrix file
is the contract both sides are tested against.

Fails closed: a missing or malformed matrix refuses every Region rather than
falling back to a built-in list that could drift from the reviewed one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

INFRA_DIR = Path(__file__).resolve().parent
DEFAULT_MATRIX_PATH = INFRA_DIR.parent / "release" / "agentcore-regions.json"
_SAFE_REGION = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d$")
_EXPECTED_SCHEMA_VERSION = 2


class UnsupportedReleaseRegionError(ValueError):
    """The platform stack would be synthesized outside the release envelope."""


def selected_release_regions(matrix_path: Path = DEFAULT_MATRIX_PATH) -> frozenset[str]:
    """The Regions the reviewed matrix marks ``production_release: true``."""
    try:
        matrix = json.loads(Path(matrix_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnsupportedReleaseRegionError(
            f"The reviewed Region matrix at {matrix_path} could not be read; refusing every Region"
        ) from exc
    if not isinstance(matrix, dict) or matrix.get("schema_version") != _EXPECTED_SCHEMA_VERSION:
        raise UnsupportedReleaseRegionError("The reviewed Region matrix is not schema version 2; refusing every Region")
    if matrix.get("partition") != "aws":
        raise UnsupportedReleaseRegionError("The reviewed Region matrix is not the commercial aws partition")
    entries = matrix.get("regions")
    if not isinstance(entries, list) or not entries:
        raise UnsupportedReleaseRegionError("The reviewed Region matrix lists no Regions")
    selected: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise UnsupportedReleaseRegionError("The reviewed Region matrix has a malformed entry")
        region = entry.get("region")
        flag = entry.get("production_release")
        if not isinstance(region, str) or not _SAFE_REGION.fullmatch(region) or not isinstance(flag, bool):
            raise UnsupportedReleaseRegionError(f"The reviewed Region matrix has a malformed entry for {region!r}")
        if flag:
            selected.add(region)
    if not selected:
        raise UnsupportedReleaseRegionError("The reviewed Region matrix selects no production Region")
    return frozenset(selected)


def require_release_region(region: object, matrix_path: Path = DEFAULT_MATRIX_PATH) -> str:
    """Return ``region`` if it is a selected release Region; otherwise fail closed."""
    selected = selected_release_regions(matrix_path)
    if not isinstance(region, str) or region not in selected:
        shown = region if isinstance(region, str) and region else "<unset>"
        raise UnsupportedReleaseRegionError(
            f"aws_region={shown} is not a selected production release Region; the platform "
            f"stack is refused. Pass -c aws_region=<one of: {', '.join(sorted(selected))}>."
        )
    return region

"""Every model the UI offers must survive the validator that gates a deploy.

The two layers encode the same policy in two places and neither imports the other:

  * ``frontend/src/utils/runtimeConfig.ts`` -- the list rendered in the runtime
    configuration panel. This is what a user can actually pick.
  * ``backend/src/app/models/deployment_models.py`` -- ``_validate_bedrock_model_id``,
    which runs when ``RuntimeConfig`` is built and raises on anything outside
    ``_BEDROCK_ACTIVE_MODEL_SUBSTRINGS``.

Drift between them is silent until deploy time and then fatal. Measured live on
``acfe2e-p0920``: a canvas carrying ``anthropic.claude-sonnet-4-5-20250929-v1:0`` came
back from ``POST /api/workflows/{id}/deploy`` as HTTP 502 with ``1 validation error for
RuntimeConfig``. That particular id came from a test helper rather than the picker, and
all 14 catalogue entries do currently pass -- so this file is a guard, not a bug report.
Without it, adding one row to the picker ships a model whose every deploy fails, with no
test anywhere objecting.

The region prefix is part of the contract. ``regionalizeModelId`` rewrites a leading
``us.`` to the deploying region's family (``eu``, ``apac``), so an id is only safe if it
passes under every prefix the backend can produce -- see ``region_inference_prefix``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.models.deployment_models import _validate_bedrock_model_id

#: The families ``region_inference_prefix`` can return. ``us`` is also its fallback for
#: regions with no profile family of their own.
_PREFIXES = ("us", "eu", "apac")

_CATALOGUE = Path(__file__).resolve().parents[2] / "frontend" / "src" / "utils" / "runtimeConfig.ts"


def _bedrock_ids() -> list[str]:
    """Pull the ``provider: 'bedrock'`` model ids out of the picker's catalogue.

    Parsed rather than hardcoded: a copy of the list here would be a third place to
    forget to update, which is the defect this file exists to prevent.
    """
    source = _CATALOGUE.read_text()
    ids = [m.group(1) for m in re.finditer(r"provider:\s*'bedrock',\s*modelId:\s*'([^']+)'", source)]
    return ids


def test_the_catalogue_is_actually_being_read():
    """Vacuity guard. If the file moves or the literal shape changes, the parse returns
    an empty list and every parametrized test below silently vanishes -- a green run
    that checked nothing."""
    assert _CATALOGUE.is_file(), f"the picker's catalogue is not at {_CATALOGUE}"
    ids = _bedrock_ids()
    assert len(ids) >= 10, (
        f"only parsed {len(ids)} bedrock ids from {_CATALOGUE.name}; the regex has "
        f"drifted from the file's shape, so this suite is no longer checking anything"
    )
    # Anchor on one id that must be present for the parse to be believable.
    assert any("claude-sonnet-5" in i for i in ids), ids


@pytest.mark.parametrize("model_id", _bedrock_ids())
@pytest.mark.parametrize("prefix", _PREFIXES)
def test_every_offered_model_passes_the_deploy_validator(model_id: str, prefix: str):
    """A user picks this row, clicks deploy, and the validator must not reject it."""
    regionalized = f"{prefix}.{model_id[3:]}" if model_id.startswith("us.") else model_id
    try:
        _validate_bedrock_model_id(regionalized)
    except ValueError as exc:  # pragma: no cover - only on real drift
        pytest.fail(
            f"the runtime configuration panel offers {model_id!r}, which in a "
            f"{prefix!r}-family region becomes {regionalized!r} and is REJECTED by the "
            f"deploy validator: {exc}. Every deploy a user makes with this row selected "
            f"fails at RuntimeConfig with an HTTP 502. Either add its substring to "
            f"_BEDROCK_ACTIVE_MODEL_SUBSTRINGS or remove the row from the picker."
        )


def test_the_validator_still_rejects_something():
    """The other half of the vacuity guard: the test above passing must mean the
    validator agrees, not that it accepts everything."""
    with pytest.raises(ValueError):
        _validate_bedrock_model_id("definitely.not-a-real-model-v1:0")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

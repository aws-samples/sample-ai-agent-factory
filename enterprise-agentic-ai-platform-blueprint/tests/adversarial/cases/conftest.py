"""Live-case fixtures and the explicit live-mode gate.

The autouse ``_adversarial_live_gate`` fixture is the loud half of the gate: if
this run asked for live verification (``AGENTICAI_ADVERSARIAL_LIVE=1``) or
requires it (``AGENTICAI_ADVERSARIAL_REQUIRE_LIVE=1``) and the credentials or
account mapping are absent, the whole live session **errors**. It is never
downgraded to a skip.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import pytest

from harness import LiveModeStatus

from cases import probes as probes_pkg


@pytest.fixture(autouse=True, scope="session")
def _adversarial_live_gate(live_mode: LiveModeStatus) -> LiveModeStatus:
    """Error when live verification was asked for but is unavailable."""
    live_mode.raise_if_unsatisfied()
    return live_mode


@pytest.fixture(scope="session")
def loaded_probes() -> tuple[str, ...]:
    """Import every ``probe_*.py`` module so registrations take effect."""
    return probes_pkg.load_probe_modules()

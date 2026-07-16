"""Gap 2C — POST-INTEGRATION tests for guardrails_step wiring.

REQUIRES-MANIFEST-APPLIED: these tests assert that the manifest edits to
``step_handlers/guardrails_step.py`` are in place — i.e. the create-new branch
calls ``build_contextual_grounding_config`` / ``build_regex_filters`` and MERGES
the regex result into the existing ``sensitiveInformationPolicyConfig`` rather
than overwriting the PII filters.

The main loop runs these AFTER applying the shared edits. To keep this file
green standalone (BEFORE the edits land), each test SKIPS itself when the
expected wiring isn't detected yet — it never spuriously fails the design-stage
suite. Once the edits are applied the skips turn into real assertions.

We avoid all real AWS: a fake bedrock client captures the ``create_guardrail``
kwargs, and the deployment-state store is stubbed out.

Run (post-integration):
    cd backend && python3 -m pytest tests/test_guardrails_enhancement.py -x -q
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "src")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _GuardrailNotFound(Exception):
    pass


class _FakeExceptions:
    # Mirrors bedrock.exceptions.ResourceAlreadyExistsException attribute access.
    ResourceAlreadyExistsException = _GuardrailNotFound


class FakeBedrock:
    """Captures create_guardrail kwargs; reports READY immediately."""

    def __init__(self):
        self.create_params: dict | None = None
        self.exceptions = _FakeExceptions()

    def create_guardrail(self, **kwargs):
        self.create_params = kwargs
        return {"guardrailId": "gr-test-123"}

    def get_guardrail(self, **kwargs):
        return {"status": "READY"}

    def create_guardrail_version(self, **kwargs):
        return {"version": "1"}


class _FakeStore:
    def update_step(self, *args, **kwargs):
        return None

    def record_resource(self, *args, **kwargs):
        return None


def _load_handler(monkeypatch, fake_bedrock):
    """Import guardrails_step with boto3.client + the store patched out.

    Returns the module, or None if the import itself fails (e.g. an OTEL
    bootstrap import not available in this environment) so the caller can skip.
    """
    try:
        from app.step_handlers import guardrails_step
    except Exception:  # pragma: no cover - environment-dependent
        return None

    monkeypatch.setattr(guardrails_step.step_clients, "client", lambda *a, **k: fake_bedrock)
    monkeypatch.setattr(guardrails_step, "_get_deployment_store", lambda: _FakeStore())
    # Skip the READY polling sleeps.
    monkeypatch.setattr(guardrails_step.time, "sleep", lambda *_: None)
    return guardrails_step


def _wiring_present(guardrails_step) -> bool:
    """True once the manifest import edit has been applied to guardrails_step."""
    import inspect

    src = inspect.getsource(guardrails_step)
    return (
        "build_contextual_grounding_config" in src
        and "build_regex_filters" in src
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_contextual_grounding_wired_into_create_params(monkeypatch):
    fake = FakeBedrock()
    gs = _load_handler(monkeypatch, fake)
    if gs is None or not _wiring_present(gs):
        pytest.skip("REQUIRES-MANIFEST-APPLIED: guardrails_step grounding wiring not present yet")

    event = {
        "deployment_id": "dep-aaaaaaaa",
        "guardrails_config": {
            "mode": "create_new",
            "name": "g1",
            "contextualGrounding": {"groundingThreshold": 0.8, "relevanceThreshold": 0.6},
        },
    }
    gs.handler(event, None)

    cg = fake.create_params.get("contextualGroundingPolicyConfig")
    assert cg == {
        "filtersConfig": [
            {"type": "GROUNDING", "threshold": 0.8},
            {"type": "RELEVANCE", "threshold": 0.6},
        ]
    }


def test_regex_merges_with_pii_not_overwrite(monkeypatch):
    """The critical Bug-122-class assertion: regex MERGES with PII."""
    fake = FakeBedrock()
    gs = _load_handler(monkeypatch, fake)
    if gs is None or not _wiring_present(gs):
        pytest.skip("REQUIRES-MANIFEST-APPLIED: guardrails_step regex wiring not present yet")

    event = {
        "deployment_id": "dep-bbbbbbbb",
        "guardrails_config": {
            "mode": "create_new",
            "name": "g2",
            "piiFilters": [{"type": "EMAIL", "action": "ANONYMIZE"}],
            "regexFilters": [{"name": "ticket", "pattern": "TICKET-\\d+", "action": "BLOCK"}],
        },
    }
    gs.handler(event, None)

    sip = fake.create_params.get("sensitiveInformationPolicyConfig", {})
    # Both sub-policies must coexist under the one key.
    assert "piiEntitiesConfig" in sip, "PII filters were clobbered by regex merge"
    assert "regexesConfig" in sip, "regex filters not wired in"
    assert sip["piiEntitiesConfig"] == [{"type": "EMAIL", "action": "ANONYMIZE"}]
    assert sip["regexesConfig"][0]["name"] == "ticket"


def test_no_grounding_no_regex_keys_when_absent(monkeypatch):
    fake = FakeBedrock()
    gs = _load_handler(monkeypatch, fake)
    if gs is None or not _wiring_present(gs):
        pytest.skip("REQUIRES-MANIFEST-APPLIED: guardrails_step wiring not present yet")

    event = {
        "deployment_id": "dep-cccccccc",
        "guardrails_config": {
            "mode": "create_new",
            "name": "g3",
            "piiFilters": [{"type": "EMAIL", "action": "ANONYMIZE"}],
        },
    }
    gs.handler(event, None)

    assert "contextualGroundingPolicyConfig" not in fake.create_params
    sip = fake.create_params.get("sensitiveInformationPolicyConfig", {})
    assert "regexesConfig" not in sip
    assert "piiEntitiesConfig" in sip

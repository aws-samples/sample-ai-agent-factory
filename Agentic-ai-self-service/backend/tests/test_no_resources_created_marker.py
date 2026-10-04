"""F-55: the ``$.no_resources_created`` marker suppresses cleanup, and ONLY in its exact shape.

Why this file exists at all. When a deployment is refused at ValidateWorkflow, the resource
manifest is empty and that emptiness is a CERTAINTY -- no resource-creating state ran. Without a
marker, ``_auto_cleanup_on_failure`` records ``delete_retained`` with "could not prove that the
empty manifest represented a deployment that created no resources", which sends an operator
hunting for orphans that cannot exist.

The marker is read out of Step Functions state input, and it SUPPRESSES cleanup. That makes its
shape check a security boundary, not an ergonomic one (ARCC cnt_jljdNeOwgPnFx2: nothing outside
the trusted server path may alter the authenticated context of an action). So this file is
deliberately two-sided:

* the ADMITTING tests prove the genuine server-authored marker actually changes the outcome --
  without them, "rejects every marker" would pass every negative test and the feature would be
  dead. That exact failure mode has shipped in this repo before.
* the NEGATIVE tests fuzz the shape one field at a time, because a single composite bad marker
  cannot tell you WHICH check rejected it.
"""

from unittest.mock import MagicMock, patch

import pytest

GENUINE = {"proven": True, "reason": "rejected at ValidateWorkflow"}
GENUINE_SFN = {
    "proven": True,
    "reason": "rejected at ValidateWorkflow, before any resource-creating task",
}


def _store(resources: list | None = None, manifest_version: int | None = None) -> MagicMock:
    """A store whose deployment has an EMPTY manifest unless told otherwise."""
    store = MagicMock()
    state = MagicMock()
    record = {
        "deployment_id": "d-1",
        "user_id": "owner-1",
        "target_account_id": "111111111111",
        "status": "failed",
        "created_resources": resources or [],
    }
    if manifest_version is not None:
        record["resource_manifest_version"] = manifest_version
        record["resource_manifest_complete"] = True
    state.model_dump.return_value = record
    store.get.return_value = state
    return store


def _delete_statuses(store: MagicMock) -> list[str]:
    return [c.args[1] for c in store.update_delete_status.call_args_list if len(c.args) > 1]


def _run(store: MagicMock, event: dict) -> list:
    """Run cleanup, returning the resources it actually tried to delete."""
    from app.step_handlers.status_update_step import _auto_cleanup_on_failure

    deleted = []
    with patch(
        "app.step_handlers.status_update_step._cleanup_resource",
        side_effect=lambda res, region, event: deleted.append(res.get("type")),
    ):
        _auto_cleanup_on_failure(store, "d-1", event)
    return deleted


# --------------------------------------------------------------------------------------
# ADMITTING: the genuine marker must actually do something.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("marker", [GENUINE, GENUINE_SFN])
def test_the_genuine_marker_records_an_honest_no_resources_outcome(marker):
    """Both server-authored markers (handler and state machine) reach the honest outcome."""
    store = _store()
    deleted = _run(store, {"no_resources_created": marker})

    assert deleted == [], "nothing was created, so nothing may be deleted"
    statuses = _delete_statuses(store)
    assert "deleted" in statuses, (
        "the genuine marker must record an honest no-resources outcome; got "
        f"{statuses}. If this is 'delete_retained' the marker is inert and the operator is "
        "told orphans might exist when they provably cannot"
    )
    assert "delete_retained" not in statuses


def test_the_genuine_marker_marks_the_empty_manifest_complete():
    """The substantive half: an incomplete manifest keeps inventing supplemental rows."""
    store = _store()
    _run(store, {"no_resources_created": GENUINE, "error": "payload rejected"})

    complete = [c for c in store.update_status.call_args_list if c.kwargs.get("resource_manifest_complete") is True]
    assert complete, (
        "the empty manifest must be marked complete; while it is incomplete, "
        "_supplemental_failure_resources keeps inferring rows from the event on every later "
        "cleanup attempt -- inventing resources for a deployment that created none"
    )


def test_an_allowlisted_reason_is_still_not_the_text_the_operator_is_shown():
    """Even a genuine reason is not echoed: the record gets a server-authored sentence."""
    store = _store()
    _run(store, {"no_resources_created": GENUINE_SFN})

    messages = " ".join(str(a) for c in store.update_delete_status.call_args_list for a in c.args)
    assert GENUINE_SFN["reason"] not in messages, (
        "the marker's own reason must not become the operator-facing record text"
    )
    assert "No resources were created" in messages


def test_the_reason_allowlist_matches_the_literal_the_handler_actually_emits():
    """Pins the coupling that exact membership creates, so drift fails loudly here.

    ``_proven_no_resources`` compares the reason against a tuple of literals. The handler that
    produces one of those literals lives in a different module, so a reworded string there would
    silently stop the marker working -- and "silently stops working" means every rejected
    deployment goes back to reporting orphans that cannot exist. Asserting the two agree is the
    only thing that turns that into a test failure.
    """
    import inspect

    from app.step_handlers import validate_step
    from app.step_handlers.status_update_step import _SERVER_AUTHORED_NO_RESOURCE_REASONS

    source = inspect.getsource(validate_step._rejected)
    emitted = [r for r in _SERVER_AUTHORED_NO_RESOURCE_REASONS if f'"{r}"' in source]
    assert emitted, (
        "validate_step._rejected emits a no_resources_created reason that is not in "
        f"_SERVER_AUTHORED_NO_RESOURCE_REASONS {_SERVER_AUTHORED_NO_RESOURCE_REASONS!r}; the "
        "marker it sets would be rejected and cleanup would report unprovable orphans"
    )


def test_the_reason_allowlist_matches_the_literal_the_state_machine_emits():
    """The same coupling for the OTHER emitter, which lives in a different tree entirely.

    There are two producers of this marker and one consumer. The handler's literal is pinned
    above; this pins the state machine's, which is authored in ``infra/stacks/platform`` and so
    is not reachable by import from here. ``infra/tests/test_f55_deployment_input_gate.py``
    asserts the ASL carries the literal, but it cannot compare it against the allowlist without
    importing this backend package into the CDK interpreter -- so a reword on EITHER side would
    still pass both suites while breaking the marker. Read as text for exactly that reason: the
    assertion needs no CDK, no synth and no infra dependency.

    A drifted literal here is silent and expensive: every deployment refused at the gate goes
    back to recording ``delete_retained`` and sending an operator to hunt for orphans that
    cannot exist.
    """
    from pathlib import Path

    from app.step_handlers.status_update_step import _SERVER_AUTHORED_NO_RESOURCE_REASONS

    machine = Path(__file__).resolve().parents[2] / "infra" / "stacks" / "platform" / "step_functions.py"
    assert machine.is_file(), f"expected the state machine definition at {machine}"
    source = machine.read_text()

    emitted = [reason for reason in _SERVER_AUTHORED_NO_RESOURCE_REASONS if f'"{reason}"' in source]
    assert emitted, (
        f"{machine.name} sets a no_resources_created reason that is not in "
        f"_SERVER_AUTHORED_NO_RESOURCE_REASONS {_SERVER_AUTHORED_NO_RESOURCE_REASONS!r}; the "
        "marker the state machine writes would be rejected by _proven_no_resources and every "
        "gate-refused deployment would report unprovable orphans"
    )


# --------------------------------------------------------------------------------------
# NEGATIVE: one malformed field at a time. None of these may suppress cleanup.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker,why",
    [
        ({"proven": 1, "reason": "r"}, "1 is truthy but is not True"),
        ({"proven": "true", "reason": "r"}, "the string 'true' is truthy"),
        ({"proven": "yes", "reason": "r"}, "any non-empty string is truthy"),
        ({"proven": [0], "reason": "r"}, "a non-empty list is truthy"),
        ({"proven": True}, "no reason at all"),
        ({"proven": True, "reason": ""}, "an empty reason"),
        ({"proven": True, "reason": "   "}, "a whitespace-only reason"),
        ({"proven": True, "reason": 42}, "a non-string reason"),
        # The reason is the only field carrying provenance, so it is matched against the
        # server-authored allowlist EXACTLY. Each of these is a near-miss that a normalizing
        # comparison (strip / casefold / prefix / substring) would have admitted.
        ({"proven": True, "reason": "rejected at validateworkflow"}, "a case variant"),
        ({"proven": True, "reason": " rejected at ValidateWorkflow "}, "a whitespace variant"),
        ({"proven": True, "reason": "rejected at ValidateWorkflow."}, "a trailing period"),
        (
            {"proven": True, "reason": "rejected at ValidateWorkflow, and also everything else"},
            "an allowlisted prefix with extra text",
        ),
        (
            {"proven": True, "reason": "definitely rejected at ValidateWorkflow"},
            "an allowlisted substring with a prefix",
        ),
        (
            {"proven": True, "reason": "rejected at ValidateWorkflow <img src=x onerror=alert(1)>"},
            "an injection payload appended to an allowlisted reason",
        ),
        ({"proven": True, "reason": "no resources were created"}, "a plausible but unlisted reason"),
        ({"proven": True, "reason": "r", "extra": "smuggled"}, "an extra key"),
        ({"reason": "r"}, "no proven key"),
        ({}, "an empty object"),
        (True, "a bare boolean instead of the marker object"),
        ("proven", "a bare string"),
        (["proven"], "a list"),
        (1, "an integer"),
    ],
)
def test_a_marker_that_is_not_exactly_right_cannot_suppress_cleanup(marker, why):
    """``proven`` is checked with ``is True``; a truthy value must not disable cleanup."""
    store = _store()
    _run(store, {"no_resources_created": marker})

    statuses = _delete_statuses(store)
    assert "deleted" not in statuses, f"cleanup was suppressed by {why}: {marker!r}"
    assert "delete_retained" in statuses, (
        f"a rejected marker must fall back to the unchanged conservative outcome; got {statuses}"
    )


def test_a_marker_nested_elsewhere_in_the_state_is_ignored():
    """Only the top-level field is read. A nested copy is caller-reachable payload."""
    store = _store()
    _run(
        store,
        {
            "config": {"no_resources_created": GENUINE},
            "error_info": {"no_resources_created": GENUINE},
        },
    )
    assert "deleted" not in _delete_statuses(store)


def test_an_absent_marker_keeps_the_previous_conservative_behaviour():
    """The regression guard: the no-marker path is exactly as it was before F-55."""
    store = _store()
    _run(store, {})
    statuses = _delete_statuses(store)
    assert "delete_retained" in statuses and "deleted" not in statuses


# --------------------------------------------------------------------------------------
# The contradiction case: the marker is only ever believed alongside its evidence.
# --------------------------------------------------------------------------------------


def test_a_genuine_marker_cannot_suppress_deletion_of_a_non_empty_manifest():
    """If the manifest holds rows, the marker is wrong -- and real resources still get deleted.

    This is why the check lives inside the empty-manifest branch instead of being an early
    return at the top of the function. An early return would let one contradictory field leak
    every resource the deployment actually created.
    """
    store = _store(
        resources=[{"type": "gateway", "id": "gw-1", "name": "gw-one", "region": "us-east-1"}],
        manifest_version=1,
    )
    deleted = _run(store, {"no_resources_created": GENUINE})

    assert "gateway" in deleted, (
        "a marker claiming nothing was created, contradicted by a manifest row, must not "
        "suppress the deletion of that row"
    )
    # The STATUS is not the discriminator here: a successful real cleanup also ends in
    # "deleted" (status_update_step.py:856). The message is what distinguishes the two, so
    # assert the honest-no-resources wording was NOT used for a deployment that had resources.
    messages = " ".join(str(a) for c in store.update_delete_status.call_args_list for a in c.args)
    assert "No resources were created" not in messages
    assert "Automatic cleanup handled" in messages


def test_a_manifest_durability_failure_does_not_escape_the_best_effort_cleanup():
    """update_status raises by design when the manifest has a recorded durability failure."""
    store = _store()
    store.update_status.side_effect = ValueError("Deployment resource manifest has a recorded durability failure")
    # Must not raise: this whole function is best-effort and may not change the failure status.
    _run(store, {"no_resources_created": GENUINE})
    assert "deleted" in _delete_statuses(store)

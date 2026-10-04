"""F-81/F-82 — the cross-tenant name lock, tested through the functions that actually run.

Why a second file next to test_versions_cross_tenant.py. That file says so itself: *"instead we
replicate the ownership check that deployment_handler now performs"*, and its helpers take
``friendly_runtime_name`` as a PARAMETER. Both defects here live in code that decides what that
name IS, so a mirror that is handed the right name cannot fail on either of them, no matter how
many cases it covers. It passed throughout.

F-81. Two delete-path callers read ``friendly_runtime_name`` off the stored deployment record --
a field that was never written -- and fell through to the raw canvas ``node_id``.
``sanitize_runtime_name`` turns a hyphen into an underscore, so the lookup key for rows stored
under ``f81lock_1790236860`` was ``f81lock-1790236860``. Nothing raised: the versions query
returned no rows and the slots get returned None, so the release appended nothing and the name
stayed locked against every other tenant permanently. Measured live 2026-09-24 against
acfe2e-p0920: DELETE removed the AgentCore runtime and left every versions and slots row intact.

F-82. ``_LIVE_CLAIM`` includes ``pending`` so an in-flight deploy holds its name, but nothing
bounded how long "in-flight" could last. Only status_update_step flips the row, and an aborted
execution -- or one that hits the state machine's own top-level timeout -- is terminated without
running the Catch, so the flip never happens. Measured live: ``sfx0920_abort`` pending since
2026-09-20 with its deployment still reading in_progress.

Every test here calls the real ``deployment_handler`` function. No re-implementation.
"""

from __future__ import annotations

import ast
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, "src")

from app.deployment_handler import (  # noqa: E402
    _pending_claim_still_live,
    _proven_runtime_name_for_destroy,
    _resolve_friendly_runtime_name,
)


def _friendly_runtime_name_of(record: dict) -> str | None:
    """The name term of the real resolver.

    There is deliberately no such helper in production any more. There used to be, described as
    safe "for callers that just need a lookup key and never delete on it" -- and its one caller
    passed the result to ``destroy_runtime``, which enumerates a non-owner-scoped trigger
    partition by that name and deletes Scheduler schedules, EventBridge rules, Lambda URL configs
    and webhook secrets from it. Dropping the name on the floor is the point: a test may ignore
    ``proven``, production may not.
    """
    name, _proven = _resolve_friendly_runtime_name(record)
    return name


from app.models.deployment_models import DeploymentState  # noqa: E402
from app.services.agent_versions_store import short_version_suffix  # noqa: E402
from app.services.runtime_deployer import sanitize_runtime_name  # noqa: E402

# ---------------------------------------------------------------------------
# F-81 — the derivation must produce the EXACT table partition key
# ---------------------------------------------------------------------------


def test_a_hyphenated_node_id_does_not_become_the_lookup_key():
    """The exact shape measured live, and the one the old order got wrong.

    A canvas node id is ``f81lock-1790236860``; the versions/slots rows are keyed
    ``f81lock_1790236860``. Returning the hyphenated form is not a near miss -- it is a key that
    cannot exist, which is why the release found nothing and reported success.
    """
    record = {
        "node_id": "f81lock-1790236860",
        "agentcore_runtime_name": "f81lock_1790236860_9a9516d1",
    }
    assert _friendly_runtime_name_of(record) == "f81lock_1790236860"
    # Stated as the invariant rather than the literal: the key must be what the deploy path
    # would have produced from the same node id.
    assert _friendly_runtime_name_of(record) == sanitize_runtime_name("f81lock-1790236860")


def test_a_persisted_friendly_name_beats_a_truncated_derivable_one():
    """``agentcore_runtime_name`` cuts the friendly portion to 39 chars before the suffix.

    So for a long name, stripping the suffix yields a DIFFERENT string from the row key, and
    nothing about the stored value reveals that it was cut. The persisted field is the only term
    that survives this, which is why it is preferred and why it had to start being written.
    """
    friendly = "a_very_long_agent_name_that_exceeds_the_cap_easily"
    assert len(friendly) > 39
    record = {
        "friendly_runtime_name": friendly,
        "agentcore_runtime_name": f"{friendly[:39]}_deadbeef",
        "node_id": "some-node",
    }
    assert _friendly_runtime_name_of(record) == friendly
    # And without it, the derived value is provably wrong -- this is the residual gap the
    # persisted field closes, asserted so nobody "simplifies" the field away.
    assert _friendly_runtime_name_of({k: v for k, v in record.items() if k != "friendly_runtime_name"}) != friendly


def test_a_flow_id_in_workflow_id_is_never_used_as_a_runtime_name():
    """On current rows ``workflow_id`` is a FLOW id; it held a node id only pre-F-55.

    A flow id is a uuid and would key nothing, so the runtime name must win over it. This is
    why ``agentcore_runtime_name`` has to be tried before either canvas field.
    """
    version_id = "01a0d26f76e7ecc2611fea5a2f6852b3"
    record = {
        "workflow_id": "3f0c9c2e-1111-2222-3333-444455556666",
        "node_id": "agent-node-7",
        "version_id": version_id,
        "agentcore_runtime_name": f"prod_bot_{short_version_suffix(version_id)}",
    }
    assert _friendly_runtime_name_of(record) == "prod_bot"
    assert _resolve_friendly_runtime_name(record) == ("prod_bot", True)
    # And the other half of the ordering rule: the runtime name only wins when its suffix is
    # provably this record's own version. Strip the correlation and the term drops out entirely
    # rather than degrading to a plausible-looking guess -- an 8-hex tail on an adopted name means
    # nothing, so the canvas id (unproven, and marked as such) is the honest answer.
    uncorrelated = dict(record, agentcore_runtime_name="prod_bot_abcd1234")
    assert _resolve_friendly_runtime_name(uncorrelated) == ("agent_node_7", False)


def test_a_pre_runtime_record_falls_back_to_a_SANITIZED_canvas_id():
    """A deploy that failed before the runtime name existed has only the canvas id.

    Sanitizing is the fix, not a nicety: unsanitized these cannot be table keys at all.
    """
    assert _friendly_runtime_name_of({"node_id": "my-early-failure"}) == "my_early_failure"
    assert _friendly_runtime_name_of({"workflow_id": "older-row-node"}) == "older_row_node"


def test_nothing_stored_returns_none_so_the_caller_can_skip():
    """A caller must be able to tell "no name" from "some name", or it queries an empty key."""
    assert _friendly_runtime_name_of({}) is None
    assert _friendly_runtime_name_of({"agentcore_runtime_name": ""}) is None


def test_the_deployment_record_can_actually_carry_the_name():
    """The field has to exist on the model AND survive serialization.

    ``DeploymentStateStore`` writes ``model_dump(mode="json", exclude_none=True)``, so a field
    absent from the model is dropped in silence -- which is precisely the failure this whole
    finding is: a delete path reading an attribute no writer ever produced.
    """
    state = DeploymentState(
        deployment_id="d1",
        started_at=datetime.now(timezone.utc).isoformat(),
        friendly_runtime_name="prod_bot",
    )
    assert state.friendly_runtime_name == "prod_bot"
    dumped = state.model_dump(mode="json", exclude_none=True)
    assert dumped["friendly_runtime_name"] == "prod_bot"
    # And the round trip the delete path performs must recover it.
    assert _friendly_runtime_name_of(dumped) == "prod_bot"


# ---------------------------------------------------------------------------
# F-82 — a "pending" claim may only block while a deploy could still be running
# ---------------------------------------------------------------------------


def test_a_pending_row_older_than_the_ceiling_stops_blocking(monkeypatch):
    monkeypatch.setenv("DEPLOY_PENDING_CLAIM_TTL_SECONDS", "2100")
    # The live row: sfx0920_abort, pending since 2026-09-20, deployment still in_progress.
    assert _pending_claim_still_live("2026-09-20T09:28:19.867503+00:00") is False


def test_a_fresh_pending_row_still_blocks(monkeypatch):
    """The guard's actual purpose. Releasing a live claim would admit a cross-tenant race,
    which is strictly worse than the permanent lock this finding is about."""
    monkeypatch.setenv("DEPLOY_PENDING_CLAIM_TTL_SECONDS", "2100")
    assert _pending_claim_still_live(datetime.now(timezone.utc).isoformat()) is True


@pytest.mark.parametrize(
    "created_at",
    [
        None,
        "",
        "not-a-timestamp",
        "2026-09-20",  # date only: parses, but naive
        "2026-09-20T09:28:19.867503",  # no offset
        "2099-01-01T00:00:00+00:00",  # clock skew into the future
    ],
)
def test_every_unreadable_timestamp_keeps_the_lock(created_at, monkeypatch):
    """Fail CLOSED, in every direction.

    True preserves today's behaviour exactly; False releases somebody's name. A naive timestamp
    is ambiguous by the reader's whole UTC offset, which can exceed the entire budget, so it is
    treated as unreadable rather than assumed to be UTC.
    """
    monkeypatch.setenv("DEPLOY_PENDING_CLAIM_TTL_SECONDS", "2100")
    assert _pending_claim_still_live(created_at) is True


@pytest.mark.parametrize("bad", ["", "   ", "not-a-number", "0", "-1"])
def test_a_missing_or_nonsense_ttl_does_not_silently_disable_the_bound(bad, monkeypatch):
    """A stack deployed before the env var existed, or one with a corrupted value, must not
    revert to the permanent lock -- nor release everything. An unusable TTL falls back to the
    same arithmetic the stack uses; a non-positive one keeps the lock."""
    monkeypatch.setenv("DEPLOY_PENDING_CLAIM_TTL_SECONDS", bad)
    old = "2026-09-20T09:28:19.867503+00:00"
    if bad in ("0", "-1"):
        assert _pending_claim_still_live(old) is True
    else:
        assert _pending_claim_still_live(old) is False


def test_the_backend_default_matches_the_bound_the_stack_publishes():
    """The fallback and the stack must agree, or a stack deployed before the env var existed
    behaves differently from a current one for no visible reason.

    Read out of the CDK module rather than copied, because a copied number is exactly how the two
    drift. Parsed with ``ast`` rather than imported: ``config.py`` imports ``aws_cdk``, which is
    not installed in the backend virtualenv, and an ``importorskip`` here would turn the one test
    that catches drift into a test that never runs -- the same shape of dead assertion this
    finding is about. ``ast.literal_eval`` also cannot execute the module.
    """
    config_py = Path(__file__).resolve().parents[2] / "infra" / "stacks" / "platform" / "config.py"
    assert config_py.is_file(), f"expected the CDK config at {config_py}"

    constants: dict[str, int] = {}
    for node in ast.parse(config_py.read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ("DEPLOYMENT_STATE_MACHINE_TIMEOUT_MINUTES", "PENDING_CLAIM_SLACK_SECONDS"):
                constants[name] = ast.literal_eval(node.value)

    assert set(constants) == {
        "DEPLOYMENT_STATE_MACHINE_TIMEOUT_MINUTES",
        "PENDING_CLAIM_SLACK_SECONDS",
    }, f"the CDK config no longer declares the bound the backend falls back to: found {sorted(constants)}"

    from app.deployment_handler import _PENDING_CLAIM_TTL_DEFAULT_SECONDS  # noqa: PLC0415

    assert _PENDING_CLAIM_TTL_DEFAULT_SECONDS == (
        constants["DEPLOYMENT_STATE_MACHINE_TIMEOUT_MINUTES"] * 60 + constants["PENDING_CLAIM_SLACK_SECONDS"]
    )


# ---------------------------------------------------------------------------
# F-81c — which callers may see an INFERRED name (none of them)
# ---------------------------------------------------------------------------


def test_an_inferred_name_is_never_handed_to_destroy_runtime():
    """``destroy_runtime(runtime_name=...)`` is a destructive consumer, not a lookup.

    It enumerates ``TriggersTable`` by runtime name -- a partition that is not owner-scoped -- and
    deletes EventBridge Scheduler schedules, EventBridge rules and targets, Lambda function-URL
    configs, webhook secrets and the trigger rows (runtime_deployer.py:1455-1560). A sanitized
    canvas id is an inference, and two tenants can produce the same one from different canvases,
    so the only safe value is None: the destroy then resolves the name from the runtime id it is
    actually deleting instead of being short-circuited by a guess.
    """
    record = {"deployment_id": "dep-1", "node_id": "shared-name", "runtime_id": "rt-1"}
    # The name IS resolvable -- that is what makes the distinction load-bearing rather than
    # incidental. The gate is ``proven``, not "did we find anything".
    assert _friendly_runtime_name_of(record) == "shared_name"
    assert _resolve_friendly_runtime_name(record) == ("shared_name", False)
    assert _proven_runtime_name_for_destroy(record) is None


def test_a_proven_name_is_still_handed_over():
    """The control: gating on ``proven`` must not silently disable trigger cleanup.

    A suite that only proves what a guard refuses is compatible with it refusing everything, and
    here "everything" means every trigger of every deleted agent leaking forever.
    """
    persisted = {"deployment_id": "dep-1", "friendly_runtime_name": "prod_bot", "node_id": "x-y"}
    assert _proven_runtime_name_for_destroy(persisted) == "prod_bot"

    version_id = "01a0d26eb4459a9516d12064ac4a8687"
    derived = {
        "deployment_id": "dep-1",
        "version_id": version_id,
        "agentcore_runtime_name": f"old_bot_{short_version_suffix(version_id)}",
        "node_id": "x-y",
    }
    assert _proven_runtime_name_for_destroy(derived) == "old_bot"


def test_a_legacy_imported_record_resolves_to_nothing():
    """Adopted runtimes were marked by a ``workflow_id`` prefix before the ``imported`` flag.

    Reading the flag directly missed exactly those rows -- the oldest adopted runtimes, whose
    AWS-chosen names are the ones most likely to contain an underscore, so the suffix strip would
    have turned ``my_agent`` into the key ``my``: some other tenant's agent entirely.
    """
    legacy = {
        "deployment_id": "dep-import",
        "workflow_id": "imported-Omar1_8fb9892d-eqyzUC97dh",
        "agentcore_runtime_name": "Omar1_8fb9892d",
        "runtime_id": "Omar1_8fb9892d-eqyzUC97dh",
    }
    assert _resolve_friendly_runtime_name(legacy) == (None, False)
    assert _proven_runtime_name_for_destroy(legacy) is None

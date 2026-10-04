"""P0-B: the backend and the browser must compute ONE policy revision.

The deploy route refuses a revision that does not match the store's current one, so a
disagreement between the two implementations is not a crash -- it silently becomes "every
deploy that carries tags is stale", which is indistinguishable from an admin having just
changed a policy. The digest below is pinned by BOTH sides against the same fixture:
here, and in frontend/src/components/deploy/resourceTagState.test.ts.
"""

from __future__ import annotations

from app.services.tag_policy_store import TagPolicy, compute_policy_revision

# Built to break on every way the two runtimes can drift: an uppercase key, a non-ASCII key
# AND value (both sides must emit them raw, not \uXXXX), punctuation that localeCompare
# weights differently from code points, a null default_value, and a policy with no
# created_at (which must serialize as ""). Under localeCompare this set sorts app-tier,
# Eclair, platform:owner, Zulu:z; under code points Zulu:z, app-tier, platform:owner,
# Eclair -- so a locale-ordered implementation cannot produce this digest.
ADVERSARIAL = [
    TagPolicy(key="Zulu:z", default_value=None, required=False, show_on_card=True, updated_at="2026-09-23T09:00:00Z"),
    TagPolicy(
        key="app-tier",
        default_value="tier/1",
        required=True,
        show_on_card=False,
        created_at="2026-09-22T09:00:00Z",
        updated_at="2026-09-23T09:00:00Z",
    ),
    TagPolicy(
        key="platform:owner",
        default_value=None,
        required=True,
        show_on_card=True,
        created_at="2026-09-21T09:00:00Z",
        updated_at="2026-09-23T09:00:00Z",
    ),
    TagPolicy(
        key="Éclair",
        default_value="crème",
        required=False,
        show_on_card=False,
        created_at="",
        updated_at="2026-09-23T09:00:00Z",
    ),
]

FRONTEND_DIGEST = "sha256:1518cafb7088ecc412bf2297aed7a82787b4d40ed7ecabf5342377ada6654384"


def test_the_backend_digest_is_the_digest_the_browser_computes() -> None:
    assert compute_policy_revision(ADVERSARIAL) == FRONTEND_DIGEST


def test_the_digest_does_not_depend_on_the_order_the_store_returned() -> None:
    assert compute_policy_revision(list(reversed(ADVERSARIAL))) == FRONTEND_DIGEST


def test_every_hashed_field_moves_the_digest() -> None:
    """Each of the six fields is in the projection because changing it is a different
    governance state. A field silently dropped from one side would make the two agree on a
    digest that hides a real policy change -- so each one is asserted to move it."""
    base = compute_policy_revision(ADVERSARIAL)
    for field, value in (
        ("key", "app-tier-2"),
        ("default_value", "tier/2"),
        ("required", False),
        ("show_on_card", True),
        ("created_at", "2026-09-22T09:00:01Z"),
        ("updated_at", "2026-09-23T09:00:01Z"),
    ):
        mutated = [p.model_copy(update={field: value}) if p.key == "app-tier" else p for p in ADVERSARIAL]
        assert compute_policy_revision(mutated) != base, field


def test_a_missing_created_at_hashes_as_the_empty_string_not_as_null() -> None:
    """``created_at`` is "" by default on the model and ``?? ''`` in the browser. If one side
    ever emitted null instead, these two would differ."""
    absent = TagPolicy(key="k", default_value=None, required=False, show_on_card=False, updated_at="t")
    explicit_empty = TagPolicy(
        key="k", default_value=None, required=False, show_on_card=False, created_at="", updated_at="t"
    )
    assert compute_policy_revision([absent]) == compute_policy_revision([explicit_empty])


def test_an_empty_policy_set_still_has_a_stable_revision() -> None:
    """A fresh org has no policies, and the browser hashes ``[]`` for it. The two must still
    agree, or the very first deploy on a new stack is refused as stale."""
    assert compute_policy_revision([]) == ("sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945")

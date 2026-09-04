"""The registry provider seam — Workstream B of the post-demo customer feedback.

A customer who already runs LiteLLM wants it to be their agent/MCP catalog
instead of the platform's internal DynamoDB registry. This package makes the
registry backend pluggable so that becomes an OPTION; DynamoDB stays the default
and its code path is unchanged.

Why the Protocol mirrors ``RegistryStore`` and not a prettier abstraction
----------------------------------------------------------------------------
The plan called for "a Protocol covering exactly what ``routers/registry.py``
needs". What the router needs *is* the store surface — nine methods it already
calls. Shaping the Protocol to that surface means the router refactor is a
one-symbol swap (``get_registry_store()`` -> ``get_registry_provider()``) with no
call-site rewrites, which is what preserves the regression signal for this
workstream: ``tests/test_registry_store.py`` and ``tests/test_registry_rbac.py``
must pass with NO edits. A more opinionated ``publish``/``approve``/``clone``
Protocol would have forced every handler body to change and destroyed that
signal for a cosmetic gain.

Deliberately NOT derived from ``AwsAgentRegistry``: four of its methods
(``submit_for_approval``, ``set_status``, ``get``, the lenient ``list_records``)
have zero production callers, so a Protocol shaped after it would over-fit.

Honesty over silent degradation
-------------------------------
LiteLLM cannot back the whole surface — it exposes no create/update/delete API
for MCP *server* records (registration is Admin-UI or ``config.yaml`` only), and
its catalog object has no canvas snapshot, no per-entry owner, and no
pending/approved/rejected review state. So a provider declares what it backs via
:func:`RegistryProvider.capabilities`, and the router answers **501 with a
specific message** for the rest. A silent fallback would look like it worked and
then quietly diverge, which is worse than a refusal that names the reason.

Provenance, not a blanket refusal
---------------------------------
``RegistryCapabilities.read_only_sources`` is per-ENTRY rather than per-operation
because the LiteLLM catalog is genuinely mixed: rows projected from LiteLLM are
read-only, while platform-published rows (which carry the canvas snapshot LiteLLM
has no counterpart for) remain fully mutable and reviewable in the sidecar. A
wholesale "approve/reject is 501 under LiteLLM" would have stranded every sidecar
row at ``pending`` — permanently invisible to non-owners — which is a worse
outcome than the refusal was trying to prevent.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from app.services.registry_store import RegistryEntry

# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

# RegistryEntry.source values. "platform" is the default and covers every row
# that predates this seam, so existing DynamoDB items deserialize as mutable.
SOURCE_PLATFORM = "platform"
SOURCE_LITELLM = "litellm"


class UnsupportedRegistryOperation(RuntimeError):
    """The active registry backend cannot perform this operation.

    Carries an operator-facing *detail* explaining WHY and what to do instead;
    the router surfaces it verbatim as an HTTP 501 body. Never a generic
    "unsupported" — the point of this exception is to replace a silent
    degradation with an actionable one.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class RegistryCapabilities(BaseModel):
    """What the active registry backend can actually do.

    Every flag defaults to True so a new provider is assumed complete and must
    opt OUT explicitly — a provider author who forgets to declare a gap gets the
    loud failure at runtime rather than a silently half-working surface.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    # Human-readable name of the authoritative catalog, for the UI's provenance
    # badge and for error messages that need to name where the truth lives.
    authoritative_catalog: str
    supports_publish: bool = True
    supports_update: bool = True
    supports_delete: bool = True
    supports_review: bool = True
    supports_clone: bool = True
    # Entries whose ``source`` is listed here are projections of an EXTERNAL
    # catalog: read-only through this API, and not reviewable here because the
    # external system owns their approval state.
    read_only_sources: tuple[str, ...] = ()
    # Shown in the UI so an operator can see the split without reading docs.
    notes: str = ""

    def is_read_only(self, entry: RegistryEntry) -> bool:
        return getattr(entry, "source", SOURCE_PLATFORM) in self.read_only_sources


@runtime_checkable
class RegistryProvider(Protocol):
    """The nine methods ``routers/registry.py`` calls, plus capabilities.

    Signatures are identical to :class:`app.services.registry_store.RegistryStore`
    on purpose — see the module docstring.
    """

    def capabilities(self) -> RegistryCapabilities: ...

    # -- writes ----------------------------------------------------------
    def put(self, entry: RegistryEntry) -> RegistryEntry: ...
    def update(self, org_id: str, agent_slug: str, updates: dict) -> RegistryEntry | None: ...
    def delete(self, org_id: str, agent_slug: str) -> bool: ...
    def increment_usage(self, org_id: str, agent_slug: str) -> None: ...

    # -- reads -----------------------------------------------------------
    def get(self, org_id: str, agent_slug: str) -> RegistryEntry | None: ...
    def list_for_org(self, org_id: str) -> list[RegistryEntry]: ...
    def list_pending(self, org_id: str) -> list[RegistryEntry]: ...
    def list_for_owner(self, owner_sub: str) -> list[RegistryEntry]: ...
    def list_public(self) -> list[RegistryEntry]: ...

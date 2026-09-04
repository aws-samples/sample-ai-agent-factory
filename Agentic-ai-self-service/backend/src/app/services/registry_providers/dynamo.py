"""The default registry provider: the platform's own DynamoDB catalog.

A pure pass-through to the pre-existing :class:`RegistryStore`. No behavior
change, no data migration, no new table. When the registry provider setting is
``dynamodb`` — which it is unless an admin explicitly changes it — every call the
router makes lands on exactly the same store method it landed on before this
package existed.

The one thing that is NOT obvious here: ``get_registry_store()`` is resolved
INSIDE every method, never captured in ``__init__``. That is deliberate and
load-bearing. ``registry_store`` keeps a module-level lazy singleton
(``_registry_store``) and the ~30 RBAC/store tests install their moto-backed
store by assigning to it directly. Capturing the store at construction time would
snapshot whichever instance existed first and silently ignore that assignment,
breaking those tests — and, in production, would pin a client built before a
region/table env change. Resolving per call costs one dict lookup.
"""

from __future__ import annotations

from app.services.registry_providers.base import (
    SOURCE_PLATFORM,
    RegistryCapabilities,
)
from app.services.registry_store import RegistryEntry, get_registry_store

_CAPABILITIES = RegistryCapabilities(
    provider="dynamodb",
    authoritative_catalog="Platform registry (DynamoDB)",
    # Everything is supported; nothing is read-only. Spelled out rather than left
    # to the defaults so a future edit to the defaults can't quietly change the
    # behavior of the DEFAULT provider.
    supports_publish=True,
    supports_update=True,
    supports_delete=True,
    supports_review=True,
    supports_clone=True,
    read_only_sources=(),
    notes="",
)


class DynamoRegistryProvider:
    """``RegistryProvider`` over the platform's AgentRegistry DynamoDB table."""

    def capabilities(self) -> RegistryCapabilities:
        return _CAPABILITIES

    # -- writes ----------------------------------------------------------

    def put(self, entry: RegistryEntry) -> RegistryEntry:
        # A platform publish is always platform-sourced, whatever the caller sent.
        entry.source = SOURCE_PLATFORM
        return get_registry_store().put(entry)

    def update(self, org_id: str, agent_slug: str, updates: dict) -> RegistryEntry | None:
        return get_registry_store().update(org_id, agent_slug, updates)

    def delete(self, org_id: str, agent_slug: str) -> bool:
        return get_registry_store().delete(org_id, agent_slug)

    def increment_usage(self, org_id: str, agent_slug: str) -> None:
        get_registry_store().increment_usage(org_id, agent_slug)

    # -- reads -----------------------------------------------------------

    def get(self, org_id: str, agent_slug: str) -> RegistryEntry | None:
        return get_registry_store().get(org_id, agent_slug)

    def list_for_org(self, org_id: str) -> list[RegistryEntry]:
        return get_registry_store().list_for_org(org_id)

    def list_pending(self, org_id: str) -> list[RegistryEntry]:
        return get_registry_store().list_pending(org_id)

    def list_for_owner(self, owner_sub: str) -> list[RegistryEntry]:
        return get_registry_store().list_for_owner(owner_sub)

    def list_public(self) -> list[RegistryEntry]:
        return get_registry_store().list_public()

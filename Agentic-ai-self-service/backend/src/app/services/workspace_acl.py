"""Pure ACL logic for Gap 2E team collaboration (workspaces + viewer/editor ACL).

The ACL is a small dict embedded on each ``WorkflowDefinition`` row::

    {
        "owner_sub": "<sub>",
        "editors": ["<sub>", ...],
        "viewers": ["<sub>", ...],
        "workspace_id": "<optional id>",
    }

Design constraints (deliberate):

* **No DynamoDB, no FastAPI, no boto3.** This module is pure data logic so it
  is trivially unit-testable with no AWS and no applied shared edits. The
  router (``routers/workspaces.py``) reads/writes the ACL through the existing
  ``get_workflow_storage()`` public API.
* **No import of ``models.workflow``.** ``WorkflowDefinition.acl`` is kept as a
  plain ``dict`` precisely to avoid a ``models -> services`` import cycle. The
  router parses that dict via :meth:`Acl.normalize` / ``Acl.model_validate``.

Role semantics (precedence: owner > editor > viewer):

* The **owner always implicitly views and edits** and is the only principal who
  may *manage* (mutate) the ACL.
* **editors** can view and edit (nodes/edges) but cannot manage the ACL.
* **viewers** can view only.
* A legacy/missing ACL (``acl is None`` or ``{}``) normalises to **owner-only**
  so pre-Gap-2E rows behave exactly as before (owner sees them; nobody else).

Per the design, this per-workflow ACL is the *authoritative* authz check.
``services/auth.py::get_caller_role`` reads ``cognito:groups`` for org-wide RBAC
but that is advisory only and must never bypass this ACL.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

VALID_ROLES = ("viewer", "editor")


class Acl(BaseModel):
    """Normalised access-control list embedded in a workflow row.

    ``owner_sub`` is the single source of truth for ownership. ``editors`` and
    ``viewers`` are disjoint sets of Cognito subs; a sub never appears in both
    (role precedence promotes/demotes it) and the owner never appears in either
    (the owner's access is implicit, not list-driven).
    """

    owner_sub: str | None = None
    editors: list[str] = Field(default_factory=list)
    viewers: list[str] = Field(default_factory=list)
    workspace_id: str | None = None

    @classmethod
    def normalize(cls, acl: dict | None, owner_sub: str | None = None) -> Acl:
        """Coerce a (possibly ``None`` / partial / legacy) acl dict into a valid Acl.

        Legacy rows store ``acl=None``; those normalise to an owner-only ACL so
        they behave exactly as pre-Gap-2E (owner-only) data.

        ``owner_sub`` (when supplied, e.g. the workflow's own ``owner_sub``
        column) wins over any ``owner_sub`` baked into the dict — the row-level
        owner is authoritative and the dict can never escalate a different sub
        to owner. The owner is also scrubbed out of editors/viewers, and
        editors take precedence over viewers for any duplicated sub.
        """
        data = dict(acl) if isinstance(acl, dict) else {}
        resolved_owner = owner_sub if owner_sub is not None else data.get("owner_sub")

        editors = _dedup_keep_order(data.get("editors") or [])
        viewers = _dedup_keep_order(data.get("viewers") or [])

        # Editor precedence: a sub listed as both editor and viewer is an editor.
        viewers = [s for s in viewers if s not in editors]
        # Owner is implicit — never carried in either list.
        if resolved_owner is not None:
            editors = [s for s in editors if s != resolved_owner]
            viewers = [s for s in viewers if s != resolved_owner]

        return cls(
            owner_sub=resolved_owner,
            editors=editors,
            viewers=viewers,
            workspace_id=data.get("workspace_id"),
        )

    def to_dict(self) -> dict:
        """Serialise back to the plain dict stored on ``WorkflowDefinition.acl``.

        ``workspace_id`` is omitted when unset so legacy rows stay compact.
        """
        out: dict = {
            "owner_sub": self.owner_sub,
            "editors": list(self.editors),
            "viewers": list(self.viewers),
        }
        if self.workspace_id is not None:
            out["workspace_id"] = self.workspace_id
        return out


def _dedup_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Permission checks (accept either a normalised Acl or a raw acl dict)
# ---------------------------------------------------------------------------


def _as_acl(acl, owner_sub: str | None = None) -> Acl:
    if isinstance(acl, Acl):
        if owner_sub is not None and acl.owner_sub != owner_sub:
            # Re-normalise so a caller-supplied owner_sub stays authoritative.
            return Acl.normalize(acl.to_dict(), owner_sub=owner_sub)
        return acl
    return Acl.normalize(acl, owner_sub=owner_sub)


def can_view(acl, caller_sub: str, owner_sub: str | None = None) -> bool:
    """True when *caller_sub* may view the workflow (owner/editor/viewer)."""
    a = _as_acl(acl, owner_sub)
    if a.owner_sub is None:
        return False  # legacy/un-owned row is invisible to everyone
    return caller_sub == a.owner_sub or caller_sub in a.editors or caller_sub in a.viewers


def can_edit(acl, caller_sub: str, owner_sub: str | None = None) -> bool:
    """True when *caller_sub* may edit the workflow (owner or editor only)."""
    a = _as_acl(acl, owner_sub)
    if a.owner_sub is None:
        return False
    return caller_sub == a.owner_sub or caller_sub in a.editors


def can_manage(acl, caller_sub: str, owner_sub: str | None = None) -> bool:
    """True only for the owner — the sole principal allowed to mutate the ACL.

    Editors must NOT be able to grant themselves owner or add members
    (escalation guard, Bug 122 class). ``can_manage`` is owner-only and the
    router gates every /share write on it (via ``assert_owner``).
    """
    a = _as_acl(acl, owner_sub)
    return a.owner_sub is not None and caller_sub == a.owner_sub


# ---------------------------------------------------------------------------
# Immutable mutators — return a new acl dict, never mutate the input
# ---------------------------------------------------------------------------


def add_member(acl, sub: str, role: str, owner_sub: str | None = None) -> dict:
    """Return a new acl dict with *sub* granted *role* (``viewer``|``editor``).

    Role precedence: adding a sub as ``editor`` promotes it out of viewers (and
    vice-versa demotes), so a sub is only ever in one list and never duplicated.
    Adding the owner is a no-op (owner access is implicit) — the owner can never
    be demoted to a lesser role via the ACL.

    Raises ``ValueError`` for an unknown role so the router can map it to 400.
    """
    if role not in VALID_ROLES:
        raise ValueError(f"Unknown role '{role}'. Expected one of {VALID_ROLES}.")

    a = _as_acl(acl, owner_sub)
    # Owner is implicit; granting the owner a role is a no-op.
    if a.owner_sub is not None and sub == a.owner_sub:
        return a.to_dict()

    editors = [s for s in a.editors if s != sub]
    viewers = [s for s in a.viewers if s != sub]
    if role == "editor":
        editors.append(sub)
    else:
        viewers.append(sub)

    return Acl(
        owner_sub=a.owner_sub,
        editors=editors,
        viewers=viewers,
        workspace_id=a.workspace_id,
    ).to_dict()


def remove_member(acl, sub: str, owner_sub: str | None = None) -> dict:
    """Return a new acl dict with *sub* removed from editors and viewers.

    Idempotent (removing an absent sub is a no-op) and never removes the owner
    (the owner is not stored in either list, so this is structurally safe).
    """
    a = _as_acl(acl, owner_sub)
    editors = [s for s in a.editors if s != sub]
    viewers = [s for s in a.viewers if s != sub]
    return Acl(
        owner_sub=a.owner_sub,
        editors=editors,
        viewers=viewers,
        workspace_id=a.workspace_id,
    ).to_dict()

"""The smallest request bodies ``POST /api/registry`` accepts.

``PublishRequest.canvas_snapshot`` is ``RegistryCanvasSnapshotV2``, not a free dict:
``schemaVersion``, ``name``, ``nodes``, ``edges``, ``viewport`` and ``governance`` are all
required. Tests written before that posted ``{"nodes": [], "edges": []}`` or even ``{}``,
which is now a 422 — so every assertion after the post was scoring a request the server had
already refused. Ten of them failed loudly, which was the good case. Two did not: the
private-visibility and non-owner controls asserted that another caller gets a 404, and with
nothing created they got that 404 for free. A privacy check passing against an empty table
is the failure mode this module exists to prevent.

Lives here rather than in ``conftest.py`` because the repo's convention for shared test
material is a named module next to it (see ``gateway_fakes.py``), and because importing
``conftest`` directly is not reliable under pytest's import modes.
"""

from __future__ import annotations


def registry_snapshot_body(name: str = "canvas", nodes: list | None = None, edges: list | None = None) -> dict:
    """A publish-ready ``canvas_snapshot``.

    Deliberately a hand-written literal rather than
    ``RegistryCanvasSnapshotV2(...).model_dump(by_alias=True)``. This has to mirror what a
    browser actually sends — ``createRegistryCanvasSnapshot`` in
    frontend/src/services/api/registry.ts — so that a new required field on the model breaks
    these tests loudly. Derived from the model it would track the model silently and hide
    exactly the frontend/backend drift worth catching.

    ``viewport`` and ``governance`` are ``{}`` because every field inside each has a default,
    so an empty object is a real value rather than a hole being papered over, and it keeps
    these tests about the registry rather than about governance defaults.

    The STORAGE model stays tolerant of the legacy shape for rows already written — see
    ``test_registry_store.test_store_put_get``, which constructs a ``RegistryEntry`` with a
    bare ``{"nodes": ..., "edges": ...}`` and must keep loading. Strict on input, tolerant on
    read, is the intended asymmetry; do not "fix" that one to match this.
    """
    return {
        "schemaVersion": 2,
        "name": name,
        "nodes": nodes if nodes is not None else [],
        "edges": edges if edges is not None else [],
        "viewport": {},
        "governance": {},
    }

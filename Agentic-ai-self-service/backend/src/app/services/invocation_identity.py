"""Tenant-bound identities for runtime invocations that use AgentCore Memory.

The generated memory-aware agent accepts ``session_id`` and ``actor_id`` in its
invoke payload.  Historically all three invoke surfaces omitted both values when
the caller started a new conversation, so the generated code fell back to
``session_id="default"`` and ``actor_id="user"``.  Every tenant and every click
of "New Session" therefore shared one memory stream.

The runtime and Memory APIs have different limits.  The installed AgentCore
service model currently requires:

* ``InvokeAgentRuntime.runtimeSessionId``: 33..256 characters
* ``CreateEvent.sessionId``: 1..100 characters
* ``CreateEvent.actorId``: 1..255 characters

An omitted session is generated inside the 33..100 intersection so the same
value can be sent to AgentCore routing and to the agent's Memory calls.  The
authenticated deployment owner is represented only by its existing
non-reversible 32-character hash; the raw Cognito subject never enters the
runtime payload or Memory records.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from app.services.resource_ownership import owner_sub_hash

_GENERATED_SESSION_MIN_LENGTH = 33
_MEMORY_SESSION_MAX_LENGTH = 100
_MEMORY_SESSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class InvocationIdentityError(RuntimeError):
    """A tenant-bound invocation identity could not be constructed safely."""


@dataclass(frozen=True)
class InvocationIdentity:
    """The two identities consumed by a memory-aware generated agent."""

    session_id: str
    actor_id: str
    generated_session: bool


def resolve_invocation_identity(
    requested_session_id: str | None,
    *,
    deployment_owner_sub: str | None,
    authenticated_caller_sub: str | None = None,
    token_factory: Callable[[], uuid.UUID] = uuid.uuid4,
) -> InvocationIdentity:
    """Return a caller-bound actor and a usable conversation session.

    ``deployment_owner_sub`` wins over ``authenticated_caller_sub``.  This is
    important for the AWS-IAM Function URL: its authenticated caller is an IAM
    principal, while the deployment record still carries the Cognito owner whose
    Memory namespace must remain stable.  ``authenticated_caller_sub`` is only a
    compatibility fallback for pre-tenancy records with no stored owner.

    A valid supplied session remains byte-for-byte unchanged. Existing clients
    use the session returned by the previous invocation, and rewriting it would
    split one conversation into two. Values outside the Runtime/Memory
    intersection are rejected rather than padded or truncated. Only an
    omitted/empty session is minted here.
    """

    owner = str(deployment_owner_sub or authenticated_caller_sub or "").strip()
    if not owner:
        raise InvocationIdentityError(
            "A deployment owner or authenticated caller is required to create a tenant-bound invocation identity."
        )

    actor_id = owner_sub_hash(owner)
    if requested_session_id not in (None, ""):
        if not isinstance(requested_session_id, str):
            raise InvocationIdentityError("The invocation session ID must be a string.")
        if not (_GENERATED_SESSION_MIN_LENGTH <= len(requested_session_id) <= _MEMORY_SESSION_MAX_LENGTH):
            raise InvocationIdentityError(
                "The invocation session ID must contain 33 to 100 characters when AgentCore Memory is enabled."
            )
        if _MEMORY_SESSION_PATTERN.fullmatch(requested_session_id) is None:
            raise InvocationIdentityError(
                "The invocation session ID must start with a letter or digit and contain only letters, digits, '-' or '_'."
            )
        return InvocationIdentity(
            session_id=requested_session_id,
            actor_id=actor_id,
            generated_session=False,
        )

    # 32-char owner hash + "-" + 32-char UUID hex = 65 characters.  That is
    # accepted by both runtimeSessionId (min 33) and Memory sessionId (max 100),
    # and uses only the common [A-Za-z0-9_-] alphabet.
    session_id = f"{actor_id}-{token_factory().hex}"
    if not (_GENERATED_SESSION_MIN_LENGTH <= len(session_id) <= _MEMORY_SESSION_MAX_LENGTH):
        raise InvocationIdentityError(
            "Generated invocation session is outside the AgentCore runtime/Memory length intersection."
        )

    return InvocationIdentity(
        session_id=session_id,
        actor_id=actor_id,
        generated_session=True,
    )


def memory_invocation_identity(
    deployment_state: dict,
    requested_session_id: str | None,
    caller_sub: str | None,
) -> InvocationIdentity | None:
    """The identity for one invoke of ``deployment_state``, or ``None`` without Memory.

    The single gate shared by ``POST /api/test-runtime``, ``/api/test-runtime-stream`` and
    the Function URL, so the three surfaces cannot drift apart the way their tenant checks
    once did (F-9). Keyed on the persisted ``memory_result.memory_id``: that is what the
    deployed agent was built with, whatever the caller claims. A runtime without Memory
    returns ``None`` and keeps its existing contract -- nothing is invented for it.

    An IAM principal (``iam:...``) is never used as the actor when the record has an
    owner: ``resolve_invocation_identity`` prefers the stored Cognito owner, so an
    operator testing through the Function URL reads the owner's namespace, not a new one.

    Raises :class:`InvocationIdentityError` with a caller-safe message; the caller must
    refuse the invoke rather than fall back to an unbound session.
    """
    memory_result = deployment_state.get("memory_result") or {}
    if not (isinstance(memory_result, dict) and memory_result.get("memory_id")):
        return None
    return resolve_invocation_identity(
        requested_session_id,
        deployment_owner_sub=deployment_state.get("user_id"),
        authenticated_caller_sub=caller_sub,
    )

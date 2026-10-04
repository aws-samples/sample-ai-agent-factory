"""One writer at a time on an existing gateway (F-66e).

``UpdateGateway`` is a full replace with no version check, so every change to an
existing gateway is a read-modify-write of the WHOLE gateway, authorizer included.
Two writers interleave badly: a redeploy that read ``allowedClients`` while a
deployment was still live, and sent its update after that deployment's teardown had
revoked its client, put the revoked client back -- after the teardown had read the
gateway back and proved it gone. The same holds for every other writer (the policy
attach, the promoter flip, the teardown detach): each re-sends the authorizer it read.

The gateway-name claim does not cover this. Its lease is keyed by name, and the
gateway step releases it once the manifest is recorded, before the policy step and
long before a teardown. So every writer holds this lock instead, keyed by gateway id,
from its read to the read-back that proves its update landed. ``GatewayLock.update``
is the only place the platform calls ``UpdateGateway``, and ``GatewayLock.delete`` the
only place it calls ``DeleteGateway`` (a test pins both), so a writer cannot update or
delete without holding the lock. Lock order is name claim, then this lock: nothing
takes a name claim while holding it.

A read-back of READY is not proof: GetGateway can return READY with the OLD
configuration right after an update (F-66d). So ``update`` waits for READY *and* the
caller's own predicate over the read-back. When that never holds, the update may still
land later, and the lock is deliberately NOT released: it expires, and the next writer
reads what actually happened instead of racing a request still in flight.

The key is region and gateway id, not account. Every writer must compute the same
key, and not every writer knows the target account without another call; gateway
ids are ``<name>-<10 random characters>``, so two accounts sharing one only makes a
writer wait, never lets two write at once.

The lock lives in the gateway-name claim table under its own ``gwlock#`` prefix, so it
needs no new table and cannot collide with a name claim; the grants are scoped to that
prefix. It carries no owner: tenancy is decided by each caller before it gets here.

A kept lock (an unconfirmed write) is never deleted by its holder, so every row also
carries the table's TTL attribute, ``gc_after``, a day past its lease. The TTL is a
collector only: takeover is decided by ``lock_expires_at``, and TTL deletes nothing
before ``gc_after``, so it can never free a lock that is still held.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from botocore.exceptions import ClientError

from app.services.gateway_name_claim import GC_GRACE_SECONDS, _conditional_failure, claims_from_env

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

#: Longer than any holder can run: the gateway step's 720s timeout is the longest.
LOCK_SECONDS = 900

#: How long a writer waits for another's update to finish before it gives up.
WAIT_SECONDS = 90

#: Read-backs, 5s apart, before an update counts as unconfirmed.
CONFIRM_ATTEMPTS = 24

_FAILED = frozenset({"FAILED", "UPDATE_UNSUCCESSFUL"})


class GatewayMutationBusy(RuntimeError):
    """Another writer holds the gateway, so nothing was read or sent."""


class GatewayUpdateUnconfirmed(RuntimeError):
    """An update was sent and never read back as applied; it may still land."""


class GatewayUpdateFailed(RuntimeError):
    """The gateway reported the update did not apply (FAILED or UPDATE_UNSUCCESSFUL)."""


class GatewayDeleteUnconfirmed(RuntimeError):
    """A delete was sent and its outcome is unknown; it may still be in progress."""


#: Error codes that mean the service refused the request outright, so nothing it
#: asked for is still in flight. Anything else -- a 5xx, throttling, a code not
#: listed here, a dropped connection -- may have been accepted.
_DEFINITIVE_REFUSALS = frozenset(
    {
        "AccessDeniedException",
        "ConflictException",
        "ExpiredTokenException",
        "ResourceNotFoundException",
        "ServiceQuotaExceededException",
        "UnrecognizedClientException",
        "ValidationException",
    }
)


def definitive_refusal(exc: BaseException) -> bool:
    """True when *exc* proves the service did not act on the request.

    An allowlist, not a denylist: a code nobody reviewed counts as "may have landed",
    so the lock is kept rather than handed to a writer racing a request still in flight.
    """
    if not isinstance(exc, ClientError):
        return False
    return exc.response.get("Error", {}).get("Code") in _DEFINITIVE_REFUSALS


def lock_key(region: str, gateway_id: str) -> str:
    if not (region and gateway_id):
        raise ValueError("a gateway mutation lock needs a region and a gateway id")
    return f"gwlock#{region}#{gateway_id}"


def _lock_table() -> Any:
    return claims_from_env()._table


class GatewayLock:
    """A held lock on one gateway: the only way to read it for a write, and to write it."""

    def __init__(self, ctrl: Any, gateway_id: str, sleep: Callable[[float], None]) -> None:
        self._ctrl = ctrl
        self.gateway_id = gateway_id
        self._sleep = sleep
        self.unconfirmed = False

    def _poll(self, done: Callable[[dict], bool], what: str, error: type[Exception]) -> dict:
        for attempt in range(CONFIRM_ATTEMPTS):
            detail = self._ctrl.get_gateway(gatewayIdentifier=self.gateway_id)
            status = detail.get("status")
            if status in _FAILED:
                raise GatewayUpdateFailed(f"gateway is {status}")
            if status == "READY" and done(detail):
                return detail
            if attempt < CONFIRM_ATTEMPTS - 1:
                self._sleep(5)
        raise error(f"gateway never read back READY {what}")

    def read(self) -> dict:
        """GetGateway once READY. An UPDATING gateway is an update still landing."""
        return self._poll(lambda _d: True, "to build on", RuntimeError)

    def update(self, request: dict, applied: Callable[[dict], bool]) -> dict:
        """Send *request*, then wait for READY with ``applied(read_back)`` true.

        A definitive refusal (``definitive_refusal``) propagates unchanged and the lock
        is released. Anything else (a 5xx, throttling, an unlisted code, a timeout, a
        dropped connection) may have landed, and so may an update that never reads
        back as applied: both raise ``GatewayUpdateUnconfirmed`` and keep the lock
        until it expires. FAILED or UPDATE_UNSUCCESSFUL is the service saying it did
        not apply: ``GatewayUpdateFailed``, and the lock is released.
        """
        try:
            self._ctrl.update_gateway(**request)
        except Exception as exc:
            if definitive_refusal(exc):
                raise
            self.unconfirmed = True
            raise GatewayUpdateUnconfirmed(f"UpdateGateway outcome unknown: {_code(exc)}") from exc
        self.unconfirmed = True
        try:
            detail = self._poll(applied, "with the update applied", GatewayUpdateUnconfirmed)
        except GatewayUpdateFailed:
            # The service's own verdict: nothing is still landing.
            self.unconfirmed = False
            raise
        self.unconfirmed = False
        return detail

    def delete(self, confirm: Callable[[], None], *, terminal: tuple[type[BaseException], ...] = ()) -> None:
        """Send DeleteGateway, then run *confirm*, the caller's proof of absence.

        The only place the platform calls ``DeleteGateway`` (a test pins that), so a
        delete cannot interleave with an update's read-modify-write: without the lock,
        an update computed before the delete could still be landing while the
        teardown reads the gateway gone.

        A definitive refusal propagates unchanged and releases the lock, so a caller's
        "still has targets" retry and its not-found handling see the ClientError they
        expect. Any other send failure raises ``GatewayDeleteUnconfirmed`` and keeps
        the lock until it expires. Once the delete is accepted, the lock is kept
        unless *confirm* returns or raises one of *terminal*, the service's own
        verdict (a ``*FAILED`` state) that nothing is still in progress.
        """
        try:
            self._ctrl.delete_gateway(gatewayIdentifier=self.gateway_id)
        except Exception as exc:
            if definitive_refusal(exc):
                raise
            self.unconfirmed = True
            raise GatewayDeleteUnconfirmed(f"DeleteGateway outcome unknown: {_code(exc)}") from exc
        self.unconfirmed = True
        try:
            confirm()
        except terminal:
            self.unconfirmed = False
            raise
        self.unconfirmed = False


def _code(exc: BaseException) -> str:
    """The error code or type only: a ClientError's message can echo the request."""
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code") or "ClientError")
    return type(exc).__name__


@contextmanager
def gateway_mutation_lock(
    ctrl: Any,
    region: str,
    gateway_id: str,
    *,
    table: Any = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    wait_seconds: float = WAIT_SECONDS,
) -> Iterator[GatewayLock]:
    """Hold the gateway's write lock for the body.

    Raises ``GatewayMutationBusy`` when another writer still holds it after
    *wait_seconds*, and propagates anything else, so a caller that cannot take the
    lock never writes. The release is conditional on still holding it, so a holder
    that outlived its lease cannot free the next holder's.
    """
    if table is None:
        table = _lock_table()
    # Looked up per call, not bound at definition, so a test patching time.sleep reaches it.
    clock = clock or time.time
    sleep = sleep or time.sleep
    key = lock_key(region, gateway_id)
    token = uuid.uuid4().hex
    deadline = clock() + wait_seconds
    while True:
        now = int(clock())
        try:
            table.put_item(
                Item={
                    "claim_key": key,
                    "lock_holder": token,
                    "lock_expires_at": now + LOCK_SECONDS,
                    "gc_after": now + LOCK_SECONDS + GC_GRACE_SECONDS,
                },
                ConditionExpression="attribute_not_exists(claim_key) OR lock_expires_at < :now",
                ExpressionAttributeValues={":now": now},
            )
            break
        except Exception as exc:
            if not _conditional_failure(exc):
                raise
            if clock() >= deadline:
                raise GatewayMutationBusy(
                    f"gateway {gateway_id} is being changed by another operation; retry once it finishes"
                ) from None
            sleep(3)
    held = GatewayLock(ctrl, gateway_id, sleep)
    try:
        yield held
    finally:
        if held.unconfirmed:
            logger.warning("Gateway %s: write unconfirmed, keeping its write lock until it expires", gateway_id)
        else:
            try:
                table.delete_item(
                    Key={"claim_key": key},
                    ConditionExpression="lock_holder = :t",
                    ExpressionAttributeValues={":t": token},
                )
            except Exception as exc:  # noqa: BLE001
                # Best-effort: an unreleased lock expires. Type only.
                logger.warning("Gateway mutation lock for %s not released: %s", gateway_id, type(exc).__name__)


def lambda_lock_key(region: str, function_name: str) -> str:
    """Lock row for one shared tool Lambda. Under the same ``gwlock#`` prefix as the gateway
    lock, so the existing lock-rows-only grants cover it and it can never collide with a
    name claim (claim keys start with an account id)."""
    if not (region and function_name):
        raise ValueError("a shared Lambda lock needs a region and a function name")
    return f"gwlock#lambda#{region}#{function_name}"


@contextmanager
def shared_lambda_lock(
    region: str,
    function_name: str,
    *,
    table: Any = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    wait_seconds: float = WAIT_SECONDS,
) -> Iterator[str]:
    """Hold the write lock of a SHARED tool Lambda for the body (F-7d).

    RevisionId fences one UpdateFunctionCode/Configuration against a concurrent change of
    the function's configuration; it does not serialize AddPermission / RemovePermission /
    the refcount read / DeleteFunction / the role release, which are separate calls on a
    resource policy the fence does not cover. So a deploy's authorize→create-or-update→
    AddPermission and a teardown's authorize→RemovePermission→refcount→delete→role release
    each run under this lock, and a teardown cannot delete the function between another
    deploy's authorization and its AddPermission. Yields the fence token. Raises
    ``GatewayMutationBusy`` when another writer still holds the lock after *wait_seconds*;
    a caller that cannot take the lock never writes.
    """
    if table is None:
        table = _lock_table()
    clock = clock or time.time
    sleep = sleep or time.sleep
    key = lambda_lock_key(region, function_name)
    token = uuid.uuid4().hex
    deadline = clock() + wait_seconds
    while True:
        now = int(clock())
        try:
            table.put_item(
                Item={
                    "claim_key": key,
                    "lock_holder": token,
                    "lock_expires_at": now + LOCK_SECONDS,
                    "gc_after": now + LOCK_SECONDS + GC_GRACE_SECONDS,
                },
                ConditionExpression="attribute_not_exists(claim_key) OR lock_expires_at < :now",
                ExpressionAttributeValues={":now": now},
            )
            break
        except Exception as exc:
            if not _conditional_failure(exc):
                raise
            if clock() >= deadline:
                raise GatewayMutationBusy(
                    f"shared tool Lambda {function_name} is being changed by another deploy or teardown; "
                    "retry once it finishes"
                ) from None
            sleep(3)
    try:
        yield token
    finally:
        try:
            table.delete_item(
                Key={"claim_key": key},
                ConditionExpression="lock_holder = :t",
                ExpressionAttributeValues={":t": token},
            )
        except Exception as exc:  # noqa: BLE001
            # Best-effort: an unreleased lock expires. Type only.
            logger.warning("Shared Lambda lock for %s not released: %s", function_name, type(exc).__name__)


def allowed_clients(detail: dict) -> list:
    return list(
        (((detail or {}).get("authorizerConfiguration") or {}).get("customJWTAuthorizer") or {}).get("allowedClients")
        or []
    )


def authorizer_is(authorizer: dict) -> Callable[[dict], bool]:
    """The read-back predicate for repointing at *authorizer*: same issuer, same clients."""
    want = (authorizer or {}).get("customJWTAuthorizer") or {}

    def _applied(detail: dict) -> bool:
        got = ((detail.get("authorizerConfiguration") or {}).get("customJWTAuthorizer")) or {}
        return got.get("discoveryUrl") == want.get("discoveryUrl") and set(allowed_clients(detail)) == set(
            want.get("allowedClients") or []
        )

    return _applied


def engine_is(arn: str, mode: str) -> Callable[[dict], bool]:
    """The read-back predicate for attaching engine *arn* in *mode*."""

    def _applied(detail: dict) -> bool:
        cfg = detail.get("policyEngineConfiguration") or {}
        return cfg.get("arn") == arn and cfg.get("mode") == mode

    return _applied


def engine_detached(detail: dict) -> bool:
    return not (detail.get("policyEngineConfiguration") or {}).get("arn")

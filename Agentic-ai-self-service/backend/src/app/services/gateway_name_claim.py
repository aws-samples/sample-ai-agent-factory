"""An atomic, persistent claim on a gateway name, taken before anything is created.

A gateway name is a shared namespace. Everything the gateway step creates is keyed on it:
the gateway itself, its ``AgentCoreGateway-<name>`` IAM role, and the ``agentcore-<name>``
resource server in the shared Cognito pool. The adoption pre-flight in ``deploy_gateway``
refuses a name another tenant's LIVE deployment is on, but it reads the manifest, and a
deployment that is still creating has no manifest row yet. Two concurrent deploys of one
name therefore both pass the pre-flight, both mint Cognito clients, and the one that
loses ``CreateGateway``'s race adopts the winner's gateway and repoints its authorizer.

The claim closes that window with a single conditional write:

* ``owner_sub`` is PERSISTENT. It is set by the first claim and never changed. A
  different owner is refused at every attempt, lease or no lease. The claim is erased
  only when the owner's final teardown succeeds.
* ``holder_deployment_id`` is a LEASE, the one deployment of that owner allowed to be
  mutating the name right now. It is released once the deployment's manifest names
  what it created, and it expires after ``HOLDER_LEASE_SECONDS``, so a hard-killed
  step blocks the owner's next deploy for minutes, not forever.

The key is ``(account, region, lower-cased name)``. Lower-casing can over-claim if the
service treats case as significant, and over-claiming is the safe direction.

Recovery. When every manifest write for a standing gateway failed, ``promote(recovery=...)``
leaves ``recovery_gateway_id``/``recovery_deployment_id`` on the durable claim and, in the
same transaction, adds the claim's key to the deployment's ``recovery#<id>`` pointer and
bumps its ``pointer_generation``. Both teardowns read the pointer and its claims by
strongly consistent, projected GetItem (the grant is conditioned on that projection),
never by an index that could lag. Nothing shrinks a pointer: once no claim it lists
still carries this deployment's evidence, ``reclaim_recovery_pointer`` sets ``gc_after``,
conditioned on an unchanged generation, and the table's TTL removes it. A teardown may
report "deleted" only once that mark landed or there is no pointer
(``unfinished_recovery``), since nothing retries a deleted deployment.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

from app.services.naming import MAX_IAM_ROLE_NAME, regional_iam_role_name

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CLAIM_TABLE_ENV = "GATEWAY_NAME_CLAIMS_TABLE_NAME"

#: Longer than the gateway step can live (a 720s Lambda timeout), so a lease never
#: expires under a holder that is still running.
HOLDER_LEASE_SECONDS = 900

#: How long after its lease a provisional claim is left for the table's TTL to collect.
#: Only garbage collection: an expired provisional claim is replaceable at once.
GC_GRACE_SECONDS = 86400


class GatewayNameClaimRefused(RuntimeError):
    """The name is claimed by another owner, or another deploy of this owner holds it."""


def claim_key(account: str, region: str, name: str) -> str:
    if not (account and region and name):
        raise ValueError("a gateway name claim needs an account, a region and a name")
    return f"{account}#{region}#{name.lower()}"


def _conditional_failure(exc: Exception) -> bool:
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") == (
        "ConditionalCheckFailedException"
    )


class GatewayNameClaims:
    """The claim table. ``table`` is a boto3 DynamoDB ``Table`` in the PLATFORM account.

    A claim is PROVISIONAL from the write that creates it until ``promote``: it carries
    ``provisional`` and a ``gc_after`` for the table's TTL, and once its lease has
    expired ANY owner may replace it, in one conditional write. Nothing about a
    provisional claim can outlive its lease, so no failure to clean one up (a throttled
    erase, a hard-killed Lambda, a response lost after the write) can squat a name.
    Only ``promote``, called once there is durable evidence that a gateway graph on
    the name exists, makes ``owner_sub`` persistent. The TTL is garbage collection
    only: correctness never waits for it.
    """

    def __init__(self, table: Any, *, clock: Callable[[], float] = time.time) -> None:
        self._table = table
        self._clock = clock

    def acquire(self, *, account: str, region: str, name: str, owner_sub: str, deployment_id: str, token: str) -> bool:
        """Take (or re-take) the name for the invocation holding *token*.

        *token* is the FENCE: unique per invocation, and the same across one
        invocation's own retries. A lease is never re-taken by *deployment_id*, which
        a Step Functions retry or a duplicate Lambda delivery reuses while the first
        invocation may still be running. *deployment_id* is recorded for recovery.

        True when the claim is PROVISIONAL and this invocation's to settle: new,
        replacing an expired provisional claim, or re-taking its own. False when it
        re-took a durable claim of its owner. Raises GatewayNameClaimRefused when
        another owner has a durable claim, or any other invocation holds a live lease.
        """
        if not owner_sub or not deployment_id or not token:
            # An empty owner would match every other empty owner.
            raise GatewayNameClaimRefused("a gateway name claim needs an owner, a deployment and a token")
        key = claim_key(account, region, name)
        now = int(self._clock())
        exp = now + HOLDER_LEASE_SECONDS
        # The lease is free: none (a durable claim released), expired, or this
        # invocation's own. Its own only under its own owner: a token is a fence, not
        # a credential, so presenting it never moves a live claim to another owner.
        free = (
            "(attribute_not_exists(holder_expires_at) OR holder_expires_at < :now "
            "OR (holder_token = :tok AND owner_sub = :owner))"
        )
        ours = "owner_sub = :owner AND " + free
        values = {":owner": owner_sub, ":dep": deployment_id, ":tok": token, ":exp": exp, ":now": now}
        try:
            self._table.update_item(
                Key={"claim_key": key},
                UpdateExpression=(
                    "SET owner_sub = :owner, holder_deployment_id = :dep, holder_token = :tok, "
                    "holder_expires_at = :exp, provisional = :t, gc_after = :gc, "
                    "created_at = if_not_exists(created_at, :now)"
                ),
                # A provisional claim whose lease is free is anyone's: it proves nothing.
                ConditionExpression="attribute_not_exists(claim_key) OR (provisional = :t AND " + free + ")",
                ExpressionAttributeValues={**values, ":t": True, ":gc": exp + GC_GRACE_SECONDS},
            )
            return True
        except Exception as exc:
            if not _conditional_failure(exc):
                raise
        try:
            self._table.update_item(
                Key={"claim_key": key},
                UpdateExpression="SET holder_deployment_id = :dep, holder_token = :tok, holder_expires_at = :exp",
                ConditionExpression="attribute_exists(claim_key) AND attribute_not_exists(provisional) AND " + ours,
                ExpressionAttributeValues=values,
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
            return False
        except Exception as exc:
            if not _conditional_failure(exc):
                raise
            held = exc.response.get("Item") or {}
            # The low-level shape ({"S": ...}) from the client; the resource layer
            # does not deserialize an error's Item either, but accept both.
            held_owner = held.get("owner_sub")
            if isinstance(held_owner, dict):
                held_owner = held_owner.get("S")
            if held_owner is not None and held_owner != owner_sub and "provisional" not in held:
                raise GatewayNameClaimRefused(
                    f"Gateway name '{name}' is already in use by another owner's deployment in this "
                    "account and region. Choose a different gateway name."
                ) from None
            if held_owner is not None and held_owner != owner_sub:
                raise GatewayNameClaimRefused(
                    f"Gateway name '{name}' is being deployed or torn down by another owner right now. "
                    "Retry in a few minutes."
                ) from None
            raise GatewayNameClaimRefused(
                f"Gateway name '{name}' is being deployed by another of your deployments right now. "
                "Retry once it finishes."
            ) from None

    def promote(
        self,
        *,
        account: str,
        region: str,
        name: str,
        owner_sub: str,
        token: str,
        recovery: dict[str, str] | None = None,
    ) -> bool:
        """Make *token*'s provisional claim durable and drop its lease, in one write.
        False if it no longer holds it (replaced after expiry, or never took it), and
        False once its lease has expired even if nobody replaced it yet: from then on
        any owner may take the name, so a stale invocation cannot make it durable.

        *recovery* is written onto the claim when the gateway it keeps is recorded
        nowhere else (a manifest write that kept failing): the claim is then the
        handle, and it names the gateway and the deployment that left it. The claim
        key is added to that deployment's recovery pointer (``recovery_pointer_key``) in
        the SAME transaction, so a teardown finds the claim by a strongly consistent
        read of a key it knows, and evidence no pointer lists cannot exist. A failed
        transaction writes neither and raises; only the claim's own condition is False.
        """
        values: dict[str, Any] = {":t": True, ":owner": owner_sub, ":tok": token, ":now": int(self._clock())}
        update = "REMOVE provisional, gc_after, holder_deployment_id, holder_token, holder_expires_at"
        condition = "provisional = :t AND owner_sub = :owner AND holder_token = :tok AND holder_expires_at >= :now"
        key = claim_key(account, region, name)
        if recovery:
            if not recovery.get("recovery_deployment_id") or not recovery.get("recovery_gateway_id"):
                raise ValueError("recovery evidence names a gateway and the deployment that left it")
            sets = []
            for i, (attr, value) in enumerate(sorted(recovery.items())):
                if not re.fullmatch(r"recovery_[a-z_]+", attr):
                    raise ValueError(f"not a recovery attribute: {attr}")
                sets.append(f"{attr} = :r{i}")
                values[f":r{i}"] = str(value)
            update = "SET " + ", ".join(sets) + " " + update
            return self._promote_with_pointer(key, recovery["recovery_deployment_id"], update, condition, values)
        try:
            self._table.update_item(
                Key={"claim_key": key},
                UpdateExpression=update,
                ConditionExpression=condition,
                ExpressionAttributeValues=values,
            )
        except Exception as exc:
            if not _conditional_failure(exc):
                raise
            return False
        return True

    def _promote_with_pointer(
        self, key: str, deployment_id: str, update: str, condition: str, values: dict[str, Any]
    ) -> bool:
        """The claim's promotion and its pointer entry, in one TransactWriteItems.

        Through the resource's own client, which serializes plain values as the Table
        does: typed values here would be wrapped a second time."""
        table_name = self._table.name
        try:
            self._table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": table_name,
                            "Key": {"claim_key": key},
                            "UpdateExpression": update,
                            "ConditionExpression": condition,
                            "ExpressionAttributeValues": values,
                        }
                    },
                    {
                        "Update": {
                            "TableName": table_name,
                            "Key": {"claim_key": recovery_pointer_key(deployment_id)},
                            # A new generation, and no TTL: see reclaim_recovery_pointer.
                            "UpdateExpression": "ADD claim_keys :k, pointer_generation :one REMOVE gc_after",
                            "ExpressionAttributeValues": {":k": {key}, ":one": 1},
                        }
                    },
                ]
            )
        except ClientError as exc:
            reasons = exc.response.get("CancellationReasons") or []
            if (
                exc.response.get("Error", {}).get("Code") == "TransactionCanceledException"
                and reasons
                and reasons[0].get("Code") == "ConditionalCheckFailed"
            ):
                return False
            raise
        return True

    def abandon(self, *, account: str, region: str, name: str, owner_sub: str, token: str) -> bool:
        """Give up *token*'s provisional claim: expire its lease now, so any owner may
        replace it at once, and leave the item to the TTL. An UpdateItem, not a
        DeleteItem, so no caller needs to delete outside the lock prefix to settle."""
        now = int(self._clock())
        try:
            self._table.update_item(
                Key={"claim_key": claim_key(account, region, name)},
                UpdateExpression="SET holder_expires_at = :gone, gc_after = :now",
                ConditionExpression="provisional = :t AND owner_sub = :owner AND holder_token = :tok",
                ExpressionAttributeValues={":t": True, ":owner": owner_sub, ":tok": token, ":gone": 0, ":now": now},
            )
        except Exception as exc:
            if not _conditional_failure(exc):
                raise
            return False
        return True

    def release(
        self, *, account: str, region: str, name: str, token: str, recorded_gateways: tuple[str, ...] = ()
    ) -> bool:
        """Drop *token*'s lease on a DURABLE claim, keeping the owner. False if it no
        longer held it. A provisional claim is settled by promote or abandon.

        *recorded_gateways* are the gateways a manifest row DynamoDB acknowledged now
        names, passed only by the deploy that wrote those rows. Recovery evidence
        naming one of them is removed in the same write as the lease: the gateway has
        a durable record again, and the deployment that left the evidence must not
        tear down what a later deployment now records. A teardown never passes any.
        """
        key = {"claim_key": claim_key(account, region, name)}
        base = "REMOVE holder_deployment_id, holder_token, holder_expires_at"
        condition = "holder_token = :tok AND attribute_not_exists(provisional)"
        gateways = sorted({str(g) for g in recorded_gateways if g})
        attempts: list[tuple[str, str, dict[str, Any]]] = []
        if gateways:
            refs = {f":g{i}": g for i, g in enumerate(gateways)}
            attempts.append(
                (
                    base + ", recovery_gateway_id, recovery_deployment_id",
                    condition + f" AND recovery_gateway_id IN ({', '.join(refs)})",
                    refs,
                )
            )
        attempts.append((base, condition, {}))
        for update, cond, extra in attempts:
            try:
                self._table.update_item(
                    Key=key,
                    UpdateExpression=update,
                    ConditionExpression=cond,
                    ExpressionAttributeValues={":tok": token, **extra},
                )
            except Exception as exc:
                if not _conditional_failure(exc):
                    raise
                continue
            return True
        return False

    def erase(self, *, account: str, region: str, name: str, owner_sub: str, token: str | None = None) -> bool:
        """Delete the claim, once the owner's final teardown has succeeded.

        Refused (False) for another owner, and while any other invocation holds a live
        lease: a deploy of this owner that is creating on the name right now still
        needs it. *token* is the teardown's own lease, which does not block its own
        erase; it is one conditional write, so no deploy can take the name between a
        release and it.
        """
        if not owner_sub:
            return False
        free = "attribute_not_exists(holder_expires_at) OR holder_expires_at < :now"
        values: dict[str, Any] = {":owner": owner_sub, ":now": int(self._clock())}
        if token:
            free += " OR holder_token = :tok"
            values[":tok"] = token
        try:
            self._table.delete_item(
                Key={"claim_key": claim_key(account, region, name)},
                ConditionExpression=f"owner_sub = :owner AND ({free})",
                ExpressionAttributeValues=values,
            )
        except Exception as exc:
            if not _conditional_failure(exc):
                raise
            return False
        return True

    def settle_provisional(
        self,
        *,
        account: str,
        region: str,
        name: str,
        owner_sub: str,
        token: str,
        keep: bool,
        recovery: dict[str, str] | None = None,
    ) -> bool:
        """Promote (*keep*) or abandon a provisional claim this invocation took.

        Best-effort by design, and safe in both directions: a claim neither write
        reached stays provisional, and becomes replaceable when its lease expires.
        True only when the write landed.
        """
        where = {"account": account, "region": region, "name": name, "owner_sub": owner_sub, "token": token}
        try:
            if keep:
                if self.promote(**where, recovery=recovery):
                    return True
                logger.warning("Gateway name claim for %s was replaced before it could be kept", name)
                return False
            return self.abandon(**where)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Provisional gateway name claim for %s not settled (%s); it expires with its lease",
                name,
                type(exc).__name__,
            )
            return False


def claims_from_env(region: str | None = None) -> GatewayNameClaims:
    """The platform's claim table. Fails closed when the stack did not configure one."""
    table_name = os.environ.get(CLAIM_TABLE_ENV, "")
    if not table_name:
        raise RuntimeError(f"{CLAIM_TABLE_ENV} is not set; refusing to deploy a gateway without a name claim")
    import boto3

    region = region or os.environ.get("APP_AWS_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
    return GatewayNameClaims(boto3.resource("dynamodb", region_name=region).Table(table_name))


def _teardown_claims() -> GatewayNameClaims:
    # A seam for tests only: claims_from_env is imported by name elsewhere, so
    # patching it would leak into whichever module imported it under the patch.
    return claims_from_env()


#: The prefix of a deployment's recovery pointer. A claim key starts with an account id,
#: so no claim can collide with it.
RECOVERY_POINTER_PREFIX = "recovery#"
_GATEWAY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}")
#: Everything the recovery reader reads, and all its GetItem grant allows it to: never a
#: claim's lease fields, whose holder_token is the fence every claim write is made under.
RECOVERY_READ_ATTRIBUTES = (
    "claim_key",
    "claim_keys",
    "owner_sub",
    "pointer_generation",
    "provisional",
    "recovery_deployment_id",
    "recovery_gateway_id",
)
_RECOVERY_PROJECTION = {
    "ProjectionExpression": ", ".join(f"#a{i}" for i in range(len(RECOVERY_READ_ATTRIBUTES))),
    "ExpressionAttributeNames": {f"#a{i}": a for i, a in enumerate(RECOVERY_READ_ATTRIBUTES)},
}


def recovery_pointer_key(deployment_id: str) -> str:
    if not deployment_id:
        raise ValueError("a recovery pointer needs a deployment id")
    return RECOVERY_POINTER_PREFIX + deployment_id


def _read_projected(table: Any, key: str) -> dict | None:
    return table.get_item(Key={"claim_key": key}, ConsistentRead=True, **_RECOVERY_PROJECTION).get("Item")


def _pointer_keys(pointer: dict) -> set[str] | None:
    """The claim keys *pointer* lists, or None if it is malformed. It is only ever
    written by an ADD of one key, and never shrunk, so an empty set is corrupt too."""
    listed = pointer.get("claim_keys")
    if not isinstance(listed, (set, frozenset)) or not listed or not all(isinstance(k, str) and k for k in listed):
        return None
    return set(listed)


def recovered_gateway_rows(
    *,
    deployment_id: str,
    owner_sub: str | None,
    account_for: Callable[[], str],
    region: str,
    claims: GatewayNameClaims | None = None,
) -> list[dict]:
    """Gateway rows for the gateways *deployment_id* left recorded only on a name claim.

    ``promote(recovery=...)`` writes ``recovery_gateway_id`` and ``recovery_deployment_id``
    onto a claim when the manifest row naming a standing gateway kept failing; this
    reads them back, so a teardown can reach a gateway no manifest row or
    ``gateway_result`` names. Each row is inventory, not authority: the name hold still
    proves the gateway ours and its live name the name held before anything deletes it.

    Every read is a strongly consistent GetItem of a key this deployment knows: its
    pointer (``recovery_pointer_key``), then each claim the pointer lists, projected to
    RECOVERY_READ_ATTRIBUTES. promote adds the claim to the pointer in the same
    transaction that writes the evidence, and nothing removes a pointer entry, so every
    claim carrying this deployment's evidence is listed, and an empty answer is
    absence, not lag. A pointer is removed only by the table's TTL, and only once
    reclaim_recovery_pointer has proven it lists no such claim. A listed claim that no longer carries this
    deployment's evidence (erased, cleared by a later deployment that recorded the
    gateway, or never promoted) is skipped: the evidence is the claim's, never the
    pointer's.

    A claim that does carry it must be durable, of *owner_sub*, keyed in this
    deployment's target (*account_for()*, called only if something is found, and
    *region*), and name a gateway id. Anything else is corrupt or foreign evidence
    about a gateway only the claim may know of, and raises GatewayNameClaimRefused
    before anything is deleted: skipping it would let the teardown succeed, drop the
    deployment's record, and orphan that gateway. A read that fails raises too: a
    teardown that cannot tell whether a gateway is recorded only here must not decide
    it is not.
    """
    if not deployment_id:
        return []
    table = (claims if claims is not None else _teardown_claims())._table

    def refuse(why: str) -> GatewayNameClaimRefused:
        # No claim key, owner or id in the text: it is shown to the deployment's user.
        logger.error("Recovery claim for %s refused: %s", deployment_id, why)
        return GatewayNameClaimRefused(
            f"A gateway name claim recording this deployment's gateway {why}; nothing was deleted."
        )

    pointer = _read_projected(table, recovery_pointer_key(deployment_id))
    if pointer is None:
        return []
    listed = _pointer_keys(pointer)
    if listed is None:
        raise refuse("is listed by a malformed recovery pointer")
    items: list[dict] = []
    for key in sorted(listed):
        item = _read_projected(table, key)
        if item and item.get("recovery_deployment_id") == deployment_id:
            items.append(item)

    rows: list[dict] = []
    account = None
    for item in items:
        parts = str(item.get("claim_key") or "").split("#")
        gateway_id = str(item.get("recovery_gateway_id") or "")
        if len(parts) != 3 or not all(parts) or "provisional" in item:
            raise refuse("is not a durable gateway name claim")
        if not owner_sub or item.get("owner_sub") != owner_sub:
            raise refuse("belongs to another owner")
        if not _GATEWAY_ID.fullmatch(gateway_id):
            raise refuse("names no valid gateway id")
        account = account or str(account_for())
        if parts[0] != account or parts[1] != region:
            raise refuse("is keyed outside this deployment's target account and region")
        logger.warning(
            "Gateway %s of %s is recorded only on its name claim; tearing it down", gateway_id, deployment_id
        )
        # Untagged: the gateway is the graph's root, never a member, and one tagged row
        # would turn a legacy manifest's every graph-capable row into an unrelated one.
        rows.append({"type": "gateway", "id": gateway_id, "name": parts[2], "region": region})
    return rows


#: What reclaim_recovery_pointer found. Only ABSENT and MARKED leave nothing of the
#: deployment's recovery evidence behind, so only they let a teardown call itself done.
POINTER_ABSENT = "absent"
POINTER_MARKED = "marked"
POINTER_ACTIVE = "active"
POINTER_RACED = "raced"
POINTER_MALFORMED = "malformed"


def reclaim_recovery_pointer(*, deployment_id: str, claims: GatewayNameClaims | None = None) -> str:
    """Let the TTL remove *deployment_id*'s recovery pointer once it lists no claim that
    still carries this deployment's evidence (erased, or cleared by a later deployment
    that recorded the gateway). Returns one of the POINTER_* outcomes; a read or write
    that fails raises.

    A read-then-delete would race a late promote: its transaction could land after the
    read, and an ADD of a key already listed changes nothing a delete could be
    conditioned on. So every promote bumps ``pointer_generation`` and removes
    ``gc_after`` in the transaction that writes the evidence, and this marks the
    pointer only if the generation it read is unchanged. A promote before the mark
    fails it (RACED); one after unmarks the pointer, and one after the TTL removed it
    writes a new one. Either way a pointer listing live evidence is never left to
    expire.

    Decided from strongly consistent reads of the claims alone, so it is safe from any
    teardown whatever it deleted. Needs only UpdateItem, which every teardown already
    holds for its name hold.
    """
    if not deployment_id:
        return POINTER_ABSENT
    table = (claims if claims is not None else _teardown_claims())._table
    clock = claims._clock if claims is not None else time.time
    key = recovery_pointer_key(deployment_id)
    pointer = _read_projected(table, key)
    if pointer is None:
        return POINTER_ABSENT
    listed, generation = _pointer_keys(pointer), pointer.get("pointer_generation")
    if listed is None or (
        generation is not None and (isinstance(generation, bool) or not isinstance(generation, (int, Decimal)))
    ):
        logger.error("Recovery pointer of %s is malformed; kept", deployment_id)
        return POINTER_MALFORMED
    for claim in sorted(listed):
        item = _read_projected(table, claim)
        if item and item.get("recovery_deployment_id") == deployment_id:
            return POINTER_ACTIVE
    # A pointer written before generations has none: the first promote gives it one.
    unchanged = (
        {"ConditionExpression": "pointer_generation = :g", "ExpressionAttributeValues": {":g": generation}}
        if generation is not None
        else {
            "ConditionExpression": "attribute_exists(claim_keys) AND attribute_not_exists(pointer_generation)",
            "ExpressionAttributeValues": {},
        }
    )
    try:
        table.update_item(
            Key={"claim_key": key},
            UpdateExpression="SET gc_after = :exp",
            ConditionExpression=unchanged["ConditionExpression"],
            ExpressionAttributeValues={
                **unchanged["ExpressionAttributeValues"],
                ":exp": int(clock()) + GC_GRACE_SECONDS,
            },
        )
    except Exception as exc:
        if not _conditional_failure(exc):
            raise
        logger.warning("Recovery pointer of %s gained evidence while it was checked; kept", deployment_id)
        return POINTER_RACED
    logger.info("Recovery pointer of %s lists no evidence; left to the TTL", deployment_id)
    return POINTER_MARKED


def unfinished_recovery(deployment_id: str, claims: GatewayNameClaims | None = None) -> str | None:
    """Why a teardown of *deployment_id* may not call itself complete, or None.

    Reclaims the recovery pointer first. A pointer still listing this deployment's
    evidence after a teardown is a gateway that teardown never saw (a promote that
    landed after it read), so "deleted" would orphan it; one that cannot be checked
    or marked would outlive the deployment's tombstone, and no later teardown could
    reach it. Either leaves the deployment retryable. Never raises; the text names an
    error code at most, as it is shown to the deployment's user.
    """
    try:
        outcome = reclaim_recovery_pointer(deployment_id=deployment_id, claims=claims)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Recovery pointer of %s could not be reclaimed: %s", deployment_id, type(exc).__name__)
        return f"Its gateway recovery record could not be checked ({type(exc).__name__}); delete again to finish."
    if outcome in (POINTER_ABSENT, POINTER_MARKED):
        return None
    if outcome == POINTER_MALFORMED:
        return "Its gateway recovery record is malformed; delete again once it is repaired."
    return "A gateway recorded only on its name claim appeared during cleanup; delete again to remove it."


def with_recovered_rows(rows: list[dict], recovered: list[dict]) -> list[dict]:
    """*rows* plus each recovered gateway row whose id no gateway row already names."""
    have = {str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("type") == "gateway"}
    return list(rows) + [r for r in recovered if r["id"] not in have]


def claim_account(target_account_id: str | None, sts_client: Any) -> str:
    """The account a gateway name lives in: the target's, else the platform's own."""
    if target_account_id:
        return str(target_account_id)
    return sts_client.get_caller_identity()["Account"]


class TeardownNameHold:
    """The names a teardown holds (F-66f), from before it decides anything to the end.

    A deploy that is still creating on a name has no manifest row, so the teardown's
    co-residency check cannot see it: the teardown found no live reference and deleted
    the gateway, its role and its resource server under a deploy that had already
    passed its pre-flight. The claim is the one record such a deploy has, so teardown
    takes the same lease BEFORE that check. Whoever writes first wins: a deploy holding
    it makes the teardown refuse with nothing deleted, and a teardown holding it makes
    the deploy refuse before its first Cognito or IAM call.

    Each hold is fenced by its own token, never by the deployment id: a gateway step
    of the same deployment that is still running, or a second delete of it, holds a
    lease this teardown must wait out, not take over. A step that failed settles its
    own claim first, so its lease does not linger; one hard-killed before it could
    blocks the cleanup until the lease expires, and the delete can be retried then.
    """

    def __init__(
        self, claims: GatewayNameClaims, *, owner_sub: str, deployment_id: str, token: str | None = None
    ) -> None:
        self._claims = claims
        self._owner = owner_sub
        self._holder = deployment_id
        # This teardown's own fence: never the deployment id, which the gateway step
        # of the same deployment, or a second delete of it, would share.
        self._token = token or uuid.uuid4().hex
        self.held: list[tuple[str, str, str]] = []
        self._created: set[tuple[str, str, str]] = set()
        self._proven: set[tuple[str, str, str]] = set()
        #: key -> the gateway kept by the claim alone, written onto it at settle.
        self._recovery: dict[tuple[str, str, str], str] = {}
        #: gateway id -> the key held for it, so a hand-off can name what it left.
        self.gateway_keys: dict[str, tuple[str, str, str]] = {}

    def take(self, keys: list[tuple[str, str, str]]) -> None:
        if keys and not (self._owner and self._holder):
            raise GatewayNameClaimRefused(
                "This deployment has no owner on record, so its gateway name cannot be held; nothing was deleted."
            )
        for account, region, name in keys:
            try:
                created = self._claims.acquire(
                    account=account,
                    region=region,
                    name=name,
                    owner_sub=self._owner,
                    deployment_id=self._holder,
                    token=self._token,
                )
            except GatewayNameClaimRefused as exc:
                self._roll_back()
                other_owner = "another owner" in str(exc)
                raise GatewayNameClaimRefused(
                    f"Gateway name '{name}' is "
                    + ("claimed by another owner" if other_owner else "held by another of your deployments right now")
                    + "; nothing was deleted. Retry the delete once it finishes."
                ) from None
            except Exception:
                self._roll_back()
                raise
            self.held.append((account, region, name))
            if created:
                self._created.add((account, region, name))

    def promote(self, key: tuple[str, str, str]) -> None:
        """*key*'s gateway was proven ours live: a claim created for it may be kept."""
        self._proven.add(key)

    def only_durable(self, gateway_ids: set[str], *, standing: set[str] = frozenset()) -> None:
        """Keep a claim only for a gateway a durable row names (*gateway_ids*), or one
        still *standing* after this teardown that no row names.

        A claim kept on inventory held only in memory would outlive every record a
        later DELETE could erase it by, so a gateway that is gone keeps no claim. One
        that still stands is different: dropping its claim would hand its name, and
        the next deploy's adoption of the gateway itself, to whichever owner deploys
        on it first. That claim is kept, and settle writes the gateway's id and the
        deployment onto it, so the claim is itself the handle.
        """
        for gateway_id, key in self.gateway_keys.items():
            if gateway_id in gateway_ids:
                continue
            if gateway_id in standing and key in self._proven:
                self._recovery[key] = gateway_id
            else:
                self._proven.discard(key)

    def _roll_back(self) -> None:
        """Undo a take that did not complete. Nothing was deleted, and nothing was
        proven yet, so settle gives up every claim this take created."""
        self._proven = set()
        self._recovery = {}
        self.settle(erase=False)

    def settle(self, *, erase: bool, handed_off: list[str] | tuple[str, ...] = ()) -> None:
        """Drop every lease, erasing the claim too when *erase* (the whole graph is gone).

        A claim this teardown CREATED is provisional in the table (see
        ``GatewayNameClaims``). It is made durable only when its gateway was proven
        ours live (``promote``) and part of the graph may still stand: the claim is
        then the one guard left on it. Otherwise it is abandoned: a stale or foreign
        row must not leave an owner on a name its real owner then cannot deploy. A
        claim that already existed (taken only because its owner is ours) is kept
        unless *erase*.

        *handed_off* names the gateways left in place for another deployment that still
        uses them. That deployment may be another owner's (a pre-claim adoption), and a
        claim created here would give the departing owner the survivor's name, so it is
        abandoned even when proven. That leaves the name exactly as unclaimed as before
        the teardown: the live gateway and the survivor's manifest row still keep other
        owners off it, and the survivor's next deploy seeds its own owner. A claim that
        already existed is only released. An unrecognized id counts every claim created
        here as handed off, the direction that never assigns a name.

        Best-effort by design, and safe whichever write is lost: a provisional claim
        expires with its lease, and a durable one only keeps other owners off a name
        this owner can still reuse.
        """
        held, self.held = self.held, []
        handed = {self.gateway_keys[g] for g in map(str, handed_off) if g in self.gateway_keys}
        if any(str(g) not in self.gateway_keys for g in handed_off):
            handed = set(self._created)
        keep = self._proven - handed if not erase else set()
        created, recovery = self._created, self._recovery
        self._created, self._proven, self._recovery = set(), set(), {}
        for account, region, name in held:
            key = (account, region, name)
            if key in created:
                evidence = None
                if key in keep and key in recovery:
                    evidence = {"recovery_gateway_id": recovery[key], "recovery_deployment_id": self._holder}
                    logger.error(
                        "Gateway %s is still standing and no manifest row names it; its name claim is the "
                        "only handle left (deployment %s)",
                        recovery[key],
                        self._holder,
                    )
                self._claims.settle_provisional(
                    account=account,
                    region=region,
                    name=name,
                    owner_sub=self._owner,
                    token=self._token,
                    keep=key in keep,
                    recovery=evidence,
                )
                continue
            if erase:
                try:
                    if self._claims.erase(
                        account=account, region=region, name=name, owner_sub=self._owner, token=self._token
                    ):
                        continue
                except Exception as exc:  # noqa: BLE001
                    # Fall through to the release: our own lease must not outlive the
                    # teardown and block its retry. If the erase did land and only its
                    # response was lost, the release finds nothing and changes nothing.
                    logger.warning("Gateway name claim for %s not erased: %s", name, type(exc).__name__)
            try:
                self._claims.release(account=account, region=region, name=name, token=self._token)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Gateway name claim for %s not released (%s); it frees when its lease expires",
                    name,
                    type(exc).__name__,
                )


_RESOURCE_SERVER_PREFIX = "agentcore-"
_GATEWAY_ROLE_PREFIX = "AgentCoreGateway-"
# naming.regional_iam_role_name's digest: 8 hex, then the optional region suffix.
_DIGESTED_ROLE = re.compile(r"-[0-9a-f]{8}(-[a-z]{2}(-[a-z]+)+-\d+)?$")


def _recorded_gateway_name(ctrl_for: Callable[[str], Any] | None, region: str, gateway_id: Any) -> str | None:
    """The name of an id-only gateway row: None when the gateway is confirmed gone."""
    if not gateway_id or ctrl_for is None:
        raise GatewayNameClaimRefused(
            f"Gateway {gateway_id} records no name, so it cannot be held against a deploy "
            "creating on it; nothing was deleted."
        )
    try:
        name = (ctrl_for(region).get_gateway(gatewayIdentifier=gateway_id) or {}).get("name")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ResourceNotFoundException":
            return None
        raise GatewayNameClaimRefused(
            f"Gateway {gateway_id} records no name and could not be read ({code}); nothing was deleted."
        ) from None
    except Exception as exc:  # noqa: BLE001 - any unknown fails closed
        raise GatewayNameClaimRefused(
            f"Gateway {gateway_id} records no name and could not be read ({type(exc).__name__}); nothing was deleted."
        ) from None
    if not name:
        raise GatewayNameClaimRefused(f"Gateway {gateway_id} reports no name; nothing was deleted.")
    return str(name)


def _gateway_names_for_role(role: str, region: str, known: set[str]) -> list[str]:
    """Every gateway name whose role, in *region*, is spelled *role*.

    The role is ``regional_iam_role_name(AgentCoreGateway-<name>, region)``, or, for
    a name another row gave, the unsuffixed spelling builds used in every region
    before that. A name too long for IAM was cut and digested; the cut spelling is a
    fixed point of that function too, so it would "resolve" to a name nobody deploys.
    Such a role resolves only against a name another row gave, else to nothing.
    """

    def spells(n: str) -> bool:
        return regional_iam_role_name(_GATEWAY_ROLE_PREFIX + n, region) == role or (
            n in known and role == _GATEWAY_ROLE_PREFIX + n
        )

    names = {n for n in known if spells(n)}
    if names:
        return sorted(names)
    if len(role) >= MAX_IAM_ROLE_NAME - 1 and _DIGESTED_ROLE.search(role):
        return []
    stem = role[len(_GATEWAY_ROLE_PREFIX) :]
    return sorted(n for n in {stem, stem.removesuffix(f"-{region}")} if n and spells(n))


def hold_gateway_names_for_teardown(
    rows: list[dict],
    *,
    owner_sub: str | None,
    deployment_id: str,
    default_account: str | None,
    default_region: str,
    sts_client_for: Callable[[], Any],
    ctrl_for: Callable[[str], Any] | None = None,
    prove_owned: Callable[[str, str], Any] | None = None,
    fallback: dict | None = None,
    claims: GatewayNameClaims | None = None,
    token: str | None = None,
) -> TeardownNameHold:
    """Hold every gateway name *rows* would delete something under, or raise with nothing held.

    A name comes from a gateway row, or from a row coupled to the name: the
    ``agentcore-<name>`` resource server and the ``AgentCoreGateway-<name>`` role are
    deleted even when the gateway is already gone, and a deploy creating on the name
    would adopt them. A gateway row recorded before rows carried the name takes it
    from *fallback* (the deployment's ``gateway_result``) when that names the same
    id, else from GetGateway through *ctrl_for(region)*. A gateway confirmed absent
    holds nothing by itself; any other unknown raises, because deleting unheld is
    exactly the race. A deployment with nothing name-coupled holds nothing and needs
    no table. *sts_client_for* is called only when a row names no account.

    Every gateway row is then proven under the lease by *prove_owned(region,
    gateway_id)*, the dispatchers' own live ownership check, which returns the live
    GetGateway detail. Its live name must be the name held, else the whole hold is
    refused. A claim this hold created is provisional until that proof passes; see
    ``TeardownNameHold.settle``.

    *token* is a lease handed over by the failed step whose cleanup this is (see
    ``failure_inventory.TOKEN_MARKER``); any other teardown fences with its own.
    """
    fallback = fallback or {}
    # (account, region, name): a gateway row names where the gateway lives. A coupled
    # row does not: a resource server's region and account are the shared POOL's, and
    # a role is global. Their name is keyed where the deployment's gateway would live.
    found: list[tuple[Any, str, str]] = []
    gateways: list[tuple[int, str, str]] = []  # (index into found, region, gateway id)
    recorded_accounts: list[tuple[int, str, str]] = []  # (index into found, row account, gateway id)
    target = (default_account, default_region)
    for row in rows:
        if row.get("type") == "gateway":
            region = str(row.get("region") or default_region)
            name = row.get("name")
            if not name and row.get("id") and fallback.get("gateway_id") == row.get("id"):
                name = fallback.get("gateway_name") or fallback.get("name")
            if not name:
                name = _recorded_gateway_name(ctrl_for, region, row.get("id"))
            if name:
                # The account is the deployment's target, the session every proof and
                # delete reads through. A row's own account is inventory, not authority.
                found.append((default_account, region, str(name)))
                gateways.append((len(found) - 1, region, str(row.get("id") or "")))
                if row.get("account"):
                    recorded_accounts.append((len(found) - 1, str(row["account"]), str(row.get("id") or "")))
        elif row.get("type") == "cognito_resource_server":
            rs_id = str(row.get("id") or "")
            if rs_id.startswith(_RESOURCE_SERVER_PREFIX) and len(rs_id) > len(_RESOURCE_SERVER_PREFIX):
                found.append((*target, rs_id[len(_RESOURCE_SERVER_PREFIX) :]))
    known = {n for _a, _r, n in found}
    for row in rows:
        role = str(row.get("name") or row.get("id") or "")
        if row.get("type") != "iam_role" or not role.startswith(_GATEWAY_ROLE_PREFIX):
            continue
        names = _gateway_names_for_role(role, default_region, known)
        if not names:
            raise GatewayNameClaimRefused(
                f"Gateway role {role} does not spell a gateway name this teardown can hold; nothing was deleted."
            )
        found.extend((*target, n) for n in names)

    keys: list[tuple[str, str, str]] = []
    resolved: list[tuple[str, str, str]] = []
    account = None
    for row_account, region, name in found:
        if not row_account:
            account = account or claim_account(None, sts_client_for())
            row_account = account
        key = (str(row_account), str(region), name)
        resolved.append(key)
        if key not in keys:
            keys.append(key)
    for index, recorded, gateway_id in recorded_accounts:
        if recorded != resolved[index][0]:
            raise GatewayNameClaimRefused(
                f"Gateway {gateway_id} is recorded in another account than this deployment's target; "
                "nothing was deleted."
            )
    hold = TeardownNameHold(
        claims if claims is not None else (_teardown_claims() if keys else GatewayNameClaims(None)),
        owner_sub=owner_sub or "",
        deployment_id=deployment_id,
        token=token,
    )
    hold.take(keys)
    hold.gateway_keys = {gateway_id: resolved[index] for index, _region, gateway_id in gateways if gateway_id}
    for index, region, gateway_id in gateways:
        if prove_owned is not None and gateway_id:
            _bind_live_name(hold, resolved[index], region, gateway_id, prove_owned)
    return hold


def _bind_live_name(
    hold: TeardownNameHold, key: tuple[str, str, str], region: str, gateway_id: str, prove_owned: Callable
) -> None:
    """Prove the gateway ours and that its LIVE name is the name held, or refuse.

    Ownership proves the id, not the name: a row recording another name would hold
    (and, if new, promote) a claim on a name that is not this gateway's, while the
    gateway's real name, the one a deploy would race on, went unheld. A gateway that
    is not ours, or already gone, is not deleted, so its claim merely stays provisional.
    """
    from app.services.resource_ownership import ResourceDeletionRefused

    try:
        detail = prove_owned(region, gateway_id)
    except ResourceDeletionRefused:
        logger.info("Gateway name claim for %s stays provisional: not proven ours", key[2])
        return
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ResourceNotFoundException":
            return
        hold._roll_back()
        raise GatewayNameClaimRefused(
            f"Gateway {gateway_id}'s ownership could not be read ({code}); nothing was deleted."
        ) from None
    except Exception as exc:  # noqa: BLE001 - any unknown fails closed
        hold._roll_back()
        raise GatewayNameClaimRefused(
            f"Gateway {gateway_id}'s ownership could not be read ({type(exc).__name__}); nothing was deleted."
        ) from None
    detail = detail if isinstance(detail, dict) else {}
    live = detail.get("name")
    arn_account = str(detail.get("gatewayArn") or "").split(":")[4:5]
    if arn_account and arn_account[0] and arn_account[0] != key[0]:
        hold._roll_back()
        raise GatewayNameClaimRefused(
            f"Gateway {gateway_id} lives in another account than this deployment's target; nothing was deleted."
        )
    if not live or str(live).lower() != key[2].lower():
        hold._roll_back()
        raise GatewayNameClaimRefused(
            f"Gateway {gateway_id} is live under a different name than its record gives; nothing was deleted."
        )
    if key in hold._created:
        hold.promote(key)

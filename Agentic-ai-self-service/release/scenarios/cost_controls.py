"""``cost.invocation_authorization`` — the three live budget controls on the deployed stack.

One cached journey proves, against ``acf-production``:

* **concurrent-transaction-reservations-stay-within-limit** — five concurrent invocations of a
  wrapper clone (which calls the deployed ``reserve_invocation_budget``/``settle_invocation_budget``
  directly) against a 2-unit agent budget: exactly two cross the dispatch boundary, three are denied
  429, and the atomic counter shows authorized == committed == 2 units, reserved == 0, with two
  COMMITTED reservation rows.
* **exhausted-budget-denies-before-dispatch** — a further invocation on the now-exhausted budget is
  denied 429 before any dispatch, writing no new reservation.
* **budget-store-outage-denies-before-dispatch** — a wrapper clone with ``BUDGET_TABLE_NAME``
  redirected to a proven-absent table denies 503 before any dispatch and writes no budget row.

Each scenario id verifies its facet from the cached journey; the journey deletes both clones, the
budget definition and all its ledger rows, and the synthetic user.
"""

from __future__ import annotations

import argparse
import base64
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from _durable import (
    BUDGET_UNIT_MICRO_USD,
    EXACT_ORACLES,
    WRAPPER_HANDLER,
    WRAPPER_MEMBER,
    WRAPPER_SOURCE,
    clone_deployment_lambda,
    ddb_delete,
    ddb_get,
    ddb_query,
    delete_clone,
    sha256_bytes,
    table_names,
)
from _live import (
    REQUIRED_USER_GROUPS,
    BearerSession,
    ObservedStack,
    RunBinding,
    ScenarioFailed,
    boto3_session,
    bound_deployment,
    family_journey,
    with_synthetic_user,
    write_json,
)

FAMILY = "cost-controls"
COST_GROUPS = REQUIRED_USER_GROUPS | {"g-admins-cost"}  # cost:read/write


def _token_sub(token: str) -> str:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    sub = str(json.loads(base64.urlsafe_b64decode(payload)).get("sub") or "")
    if not sub:
        raise ScenarioFailed("access token carries no sub claim")
    return sub


def _budget_rows(session: Any, owner_sub: str, key: str) -> list[dict[str, Any]]:
    rows = ddb_query(session, table_names()["budget"], expression="org_id = :org", values={":org": "default"})
    selected = []
    for row in rows:
        sk = str(row.get("sk") or "")
        direct = sk == f"BUDGET#agent#{key}" or sk.startswith(f"SPEND#agent#{key}#")
        reservation = row.get("owner_sub") == owner_sub and row.get("runtime_name") == key
        if direct or reservation:
            selected.append(row)
    return selected


def _reservations(session: Any, owner_sub: str, key: str) -> list[dict[str, Any]]:
    return [r for r in _budget_rows(session, owner_sub, key) if str(r.get("sk") or "").startswith("RESERVATION#")]


def _create_budget(http: Any, key: str, limit: float) -> None:
    response = http.post(
        f"{http.base_url}/api/cost/budgets",
        json={"scope": "agent", "key": key, "limit_usd": limit, "warn_pct": 100},
        headers={"X-Session-Id": f"cost-create-{key}"[:128]},
        timeout=60,
    )
    if response.status_code != 200:
        raise ScenarioFailed(f"budget create returned {response.status_code}: {response.text[:300]}")


def _invoke(client: Any, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.invoke(
        FunctionName=name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload, separators=(",", ":")).encode(),
    )
    stream = response.get("Payload")
    raw = stream.read() if hasattr(stream, "read") else stream
    if response.get("FunctionError"):
        raise ScenarioFailed(f"clone {name} returned FunctionError: {(raw or b'')[:300]!r}")
    return json.loads(raw or b"{}")


def _prove_concurrency(client: Any, session: Any, clone: str, owner_sub: str, key: str) -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="budget-gate") as pool:
        results = [
            f.result(timeout=120)
            for f in [
                pool.submit(_invoke, client, clone, {"ownerSub": owner_sub, "runtimeName": key, "holdSeconds": 2})
                for _ in range(5)
            ]
        ]
    successes = [r for r in results if r.get("statusCode") == 200]
    denials = [r for r in results if r.get("statusCode") == EXACT_ORACLES["budgetExhaustedStatus"]]
    if len(successes) != 2 or len(denials) != 3:
        raise ScenarioFailed(
            f"expected exactly 2 reservations / 3 denials, got {[r.get('statusCode') for r in results]}"
        )
    if any(r.get("dispatchBoundaryCrossed") is not True for r in successes):
        raise ScenarioFailed("an authorized reservation did not cross the dispatch boundary")
    if any(
        r.get("detail") != EXACT_ORACLES["budgetExhaustedDetail"] or r.get("dispatchBoundaryCrossed") is not False
        for r in denials
    ):
        raise ScenarioFailed("a concurrent exhausted-budget denial differed from the exact oracle")
    month = datetime.now(UTC).strftime("%Y-%m")
    counter = ddb_get(session, table_names()["budget"], {"org_id": "default", "sk": f"SPEND#agent#{key}#{month}"})
    reservations = _reservations(session, owner_sub, key)
    if (
        counter is None
        or int(counter.get("authorized_microusd", -1)) != 2 * BUDGET_UNIT_MICRO_USD
        or int(counter.get("committed_microusd", -1)) != 2 * BUDGET_UNIT_MICRO_USD
        or int(counter.get("reserved_microusd", -1)) != 0
        or len(reservations) != 2
        or any(row.get("state") != "COMMITTED" for row in reservations)
    ):
        raise ScenarioFailed("the atomic reservation ledger/counter oracle differed")
    return {
        "attempts": len(results),
        "authorized": len(successes),
        "denied": len(denials),
        "limitMicrousd": 2 * BUDGET_UNIT_MICRO_USD,
        "authorizedMicrousd": int(counter["authorized_microusd"]),
        "reservedMicrousd": int(counter["reserved_microusd"]),
        "reservationRows": len(reservations),
    }


def _prove_exhausted(client: Any, session: Any, clone: str, owner_sub: str, key: str) -> dict[str, Any]:
    exhausted = _invoke(client, clone, {"ownerSub": owner_sub, "runtimeName": key, "holdSeconds": 0})
    if (
        exhausted.get("statusCode") != EXACT_ORACLES["budgetExhaustedStatus"]
        or exhausted.get("detail") != EXACT_ORACLES["budgetExhaustedDetail"]
        or exhausted.get("dispatchBoundaryCrossed") is not False
        or len(_reservations(session, owner_sub, key)) != 2
    ):
        raise ScenarioFailed("post-exhaustion denial or no-partial-write oracle differed")
    return exhausted


def _prove_outage(client: Any, session: Any, clone: str, owner_sub: str, outage_key: str) -> dict[str, Any]:
    outage = _invoke(client, clone, {"ownerSub": owner_sub, "runtimeName": outage_key, "holdSeconds": 0})
    if (
        outage.get("statusCode") != EXACT_ORACLES["budgetOutageStatus"]
        or outage.get("detail") != EXACT_ORACLES["budgetOutageDetail"]
        or outage.get("dispatchBoundaryCrossed") is not False
        or _budget_rows(session, owner_sub, outage_key)
    ):
        raise ScenarioFailed("budget-store outage denial or no-dispatch oracle differed")
    return outage


def _cleanup(http: Any, session: Any, owner_sub: str, key: str) -> dict[str, Any]:
    budget = table_names()["budget"]
    if ddb_get(session, budget, {"org_id": "default", "sk": f"BUDGET#agent#{key}"}) is not None:
        http.delete(
            f"{http.base_url}/api/cost/budgets/agent/{key}",
            headers={"X-Session-Id": f"cleanup-{key}"[:128]},
            timeout=60,
        )
    removed = 0
    for row in _budget_rows(session, owner_sub, key):
        sk = str(row["sk"])
        if sk.startswith("RESERVATION#"):
            condition = "owner_sub = :owner AND runtime_name = :runtime AND reservation_id = :reservation"
            values = {":owner": owner_sub, ":runtime": key, ":reservation": row.get("reservation_id")}
        else:
            condition = "owner_sub = :owner"
            values = {":owner": owner_sub}
        ddb_delete(session, budget, {"org_id": "default", "sk": sk}, condition=condition, values=values)
        removed += 1
    return {"budget_rows_removed": removed}


def _journey(args: argparse.Namespace, binding: RunBinding, observed: ObservedStack, artifacts: Path) -> dict[str, Any]:
    base = observed.cloudfront_url
    token = uuid.uuid4().hex[:12]
    key = f"cost_{token}"
    outage_key = f"cost_outage_{token}"
    outcome: dict[str, Any] = {"region": binding.region, "run_token": token}
    session = boto3_session(binding.region)
    lam = session.client("lambda")
    with with_synthetic_user(observed, label="cost", groups=COST_GROUPS) as user:
        owner_sub = _token_sub(user.token)
        outcome["owner_sub_hash"] = sha256_bytes(owner_sub.encode())[:16]
        outcome["wrapper_source_sha256"] = sha256_bytes(WRAPPER_SOURCE.encode())
        http = BearerSession(user, base)
        clone = outage_clone = None
        try:
            _create_budget(http, key, 2.0)
            _assert_absent(session, f"acfpr-missing-budget-{token}")
            clone = clone_deployment_lambda(
                session,
                observed,
                run_id=token,
                commit=binding.commit,
                env_overrides={},
                suffix="budget",
                wrapper=(WRAPPER_MEMBER, WRAPPER_SOURCE),
                handler=WRAPPER_HANDLER,
            )
            outage_clone = clone_deployment_lambda(
                session,
                observed,
                run_id=token,
                commit=binding.commit,
                env_overrides={"BUDGET_TABLE_NAME": f"acfpr-missing-budget-{token}"},
                suffix="budgetout",
                wrapper=(WRAPPER_MEMBER, WRAPPER_SOURCE),
                handler=WRAPPER_HANDLER,
            )
            outcome["concurrent-transaction-reservations-stay-within-limit"] = _prove_concurrency(
                lam, session, clone, owner_sub, key
            )
            outcome["exhausted-budget-denies-before-dispatch"] = _prove_exhausted(lam, session, clone, owner_sub, key)
            outcome["budget-store-outage-denies-before-dispatch"] = _prove_outage(
                lam, session, outage_clone, owner_sub, outage_key
            )
        finally:
            if clone:
                delete_clone(session, clone, run_id=token, commit=binding.commit)
            if outage_clone:
                delete_clone(session, outage_clone, run_id=token, commit=binding.commit)
            outcome["cleanup"] = _cleanup(http, session, owner_sub, key)
            http.close()
    write_json(artifacts / "journey-outcome.json", outcome)
    return outcome


def _assert_absent(session: Any, table: str) -> None:
    from botocore.exceptions import ClientError

    try:
        session.client("dynamodb").describe_table(TableName=table)
    except ClientError as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
        return
    raise ScenarioFailed(f"outage sentinel table unexpectedly exists: {table}")


def _scenario(args: argparse.Namespace, artifact_dir: Path, claim: str) -> dict:
    binding, observed, comparison = bound_deployment(args, artifact_dir)
    journey = family_journey(
        binding,
        observed,
        family=FAMILY,
        run=lambda artifacts: _journey(args, binding, observed, artifacts),
        artifact_dir=artifact_dir,
    )
    outcome = journey["outcome"]
    facet = outcome.get(claim) or {}
    if claim == "concurrent-transaction-reservations-stay-within-limit":
        holds = facet.get("authorized") == 2 and facet.get("denied") == 3 and facet.get("reservedMicrousd") == 0
    elif claim == "exhausted-budget-denies-before-dispatch":
        holds = (
            facet.get("statusCode") == EXACT_ORACLES["budgetExhaustedStatus"]
            and facet.get("dispatchBoundaryCrossed") is False
        )
    elif claim == "budget-store-outage-denies-before-dispatch":
        holds = (
            facet.get("statusCode") == EXACT_ORACLES["budgetOutageStatus"]
            and facet.get("dispatchBoundaryCrossed") is False
        )
    else:
        raise ScenarioFailed(f"unknown cost claim {claim}")
    report = {
        "scenario": args.scenario_id,
        "region": binding.region,
        "deployment_binding": comparison,
        "journey": {k: v for k, v in journey.items() if k != "outcome"},
        "claim": claim,
        "claim_holds": holds,
        "facet": facet,
    }
    write_json(artifact_dir / "report", report)
    if not holds:
        raise ScenarioFailed(f"{claim}: {json.dumps(facet)[:300]}")
    return report


def concurrent_transaction_reservations_stay_within_limit(args, artifact_dir):
    return _scenario(args, artifact_dir, "concurrent-transaction-reservations-stay-within-limit")


def exhausted_budget_denies_before_dispatch(args, artifact_dir):
    return _scenario(args, artifact_dir, "exhausted-budget-denies-before-dispatch")


def budget_store_outage_denies_before_dispatch(args, artifact_dir):
    return _scenario(args, artifact_dir, "budget-store-outage-denies-before-dispatch")

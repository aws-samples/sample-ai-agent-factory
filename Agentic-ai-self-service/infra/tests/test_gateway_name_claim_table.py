"""The gateway-name claim table, and exactly which Lambdas may touch it, and how.

The callers are DERIVED from the backend's call graph (the same one
test_teardown_reads_are_granted_where_they_are_called builds), not listed by hand:
teardown lives in dispatchers more than one Lambda can reach, and a hand list is how a
grant goes missing from the one nobody thought of.

* ``_claim_gateway_name`` (the gateway step's acquire, promote, abandon and release)
  needs ``UpdateItem`` and nothing else.
* ``hold_gateway_names_for_teardown`` (both teardown dispatchers, F-66f) takes a
  provisional claim, abandons or releases it (``UpdateItem``), and erases the claim of
  a gateway confirmed deleted (``DeleteItem``). It never reads or puts one.
* ``gateway_mutation_lock`` (every gateway writer, F-66e) needs ``PutItem`` and
  ``DeleteItem``, and only on keys under ``gwlock#``: a lock writer must never be able
  to overwrite or delete a name claim.
* ``recovered_gateway_rows`` (both teardown dispatchers) finds a gateway recorded only
  on its claim: ``GetItem`` of the deployment's recovery pointer and the claims it
  lists, projected to the attributes the reader checks. It is the table's only read,
  and there is no index to read instead.
* promote's recovery transaction (TransactWriteItems of two Updates) needs no grant of
  its own: IAM has no TransactWriteItems action, and each Update is authorized as
  ``UpdateItem``, which the gateway step already holds.
"""

from __future__ import annotations

import ast

import pytest

from tests import test_teardown_reads_are_granted_where_they_are_called as _call_graph
from tests.iam_attachment import statements_for_role

_derived = _call_graph._derived
# The module-scoped fixtures: one synth and one AST walk, shared with that module.
graph = _call_graph.graph
template_json = _call_graph.template_json
entries = _call_graph.entries

CLAIM_TABLE_ENV = "GATEWAY_NAME_CLAIMS_TABLE_NAME"

TEARDOWN_HOLD = "hold_gateway_names_for_teardown"
RECOVERY_SEED = "recovered_gateway_rows"
# Reads the pointer and its claims, then marks the pointer for its TTL.
RECLAIM_SEED = "reclaim_recovery_pointer"
SEEDS = {
    "dynamodb:UpdateItem": ("_claim_gateway_name", TEARDOWN_HOLD, RECLAIM_SEED),
    "dynamodb:DeleteItem": (TEARDOWN_HOLD,),
    "dynamodb:GetItem": (RECOVERY_SEED, RECLAIM_SEED),
}

_TEARDOWN = {"deployment_handler.py", "step_handlers/status_update_step.py"}
_REVIEWED = {
    # handler -> deploy_gateway(claim_gateway_name=_claim_gateway_name), plus the
    # teardown hold's acquire/abandon/release.
    "dynamodb:UpdateItem": {"step_handlers/gateway_step.py", *_TEARDOWN},
    # The teardown hold's erase, after the gateway is confirmed deleted.
    "dynamodb:DeleteItem": _TEARDOWN,
    # The recovery reader, in both teardown dispatchers.
    "dynamodb:GetItem": _TEARDOWN,
}


def _derived_all(graph, template_json, entries, action: str) -> set[str]:
    return set().union(*(_derived(graph, template_json, entries, op) for op in SEEDS[action]))


LOCK_SEED = "gateway_mutation_lock"
LOCK_ACTIONS = {"dynamodb:PutItem", "dynamodb:DeleteItem"}
LOCK_KEYS = {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["gwlock#*"]}}

# The revoke, the policy detach and the gateway delete (teardown), the redeploy
# adoption, the policy attach, the promoter the deployment Lambda runs on status polls
# and the sweep, and the failed deployment's own cleanup delete.
_REVIEWED_LOCK = {
    "deployment_handler.py",
    "step_handlers/gateway_step.py",
    "step_handlers/policy_step.py",
    "step_handlers/status_update_step.py",
}


@pytest.fixture(scope="module")
def claim_table(template_json) -> str:
    found = [
        lid
        for lid, r in template_json["Resources"].items()
        if r["Type"] == "AWS::DynamoDB::Table" and "gateway-name-claims" in str(r["Properties"].get("TableName"))
    ]
    assert len(found) == 1, found
    return found[0]


def _on_table(statement: dict, table: str) -> bool:
    resources = statement.get("Resource")
    resources = resources if isinstance(resources, list) else [resources]
    return any(isinstance(r, dict) and r.get("Fn::GetAtt") == [table, "Arn"] for r in resources)


def _table_actions(template_json, role: str, table: str) -> set[str]:
    out: set[str] = set()
    for _src, st in statements_for_role(template_json, role):
        if st.get("Effect") != "Allow" or not _on_table(st, table):
            continue
        acts = st.get("Action")
        out |= {acts} if isinstance(acts, str) else set(acts or [])
    return out


def test_the_table_is_keyed_encrypted_and_recoverable(template_json, claim_table):
    props = template_json["Resources"][claim_table]["Properties"]
    assert props["KeySchema"] == [{"AttributeName": "claim_key", "KeyType": "HASH"}]
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert props["SSESpecification"]["SSEEnabled"] is True
    assert props["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True


def test_an_abandoned_claim_expires_on_its_own(template_json, claim_table):
    """abandon() and a crashed acquire leave a provisional claim with ``gc_after`` set
    and no live lease. It is already free to claim; the TTL only keeps the table from
    growing one dead row per failed deploy. A durable claim never carries gc_after."""
    ttl = template_json["Resources"][claim_table]["Properties"].get("TimeToLiveSpecification")
    assert ttl == {"AttributeName": "gc_after", "Enabled": True}, ttl


@pytest.mark.parametrize("op", sorted({op for ops in SEEDS.values() for op in ops}))
def test_the_seed_is_a_real_call_site(graph, op):
    assert graph.seeds(op), f"nothing in backend/src/app references {op}"


def test_every_seed_is_defined_in_the_claim_module():
    """A seed that is only a stale string still 'references' something (a docstring,
    a test helper); it must name a function the claim path actually defines."""
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "backend" / "src" / "app"
    defined = {
        node.name
        for path in (src / "services" / "gateway_name_claim.py", src / "step_handlers" / "gateway_step.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef)
    }
    assert {op for ops in SEEDS.values() for op in ops} <= defined


@pytest.mark.parametrize("action", sorted(SEEDS))
def test_the_derived_callers_are_the_reviewed_ones(graph, template_json, entries, action):
    derived = _derived_all(graph, template_json, entries, action)
    assert derived == _REVIEWED[action], (derived, _REVIEWED[action])


@pytest.mark.parametrize("action", sorted(SEEDS))
def test_every_caller_holds_exactly_its_action_on_the_table(graph, template_json, entries, claim_table, action):
    for rel in _derived_all(graph, template_json, entries, action):
        got = _table_actions(template_json, entries[rel], claim_table)
        assert action in got, f"{rel} reaches {SEEDS[action]} but cannot {action} the claim table"


def test_no_role_holds_more_than_its_callers_need(graph, template_json, entries, claim_table):
    """Least privilege, and the vacuity guard for the test above: only the recovery
    reader may read the table, the gateway step may not delete a claim, and teardown
    may not write one."""
    need: dict[str, set[str]] = {}
    for action in SEEDS:
        for rel in _derived_all(graph, template_json, entries, action):
            need.setdefault(rel, set()).add(action)
    for rel in _derived(graph, template_json, entries, LOCK_SEED):
        need.setdefault(rel, set()).update(LOCK_ACTIONS)
    for rel, role in sorted(entries.items()):
        got = _table_actions(template_json, role, claim_table)
        assert got <= need.get(rel, set()), f"{rel} holds {sorted(got - need.get(rel, set()))} on the claim table"


def test_no_role_holds_a_wildcard_dynamodb_grant(template_json, entries):
    """The table-scoped check above is blind to a grant on '*'."""
    for rel, role in sorted(entries.items()):
        for _src, st in statements_for_role(template_json, role):
            acts = st.get("Action")
            acts = [acts] if isinstance(acts, str) else (acts or [])
            if st.get("Effect") == "Allow" and any(a.startswith("dynamodb:") or a == "*" for a in acts):
                resources = st.get("Resource")
                resources = resources if isinstance(resources, list) else [resources]
                assert "*" not in resources, f"{rel} holds {acts} on '*'"


def test_every_caller_is_told_the_table_name(graph, template_json, entries, claim_table):
    """claims_from_env fails closed on an unset name, so a missing env var is a
    deploy that fails (or a claim never erased), not a silent skip."""
    callers = set().union(*(_derived_all(graph, template_json, entries, a) for a in SEEDS))
    callers |= _derived(graph, template_json, entries, LOCK_SEED)
    for resource in template_json["Resources"].values():
        if resource["Type"] != "AWS::Lambda::Function":
            continue
        handler = resource["Properties"].get("Handler") or ""
        rel = handler.removeprefix("src/app/").rpartition(".")[0] + ".py"
        if rel not in callers:
            continue
        env = resource["Properties"].get("Environment", {}).get("Variables", {})
        assert env.get(CLAIM_TABLE_ENV) == {"Ref": claim_table}, (rel, env.get(CLAIM_TABLE_ENV))


def test_the_lock_writers_are_the_reviewed_ones(graph, template_json, entries):
    assert graph.seeds(LOCK_SEED), f"nothing in backend/src/app references {LOCK_SEED}"
    assert _derived(graph, template_json, entries, LOCK_SEED) == _REVIEWED_LOCK


def test_every_lock_writer_can_take_and_release_the_lock(graph, template_json, entries, claim_table):
    for rel in _derived(graph, template_json, entries, LOCK_SEED):
        got = _table_actions(template_json, entries[rel], claim_table)
        assert LOCK_ACTIONS <= got, f"{rel} reaches {LOCK_SEED}() but holds only {sorted(got)} on the claim table"
        conditioned = [
            st
            for _src, st in statements_for_role(template_json, entries[rel])
            if st.get("Effect") == "Allow"
            and _on_table(st, claim_table)
            and "dynamodb:PutItem" in ([st["Action"]] if isinstance(st["Action"], str) else st["Action"])
        ]
        assert conditioned and all(st.get("Condition") == LOCK_KEYS for st in conditioned), (rel, conditioned)


def test_no_role_can_put_an_item_outside_the_lock_prefix(template_json, entries, claim_table):
    """PutItem replaces a whole item. Unconditioned, a lock writer could overwrite the
    name claim that says who owns a gateway name; the claim itself uses UpdateItem."""
    for rel, role in sorted(entries.items()):
        for _src, st in statements_for_role(template_json, role):
            acts = [st.get("Action")] if isinstance(st.get("Action"), str) else (st.get("Action") or [])
            if st.get("Effect") == "Allow" and _on_table(st, claim_table) and "dynamodb:PutItem" in acts:
                assert st.get("Condition") == LOCK_KEYS, (rel, st)


def test_only_teardown_can_delete_outside_the_lock_prefix(graph, template_json, entries, claim_table):
    """The union in _table_actions reads a lock writer's DeleteItem as 'needed', so an
    extra unconditioned DeleteItem statement on it would pass every test above while
    letting it erase the durable claim that says who owns a gateway name. Only the
    callers that reach the teardown hold's erase may delete a claim."""
    erasers = _derived_all(graph, template_json, entries, "dynamodb:DeleteItem")
    assert erasers, "no caller erases a claim, so this check is vacuous"
    checked = 0
    for rel, role in sorted(entries.items()):
        if rel in erasers:
            continue
        for _src, st in statements_for_role(template_json, role):
            acts = [st.get("Action")] if isinstance(st.get("Action"), str) else (st.get("Action") or [])
            if st.get("Effect") == "Allow" and _on_table(st, claim_table) and "dynamodb:DeleteItem" in acts:
                checked += 1
                assert st.get("Condition") == LOCK_KEYS, (rel, st)
    assert checked, "no lock-only DeleteItem statement was found, so this check is vacuous"


# The recovery reader (gateway_name_claim.recovered_gateway_rows): a gateway recorded
# only on its name claim is found by GetItem of the deployment's recovery pointer and
# the claims it lists, from both teardown dispatchers. Nothing else may read the table,
# and the reader may read nothing but the attributes it checks.
BACKEND_CLAIM_MODULE = "backend/src/app/services/gateway_name_claim.py"
READ_ACTIONS = ("dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:Scan")


def _backend_constant(name: str):
    import ast
    import pathlib

    tree = ast.parse((pathlib.Path(__file__).resolve().parents[2] / BACKEND_CLAIM_MODULE).read_text())
    for node in tree.body:
        targets = [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{BACKEND_CLAIM_MODULE} defines no {name}")


def _projected(attributes) -> dict:
    return {
        "ForAllValues:StringEquals": {"dynamodb:Attributes": list(attributes)},
        "StringEquals": {"dynamodb:Select": "SPECIFIC_ATTRIBUTES"},
    }


def test_the_table_has_no_index(template_json, claim_table):
    """The reader never queries: an index would be a second, lagging copy of every
    claim's evidence that some grant could be widened to read."""
    props = template_json["Resources"][claim_table]["Properties"]
    assert "GlobalSecondaryIndexes" not in props and "LocalSecondaryIndexes" not in props, props
    assert props["AttributeDefinitions"] == [{"AttributeName": "claim_key", "AttributeType": "S"}]


def test_the_granted_projection_is_the_readers():
    from stacks.platform.tables import RECOVERY_READ_ATTRIBUTES

    assert tuple(_backend_constant("RECOVERY_READ_ATTRIBUTES")) == RECOVERY_READ_ATTRIBUTES
    assert _backend_constant("RECOVERY_POINTER_PREFIX") == "recovery#"
    lease = {"holder_token", "holder_deployment_id", "holder_expires_at"}
    assert not lease & set(RECOVERY_READ_ATTRIBUTES), "the reader must never be able to read the fence"


def test_the_recovery_readers_are_both_teardown_dispatchers(graph, template_json, entries):
    assert _derived(graph, template_json, entries, RECOVERY_SEED) == _TEARDOWN


def _reads(template_json, role: str, table: str) -> list[dict]:
    out = []
    for _src, st in statements_for_role(template_json, role):
        acts = [st.get("Action")] if isinstance(st.get("Action"), str) else (st.get("Action") or [])
        if st.get("Effect") == "Allow" and _mentions(st.get("Resource"), table):
            if any(a in READ_ACTIONS or a in ("dynamodb:*", "*") for a in acts):
                out.append(st)
    return out


def _mentions(value, table: str) -> bool:
    return f'"{table}"' in __import__("json").dumps(value)


def test_no_read_of_the_claim_table_but_the_readers_projected_getitem(graph, template_json, entries, claim_table):
    """Least privilege: exactly GetItem, exactly the table ARN (never /index/*), exactly
    its callers, and only the projected attributes. _mentions catches a grant on any
    ARN built from the table, which _on_table (GetAtt Arn only) would not."""
    from stacks.platform.tables import RECOVERY_READ_ATTRIBUTES

    readers = _derived(graph, template_json, entries, RECOVERY_SEED)
    checked = 0
    for rel, role in sorted(entries.items()):
        for st in _reads(template_json, role, claim_table):
            checked += 1
            assert rel in readers, f"{rel} can read the claim table: {st}"
            resources = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
            assert st["Action"] in ("dynamodb:GetItem", ["dynamodb:GetItem"]), (rel, st)
            assert resources == [{"Fn::GetAtt": [claim_table, "Arn"]}], (rel, st)
            assert st.get("Condition") == _projected(RECOVERY_READ_ATTRIBUTES), (rel, st)
    assert checked == len(readers), "the vacuity guard: one GetItem statement per reader"


def _lock_seconds() -> int:
    """LOCK_SECONDS as the backend defines it, read from the source, not restated here."""
    src = _call_graph._BACKEND_SRC / "services" / "gateway_mutation_lock.py"
    for node in ast.parse(src.read_text()).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "LOCK_SECONDS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"LOCK_SECONDS not found in {src}")


def test_every_lock_holder_times_out_before_its_lock_expires(graph, template_json, entries):
    """The lock has no fencing token and no renewal: mutual exclusion holds only while
    no holder outlives its lease. A Lambda's timeout bounds how long it can hold the
    lock, so every Lambda that can reach ``gateway_mutation_lock`` must time out
    strictly before LOCK_SECONDS. Raising a timeout past it would bring back the
    overlap F-66e closed, and nothing else would fail.
    """
    lock_seconds = _lock_seconds()
    callers = _derived(graph, template_json, entries, LOCK_SEED)
    assert callers, f"nothing reaches {LOCK_SEED}()"
    timeouts: dict[str, int] = {}
    for resource in template_json["Resources"].values():
        if resource["Type"] != "AWS::Lambda::Function":
            continue
        handler = resource["Properties"].get("Handler") or ""
        rel = handler.removeprefix("src/app/").rpartition(".")[0] + ".py"
        if rel in callers:
            timeout = resource["Properties"].get("Timeout", 3)
            assert isinstance(timeout, int), (rel, timeout)
            timeouts[rel] = max(timeout, timeouts.get(rel, 0))
    # Reach: every derived holder was found as a deployed function.
    assert set(timeouts) == callers, (sorted(callers), timeouts)
    over = {rel: t for rel, t in timeouts.items() if not t < lock_seconds}
    assert not over, f"these can hold the gateway lock past its {lock_seconds}s lease: {over}"

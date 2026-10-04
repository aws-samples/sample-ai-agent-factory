"""F-7d: a per-scope or per-deployment tool resource is deleted only on its EXACT binding.

The create path binds a custom tool's function and role to the full owner+gateway scope
digest (``ToolScope``) and a KB tool's function and role to the exact ``DeploymentId``, on
top of the stack pair. This file proves every teardown path REQUIRES that binding before it
mutates -- the direct abort cleanup (``cleanup_gateway_resources``), the API's manifest
dispatcher (``deployment_handler._delete_managed_resource``) and the failure-path dispatcher
(``status_update_step._cleanup_resource``) -- and that a same-stack name collision (peers 5a
and 3a forced one: two scopes whose truncated digest coincided) is kept, not deleted.

Fakes carry OUR stack tags throughout, so what each negative case measures is the binding
check alone: the stack gate would have let every one of these through.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# Imported at collection time on purpose: this module's fixture sets ENVIRONMENT to a deployed
# value so the stack id matches the fakes' tags, and deployment_handler resolves its config from
# SSM at import when the environment is not local.
import app.deployment_handler as dh
import pytest
from app.services import gateway_deployer as gd
from app.services import resource_ownership as ro

REGION = "us-east-1"
GW_ID = "gw-abc123"
SCOPE_A = "a" * 64
SCOPE_B = "b" * 64
DEP_A = "deadbeef-0000-4000-8000-000000000001"
DEP_B = "deadbeef-ffff-4fff-8fff-ffffffffffff"


@pytest.fixture(autouse=True)
def _stack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROJECT_NAME", "acfe2e")
    monkeypatch.setenv("ENVIRONMENT", "p0920")
    monkeypatch.setenv("APP_AWS_REGION", REGION)
    monkeypatch.setenv("AWS_REGION", REGION)


def _lam(name: str, tags: dict[str, str]) -> MagicMock:
    client = MagicMock()
    client.get_function.return_value = {
        "Configuration": {"FunctionArn": f"arn:aws:lambda:{REGION}:123456789012:function:{name}", "RevisionId": "1"}
    }
    client.list_tags.return_value = {"Tags": tags}
    return client


def _iam(role: str, tags: dict[str, str] | None) -> MagicMock:
    iam = MagicMock()
    iam.get_role.return_value = {
        "Role": {
            "Arn": f"arn:aws:iam::123456789012:role/{role}",
            "RoleName": role,
            "Tags": [{"Key": k, "Value": v} for k, v in (tags or {}).items()],
        }
    }
    return iam


def _custom_names() -> tuple[str, str]:
    fn, role, _safe, _binding = gd._custom_tool_resource_names("lookup", "owner-a", GW_ID, REGION)
    return fn, role


def _ours(extra: dict[str, str] | None = None) -> dict[str, str]:
    return ro.owner_tags(REGION, extra=extra)


# ---------------------------------------------------------------------------
# The requirement itself
# ---------------------------------------------------------------------------


def test_the_binding_requirement_by_name_family() -> None:
    fn, role = _custom_names()
    assert gd.tool_binding_requirement(fn, SCOPE_A, DEP_A) == {"ToolScope": SCOPE_A}
    assert gd.tool_binding_requirement(role, SCOPE_A, DEP_A) == {"ToolScope": SCOPE_A}
    kb_fn = gd.scoped_function_name("KBTool", ro.stack_id(REGION), "0" * 12)
    kb_role = gd.scoped_role_name("KBTool", ro.stack_id(REGION), "0" * 12)
    assert gd.tool_binding_requirement(kb_fn, None, DEP_A) == {"DeploymentId": DEP_A}
    assert gd.tool_binding_requirement(kb_role, None, DEP_A) == {"DeploymentId": DEP_A}
    # Legacy unscoped names never carried a binding: stack gate only.
    assert gd.tool_binding_requirement("AgentCore-CustomTool-lookup-abcdef12", None, DEP_A) is None
    assert gd.tool_binding_requirement("AgentCoreCustomToolRole-lookup-abcdef12", None, DEP_A) is None
    # A scoped custom name with NO recorded binding is refused outright.
    with pytest.raises(ro.ForeignResourceError, match="no ToolScope binding"):
        gd.tool_binding_requirement(fn, None, DEP_A)
    with pytest.raises(ro.ForeignResourceError, match="no deployment id"):
        gd.tool_binding_requirement(kb_fn, None, "")


# ---------------------------------------------------------------------------
# Direct abort cleanup
# ---------------------------------------------------------------------------


def _direct_cleanup(lam: MagicMock, iam: MagicMock, cfg: dict) -> list[str]:
    ctrl = MagicMock()
    ctrl.get_gateway.side_effect = Exception("no gateway in this test")
    with (
        patch.object(gd, "_create_agentcore_control_client", return_value=ctrl),
        patch.object(gd, "_create_lambda_client", return_value=lam),
        patch.object(gd, "_create_iam_client", return_value=iam),
        patch.object(gd, "delete_owned_iam_role") as delete_role,
        patch.object(gd, "time", MagicMock()),
    ):
        log = gd.cleanup_gateway_resources("rt-x", REGION, cfg, deployment_id=DEP_A)
    lam.delete_role = delete_role  # exposed for the caller's assertions
    return log


def test_direct_cleanup_keeps_a_same_stack_function_bound_to_another_scope() -> None:
    fn, role = _custom_names()
    lam = _lam(fn, _ours({"ToolScope": SCOPE_B}))  # forced collision: our stack, scope B's function
    iam = _iam(role, _ours({"ToolScope": SCOPE_B}))
    cfg = {
        "custom_tool_lambdas": [fn],
        "custom_tool_roles": [role],
        "custom_tool_bindings": {fn: SCOPE_A, role: SCOPE_A},
    }
    log = _direct_cleanup(lam, iam, cfg)
    lam.delete_function.assert_not_called()
    lam.delete_role.assert_not_called()
    lam.remove_permission.assert_not_called()
    joined = "\n".join(log)
    assert f"Custom tool Lambda {fn} kept" in joined and "ToolScope=" in joined
    assert f"IAM role {role} kept" in joined


def test_direct_cleanup_deletes_the_exactly_bound_pair() -> None:
    fn, role = _custom_names()
    lam = _lam(fn, _ours({"ToolScope": SCOPE_A}))
    iam = _iam(role, _ours({"ToolScope": SCOPE_A}))
    cfg = {
        "custom_tool_lambdas": [fn],
        "custom_tool_roles": [role],
        "custom_tool_bindings": {fn: SCOPE_A, role: SCOPE_A},
    }
    log = _direct_cleanup(lam, iam, cfg)
    lam.delete_function.assert_called_once_with(FunctionName=fn)
    lam.delete_role.assert_called_once()
    assert f"Custom tool Lambda {fn} deleted" in log and f"IAM role {role} deleted" in log


def test_direct_cleanup_refuses_a_scoped_name_with_no_recorded_binding() -> None:
    """A row that lost its binding is not authority over a scoped resource."""
    fn, role = _custom_names()
    lam = _lam(fn, _ours({"ToolScope": SCOPE_A}))
    iam = _iam(role, _ours({"ToolScope": SCOPE_A}))
    cfg = {"custom_tool_lambdas": [fn], "custom_tool_roles": [role]}  # no custom_tool_bindings at all
    log = _direct_cleanup(lam, iam, cfg)
    lam.delete_function.assert_not_called()
    lam.delete_role.assert_not_called()
    assert any("no ToolScope binding" in line for line in log), log


def test_direct_cleanup_still_deletes_a_legacy_unscoped_function_on_the_stack_gate() -> None:
    """Pre-token names never carried ToolScope and cannot collide with scoped names, so the
    stack-ownership gate alone decides for them, exactly as it did before F-7d."""
    fn = "AgentCore-CustomTool-lookup-abcdef12"
    lam = _lam(fn, _ours())
    log = _direct_cleanup(lam, _iam("AgentCoreCustomToolRole-lookup-abcdef12", _ours()), {"custom_tool_lambdas": [fn]})
    lam.delete_function.assert_called_once_with(FunctionName=fn)
    assert f"Custom tool Lambda {fn} deleted" in log


# ---------------------------------------------------------------------------
# The API's manifest dispatcher
# ---------------------------------------------------------------------------


def _manifest_delete(row: dict, lam: MagicMock, iam: MagicMock, deployment_id: str = DEP_A) -> tuple[str, MagicMock]:
    # The dispatcher shadows the module's ``boto3`` with a target-aware shim whose .client()
    # delegates to step_clients.client(event, service, ...), so that is the seam to patch --
    # patching dh.boto3.client reaches nothing and every ownership read hits real AWS.
    def _client(_event, service, **_kw):
        return lam if service == "lambda" else iam

    with (
        patch("app.services.step_clients.client", side_effect=_client),
        patch.object(dh, "delete_owned_iam_role") as delete_role,
    ):
        msg = dh._delete_managed_resource(row, REGION, deployment_id=deployment_id)
    return msg, delete_role


def test_manifest_dispatcher_keeps_a_function_row_bound_to_another_scope() -> None:
    fn, _role = _custom_names()
    lam = _lam(fn, _ours({"ToolScope": SCOPE_B}))
    msg, _ = _manifest_delete({"type": "lambda", "name": fn, "region": REGION, "tool_scope": SCOPE_A}, lam, MagicMock())
    lam.delete_function.assert_not_called()
    assert "ToolScope=" in msg and "kept" in msg


def test_manifest_dispatcher_keeps_a_role_row_bound_to_another_scope() -> None:
    fn, role = _custom_names()
    iam = _iam(role, _ours({"ToolScope": SCOPE_B}))
    msg, delete_role = _manifest_delete(
        {"type": "iam_role", "name": role, "region": REGION, "tool_scope": SCOPE_A, "paired_function": fn},
        MagicMock(),
        iam,
    )
    delete_role.assert_not_called()
    assert "ToolScope=" in msg and "kept" in msg


def test_manifest_dispatcher_deletes_the_exactly_bound_pair() -> None:
    fn, role = _custom_names()
    lam = _lam(fn, _ours({"ToolScope": SCOPE_A}))
    iam = _iam(role, _ours({"ToolScope": SCOPE_A}))
    msg, _ = _manifest_delete({"type": "lambda", "name": fn, "region": REGION, "tool_scope": SCOPE_A}, lam, iam)
    lam.delete_function.assert_called_once_with(FunctionName=fn)
    assert "deleted" in msg
    msg, delete_role = _manifest_delete(
        {"type": "iam_role", "name": role, "region": REGION, "tool_scope": SCOPE_A, "paired_function": fn}, lam, iam
    )
    delete_role.assert_called_once()
    assert "deleted" in msg


def test_manifest_dispatcher_binds_a_scoped_kb_function_to_this_deployment() -> None:
    kb_fn = gd.scoped_function_name("KBTool", ro.stack_id(REGION), gd.deployment_scope_suffix(DEP_B))
    lam = _lam(kb_fn, _ours({"DeploymentId": DEP_B}))  # forced collision: deployment B's function
    msg, _ = _manifest_delete(
        {"type": "lambda", "name": kb_fn, "region": REGION}, lam, MagicMock(), deployment_id=DEP_A
    )
    lam.delete_function.assert_not_called()
    assert "DeploymentId=" in msg and "kept" in msg


# ---------------------------------------------------------------------------
# The failure-path dispatcher
# ---------------------------------------------------------------------------


def _failure_delete(row: dict, lam: MagicMock, iam: MagicMock):
    from app.step_handlers import status_update_step as sus

    def _client(_event, service, **_kw):
        return lam if service == "lambda" else iam

    with (
        patch("app.services.step_clients.client", side_effect=_client),
        patch.object(sus, "delete_owned_iam_role") as dr,
    ):
        sus._cleanup_resource(row, REGION, {"deployment_id": DEP_A})
    return dr


def test_failure_path_keeps_a_function_row_bound_to_another_scope() -> None:
    from app.step_handlers import status_update_step as sus

    fn, _role = _custom_names()
    lam = _lam(fn, _ours({"ToolScope": SCOPE_B}))
    with pytest.raises(sus._ResourceRetained, match="ToolScope="):
        _failure_delete({"type": "lambda", "name": fn, "region": REGION, "tool_scope": SCOPE_A}, lam, MagicMock())
    lam.delete_function.assert_not_called()


def test_failure_path_keeps_a_role_row_bound_to_another_scope() -> None:
    from app.step_handlers import status_update_step as sus

    fn, role = _custom_names()
    iam = _iam(role, _ours({"ToolScope": SCOPE_B}))
    with pytest.raises(sus._ResourceRetained, match="ToolScope="):
        _failure_delete(
            {"type": "iam_role", "name": role, "region": REGION, "tool_scope": SCOPE_A, "paired_function": fn},
            MagicMock(),
            iam,
        )


def test_failure_path_deletes_the_exactly_bound_pair() -> None:
    fn, role = _custom_names()
    lam = _lam(fn, _ours({"ToolScope": SCOPE_A}))
    iam = _iam(role, _ours({"ToolScope": SCOPE_A}))
    _failure_delete({"type": "lambda", "name": fn, "region": REGION, "tool_scope": SCOPE_A}, lam, iam)
    lam.delete_function.assert_called_once_with(FunctionName=fn)
    dr = _failure_delete(
        {"type": "iam_role", "name": role, "region": REGION, "tool_scope": SCOPE_A, "paired_function": fn}, lam, iam
    )
    dr.assert_called_once()


# ---------------------------------------------------------------------------
# The writers persist what the dispatchers require
# ---------------------------------------------------------------------------


def test_the_manifest_writer_persists_the_binding_and_the_paired_function() -> None:
    from app.step_handlers.gateway_step import _record_gateway_resources

    fn, role = _custom_names()
    store = MagicMock()
    _record_gateway_resources(
        store,
        DEP_A,
        REGION,
        {
            "gateway_id": GW_ID,
            "gateway_name": "gw",
            "gateway_role_name": "AgentCoreGateway-gw",
            "custom_tool_lambdas": [fn],
            "custom_tool_roles": [role],
            "custom_tool_bindings": {fn: SCOPE_A, role: SCOPE_A},
        },
    )
    rows = [call.args[1] for call in store.record_resource.call_args_list]
    fn_rows = [r for r in rows if r.get("type") == "lambda" and r.get("name") == fn]
    role_rows = [r for r in rows if r.get("type") == "iam_role" and r.get("name") == role]
    assert fn_rows and fn_rows[0]["tool_scope"] == SCOPE_A
    assert role_rows and role_rows[0]["tool_scope"] == SCOPE_A and role_rows[0]["paired_function"] == fn


# ---------------------------------------------------------------------------
# F-74c: the ADOPTED rows, and pairing when a gateway has more than one tool
# ---------------------------------------------------------------------------


def _two_tools() -> tuple[str, str, str, str]:
    fn_a, role_a, _s, bind_a = gd._custom_tool_resource_names("alpha", "owner-a", GW_ID, REGION)
    fn_b, role_b, _s2, bind_b = gd._custom_tool_resource_names("beta", "owner-a", GW_ID, REGION)
    # The premise of the pairing bug: the binding is scoped to owner+gateway, so two DIFFERENT
    # tools on one gateway carry the SAME value. If this ever stops being true the inference
    # these tests guard against stops being wrong, and the test should be revisited, not deleted.
    assert bind_a == bind_b, (bind_a, bind_b)
    return fn_a, role_a, fn_b, role_b


def _recorded(result: dict) -> list[dict]:
    from app.step_handlers.gateway_step import _record_gateway_resources

    store = MagicMock()
    _record_gateway_resources(
        store,
        DEP_A,
        REGION,
        {"gateway_id": GW_ID, "gateway_name": "gw", "gateway_role_name": "AgentCoreGateway-gw", **result},
    )
    return [call.args[1] for call in store.record_resource.call_args_list]


def test_an_adopted_custom_tool_row_carries_the_binding_its_reclaim_needs() -> None:
    """The leak. A redeploy ADOPTS the function and role, so the last deployment standing on
    the gateway -- the only one whose teardown may actually delete them -- holds an adopted
    row. Written without ``tool_scope`` it hits ``tool_binding_requirement``'s fail-closed
    branch and refuses on the name alone, and both resources survive every teardown forever.
    Proven live on acfe2e-p0920: deployment ff8b55b6 finished delete_failed with
    "holds no ToolScope binding for it" for AgentCore-28ac3280e6-CustomTool-f74bprobe-* and
    its role, and both were still in the account afterwards.
    """
    fn, role = _custom_names()
    rows = _recorded(
        {
            "custom_tool_lambdas_adopted": [fn],
            "custom_tool_roles_adopted": [role],
            "custom_tool_bindings": {fn: SCOPE_A, role: SCOPE_A},
            "custom_tool_pairs": {role: fn},
        }
    )
    fn_row = next(r for r in rows if r.get("name") == fn)
    role_row = next(r for r in rows if r.get("name") == role)
    # Still a live reference, NOT this deployment's to abort-delete (F-66) ...
    assert fn_row["created_by_deployment"] is False
    assert role_row["created_by_deployment"] is False
    # ... and still able to authorize the delete when it becomes the last one.
    assert fn_row["tool_scope"] == SCOPE_A
    assert role_row["tool_scope"] == SCOPE_A
    assert role_row["paired_function"] == fn
    # The binding on the row is the authority the teardown gate asks for, on this exact name.
    assert gd.tool_binding_requirement(fn, fn_row["tool_scope"], DEP_A) == {"ToolScope": SCOPE_A}
    assert gd.tool_binding_requirement(role, role_row["tool_scope"], DEP_A) == {"ToolScope": SCOPE_A}


def test_each_role_row_names_its_own_function_when_a_gateway_has_two_tools() -> None:
    """``paired_function`` is the ``shared_lambda_lock`` key the role's teardown arm takes.
    Inferred from the binding it resolved to the FIRST tool's function for BOTH roles, so
    beta's role deleted under alpha's lambda lock while its own lambda was unfenced. Every
    existing test used a single tool, which is the only case where the inference is right.
    """
    fn_a, role_a, fn_b, role_b = _two_tools()
    rows = _recorded(
        {
            "custom_tool_lambdas": [fn_a, fn_b],
            "custom_tool_roles": [role_a, role_b],
            "custom_tool_bindings": dict.fromkeys([fn_a, role_a, fn_b, role_b], SCOPE_A),
            "custom_tool_pairs": {role_a: fn_a, role_b: fn_b},
        }
    )
    paired = {r["name"]: r["paired_function"] for r in rows if r.get("type") == "iam_role" and "paired_function" in r}
    assert paired == {role_a: fn_a, role_b: fn_b}


def test_an_unpairable_legacy_row_says_so_instead_of_naming_another_tool() -> None:
    """Rows written before the producer recorded pairs have only the binding. With one tool it
    identifies the function; with two it identifies two, and guessing would hand back a lock
    on a lambda nobody is touching. Empty is the honest answer -- the teardown arms fall back
    to the role's own name, which over-locks rather than mis-locks.
    """
    fn_a, role_a, fn_b, role_b = _two_tools()
    one = {fn_a: SCOPE_A, role_a: SCOPE_A}
    assert gd.paired_custom_tool_function(role_a, None, one) == fn_a
    both = dict.fromkeys([fn_a, role_a, fn_b, role_b], SCOPE_A)
    assert gd.paired_custom_tool_function(role_a, None, both) == ""
    assert gd.paired_custom_tool_function(role_b, {}, both) == ""
    # An explicit pair always wins over the inference, including when the inference would work.
    assert gd.paired_custom_tool_function(role_b, {role_b: fn_b}, both) == fn_b
    # No binding at all -> nothing to say. (The delete is refused upstream anyway.)
    assert gd.paired_custom_tool_function(role_a, None, {}) == ""


def test_the_step_writer_adds_nothing_of_its_own_to_the_binding_fields() -> None:
    """``services/deployment`` writes the manifest on the non-Step-Functions path and had its
    own copy of both defects. It has no test of its own -- the block sits inside a long async
    deploy -- so what is pinned here is the seam: every binding field in a recorded row comes
    from the shared helper, and the step writer contributes only bookkeeping (``region``,
    ``gateway_graph``, ``created_by_deployment``) on top. Both writers now call that helper, so
    a change to the row shape moves both or neither.
    """
    fn, role = _custom_names()
    bindings = {fn: SCOPE_A, role: SCOPE_A}
    pairs = {role: fn}
    for res_type, name, key in (("lambda", fn, "lambdas"), ("iam_role", role, "roles")):
        row = gd.custom_tool_manifest_row(res_type, name, bindings, pairs)
        from_step = next(
            r
            for r in _recorded(
                {
                    f"custom_tool_{key}_adopted": [name],
                    "custom_tool_bindings": bindings,
                    "custom_tool_pairs": pairs,
                }
            )
            if r.get("name") == name
        )
        assert row.items() <= from_step.items(), (row, from_step)
        # And the helper is the ONLY source of them: nothing binding-related is added later.
        assert set(from_step) - set(row) == {"region", "gateway_graph", "created_by_deployment"}, from_step

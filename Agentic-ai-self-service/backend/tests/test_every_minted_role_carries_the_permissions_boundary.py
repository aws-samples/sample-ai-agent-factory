"""F-06 (backend half): every IAM role the platform mints carries the permissions boundary.

Infra ships ``AgentCoreRoleBoundary`` and hands its ARN to every Lambda as
``AGENTCORE_ROLE_PERMISSIONS_BOUNDARY_ARN``. Before infra can condition ``CreateRole`` /
``PutRolePolicy`` on ``iam:PermissionsBoundary`` (the enforcement flip), two things must be true of
the backend: every ``create_role`` passes the boundary, and every "already exists -> adopt" branch
retrofits it onto a role created before the boundary existed -- otherwise the very next
``PutRolePolicy`` on that role is denied under enforcement. Both must be no-ops while the
variable is unset, so the backend lands first and nothing fails closed prematurely.

The two structural tests walk the source the same way ``test_foreign_role_is_not_mutated`` does,
so an eleventh ``create_role`` or a new adopt branch cannot arrive without the boundary. The
behavioural tests drive the four sites that can be called with a fake IAM client.

Fixture values are fake: the account id is the documented test account, the ARN is invented.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from app.services import iam_boundary
from app.services import resource_ownership as ro

BOUNDARY = "arn:aws:iam::166827918465:policy/acfe2e-p0920-agentcore-role-boundary"
OTHER_BOUNDARY = "arn:aws:iam::166827918465:policy/some-older-boundary"
REGION = "us-east-1"
ACCOUNT = "166827918465"
SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "app"


@pytest.fixture(autouse=True)
def _deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env a step handler runs with, minus the boundary (each test decides that)."""
    monkeypatch.setenv("PROJECT_NAME", "acfe2e")
    monkeypatch.setenv("ENVIRONMENT", "p0920")
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.delenv(iam_boundary.BOUNDARY_ENV_VAR, raising=False)


@pytest.fixture
def boundary_set(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(iam_boundary.BOUNDARY_ENV_VAR, BOUNDARY)
    return BOUNDARY


@pytest.fixture(autouse=True)
def _no_propagation_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import gateway_deployer, harness_deployer, runtime_deployer, tool_tester

    for module in (gateway_deployer, harness_deployer, runtime_deployer, tool_tester):
        monkeypatch.setattr(module.time, "sleep", lambda *_a, **_k: None)


class _AlreadyExists(Exception):
    pass


class _FakeIam:
    """Records every call. ``collide`` makes create_role raise EntityAlreadyExists."""

    def __init__(self, *, collide: bool = False, tags=None, boundary: str | None = None, put_boundary_error=None):
        self.collide = collide
        self.tags = tags
        self.boundary = boundary
        self.put_boundary_error = put_boundary_error
        self.calls: list[tuple[str, dict]] = []

        class _Exceptions:
            EntityAlreadyExistsException = _AlreadyExists

        self.exceptions = _Exceptions()

    def _record(self, name: str, kwargs: dict) -> None:
        self.calls.append((name, kwargs))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def create_role(self, **kwargs):
        self._record("create_role", kwargs)
        if self.collide:
            raise _AlreadyExists("Role with name already exists.")
        return {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/{kwargs['RoleName']}", "RoleName": kwargs["RoleName"]}}

    def get_role(self, **kwargs):
        self._record("get_role", kwargs)
        role = {"Arn": f"arn:aws:iam::{ACCOUNT}:role/{kwargs['RoleName']}", "Tags": self.tags}
        if self.boundary:
            role["PermissionsBoundary"] = {"PermissionsBoundaryType": "Policy", "PermissionsBoundaryArn": self.boundary}
        return {"Role": role}

    def put_role_permissions_boundary(self, **kwargs):
        self._record("put_role_permissions_boundary", kwargs)
        if self.put_boundary_error is not None:
            raise self.put_boundary_error

    def tag_role(self, **kwargs):
        self._record("tag_role", kwargs)

    def put_role_policy(self, **kwargs):
        self._record("put_role_policy", kwargs)

    def attach_role_policy(self, **kwargs):
        self._record("attach_role_policy", kwargs)

    def update_assume_role_policy(self, **kwargs):
        self._record("update_assume_role_policy", kwargs)

    def list_attached_role_policies(self, **kwargs):
        self._record("list_attached_role_policies", kwargs)
        return {"AttachedPolicies": [], "IsTruncated": False}


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_unset_or_blank_variable_means_no_boundary(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(iam_boundary.BOUNDARY_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(iam_boundary.BOUNDARY_ENV_VAR, value)
    assert iam_boundary.boundary_arn() is None
    assert iam_boundary.create_role_kwargs() == {}
    iam = _FakeIam(collide=True, tags=ro.owner_tag_list(REGION))
    assert iam_boundary.ensure_role_boundary(iam, "AgentCoreRuntime-x") is False
    assert iam.calls == [], "nothing may be read or written while no boundary is configured"


def test_a_set_variable_is_sent_on_create(boundary_set):
    assert iam_boundary.create_role_kwargs() == {"PermissionsBoundary": BOUNDARY}


def test_ensure_puts_the_boundary_on_a_role_that_lacks_it_without_a_second_read(boundary_set):
    iam = _FakeIam()
    role = {"Arn": "arn:aws:iam::166827918465:role/AgentCoreRuntime-x", "Tags": []}
    assert iam_boundary.ensure_role_boundary(iam, "AgentCoreRuntime-x", role=role) is True
    assert iam.calls == [
        ("put_role_permissions_boundary", {"RoleName": "AgentCoreRuntime-x", "PermissionsBoundary": BOUNDARY})
    ]


def test_ensure_replaces_a_different_boundary(boundary_set):
    iam = _FakeIam()
    role = {"PermissionsBoundary": {"PermissionsBoundaryArn": OTHER_BOUNDARY}}
    assert iam_boundary.ensure_role_boundary(iam, "AgentCoreRuntime-x", role=role) is True
    assert iam.names() == ["put_role_permissions_boundary"]


def test_ensure_skips_the_put_when_the_boundary_is_already_this_one(boundary_set):
    iam = _FakeIam()
    role = {"PermissionsBoundary": {"PermissionsBoundaryType": "Policy", "PermissionsBoundaryArn": BOUNDARY}}
    assert iam_boundary.ensure_role_boundary(iam, "AgentCoreRuntime-x", role=role) is False
    assert iam.calls == []


def test_ensure_reads_once_when_no_role_is_supplied(boundary_set):
    iam = _FakeIam(tags=[])
    assert iam_boundary.ensure_role_boundary(iam, "AgentCoreRuntime-x") is True
    assert iam.names() == ["get_role", "put_role_permissions_boundary"]


# ---------------------------------------------------------------------------
# Structural: every site, not just the four driven below
# ---------------------------------------------------------------------------


def _create_role_calls():
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "create_role":
                yield path, node


def _passes_boundary_kwargs(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg is None and isinstance(kw.value, ast.Call):
            func = kw.value.func
            if (isinstance(func, ast.Name) and func.id == "create_role_kwargs") or (
                isinstance(func, ast.Attribute) and func.attr == "create_role_kwargs"
            ):
                return True
    return False


def test_every_create_role_in_the_app_passes_the_boundary_kwargs():
    """Ten sites today (infra ledger F-06 lists them); an eleventh must not arrive without it."""
    sites = list(_create_role_calls())
    assert len(sites) >= 10, [f"{p.relative_to(SRC)}:{n.lineno}" for p, n in sites]
    missing = [f"{p.relative_to(SRC).as_posix()}:{n.lineno}" for p, n in sites if not _passes_boundary_kwargs(n)]
    assert missing == []


def _already_exists_handlers():
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is not None:
                if "EntityAlreadyExistsException" in ast.unparse(node.type):
                    yield path, node


def _calls_ensure_boundary(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(handler):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "ensure_role_boundary":
                return True
    return False


def test_every_already_exists_branch_retrofits_the_boundary():
    """A role minted before the boundary existed is otherwise unbounded forever, and under
    enforcement its next PutRolePolicy is denied with no hint why."""
    handlers = list(_already_exists_handlers())
    assert len(handlers) >= 10, [f"{p.relative_to(SRC)}:{n.lineno}" for p, n in handlers]
    missing = [f"{p.relative_to(SRC).as_posix()}:{n.lineno}" for p, n in handlers if not _calls_ensure_boundary(n)]
    assert missing == []


def test_the_retrofit_follows_the_ownership_proof_in_every_branch():
    """Order inside each handler: assert_this_deployment_may_mutate / can_this_deployment_mutate
    BEFORE ensure_role_boundary. A foreign role must never be mutated, and a boundary put is a
    mutation."""
    out_of_order = []
    for path, handler in _already_exists_handlers():
        proof_line = None
        ensure_line = None
        for node in ast.walk(handler):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name in (
                    "assert_this_deployment_may_mutate",
                    "can_this_deployment_mutate",
                    "assert_resource_bound_to_deployment",
                ):
                    proof_line = min(proof_line or node.lineno, node.lineno)
                if name == "ensure_role_boundary":
                    ensure_line = node.lineno
        if ensure_line is not None and (proof_line is None or proof_line > ensure_line):
            out_of_order.append(f"{path.relative_to(SRC).as_posix()}:{ensure_line}")
    assert out_of_order == []


# ---------------------------------------------------------------------------
# Behavioural: the four sites a fake IAM client can drive
# ---------------------------------------------------------------------------


def _drive_runtime(iam):
    from app.services import runtime_deployer

    return runtime_deployer.create_runtime_iam_role(
        iam_client=iam, role_name="AgentCoreRuntime-boundary-probe", account_id=ACCOUNT, region=REGION
    )


def _drive_harness(iam):
    from app.services import harness_deployer

    return harness_deployer.create_harness_iam_role(
        iam, "AgentCoreHarness-boundary-probe", harness_name="probe", region=REGION
    )


def _drive_tool_lambda(iam):
    from app.services import gateway_deployer

    return gateway_deployer._ensure_lambda_role(iam, "AgentCoreProbeToolLambdaRole", "probe tool lambda", region=REGION)


def _drive_sandbox(iam):
    from app.services import tool_tester

    return tool_tester._ensure_sandbox_role(iam, region=REGION)


SITES = {
    "runtime_deployer.create_runtime_iam_role": _drive_runtime,
    "harness_deployer.create_harness_iam_role": _drive_harness,
    "gateway_deployer._ensure_lambda_role": _drive_tool_lambda,
    "tool_tester._ensure_sandbox_role": _drive_sandbox,
}


def _created_kwargs(iam: _FakeIam) -> dict:
    return next(kwargs for name, kwargs in iam.calls if name == "create_role")


@pytest.mark.parametrize("site", sorted(SITES))
def test_a_created_role_carries_the_boundary(boundary_set, site):
    iam = _FakeIam()
    SITES[site](iam)
    assert _created_kwargs(iam)["PermissionsBoundary"] == BOUNDARY
    assert "put_role_permissions_boundary" not in iam.names(), "a fresh role needs no retrofit"


@pytest.mark.parametrize("site", sorted(SITES))
def test_without_the_variable_a_created_role_is_exactly_what_it_was_before(site):
    iam = _FakeIam()
    SITES[site](iam)
    assert "PermissionsBoundary" not in _created_kwargs(iam)
    assert "put_role_permissions_boundary" not in iam.names()


@pytest.mark.parametrize("site", sorted(SITES))
def test_an_adopted_role_without_the_boundary_is_retrofitted_after_the_ownership_read(boundary_set, site):
    iam = _FakeIam(collide=True, tags=ro.owner_tag_list(REGION))
    SITES[site](iam)
    names = iam.names()
    puts = [kwargs for name, kwargs in iam.calls if name == "put_role_permissions_boundary"]
    assert len(puts) == 1, names
    assert puts[0]["PermissionsBoundary"] == BOUNDARY
    assert names.index("get_role") < names.index("put_role_permissions_boundary")
    # the retrofit precedes every widening of the role
    for widening in ("put_role_policy", "attach_role_policy"):
        if widening in names:
            assert names.index("put_role_permissions_boundary") < names.index(widening), names


@pytest.mark.parametrize("site", sorted(SITES))
def test_an_adopted_role_that_already_carries_the_boundary_is_not_re_put(boundary_set, site):
    iam = _FakeIam(collide=True, tags=ro.owner_tag_list(REGION), boundary=BOUNDARY)
    SITES[site](iam)
    assert "put_role_permissions_boundary" not in iam.names()


@pytest.mark.parametrize("site", sorted(SITES))
def test_without_the_variable_an_adopted_role_is_left_alone(site):
    iam = _FakeIam(collide=True, tags=ro.owner_tag_list(REGION))
    SITES[site](iam)
    assert "put_role_permissions_boundary" not in iam.names()


@pytest.mark.parametrize("site", sorted(SITES))
def test_a_foreign_role_is_refused_before_any_boundary_put(boundary_set, site):
    iam = _FakeIam(collide=True, tags=None)
    with pytest.raises(ro.ForeignResourceError):
        SITES[site](iam)
    assert "put_role_permissions_boundary" not in iam.names()


@pytest.mark.parametrize("site", sorted(SITES))
def test_a_refused_boundary_put_fails_the_deploy_before_the_role_is_widened(boundary_set, site):
    """Fail closed: an unbounded role that IAM will not let us bound must not receive the
    inline policy / managed policies this deploy was about to write onto it."""
    denied = RuntimeError("AccessDenied: iam:PutRolePermissionsBoundary")
    iam = _FakeIam(collide=True, tags=ro.owner_tag_list(REGION), put_boundary_error=denied)
    with pytest.raises(RuntimeError, match="PutRolePermissionsBoundary"):
        SITES[site](iam)
    after = iam.names()[iam.names().index("put_role_permissions_boundary") + 1 :]
    assert "put_role_policy" not in after and "attach_role_policy" not in after, iam.names()


@pytest.mark.parametrize("site", sorted(SITES))
def test_a_denied_put_is_terminal_and_never_retried(boundary_set, site):
    """An AccessDenied on PutRolePermissionsBoundary is a grant that is missing, not a transient:
    it raises at once (infra treats a denial retried for a deadline as a defect class), is
    issued exactly once, and nothing is written onto the role afterwards."""
    from botocore.exceptions import ClientError

    denied = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "not authorized to perform: iam:PutRolePermissionsBoundary"}},
        "PutRolePermissionsBoundary",
    )
    iam = _FakeIam(collide=True, tags=ro.owner_tag_list(REGION), put_boundary_error=denied)
    with pytest.raises(ClientError, match="PutRolePermissionsBoundary"):
        SITES[site](iam)
    names = iam.names()
    assert names.count("put_role_permissions_boundary") == 1, names
    after = names[names.index("put_role_permissions_boundary") + 1 :]
    assert "put_role_policy" not in after and "attach_role_policy" not in after, names


# ---------------------------------------------------------------------------
# The shared runtime role is never the target of the put (Bug 62, ahead of the put)
# ---------------------------------------------------------------------------

SHARED_ROLE_NAME = "AgentCoreRuntime-acfe2e-p0920-shared"
SHARED_MCP_ROLE_NAME = "AgentCoreRuntime-acfe2e-p0920-mcp-shared"
CUSTOM_SHARED_ROLE_NAME = "AgentCoreCustomExecutionIdentity"


@pytest.fixture
def shared_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHARED_RUNTIME_ROLE_ARN", f"arn:aws:iam::{ACCOUNT}:role/{SHARED_ROLE_NAME}")
    monkeypatch.setenv("SHARED_MCP_RUNTIME_ROLE_ARN", f"arn:aws:iam::{ACCOUNT}:role/{SHARED_MCP_ROLE_NAME}")


@pytest.mark.parametrize(
    "role_name",
    [
        SHARED_ROLE_NAME,  # the name SHARED_RUNTIME_ROLE_ARN carries
        SHARED_MCP_ROLE_NAME,  # the name SHARED_MCP_RUNTIME_ROLE_ARN carries
        "AgentCoreRuntime-another-stack-shared",  # the suffix convention the Bug-62 teardown guard skips
        "AgentCoreRuntime-acfe2e-p0920-eu-central-1-shared",  # the shape outside the home region
        "AgentCoreRuntime-acfe2e-p0920-eu-central-1-mcp-shared",
        "AgentCoreRuntime-another-stack-mcp-shared",
        "AgentCoreFlowsRuntimeRole",  # the pre-Bug-60 name
    ],
)
def test_the_shared_runtime_role_is_refused_before_any_iam_call(boundary_set, shared_roles, role_name):
    """The shared role carries the stack's tags, so the ownership proof says 'ours'; the name
    guard is what keeps the put (which infra explicitly denies) from ever being issued."""
    iam = _FakeIam()
    unbounded = {"Arn": f"arn:aws:iam::{ACCOUNT}:role/{role_name}", "Tags": ro.owner_tag_list(REGION)}
    with pytest.raises(iam_boundary.SharedRuntimeRoleRefused, match=role_name):
        iam_boundary.ensure_role_boundary(iam, role_name, role=unbounded)
    assert iam.names() == []


def test_a_custom_named_shared_role_is_known_through_the_env_var(boundary_set, monkeypatch):
    monkeypatch.setenv("SHARED_RUNTIME_ROLE_ARN", f"arn:aws:iam::{ACCOUNT}:role/{CUSTOM_SHARED_ROLE_NAME}")
    iam = _FakeIam()
    with pytest.raises(iam_boundary.SharedRuntimeRoleRefused):
        iam_boundary.ensure_role_boundary(iam, CUSTOM_SHARED_ROLE_NAME, role={})
    assert iam.names() == []
    assert iam_boundary.is_shared_runtime_role(CUSTOM_SHARED_ROLE_NAME)
    assert not iam_boundary.is_shared_runtime_role("AgentCoreRuntime-my-agent")


def test_without_the_variable_the_shared_role_is_left_exactly_alone(shared_roles):
    """Unset == today's behaviour for the shared role too: no call and no new refusal here."""
    iam = _FakeIam()
    assert iam_boundary.ensure_role_boundary(iam, SHARED_ROLE_NAME, role={}) is False
    assert iam.names() == []


def test_a_per_deploy_role_that_resolves_to_the_shared_roles_name_is_refused_before_it_is_widened(
    boundary_set, shared_roles
):
    """iam_step's legacy path names roles ``AgentCoreRuntime-{runtime_name}`` and the stack's shared
    role is ``AgentCoreRuntime-{project}-{env}-shared``: a runtime called ``acfe2e-p0920-shared``
    collides, CreateRole says it exists, and the tags say it is ours. Nothing may be written."""
    from app.services import runtime_deployer

    iam = _FakeIam(collide=True, tags=ro.owner_tag_list(REGION))
    with pytest.raises(iam_boundary.SharedRuntimeRoleRefused):
        runtime_deployer.create_runtime_iam_role(iam, SHARED_ROLE_NAME, ACCOUNT, REGION, [])
    names = iam.names()
    assert "put_role_permissions_boundary" not in names, names
    assert "put_role_policy" not in names and "attach_role_policy" not in names, names

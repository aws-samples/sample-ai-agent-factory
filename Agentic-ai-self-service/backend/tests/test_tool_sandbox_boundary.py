"""The tool sandbox: what the AST check must refuse, and what isolates the rest.

F-11. ``tool_tester`` had no tests at all, which is how the gate below shipped
bypassable with a comment in ``gateway_deployer`` claiming it "prevents arbitrary
code execution".

The split this file pins is the whole point, and it comes from ARCC
cnt_MSVB0Kk8WMwmmW ("Isolation requirements for executing customer-provided code
in multi-tenant environments"), which says a language restriction "may not be
substituted" for isolation:

* ``TestTheDenylistClosesTheClass`` -- the AST check is defence in depth. It must
  refuse the escape *shapes*, not a list of bad names, because the original name
  list was walked around by ``getattr(builtins, '__im' + 'port__')``.
* ``TestTheSandboxIsTheBoundary`` -- the network and IAM scoping applied when the
  function is created. This is what actually holds.

``TestRealGeneratedToolsStillPass`` exists because a refusal-only suite hides a
dead happy path: a validator that rejects everything passes every test above it.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from app.services import tool_tester
from app.services.resource_ownership import ForeignResourceError
from app.services.tool_tester import (
    SandboxPolicyError,
    _deploy_temp_lambda,
    _ensure_sandbox_role,
    _sandbox_concurrency,
    _sandbox_role_name,
    _sandbox_vpc_config,
    _validate_code_safety,
)
from botocore.exceptions import ClientError

# NOTE: ``tool_tester.test_tool`` is reached through the module and never imported
# by name -- pytest would collect a top-level callable called ``test_tool`` as a
# test case and error on its arguments.

HANDLER = "def lambda_handler(event, context):\n    return {'r': tool()}\n"


def _module(body: str) -> str:
    """A complete candidate module: the payload plus the required entry point."""
    return f"{body}\n\n{HANDLER}"


# ---------------------------------------------------------------------------
# Defence in depth
# ---------------------------------------------------------------------------

# Every one of these was ACCEPTED by the original validator and reaches os or
# the interpreter's object graph. Reproduced against the real function before
# the fix was written -- a peer-reported finding is not evidence on its own.
ESCAPES = {
    "rebuild __import__ through getattr": "import builtins\n\n\ndef tool():\n"
    "    return getattr(builtins, '__im' + 'port__')('os').environ\n",
    "walk __globals__ with no import at all": "def tool():\n"
    "    g = getattr(tool, '__glo' + 'bals__')\n"
    "    return g['__bui' + 'ltins__']\n",
    "rebuild os.system through getattr": "import builtins\n\n\ndef tool():\n"
    "    m = getattr(builtins, '__im' + 'port__')('os')\n"
    "    return getattr(m, 'sys' + 'tem')\n",
    "read the role's AWS env": "import builtins\n\n\ndef tool():\n"
    "    env = getattr(builtins, '__im' + 'port__')('os').environ\n"
    "    return sorted(k for k in env if 'AWS' in k)\n",
    # These four mention neither `builtins` nor `getattr`, so only the
    # shape-based rules can refuse them. They were found by attacking the first
    # version of the fix, which refused the four above via a name on a list.
    "subclasses walk off a literal": "def tool():\n    return [].__class__.__mro__[1].__subclasses__()\n",
    "a lambda's __globals__": "def tool():\n    f = lambda: 0\n    return f.__globals__\n",  # noqa: E731
    "bare __builtins__ with a concatenated key": "def tool():\n    return __builtins__['__im' + 'port__']('os')\n",
    "bare __builtins__ with a plain attribute": "def tool():\n    return __builtins__.open('/etc/hostname')\n",
    "dynamic import via __loader__": "def tool():\n    return __loader__.load_module('os')\n",
}


class TestTheDenylistClosesTheClass:
    @pytest.mark.parametrize("label", sorted(ESCAPES))
    def test_each_known_escape_is_refused(self, label):
        ok, err = _validate_code_safety(_module(ESCAPES[label]))
        assert ok is False, f"{label}: accepted, so the AST check is not even defence in depth"
        assert err, "a refusal must say why, or the UI shows an empty error"

    def test_the_original_named_denylist_still_works(self):
        """The old checks are kept, not replaced -- they catch the careless case."""
        for body, expected in [
            ("import os\n\n\ndef tool():\n    return os.environ\n", "Blocked import: os"),
            ("def tool():\n    exec('1')\n", "Blocked function call: exec"),
            ("def tool(m):\n    return m.popen('x')\n", "Blocked function call: popen"),
        ]:
            ok, err = _validate_code_safety(_module(body))
            assert ok is False
            assert err == expected

    def test_a_dunder_is_refused_wherever_it_appears(self):
        """Attribute, subscript and bare name are three different AST nodes.

        The first version of this fix only handled Attribute, so ``__builtins__``
        as a bare name -- which is already in scope at module level -- walked
        straight through with a non-dunder attribute after it.
        """
        for body in [
            "def tool():\n    return tool.__globals__\n",  # Attribute
            "def tool():\n    return tool.__dict__['__x__']\n",  # Subscript
            "def tool():\n    return __loader__\n",  # Name
        ]:
            ok, _ = _validate_code_safety(_module(body))
            assert ok is False, body

    def test_inert_dunders_are_still_allowed(self):
        """Refusing __name__ would reject ordinary Python for no security gain."""
        ok, err = _validate_code_safety(_module("def tool():\n    return __name__\n"))
        assert ok is True, err

    def test_a_short_name_is_not_mistaken_for_a_dunder(self):
        """``__`` and ``___`` start and end with __ but are not dunders."""
        ok, err = _validate_code_safety(_module("def tool():\n    __ = 1\n    return __\n"))
        assert ok is True, err

    def test_oversize_code_is_refused_before_parsing(self):
        ok, err = _validate_code_safety("x = 1\n" * 20000)
        assert ok is False
        assert "too large" in err

    def test_a_syntax_error_names_the_line(self):
        ok, err = _validate_code_safety("def tool(:\n    pass\n")
        assert ok is False
        assert "line 1" in err


# ---------------------------------------------------------------------------
# The happy path. Without these, every assertion above is satisfied by a
# validator that refuses all input -- which is a feature outage, not a fix.
# ---------------------------------------------------------------------------

# The dual-mode handler that tool_generator.GENERATION_PROMPT tells the model to
# emit, verbatim in shape. If this is ever refused, tool generation is dead.
GENERATED_DUAL_MODE = """
import json


def lambda_handler(event, context):
    try:
        custom = context.client_context.custom
        if isinstance(custom, str):
            custom = json.loads(custom)
        tool_name = custom.get('bedrockAgentCoreToolName', '')
        params = event
    except (AttributeError, TypeError):
        tool_name = event.get('toolName', '')
        params = event.get('input', {})
    query = params.get("query", "")
    return {"statusCode": 200, "body": json.dumps({"tool": tool_name, "query": query})}
"""

GENERATED_HTTP_TOOL = """
import json
import urllib.parse
import urllib.request


def lambda_handler(event, context):
    params = event.get("input", event)
    token = params.get("token", "")
    url = "https://api.example.com/v1/search?" + urllib.parse.urlencode({"q": params.get("q", "")})
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())
    except Exception as exc:
        body = {"mock": True, "error": str(exc)}
    return {"statusCode": 200, "body": json.dumps(body)}
"""

GENERATED_BOTO3_TOOL = """
import json
from datetime import datetime, timezone

import boto3


def lambda_handler(event, context):
    params = event.get("input", event)
    table = boto3.resource("dynamodb").Table(params.get("table", "t"))
    item = table.get_item(Key={"id": params["id"]}).get("Item", {})
    item["read_at"] = datetime.now(timezone.utc).isoformat()
    return {"statusCode": 200, "body": json.dumps(item, default=str)}
"""

GENERATED_PURE_PYTHON = """
import json
import math
import re
from decimal import Decimal


def lambda_handler(event, context):
    params = event.get("input", event)
    text = re.sub(r"\\s+", " ", str(params.get("text", "")))
    score = math.sqrt(len(text)) * float(Decimal("1.5"))
    return {"statusCode": 200, "body": json.dumps({"text": text, "score": round(score, 3)})}
"""


class TestRealGeneratedToolsStillPass:
    @pytest.mark.parametrize(
        "label,code",
        [
            ("the prompt's dual-mode handler", GENERATED_DUAL_MODE),
            ("an HTTP API tool over urllib", GENERATED_HTTP_TOOL),
            ("a boto3 + datetime tool", GENERATED_BOTO3_TOOL),
            ("a json/math/re/decimal tool", GENERATED_PURE_PYTHON),
        ],
    )
    def test_it_is_accepted(self, label, code):
        ok, err = _validate_code_safety(code)
        assert ok is True, f"{label} was refused ({err}) -- the tool generator is broken"

    def test_outbound_http_is_not_an_ast_concern(self):
        """urllib stays allowed on purpose.

        The generator's own contract is "stdlib + urllib + boto3" because tools
        call HTTP APIs, so refusing egress here would delete the feature. Egress
        is the security group's job. Pinned so nobody "hardens" it into the
        validator and silently breaks every API-calling tool.
        """
        ok, _ = _validate_code_safety(GENERATED_HTTP_TOOL)
        assert ok is True

    def test_the_required_entry_point_is_still_required(self):
        ok, err = _validate_code_safety("import json\n\n\ndef helper():\n    return json.dumps({})\n")
        assert ok is False
        assert "lambda_handler" in err


# ---------------------------------------------------------------------------
# The actual boundary
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_logs_client(monkeypatch):
    """No test in this file may build a real CloudWatch Logs client.

    ``test_tool`` governs the sandbox's log group before creating the function, so
    every test that reaches step 1.5 would otherwise call ``boto3.client("logs")``
    and then ``create_log_group`` against whatever account the developer or CI
    happens to be authenticated to -- creating real log groups as a side effect of
    a unit test. Autouse so a test added later cannot reintroduce that by omission;
    the tests that assert the governing behaviour install their own mock over this.
    """
    monkeypatch.setattr(tool_tester, "_create_logs_client", lambda region=None: MagicMock())


@pytest.fixture
def clean_env(monkeypatch):
    for var in (
        "TOOL_SANDBOX_SUBNET_IDS",
        "TOOL_SANDBOX_SECURITY_GROUP_IDS",
        "TOOL_SANDBOX_RESERVED_CONCURRENCY",
        # Deleted, not set: the default posture is the thing under test now that
        # isolation is required unless explicitly waived, so a value inherited from
        # the developer's shell would quietly decide the outcome of half this file.
        "TOOL_SANDBOX_REQUIRE_ISOLATION",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PROJECT_NAME", "probe")
    monkeypatch.setenv("ENVIRONMENT", "t1")
    return monkeypatch


class TestTheSandboxIsTheBoundary:
    def test_a_vpc_is_attached_when_the_deployment_provides_one(self, clean_env):
        clean_env.setenv("TOOL_SANDBOX_SUBNET_IDS", "subnet-1, subnet-2")
        clean_env.setenv("TOOL_SANDBOX_SECURITY_GROUP_IDS", "sg-1")
        client = MagicMock()
        client.get_function.return_value = {"Configuration": {"State": "Active"}}

        _deploy_temp_lambda(client, "fn", "arn:role", b"z", vpc_config=_sandbox_vpc_config())

        kwargs = client.create_function.call_args.kwargs
        assert kwargs["VpcConfig"] == {
            "SubnetIds": ["subnet-1", "subnet-2"],
            "SecurityGroupIds": ["sg-1"],
        }

    def test_subnets_without_a_security_group_are_refused(self, clean_env):
        """The VPC default group usually allows all egress, which inverts the point."""
        clean_env.setenv("TOOL_SANDBOX_SUBNET_IDS", "subnet-1")
        with pytest.raises(SandboxPolicyError, match="SECURITY_GROUP"):
            _sandbox_vpc_config()

    def test_no_vpc_property_is_sent_when_unconfigured(self, clean_env):
        """An empty VpcConfig is rejected by Lambda, so it must be omitted."""
        assert _sandbox_vpc_config() is None
        client = MagicMock()
        client.get_function.return_value = {"Configuration": {"State": "Active"}}

        _deploy_temp_lambda(client, "fn", "arn:role", b"z", vpc_config=None)

        assert "VpcConfig" not in client.create_function.call_args.kwargs

    def test_running_without_isolation_is_logged_as_such(self, clean_env, caplog):
        """The posture must be visible. An existing deployment has no sandbox VPC
        and failing closed would delete a working feature on upgrade, so the
        warning is the only thing that distinguishes "chosen" from "forgotten"."""
        caplog.set_level("WARNING", logger=tool_tester.__name__)
        client = MagicMock()
        client.get_function.return_value = {"Configuration": {"State": "Active"}}

        _deploy_temp_lambda(client, "fn", "arn:role", b"z", vpc_config=None)

        assert any("WITHOUT network isolation" in r.message for r in caplog.records)

    def test_concurrency_is_reserved_before_the_first_invoke(self, clean_env):
        client = MagicMock()
        client.get_function.return_value = {"Configuration": {"State": "Active"}}

        _deploy_temp_lambda(client, "fn", "arn:role", b"z")

        client.put_function_concurrency.assert_called_once_with(FunctionName="fn", ReservedConcurrentExecutions=2)

    def test_a_missing_concurrency_grant_does_not_break_tool_testing(self, clean_env):
        """It is a blast-radius limit, not the boundary; degrade, do not fail."""
        client = MagicMock()
        client.get_function.return_value = {"Configuration": {"State": "Active"}}
        client.put_function_concurrency.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "PutFunctionConcurrency"
        )

        _deploy_temp_lambda(client, "fn", "arn:role", b"z")  # must not raise

        client.create_function.assert_called_once()

    def test_the_concurrency_cap_is_configurable(self, clean_env):
        clean_env.setenv("TOOL_SANDBOX_RESERVED_CONCURRENCY", "7")
        assert _sandbox_concurrency() == 7

    @pytest.mark.parametrize("bad", ["", "abc", "0", "-3"])
    def test_a_useless_concurrency_value_falls_back_to_the_default(self, clean_env, bad):
        """0 would make every tool test fail as if the feature were broken."""
        clean_env.setenv("TOOL_SANDBOX_RESERVED_CONCURRENCY", bad)
        assert _sandbox_concurrency() == 2

    def test_the_function_carries_owner_tags(self, clean_env):
        """Teardown must be able to tell this function from a foreign one."""
        client = MagicMock()
        client.get_function.return_value = {"Configuration": {"State": "Active"}}

        _deploy_temp_lambda(client, "fn", "arn:role", b"z", region="us-east-1")

        assert client.create_function.call_args.kwargs["Tags"]["AgentCoreStack"] == "probe-t1-us-east-1"

    def test_the_tags_name_the_region_being_deployed_to(self, clean_env):
        """Not the ambient one. owner_tag_list() defaults to AWS_REGION, so a
        function created in eu-central-1 was tagged with whatever region the
        caller's environment happened to name, and its own region's teardown
        then could not recognise it. This platform deploys one project/env to
        two regions, so the two differ in practice."""
        clean_env.setenv("AWS_REGION", "us-west-2")
        client = MagicMock()
        client.get_function.return_value = {"Configuration": {"State": "Active"}}

        _deploy_temp_lambda(client, "fn", "arn:role", b"z", region="eu-central-1")

        tags = client.create_function.call_args.kwargs["Tags"]
        assert tags["AgentCoreStack"] == "probe-t1-eu-central-1"


class TestTheSandboxRoleIsNotShared:
    def test_the_role_name_is_scoped_to_the_deployment(self, clean_env):
        """Not the account-global AgentCoreToolTestRole it used to use.

        A shared role means one tenant's untrusted code runs with whatever
        another deployment granted.
        """
        assert _sandbox_role_name("us-east-1") == "AgentCore-ToolSandbox-probe-t1-us-east-1"
        assert _sandbox_role_name("us-east-1") != tool_tester.TOOL_TEST_ROLE_NAME

    def test_a_long_project_name_still_yields_a_legal_role_name(self, clean_env):
        clean_env.setenv("PROJECT_NAME", "p" * 80)
        name = _sandbox_role_name("eu-central-1")
        assert len(name) <= 64, name

    def test_two_regions_do_not_share_a_role(self, clean_env):
        """IAM roles are global, so the region has to be in the name."""
        assert _sandbox_role_name("us-east-1") != _sandbox_role_name("eu-central-1")

    def test_an_untagged_role_of_the_same_name_is_refused(self, clean_env):
        """Fail closed, unlike _ensure_lambda_role, which adopts on purpose.

        Adoption is right for the account-global singletons that predate this
        stack. It is wrong here: the role backs code the platform did not write,
        so running under permissions nobody in this system chose is the exact
        escalation this finding is about.
        """
        iam = MagicMock()
        iam.exceptions.EntityAlreadyExistsException = ClientError
        iam.create_role.side_effect = ClientError(
            {"Error": {"Code": "EntityAlreadyExists", "Message": "exists"}}, "CreateRole"
        )
        iam.get_role.return_value = {"Role": {"Arn": "arn:aws:iam::1:role/x", "Tags": []}}

        # ForeignResourceError, via the shared helper every other adopt-by-name
        # branch in the app uses -- pinned by
        # test_foreign_role_is_not_mutated.test_every_already_exists_branch_consults_ownership,
        # which failed when this module hand-rolled its own tag comparison.
        with pytest.raises(ForeignResourceError, match="will not modify it"):
            _ensure_sandbox_role(iam, region="us-east-1")

        iam.attach_role_policy.assert_not_called()

    def test_this_deployment_s_own_role_is_reused(self, clean_env, monkeypatch):
        # This role is reused with nothing attached, so the propagation wait now
        # fires (see TestIamPropagationIsNotAMissingGrant); don't pay it for real.
        monkeypatch.setattr(tool_tester.time, "sleep", lambda _s: None)
        iam = MagicMock()
        iam.exceptions.EntityAlreadyExistsException = ClientError
        iam.create_role.side_effect = ClientError(
            {"Error": {"Code": "EntityAlreadyExists", "Message": "exists"}}, "CreateRole"
        )
        iam.get_role.return_value = {
            "Role": {
                "Arn": "arn:aws:iam::1:role/x",
                "Tags": [{"Key": "AgentCoreStack", "Value": "probe-t1-us-east-1"}],
            }
        }
        iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}

        assert _ensure_sandbox_role(iam, region="us-east-1") == "arn:aws:iam::1:role/x"

    def test_ownership_is_checked_against_the_target_region(self, clean_env, monkeypatch):
        """Regression: the check used the ambient AWS_REGION while the tag was
        written with the target region, so a deployment operating on its second
        region rejected the very role it had created and tool testing died."""
        monkeypatch.setattr(tool_tester.time, "sleep", lambda _s: None)
        clean_env.setenv("AWS_REGION", "us-west-2")
        iam = MagicMock()
        iam.exceptions.EntityAlreadyExistsException = ClientError
        iam.create_role.side_effect = ClientError(
            {"Error": {"Code": "EntityAlreadyExists", "Message": "exists"}}, "CreateRole"
        )
        iam.get_role.return_value = {
            "Role": {
                "Arn": "arn:aws:iam::1:role/x",
                "Tags": [{"Key": "AgentCoreStack", "Value": "probe-t1-eu-central-1"}],
            }
        }
        iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}

        assert _ensure_sandbox_role(iam, region="eu-central-1") == "arn:aws:iam::1:role/x"

    def test_a_new_role_is_created_with_owner_tags(self, clean_env):
        iam = MagicMock()
        iam.exceptions.EntityAlreadyExistsException = ClientError
        iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::1:role/new"}}
        iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}

        _ensure_sandbox_role(iam, region="us-east-1")

        tags = {t["Key"]: t["Value"] for t in iam.create_role.call_args.kwargs["Tags"]}
        assert tags["AgentCoreStack"] == "probe-t1-us-east-1"

    def test_eni_permissions_are_attached_only_with_a_vpc(self, clean_env):
        """AWSLambdaBasicExecutionRole cannot create an ENI, so a VPC sandbox
        whose role lacks this goes green and then fails every invoke. Granting it
        unconditionally would hand ENI management to code that never needs it."""
        for need_vpc, expected in [(False, 1), (True, 2)]:
            iam = MagicMock()
            iam.exceptions.EntityAlreadyExistsException = ClientError
            iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::1:role/new"}}
            iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}

            _ensure_sandbox_role(iam, region="us-east-1", need_vpc=need_vpc)

            attached = [c.kwargs["PolicyArn"] for c in iam.attach_role_policy.call_args_list]
            assert len(attached) == expected, attached
            assert (tool_tester.VPC_ACCESS_POLICY in attached) is need_vpc

    def test_an_already_attached_policy_is_not_reattached(self, clean_env):
        iam = MagicMock()
        iam.exceptions.EntityAlreadyExistsException = ClientError
        iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::1:role/new"}}
        iam.list_attached_role_policies.return_value = {
            "AttachedPolicies": [{"PolicyArn": tool_tester.BASIC_EXECUTION_POLICY}]
        }

        _ensure_sandbox_role(iam, region="us-east-1")

        iam.attach_role_policy.assert_not_called()

    def test_attached_policies_on_page_two_are_not_reattached(
        self,
        clean_env,
        monkeypatch,
    ):
        monkeypatch.setattr(tool_tester.time, "sleep", lambda _s: None)
        iam = MagicMock()
        iam.exceptions.EntityAlreadyExistsException = ClientError
        iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::1:role/new"}}
        iam.list_attached_role_policies.side_effect = [
            {
                "AttachedPolicies": [
                    {"PolicyArn": tool_tester.BASIC_EXECUTION_POLICY},
                ],
                "IsTruncated": True,
                "Marker": "page-2",
            },
            {
                "AttachedPolicies": [
                    {"PolicyArn": tool_tester.VPC_ACCESS_POLICY},
                ],
                "IsTruncated": False,
            },
        ]

        _ensure_sandbox_role(
            iam,
            region="us-east-1",
            need_vpc=True,
        )

        assert [invocation.kwargs for invocation in iam.list_attached_role_policies.call_args_list] == [
            {"RoleName": tool_tester._sandbox_role_name("us-east-1")},
            {
                "RoleName": tool_tester._sandbox_role_name("us-east-1"),
                "Marker": "page-2",
            },
        ]
        iam.attach_role_policy.assert_not_called()


class TestNoExecutionEnvironmentIsShared:
    def test_each_test_gets_its_own_function_name(self):
        """Stronger than the per-customer function ARCC cnt_MSVB0Kk8WMwmmW asks
        for: a fresh uuid per invocation means no warm environment is ever reused
        between callers, so there is nothing to leak into."""
        names = set()
        for _ in range(50):
            names.add(_fresh_function_name())
        assert len(names) == 50

    def test_the_function_is_deleted_even_when_a_test_case_raises(self, clean_env, monkeypatch):
        deleted: list[str] = []
        lambda_client = MagicMock()
        lambda_client.get_function.return_value = {"Configuration": {"State": "Active"}}
        lambda_client.delete_function.side_effect = lambda FunctionName: deleted.append(FunctionName)

        monkeypatch.setattr(tool_tester, "_create_lambda_client", lambda region: lambda_client)
        monkeypatch.setattr(tool_tester, "_create_iam_client", MagicMock())
        monkeypatch.setattr(tool_tester, "_ensure_sandbox_role", lambda *a, **k: "arn:role")
        monkeypatch.setattr(tool_tester, "_create_lambda_zip", lambda code: b"z")
        monkeypatch.setattr(tool_tester, "_deploy_temp_lambda", MagicMock(side_effect=RuntimeError("deploy blew up")))

        result = tool_tester.test_tool(GENERATED_PURE_PYTHON, [{"name": "t"}])

        assert result["success"] is False
        assert len(deleted) == 1, "a leaked sandbox function keeps running untrusted code"

    def test_unsafe_code_never_reaches_aws(self, monkeypatch):
        """The refusal must happen before any client is built, or a rejected
        payload still costs an IAM role and a Lambda."""
        boom = MagicMock(side_effect=AssertionError("must not be called"))
        monkeypatch.setattr(tool_tester, "_create_lambda_client", boom)
        monkeypatch.setattr(tool_tester, "_create_iam_client", boom)

        result = tool_tester.test_tool(_module(ESCAPES["a lambda's __globals__"]), [])

        assert result["success"] is False
        assert "safety validation failed" in result["error"]


def _fresh_function_name() -> str:
    """Mirror of the name construction in test_tool, which is inline there."""
    import uuid

    return f"{tool_tester.TOOL_TEST_FN_PREFIX}{uuid.uuid4().hex[:8]}"


class TestCustomToolNamesAreTenantScoped:
    """The persistent path, which is worse than the ephemeral one.

    ``deploy_gateway`` names a custom tool's Lambda after the TOOL, so two
    tenants who both created a tool called "lookup" got one function: the second
    deploy took ``_create_or_update_lambda``'s already-exists branch and
    ``update_function_code`` replaced the first tenant's code, while the resource
    policy kept the first tenant's gateway authorized to invoke it.
    """

    @staticmethod
    def _names(
        tool_name: str,
        owner_sub: str,
        gateway_id: str = "gw-a",
        region: str = "us-east-1",
    ) -> tuple[str, str]:
        from app.services.gateway_deployer import _custom_tool_resource_names

        function_name, role_name, _safe_name, _binding = _custom_tool_resource_names(
            tool_name,
            owner_sub,
            gateway_id,
            region,
        )
        return function_name, role_name

    @staticmethod
    def _binding(
        tool_name: str,
        owner_sub: str,
        gateway_id: str = "gw-a",
        region: str = "us-east-1",
    ) -> str:
        from app.services.gateway_deployer import _custom_tool_resource_names

        return _custom_tool_resource_names(tool_name, owner_sub, gateway_id, region)[3]

    def test_two_tenants_with_the_same_tool_name_get_different_functions(self):
        a_fn, a_role = self._names("lookup", "sub-aaaa")
        b_fn, b_role = self._names("lookup", "sub-bbbb")
        assert a_fn != b_fn
        assert a_role != b_role

    def test_a_redeploy_onto_the_same_gateway_updates_the_one_function_its_target_invokes(self):
        """F-66: the gateway has ONE ``CT-<tool>`` target, so it can invoke only one function.

        A per-deployment name gave the redeploy its own function while the shared target
        kept invoking the first deployment's, which that deployment's teardown then deleted.
        """
        assert self._names("lookup", "sub-aaaa", "gw-a") == self._names("lookup", "sub-aaaa", "gw-a")

    def test_the_same_tenant_on_two_gateways_gets_two_functions(self):
        """Each gateway's target invokes its own function; one teardown cannot reach the other."""
        assert self._names("lookup", "sub-aaaa", "gw-a") != self._names("lookup", "sub-aaaa", "gw-b")

    def test_both_names_stay_within_the_aws_limits(self):
        fn, role = self._names("z" * 120, "sub-aaaa")
        assert len(fn) <= 64, fn
        assert len(role) <= 64, role

    def test_missing_gateway_id_is_refused(self):
        from app.services.gateway_deployer import _custom_tool_resource_names

        with pytest.raises(RuntimeError, match="gateway id"):
            _custom_tool_resource_names("lookup", "sub-aaaa", "", "us-east-1")

    def test_the_scope_binding_separates_the_same_things_the_names_do(self):
        """The binding is the ``ToolScope`` tag, and the tag — not the name — is what is
        verified on the function and the role before either is reused. So it has to draw
        the same two boundaries the names draw, or the verification passes across a
        boundary the name was keeping apart.
        """
        assert self._binding("lookup", "sub-aaaa") != self._binding("lookup", "sub-bbbb")
        assert self._binding("lookup", "sub-aaaa", "gw-a") != self._binding("lookup", "sub-aaaa", "gw-b")
        assert self._binding("lookup", "sub-aaaa", "gw-a") == self._binding("lookup", "sub-aaaa", "gw-a")

    def test_the_binding_is_the_scope_and_not_the_tool(self):
        """Two tools deployed to one owner's gateway are in ONE scope. If the binding
        varied by tool name it would stop being a scope identity, and the co-residency
        gate that keeps a shared function alive would be reading a per-tool value.
        """
        assert self._binding("lookup", "sub-aaaa") == self._binding("search", "sub-aaaa")

    def test_the_name_carries_only_a_prefix_of_the_binding(self):
        """ "The name is never the sole binding": the name clips the digest to 12 hex to
        fit 64 characters, so two scopes CAN collide in the name. What makes that safe is
        that the full digest is still checked, so the clip must be a genuine prefix of the
        value being checked rather than a separately-derived string that merely looks like
        one.
        """
        fn, role = self._names("lookup", "sub-aaaa")
        binding = self._binding("lookup", "sub-aaaa")
        assert len(binding) == 64, binding
        assert binding[:12] in fn, (fn, binding)
        assert binding[:12] in role, (role, binding)
        # A prefix, not merely a substring found anywhere in the digest.
        assert fn.endswith(binding[:12]), fn


def test_no_comment_claims_the_ast_check_is_a_boundary():
    """It claimed to "prevent arbitrary code execution" while being bypassable,
    which made the missing isolation read as a deliberate choice. ARCC
    cnt_MSVB0Kk8WMwmmW: a language restriction may not substitute for isolation.
    """
    root = os.path.dirname(os.path.abspath(tool_tester.__file__))
    for name in ("tool_tester.py", "gateway_deployer.py"):
        with open(os.path.join(root, name)) as fh:
            text = fh.read()
        assert "This prevents arbitrary code execution" not in text, name


# ---------------------------------------------------------------------------
# Enforced mode, and what a failure is allowed to say
# ---------------------------------------------------------------------------


class TestIsolationIsRequiredByDefault:
    """Isolation is the default, and only an explicit opt-out turns it off.

    This class previously pinned the opposite: the switch defaulted OFF, argued for
    on the grounds that a sandbox with no egress cannot test a tool that calls an
    HTTP API, and most generated tools do. A reviewer pushed back and ARCC settles
    it -- ``cnt_MSVB0Kk8WMwmmW`` requires customer-provided code to run "from a
    private network ... with no public internet access", and a control that is off
    until an operator finds the switch is not a control. The platform stack now
    provisions the isolated VPC itself, so the default costs nothing here.

    The consequence is deliberate and is asserted below: an *unset* variable
    enforces, so a deployment that never heard of this setting fails closed.
    """

    def test_no_subnets_refuses_before_any_aws_call(self, clean_env, monkeypatch):
        """With nothing configured at all -- the pre-upgrade environment."""
        created = []
        monkeypatch.setattr(tool_tester, "_create_lambda_client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(tool_tester, "_create_iam_client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(tool_tester, "_deploy_temp_lambda", lambda *a, **k: created.append(a))

        result = tool_tester.test_tool(HANDLER, [], region="us-east-1")

        assert result["success"] is False
        # The message must name both the missing input and the way out, or an
        # operator sees a feature that stopped working and no way to act on it.
        assert "TOOL_SANDBOX_SUBNET_IDS" in result["error"]
        assert "TOOL_SANDBOX_REQUIRE_ISOLATION" in result["error"]
        assert created == [], "must refuse before creating the function"

    def test_the_configured_happy_path_still_runs(self, clean_env, monkeypatch):
        """The happy path, isolated. If this fails, the default deleted the feature.

        A refusal-only suite around a fail-closed default is compatible with
        refusing everything, which is why this test is here and not optional.
        """
        clean_env.setenv("TOOL_SANDBOX_SUBNET_IDS", "subnet-1,subnet-2")
        clean_env.setenv("TOOL_SANDBOX_SECURITY_GROUP_IDS", "sg-1")
        seen: list[dict | None] = []
        monkeypatch.setattr(tool_tester, "_create_lambda_client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(tool_tester, "_create_iam_client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(tool_tester, "_ensure_sandbox_role", lambda *a, **k: "arn:role")
        monkeypatch.setattr(tool_tester, "_create_lambda_zip", lambda code: b"z")
        monkeypatch.setattr(tool_tester, "_deploy_temp_lambda", lambda *a, **k: seen.append(k.get("vpc_config")))

        result = tool_tester.test_tool(HANDLER, [], region="us-east-1")

        assert result["success"] is True
        assert seen == [{"SubnetIds": ["subnet-1", "subnet-2"], "SecurityGroupIds": ["sg-1"]}]

    def test_an_explicit_opt_out_runs_without_a_vpc(self, clean_env, monkeypatch):
        """The escape hatch, for a deployment whose Lambda cannot be placed in a VPC."""
        clean_env.setenv("TOOL_SANDBOX_REQUIRE_ISOLATION", "false")
        monkeypatch.setattr(tool_tester, "_create_lambda_client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(tool_tester, "_create_iam_client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(tool_tester, "_ensure_sandbox_role", lambda *a, **k: "arn:role")
        monkeypatch.setattr(tool_tester, "_deploy_temp_lambda", lambda *a, **k: None)
        monkeypatch.setattr(tool_tester, "_create_lambda_zip", lambda code: b"z")

        assert tool_tester.test_tool(HANDLER, [], region="us-east-1")["success"] is True

    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", " off "])
    def test_only_a_falsy_spelling_waives_it(self, clean_env, value):
        clean_env.setenv("TOOL_SANDBOX_REQUIRE_ISOLATION", value)
        assert tool_tester._isolation_required() is False

    @pytest.mark.parametrize("value", ["", "1", "true", "yes", "on", "maybe", "disabled"])
    def test_everything_else_enforces_including_empty_and_gibberish(self, clean_env, value):
        """An unrecognized value must not read as an opt-out.

        ``"disabled"`` is the trap: an operator who means to turn this off and picks
        a word the parser does not know gets the *safe* outcome, not the one they
        intended. A typo'd opt-in used to mean "not enforced"; a typo'd opt-out now
        means "still enforced".
        """
        clean_env.setenv("TOOL_SANDBOX_REQUIRE_ISOLATION", value)
        assert tool_tester._isolation_required() is True

    def test_an_unset_variable_enforces(self, clean_env):
        """The whole point of the flip. ``clean_env`` deletes the variable."""
        assert tool_tester._isolation_required() is True

    @pytest.mark.parametrize("waived", [False, True])
    def test_the_concurrency_cap_is_never_coupled_to_the_boundary(self, clean_env, waived):
        """A missing concurrency grant must not take tool testing down, either way.

        This assertion is here because the opposite was implemented first: the
        concurrency failure raised whenever ``_isolation_required()`` was true, which
        was harmless while that defaulted off and became a total feature outage the
        moment it defaulted on -- gated on one IAM action, exactly the shape the
        missing ``lambda:TagResource`` had just produced live. Parametrized over both
        settings so re-coupling it to either fails here.
        """
        if waived:
            clean_env.setenv("TOOL_SANDBOX_REQUIRE_ISOLATION", "false")
        client = MagicMock()
        client.get_function.return_value = {"Configuration": {"State": "Active"}}
        client.put_function_concurrency.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "PutFunctionConcurrency"
        )

        _deploy_temp_lambda(client, "fn", "arn:role", b"z", vpc_config={"SubnetIds": ["s"], "SecurityGroupIds": ["g"]})

        client.create_function.assert_called_once()


class TestAFailureDoesNotNameTheAccount:
    """``test_tool`` returned ``str(exc)``, which the poll route republishes.

    ARCC cnt_94E30Xo4RZHtSJ: return generic messages, keep the detail in the log.
    A boto3 error on this path names the account id and the role ARN.
    """

    def test_an_unexpected_failure_is_generic(self, clean_env, monkeypatch):
        monkeypatch.setattr(tool_tester, "_create_lambda_client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(tool_tester, "_create_iam_client", lambda *a, **k: MagicMock())

        def _boom(*a, **k):
            raise ClientError(
                {
                    "Error": {
                        "Code": "AccessDenied",
                        "Message": "User: arn:aws:sts::166827918465:assumed-role/AgentCore-Step is not authorized",
                    }
                },
                "AttachRolePolicy",
            )

        monkeypatch.setattr(tool_tester, "_ensure_sandbox_role", _boom)

        result = tool_tester.test_tool(HANDLER, [], region="us-east-1")

        assert result["success"] is False
        assert "166827918465" not in result["error"]
        assert "assumed-role" not in result["error"]
        assert "AccessDenied" not in result["error"]
        assert result["error"]

    def test_a_policy_refusal_is_still_published(self, clean_env, monkeypatch):
        """The carve-out: a SandboxPolicyError is the one message an operator needs."""
        clean_env.setenv("TOOL_SANDBOX_REQUIRE_ISOLATION", "true")
        monkeypatch.setattr(tool_tester, "_create_lambda_client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(tool_tester, "_create_iam_client", lambda *a, **k: MagicMock())

        result = tool_tester.test_tool(HANDLER, [], region="us-east-1")

        assert "Tool testing is disabled until" in result["error"]

    def test_the_code_refusal_still_names_the_blocked_construct(self, clean_env):
        """Unchanged and load-bearing: the user must be able to fix their own tool."""
        result = tool_tester.test_tool("import os\n\n\n" + HANDLER, [], region="us-east-1")
        assert result["error"] == "Code safety validation failed: Blocked import: os"


# ---------------------------------------------------------------------------
# Telling the user the sandbox is why their correct tool failed
# ---------------------------------------------------------------------------


class TestTheIsolationExplainsItself:
    """A network-isolated sandbox makes a *correct* HTTP tool fail its test.

    That is the trade-off the isolation buys (ARCC cnt_MSVB0Kk8WMwmmW), and it is
    only acceptable if it is legible. Unexplained, the user sees a connection
    timeout from code that works and rewrites it; worse, the generator panel's
    auto-fix loop asks the model to "repair" it, and the only repair available is
    to delete the network call the user asked for.

    So ``_isolation_note`` has to fire on a real network failure and stay silent
    otherwise -- a note on every result is noise, and noise is how the real
    explanation gets skipped.
    """

    @pytest.mark.parametrize("marker", tool_tester._NETWORK_FAILURE_MARKERS)
    def test_every_marker_produces_the_note(self, marker):
        """Parametrized over the implementation's own tuple, not a copy of it.

        Note what this can and cannot catch: deleting a marker deletes its own
        parametrization, so this test alone cannot detect a lost marker. That is
        what ``test_a_real_error_string_is_recognised`` below is for -- it holds a
        corpus of verbatim error text that does not move when the tuple does.
        """
        note = tool_tester._isolation_note([{"passed": False, "error": f"boom: {marker}"}], isolated=True)
        assert note is not None
        assert "no internet access" in note

    # Verbatim strings, as Python, urllib3 and botocore actually render them from a
    # subnet with no route out. Hardcoded on purpose: this corpus is the only thing
    # here that keeps failing when a marker is deleted from the implementation.
    @pytest.mark.parametrize(
        "error",
        [
            "<urlopen error [Errno -3] Temporary failure in name resolution>",
            "socket.gaierror: [Errno -2] Name or service not known",
            "socket.gaierror: [Errno 8] nodename nor servname provided, or not known",
            "ConnectionRefusedError: [Errno 111] Connection refused",
            "TimeoutError: [Errno 110] Connection timed out",
            "socket.timeout: timed out",
            "OSError: [Errno 101] Network is unreachable",
            "botocore.exceptions.EndpointConnectionError: Could not connect to the "
            'endpoint URL: "https://logs.us-east-1.amazonaws.com/"',
            "botocore.exceptions.ConnectTimeoutError: Connect timeout on endpoint URL: "
            '"https://bedrock-runtime.us-east-1.amazonaws.com/"',
            "botocore.exceptions.ReadTimeoutError: Read timeout on endpoint URL: "
            '"https://bedrock-runtime.us-east-1.amazonaws.com/"',
            "requests.exceptions.ConnectionError: HTTPSConnectionPool(host='api.example.com', "
            "port=443): Max retries exceeded with url: /v1/forecast",
            "urllib3.exceptions.ConnectTimeoutError: Connection to api.example.com timed out. (connect timeout=5)",
        ],
    )
    def test_a_real_error_string_is_recognised(self, error):
        assert tool_tester._isolation_note([{"passed": False, "error": error}], isolated=True) is not None

    # Payloads copied verbatim from live probes against the deployed sandbox, one per
    # era of its network configuration. Hardening the sandbox changed the errno it
    # produces, and the marker list did not follow, so for a while the note stopped
    # firing on the very failure it exists to explain.
    #
    #   before the DNS firewall: the name resolved and the address was unroutable
    #                            -> OSError(99) EADDRNOTAVAIL, after ~8.7 s
    #   after the DNS firewall:   the name does not resolve at all
    #                            -> OSError(16) EBUSY, after ~300 ms
    #
    # Both are recorded rather than replaced, because a sandbox that lost its DNS
    # firewall would regress to the first shape and this suite should still classify
    # it. The `raised`/`host` keys are the shape a tool that catches its own error
    # returns; `error` is the harness's own summary, which says nothing about the
    # network, which is exactly why the payload has to be scanned too.
    @pytest.mark.parametrize(
        ("era", "output"),
        [
            (
                "pre-firewall, EADDRNOTAVAIL",
                {
                    "error": "Network error contacting Open-Meteo: <urlopen error [Errno 99] "
                    "Cannot assign requested address>"
                },
            ),
            (
                "post-firewall, EBUSY via urllib",
                {"error": "Network error contacting Open-Meteo: <urlopen error [Errno 16] Device or resource busy>"},
            ),
            (
                "post-firewall, EBUSY with no urllib wrapper at all",
                {"host": "api.open-meteo.com", "raised": "OSError(16, 'Device or resource busy')"},
            ),
        ],
    )
    def test_what_the_deployed_sandbox_actually_returns_is_classified(self, era, output):
        """The regression test for the shape that slipped through.

        The third case is the one that mattered. ``http.client`` is importable --
        ``BLOCKED_IMPORTS`` holds ``"http.server"``, and the check compares the root
        package, which is ``"http"`` -- so a tool can reach the network without
        ``urllib``, and then nothing wraps the error in the ``"urlopen error"`` text
        that every other marker-matching case here happens to contain. Measured live:
        ``sandboxIsolated`` was ``True``, the failure was caused entirely by the
        sandbox, and ``note`` came back ``None``.
        """
        results = [
            {
                "testCaseName": "blocked_name_open_meteo",
                "passed": False,
                "error": "Expected statusCode 200, got 502",
                "actualOutput": output,
                "durationMs": 297,
            }
        ]
        assert tool_tester._isolation_note(results, isolated=True) is not None, (
            f"a live-measured sandbox failure ({era}) was not classified as a network "
            "failure, so no note fires and the frontend offers an auto-fix whose only "
            "available repair is deleting the network call the user asked for"
        )

    def test_the_allowed_destination_still_reaches_and_is_not_excused(self):
        """The other half, so the note cannot be made unconditional.

        The same live probe that produced the EBUSY payloads above also reached
        ``logs.us-east-1.amazonaws.com`` and got an HTTP 404 back in 716 ms, through
        the one endpoint the sandbox is allowed. A 404 from a destination that *is*
        reachable is the tool's own problem, and attaching a network note to it would
        send the user looking for an isolation issue that is not there.
        """
        results = [
            {
                "testCaseName": "allowed_name_logs_endpoint",
                "passed": False,
                "error": "Expected statusCode 200, got 404",
                "actualOutput": {"host": "logs.us-east-1.amazonaws.com", "reached": True, "status": 404},
                "durationMs": 716,
            }
        ]
        assert tool_tester._isolation_note(results, isolated=True) is None

    # A real Lambda error payload as ``_run_test_case`` builds it: ``errorMessage``
    # goes to ``error`` and the class name stays in ``actualOutput["errorType"]``.
    # In each of these the message alone says nothing about the network, so the
    # class name is the only signal there is.
    @pytest.mark.parametrize(
        ("error_type", "error_message"),
        [
            ("EndpointConnectionError", "An error occurred"),
            ("gaierror", "[Errno -3]"),
            ("ConnectTimeoutError", "endpoint https://bedrock-runtime.us-east-1.amazonaws.com/"),
            ("ReadTimeoutError", "endpoint https://bedrock-runtime.us-east-1.amazonaws.com/"),
            ("MaxRetryError", "HTTPSConnectionPool(host='api.example.com', port=443)"),
            ("URLError", "[Errno -3]"),
            ("ConnectionError", "HTTPSConnectionPool(host='api.example.com', port=443)"),
            ("NewConnectionError", "Failed to establish a new connection"),
            # Deliberately NOT here: ConnectionRefusedError. Its str() is always
            # "[Errno 111] Connection refused", so the message covers it and a
            # class-name marker for it would be an entry no test could defend --
            # exactly the dead weight test_no_marker_is_dead_weight exists to stop.
        ],
    )
    def test_the_exception_class_name_alone_is_enough(self, error_type, error_message):
        results = [
            {
                "testCaseName": "london",
                "passed": False,
                "error": error_message,
                "actualOutput": {"errorType": error_type, "errorMessage": error_message},
                "durationMs": 9000,
            }
        ]
        assert tool_tester._isolation_note(results, isolated=True) is not None

    @pytest.mark.parametrize(
        "error_type", ["KeyError", "TypeError", "ValueError", "IndexError", "AttributeError", "ZeroDivisionError"]
    )
    def test_a_logic_exception_class_does_not_fire(self, error_type):
        """The class-name markers must not be broad enough to catch a real bug."""
        results = [
            {
                "passed": False,
                "error": "'temp'",
                "actualOutput": {"errorType": error_type, "errorMessage": "'temp'"},
            }
        ]
        assert tool_tester._isolation_note(results, isolated=True) is None

    def test_a_non_dict_actual_output_does_not_crash_the_scan(self):
        """On the success path ``actualOutput`` is the parsed body, which can be a
        list, a string or a number. Only failed cases are scanned, but a malformed
        or unusual body must not be able to raise out of the scan either way."""
        results = [
            {"passed": True, "actualOutput": ["a", "b"]},
            {"passed": True, "actualOutput": "plain text"},
            {"passed": False, "actualOutput": None, "error": "KeyError: 'temp'"},
        ]
        assert tool_tester._isolation_note(results, isolated=True) is None

    def test_no_marker_is_dead_weight(self):
        """A marker containing another marker can never be the one that matches.

        ``"connection timed out"`` shipped in this tuple and was pure decoration:
        ``"timed out"`` matched every string it could. Harmless, but it also meant a
        mutant that deleted it survived, so the tuple was accumulating entries no
        test could defend.
        """
        markers = tool_tester._NETWORK_FAILURE_MARKERS
        subsumed = [(a, b) for a in markers for b in markers if a != b and b in a]
        assert subsumed == [], f"marker(s) already covered by a shorter one: {subsumed}"

    def test_a_marker_is_not_so_short_it_fires_on_logic_errors(self):
        """The other direction: the note must not appear on a real bug in the tool.

        These are failures a user has to fix themselves. Telling them the sandbox
        did it sends them to look for a network problem that is not there.
        """
        for error in (
            "KeyError: 'temp'",
            "TypeError: unsupported operand type(s) for +: 'int' and 'str'",
            "ValueError: invalid literal for int() with base 10: 'abc'",
            "AssertionError",
            "Expected output key 'temp' not found in response",
            "json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)",
            "Task timed out after 10.01 seconds",  # the sandbox's own limit, see below
        ):
            note = tool_tester._isolation_note([{"passed": False, "error": error}], isolated=True)
            if "Task timed out" in error:
                # Known and accepted: a Lambda timeout DOES match "timed out", and a
                # tool that hangs on a socket with no route out is the single most
                # likely way to produce one in this sandbox. Claiming it might be the
                # network is right far more often than it is wrong, and the note only
                # ever adds an explanation -- it never marks a failure as passed.
                assert note is not None
            else:
                assert note is None, f"false positive on {error!r}"

    def test_matching_is_case_insensitive(self):
        """Python and botocore both capitalize these differently."""
        results = [{"passed": False, "error": "URLOpen Error: Temporary Failure In Name Resolution"}]
        assert tool_tester._isolation_note(results, isolated=True) is not None

    def test_an_un_isolated_run_gets_no_note(self):
        """With egress, a connection error IS the tool's problem. Saying otherwise
        would tell the user to ignore a genuine bug."""
        results = [{"passed": False, "error": "connection refused"}]
        assert tool_tester._isolation_note(results, isolated=False) is None

    def test_a_logic_failure_gets_no_note(self):
        results = [{"passed": False, "error": "KeyError: 'city'"}]
        assert tool_tester._isolation_note(results, isolated=True) is None

    def test_a_passing_run_gets_no_note(self):
        results = [{"passed": True, "error": None}, {"passed": True}]
        assert tool_tester._isolation_note(results, isolated=True) is None

    def test_no_test_cases_gets_no_note(self):
        assert tool_tester._isolation_note([], isolated=True) is None

    def test_a_none_error_does_not_crash_the_scan(self):
        """``error`` is None on a pass and absent on a malformed row; both appear
        in the same list as the failure being explained."""
        results = [{"passed": True, "error": None}, {"passed": False, "error": "connection timed out"}]
        assert tool_tester._isolation_note(results, isolated=True) is not None

    def test_the_note_says_the_deployed_agent_works(self):
        """The actionable half. Without it the note explains the failure and still
        leaves the user believing the tool is broken."""
        note = tool_tester._isolation_note([{"passed": False, "error": "urlopen error"}], isolated=True)
        assert "will work once" in note
        assert "deployed" in note


class TestAToolThatHandlesItsOwnNetworkError:
    """The shape a live browser run failed on, which every unit test had missed.

    A generated tool that is *well written* wraps its HTTP call in try/except and
    returns ``{"statusCode": 502, "body": {"error": "Network error: ..."}}``. That is
    a successful Lambda invocation, so there is no ``FunctionError`` and no
    ``errorType``; ``error`` is only the harness's status-code complaint. The cause
    is in the body. Scanning ``error`` and ``errorType`` alone therefore classified
    exactly the tools that let the exception escape -- the badly written ones -- and
    the frontend, which suppresses auto-fix only when a note is present, went on to
    ask the model to "fix" correct code by deleting the network call.

    The payloads below are verbatim from the live run that found it.
    """

    #: Verbatim ``actualOutput`` from test-46f4ea814701, run through the deployed UI.
    LIVE_BODY = {"error": "Network error: <urlopen error [Errno 99] Cannot assign requested address>"}

    def test_the_live_payload_is_classified(self):
        results = [
            {
                "testCaseName": "london_celsius",
                "passed": False,
                "error": "Expected statusCode 200, got 502",
                "actualOutput": self.LIVE_BODY,
                "durationMs": 8755,
            }
        ]
        assert tool_tester._isolation_note(results, isolated=True) is not None

    def test_the_whole_live_run_including_a_genuine_failure(self):
        """Two network cases and one real one, exactly as the browser produced them.

        The note must still fire. Suppressing auto-fix for the whole run is the
        deliberate trade: auto-fixing a network failure removes the feature, which is
        unrecoverable, while a missed auto-fix on the 400 costs one manual edit.
        """
        results = [
            {"passed": False, "error": "Expected statusCode 200, got 502", "actualOutput": self.LIVE_BODY},
            {"passed": False, "error": "Expected statusCode 200, got 502", "actualOutput": self.LIVE_BODY},
            {
                "passed": False,
                "error": "Expected statusCode 200, got 400",
                "actualOutput": {"error": "Missing required parameter: city"},
            },
        ]
        note = tool_tester._isolation_note(results, isolated=True)
        assert note is not None
        assert "unrelated to the network" in note

    def test_a_body_only_logic_error_still_gets_no_note(self):
        """The other direction: scanning the body must not fire on the tool's bug."""
        results = [
            {
                "passed": False,
                "error": "Expected statusCode 200, got 400",
                "actualOutput": {"error": "Missing required parameter: city"},
            }
        ]
        assert tool_tester._isolation_note(results, isolated=True) is None

    def test_a_passing_case_cannot_attach_the_note(self):
        """A tool may legitimately return these words as data.

        Scanning passing cases would let a working tool that reports, say, an
        upstream's own timeout be told the sandbox broke it. Only a failure can be
        explained by the boundary.
        """
        results = [
            {"passed": True, "error": None, "actualOutput": {"upstream_status": "read timeout", "cached": True}},
            {"passed": True, "error": None, "actualOutput": {"note": "connection refused by peer, used cache"}},
        ]
        assert tool_tester._isolation_note(results, isolated=True) is None

    def test_a_missing_key_failure_carrying_a_network_body_is_classified(self):
        """``actualOutput`` is the body on the missing-keys path too, not just on a
        non-200. A tool that returns HTTP 200 with an error payload lands here."""
        results = [
            {
                "passed": False,
                "error": "Missing expected keys in response: ['temp']",
                "actualOutput": {"error": "could not connect to the endpoint URL"},
            }
        ]
        assert tool_tester._isolation_note(results, isolated=True) is not None

    @pytest.mark.parametrize(
        "body",
        [
            ["urlopen error timed out"],
            "urlopen error timed out",
            {"nested": {"detail": {"cause": "urlopen error"}}},
        ],
    )
    def test_a_marker_is_found_wherever_the_tool_put_it(self, body):
        """The body is whatever the tool chose to return: a list, a bare string, or
        a nested object. A scan that only reached top-level dict keys would miss the
        nested case, which is the shape a tool wrapping its own exception produces."""
        results = [{"passed": False, "error": "Expected statusCode 200, got 502", "actualOutput": body}]
        assert tool_tester._isolation_note(results, isolated=True) is not None

    def test_an_unserializable_body_does_not_raise(self):
        """``default=str`` exists for this. The scan must never be what fails."""

        class Opaque:
            def __str__(self):
                return "urlopen error"

        results = [{"passed": False, "error": "boom", "actualOutput": {"obj": Opaque()}}]
        assert tool_tester._isolation_note(results, isolated=True) is not None

    def test_an_un_isolated_run_with_a_handled_network_error_gets_no_note(self):
        """With egress, a connection error in the body IS the tool's problem."""
        results = [{"passed": False, "error": "Expected statusCode 200, got 502", "actualOutput": self.LIVE_BODY}]
        assert tool_tester._isolation_note(results, isolated=False) is None


class TestTheResultCarriesThePosture:
    """``sandboxIsolated`` tells the UI which posture produced this result."""

    def _run(self, clean_env, monkeypatch, *, isolated: bool, case_error: str | None):
        if isolated:
            clean_env.setenv("TOOL_SANDBOX_SUBNET_IDS", "subnet-1")
            clean_env.setenv("TOOL_SANDBOX_SECURITY_GROUP_IDS", "sg-1")
        else:
            clean_env.setenv("TOOL_SANDBOX_REQUIRE_ISOLATION", "false")
        monkeypatch.setattr(tool_tester, "_create_lambda_client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(tool_tester, "_create_iam_client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(tool_tester, "_ensure_sandbox_role", lambda *a, **k: "arn:role")
        monkeypatch.setattr(tool_tester, "_deploy_temp_lambda", lambda *a, **k: None)
        monkeypatch.setattr(tool_tester, "_cleanup_temp_lambda", lambda *a, **k: None)
        monkeypatch.setattr(
            tool_tester,
            "_run_test_case",
            lambda *a, **k: {"testCaseName": "t", "passed": case_error is None, "error": case_error, "durationMs": 1},
        )
        return tool_tester.test_tool(HANDLER, [{"name": "t", "input": {}}], region="us-east-1")

    def test_an_isolated_run_says_so(self, clean_env, monkeypatch):
        result = self._run(clean_env, monkeypatch, isolated=True, case_error=None)
        assert result["sandboxIsolated"] is True
        assert result["note"] is None

    def test_an_isolated_network_failure_carries_the_note(self, clean_env, monkeypatch):
        result = self._run(
            clean_env,
            monkeypatch,
            isolated=True,
            case_error="<urlopen error [Errno -3] Temporary failure in name resolution>",
        )
        assert result["sandboxIsolated"] is True
        assert result["note"] is not None

    def test_an_opted_out_run_says_so_too(self, clean_env, monkeypatch):
        """Reported, not omitted: "ran with egress" is a posture the user should see."""
        result = self._run(clean_env, monkeypatch, isolated=False, case_error="connection refused")
        assert result["sandboxIsolated"] is False
        assert result["note"] is None

    def test_a_refusal_reports_no_posture_at_all(self, clean_env):
        """It never reached the sandbox, so there is no posture to report -- and
        ``False`` here would claim a run happened with full egress."""
        result = tool_tester.test_tool(HANDLER, [], region="us-east-1")
        assert result["success"] is False
        assert "sandboxIsolated" not in result


class TestTheSandboxLogGroupIsGovernedBeforeLambdaMakesOne:
    """A group Lambda creates implicitly keeps its logs forever.

    MEASURED on acfe2e-p0920 on 2026-09-21: 53 ``/aws/lambda/AgentCore-ToolTest-*``
    log groups, ``retentionInDays: null`` on all 53. The function name carries a
    fresh uuid4 per tool test and deleting the function does not delete the group,
    so the feature's steady state is one immortal log group per test.

    Ordering is the entire fix. A sweep cannot work: deleting a group while its
    function still exists makes Lambda recreate it, whole stream included.
    """

    def _run(self, clean_env, monkeypatch, logs_client):
        clean_env.setenv("TOOL_SANDBOX_SUBNET_IDS", "subnet-1")
        clean_env.setenv("TOOL_SANDBOX_SECURITY_GROUP_IDS", "sg-1")
        order: list[str] = []
        monkeypatch.setattr(tool_tester, "_create_lambda_client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(tool_tester, "_create_iam_client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(tool_tester, "_ensure_sandbox_role", lambda *a, **k: "arn:role")
        monkeypatch.setattr(tool_tester, "_create_logs_client", lambda region=None: logs_client)
        # Only when the caller has not configured its own failure: overwriting it
        # would silently turn a failure test into a success test.
        if logs_client.create_log_group.side_effect is None:
            logs_client.create_log_group.side_effect = lambda **kw: order.append("create_log_group")
        monkeypatch.setattr(tool_tester, "_deploy_temp_lambda", lambda *a, **k: order.append("create_function"))
        monkeypatch.setattr(tool_tester, "_cleanup_temp_lambda", lambda *a, **k: None)
        monkeypatch.setattr(
            tool_tester,
            "_run_test_case",
            lambda *a, **k: {"testCaseName": "t", "passed": True, "error": None, "durationMs": 1},
        )
        result = tool_tester.test_tool(HANDLER, [{"name": "t", "input": {}}], region="us-east-1")
        return result, order

    def test_the_group_is_created_with_retention_before_the_function(self, clean_env, monkeypatch):
        logs_client = MagicMock()
        result, order = self._run(clean_env, monkeypatch, logs_client)
        assert result["success"] is True

        created = logs_client.create_log_group.call_args.kwargs
        group = created["logGroupName"]
        assert group.startswith("/aws/lambda/" + tool_tester.TOOL_TEST_FN_PREFIX), (
            f"The governed group is {group}, which is not the sandbox function's own log group. "
            "Lambda derives the name from the function name, so anything else leaves the real "
            "group ungoverned and creates a second one nothing writes to."
        )

        retention = logs_client.put_retention_policy.call_args.kwargs
        assert retention["logGroupName"] == group
        assert retention["retentionInDays"] == tool_tester.SANDBOX_LOG_RETENTION_DAYS

        assert order == ["create_log_group", "create_function"], (
            f"Order was {order}. Creating the function first lets Lambda create the log group "
            "itself with no retention, and CreateLogGroup then fails with "
            "ResourceAlreadyExistsException -- so the retention call is skipped and the group "
            "keeps its logs forever. That is the defect, not a detail."
        )

    def test_the_group_carries_the_owner_tags(self, clean_env, monkeypatch):
        """Otherwise teardown cannot tell 53 of our groups from a foreign one."""
        logs_client = MagicMock()
        self._run(clean_env, monkeypatch, logs_client)
        tags = logs_client.create_log_group.call_args.kwargs.get("tags") or {}
        assert tags, "create_log_group was called with no tags; an untagged group has no provenance."
        # Spelled out rather than compared against owner_tag_list() itself: a
        # tautology here would pass even if the ownership identity changed shape,
        # and teardown matches on these two literal keys.
        assert tags.get("ManagedBy") == "agentcore-flows" and tags.get("AgentCoreStack") == "probe-t1-us-east-1", (
            f"Tags were {tags}. They must come from resource_ownership.owner_tag_list so the group "
            "carries the same identity as every other resource this deployment creates -- under "
            "PROJECT_NAME=probe ENVIRONMENT=t1 in us-east-1 that is "
            "ManagedBy=agentcore-flows and AgentCoreStack=probe-t1-us-east-1."
        )

    def test_a_denied_tag_falls_back_to_an_untagged_group_not_no_group(self, clean_env, monkeypatch):
        """Lambda authorizes create_function's Tags as a separate action; CloudWatch
        should not, but a tag is not worth losing the retention over if it does."""
        logs_client = MagicMock()
        logs_client.create_log_group.side_effect = [
            Exception("AccessDeniedException: not authorized to perform: logs:TagResource"),
            None,
        ]
        result, _ = self._run(clean_env, monkeypatch, logs_client)
        assert result["success"] is True
        assert logs_client.create_log_group.call_count == 2
        assert "tags" not in logs_client.create_log_group.call_args_list[1].kwargs
        logs_client.put_retention_policy.assert_called_once()

    def test_a_deployment_that_cannot_create_the_group_still_tests_tools(self, clean_env, monkeypatch):
        """The half that matters more. A missing CloudWatch action must cost a
        retention policy, not the feature -- which is exactly what a missing
        ``lambda:TagResource`` cost live before its grant existed."""
        logs_client = MagicMock()
        logs_client.create_log_group.side_effect = Exception(
            "AccessDeniedException: not authorized to perform: logs:CreateLogGroup"
        )
        result, order = self._run(clean_env, monkeypatch, logs_client)
        assert result["success"] is True, (
            "A log-group failure aborted the tool test. Governing a log group is housekeeping; "
            "refusing to test tools because of it trades the product for pennies."
        )
        assert "create_function" in order


# ---------------------------------------------------------------------------
# The isolated sandbox has to actually come up, and every budget in the chain
# has to be long enough for it. Both halves of this were wrong live.
# ---------------------------------------------------------------------------


class TestIamPropagationIsNotAMissingGrant:
    """CreateFunction validates the execution role's EC2 permissions synchronously.

    Found live on acfe2e-p0920: the sandbox role already existed from a run made
    before isolation was enabled, so it held only the basic-execution policy.
    ``_ensure_sandbox_role`` attached the VPC policy, did not wait because the role
    was not newly *created*, and CreateFunction failed 0.8 seconds later with "The
    provided execution role does not have permissions to call CreateNetworkInterface
    on EC2". Two defects in one: the wait was on the wrong condition, and there was
    no retry behind it.
    """

    def _iam_with_existing_role(self, attached: list[str]):
        iam = MagicMock()
        iam.exceptions.EntityAlreadyExistsException = type("EntityAlreadyExistsException", (ClientError,), {})

        def _create_role(**_kwargs):
            raise iam.exceptions.EntityAlreadyExistsException({"Error": {"Code": "EntityAlreadyExists"}}, "CreateRole")

        iam.create_role.side_effect = _create_role
        iam.get_role.return_value = {
            "Role": {
                "Arn": "arn:aws:iam::1:role/x",
                "Tags": [
                    {"Key": "ManagedBy", "Value": "agentcore-flows"},
                    {"Key": "AgentCoreStack", "Value": "probe-t1-us-east-1"},
                ],
            }
        }
        iam.list_attached_role_policies.return_value = {"AttachedPolicies": [{"PolicyArn": a} for a in attached]}
        return iam

    def test_a_reused_role_waits_when_a_policy_was_just_attached(self, clean_env, monkeypatch):
        """The case that failed live. `created` is False and the wait still has to happen."""
        slept: list[float] = []
        monkeypatch.setattr(tool_tester.time, "sleep", lambda s: slept.append(s))
        iam = self._iam_with_existing_role([tool_tester.BASIC_EXECUTION_POLICY])

        _ensure_sandbox_role(iam, region="us-east-1", need_vpc=True)

        iam.attach_role_policy.assert_called_once_with(
            RoleName=tool_tester._sandbox_role_name("us-east-1"),
            PolicyArn=tool_tester.VPC_ACCESS_POLICY,
        )
        assert slept, "attached a policy and then did not wait for it to propagate"

    def test_a_role_that_already_has_everything_does_not_wait(self, clean_env, monkeypatch):
        """The steady state, and the reason the wait is conditional rather than always.

        Every tool test after the first goes through here, so an unconditional 10s
        sleep would tax the warm path forever to pay for the cold one.
        """
        slept: list[float] = []
        monkeypatch.setattr(tool_tester.time, "sleep", lambda s: slept.append(s))
        iam = self._iam_with_existing_role([tool_tester.BASIC_EXECUTION_POLICY, tool_tester.VPC_ACCESS_POLICY])

        _ensure_sandbox_role(iam, region="us-east-1", need_vpc=True)

        iam.attach_role_policy.assert_not_called()
        assert slept == []

    def test_the_propagation_rejection_is_retried(self, clean_env, monkeypatch):
        monkeypatch.setattr(tool_tester.time, "sleep", lambda _s: None)
        client = MagicMock()
        client.get_function.return_value = {"Configuration": {"State": "Active"}}
        client.create_function.side_effect = [
            ClientError(
                {
                    "Error": {
                        "Code": "InvalidParameterValueException",
                        "Message": (
                            "The provided execution role does not have permissions "
                            "to call CreateNetworkInterface on EC2"
                        ),
                    }
                },
                "CreateFunction",
            ),
            None,
        ]

        _deploy_temp_lambda(
            client,
            "fn",
            "arn:role",
            b"z",
            vpc_config={"SubnetIds": ["s"], "SecurityGroupIds": ["g"]},
        )

        assert client.create_function.call_count == 2

    def test_a_real_missing_grant_is_not_retried(self, clean_env, monkeypatch):
        """Retrying an AccessDenied buys a 25-second wait and the same answer."""
        monkeypatch.setattr(tool_tester.time, "sleep", lambda _s: None)
        client = MagicMock()
        client.get_function.return_value = {"Configuration": {"State": "Active"}}
        client.create_function.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "not authorized to perform lambda:CreateFunction"}},
            "CreateFunction",
        )

        with pytest.raises(ClientError):
            _deploy_temp_lambda(client, "fn", "arn:role", b"z")

        assert client.create_function.call_count == 1

    def test_the_retry_gives_up_and_raises_the_original_error(self, clean_env, monkeypatch):
        """It must not mask a genuinely broken role as something else forever."""
        monkeypatch.setattr(tool_tester.time, "sleep", lambda _s: None)
        client = MagicMock()
        client.create_function.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InvalidParameterValueException",
                    "Message": "The provided execution role does not have permissions to call x",
                }
            },
            "CreateFunction",
        )

        with pytest.raises(ClientError):
            _deploy_temp_lambda(client, "fn", "arn:role", b"z")

        assert client.create_function.call_count == tool_tester._ROLE_RETRY_ATTEMPTS

    def test_an_untrusted_trust_policy_lag_is_also_retried(self, clean_env, monkeypatch):
        """The same class of failure one step earlier, for a brand-new role."""
        monkeypatch.setattr(tool_tester.time, "sleep", lambda _s: None)
        client = MagicMock()
        client.get_function.return_value = {"Configuration": {"State": "Active"}}
        client.create_function.side_effect = [
            ClientError(
                {
                    "Error": {
                        "Code": "InvalidParameterValueException",
                        "Message": "The role defined for the function cannot be assumed by Lambda.",
                    }
                },
                "CreateFunction",
            ),
            None,
        ]

        _deploy_temp_lambda(client, "fn", "arn:role", b"z")

        assert client.create_function.call_count == 2

    def test_no_propagation_marker_is_dead_weight(self):
        """A marker subsumed by a shorter one could never be consulted.

        The same defect this file already found in ``_NETWORK_FAILURE_MARKERS``: a
        surviving mutant showed that deleting ``"connection timed out"`` changed
        nothing because ``"timed out"`` matched strictly more.
        """
        markers = tool_tester._ROLE_PROPAGATION_MARKERS
        subsumed = [(a, b) for a in markers for b in markers if a != b and b in a]
        assert subsumed == [], f"marker(s) already covered by a shorter one: {subsumed}"

    def test_the_markers_are_lowercase(self):
        """They are matched against a lowercased message, so an uppercase letter
        anywhere in one makes it permanently unmatchable."""
        for marker in tool_tester._ROLE_PROPAGATION_MARKERS:
            assert marker == marker.lower(), marker


class TestTheSandboxIsGivenTimeToComeUp:
    """A VPC-attached function waits on Lambda building a Hyperplane ENI.

    Measured live in the acfe2e-p0920 sandbox VPC across three consecutive runs:
    223.3s, 223.9s, then 6.1s once the (subnet, security-group) mapping was warm.
    The code allowed 60s, the host Lambda's timeout was 120s and the browser gave up
    polling at 120s -- so an isolated sandbox could not have succeeded on a cold
    mapping at any layer of the chain.
    """

    def test_a_vpc_run_is_given_far_longer_than_a_plain_one(self):
        """Pinned as a relation, not two magic numbers: the point is that attaching a
        VPC costs minutes, so the budgets must differ by roughly that order."""
        assert tool_tester.ACTIVE_WAIT_SECONDS_VPC >= 4 * tool_tester.ACTIVE_WAIT_SECONDS

    def test_the_vpc_budget_covers_the_measured_cold_cost(self):
        """224s was measured twice. A budget under it is a budget that fails."""
        assert tool_tester.ACTIVE_WAIT_SECONDS_VPC > 224

    def test_it_waits_past_the_old_sixty_second_ceiling(self, clean_env, monkeypatch):
        """The regression this exists to prevent: 40 polls at 2s is 80s, which the
        previous `range(30)` loop could not reach however long the ENI took."""
        elapsed = {"t": 0.0}
        monkeypatch.setattr(tool_tester.time, "monotonic", lambda: elapsed["t"])
        monkeypatch.setattr(tool_tester.time, "sleep", lambda s: elapsed.__setitem__("t", elapsed["t"] + s))
        client = MagicMock()
        states = ["Pending"] * 60 + ["Active"]
        client.get_function.side_effect = [{"Configuration": {"State": s}} for s in states]

        _deploy_temp_lambda(
            client,
            "fn",
            "arn:role",
            b"z",
            vpc_config={"SubnetIds": ["s"], "SecurityGroupIds": ["g"]},
        )

        assert elapsed["t"] > 60

    def test_a_failed_state_is_terminal_rather_than_waited_out(self, clean_env, monkeypatch):
        """Waiting five more minutes on a Failed function changes nothing, and the
        reason names the actual problem.

        The clock is patched as well as the sleep, and that is not tidiness: with
        only ``sleep`` stubbed out, a regression here busy-loops until the real 300s
        budget expires. Verified by mutation -- making the Failed branch unreachable
        took this test five minutes of wall clock to fail instead of milliseconds.
        """
        elapsed = {"t": 0.0}
        monkeypatch.setattr(tool_tester.time, "monotonic", lambda: elapsed["t"])
        monkeypatch.setattr(tool_tester.time, "sleep", lambda s: elapsed.__setitem__("t", elapsed["t"] + s))
        client = MagicMock()
        client.get_function.return_value = {
            "Configuration": {"State": "Failed", "StateReason": "subnet has no available addresses"}
        }

        with pytest.raises(SandboxPolicyError, match="no available addresses"):
            _deploy_temp_lambda(
                client,
                "fn",
                "arn:role",
                b"z",
                vpc_config={"SubnetIds": ["s"], "SecurityGroupIds": ["g"]},
            )

        assert client.get_function.call_count == 1

    def test_giving_up_says_what_happened_and_what_to_do(self, clean_env, monkeypatch):
        """It raised a bare TimeoutError, which the generic handler turned into "Tool
        testing failed unexpectedly" -- what the live probe actually saw. A
        SandboxPolicyError is published verbatim instead."""
        elapsed = {"t": 0.0}
        monkeypatch.setattr(tool_tester.time, "monotonic", lambda: elapsed["t"])
        monkeypatch.setattr(tool_tester.time, "sleep", lambda s: elapsed.__setitem__("t", elapsed["t"] + s))
        client = MagicMock()
        client.get_function.return_value = {"Configuration": {"State": "Pending"}}

        with pytest.raises(SandboxPolicyError) as err:
            _deploy_temp_lambda(
                client,
                "fn",
                "arn:role",
                b"z",
                vpc_config={"SubnetIds": ["s"], "SecurityGroupIds": ["g"]},
            )

        message = str(err.value)
        assert str(tool_tester.ACTIVE_WAIT_SECONDS_VPC) in message
        assert "network interface" in message
        assert "retry" in message.lower()

    def test_the_timeout_message_reaches_the_caller(self, clean_env, monkeypatch):
        """End of the chain: a SandboxPolicyError is the one exception published
        verbatim, so the user is told about the ENI rather than "unexpectedly"."""
        clean_env.setenv("TOOL_SANDBOX_SUBNET_IDS", "subnet-1")
        clean_env.setenv("TOOL_SANDBOX_SECURITY_GROUP_IDS", "sg-1")
        monkeypatch.setattr(tool_tester, "_create_lambda_client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(tool_tester, "_create_iam_client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(tool_tester, "_ensure_sandbox_role", lambda *a, **k: "arn:role")
        monkeypatch.setattr(tool_tester, "_cleanup_temp_lambda", lambda *a, **k: None)
        monkeypatch.setattr(
            tool_tester,
            "_deploy_temp_lambda",
            lambda *a, **k: (_ for _ in ()).throw(SandboxPolicyError("did not become ready within 300s")),
        )

        result = tool_tester.test_tool(HANDLER, [], region="us-east-1")

        assert result["success"] is False
        assert "300s" in result["error"]
        assert "unexpectedly" not in result["error"]

    def test_a_plain_run_still_uses_the_short_budget(self, clean_env, monkeypatch):
        """The opted-out path must not inherit a five-minute wait for a function that
        is Active in seconds."""
        elapsed = {"t": 0.0}
        monkeypatch.setattr(tool_tester.time, "monotonic", lambda: elapsed["t"])
        monkeypatch.setattr(tool_tester.time, "sleep", lambda s: elapsed.__setitem__("t", elapsed["t"] + s))
        client = MagicMock()
        client.get_function.return_value = {"Configuration": {"State": "Pending"}}

        with pytest.raises(SandboxPolicyError) as err:
            _deploy_temp_lambda(client, "fn", "arn:role", b"z", vpc_config=None)

        assert str(tool_tester.ACTIVE_WAIT_SECONDS) in str(err.value)
        assert elapsed["t"] <= tool_tester.ACTIVE_WAIT_SECONDS + 2

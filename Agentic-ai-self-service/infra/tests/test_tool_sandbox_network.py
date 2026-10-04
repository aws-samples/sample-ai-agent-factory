"""The tool-test sandbox's network, asserted as "there is no way out".

ARCC ``cnt_MSVB0Kk8WMwmmW`` requires customer-provided code to run "from a private
network ... with no public internet access", and names a VPC with no public
internet access as the Lambda mitigation. ARCC ``cnt_dh50RmkA8h91jK`` and
``cnt_fImfV93NdsOrCd`` add the egress half: restrict outbound to the VPC endpoints
the workload genuinely needs.

The interesting assertions here are the *absences*, because that is what isolation
is made of, and an absence is exactly what a hand-written unit test forgets to
check. A NAT gateway, an internet gateway, a ``0.0.0.0/0`` route or an
``allow_all_outbound`` security group each individually undoes the whole module,
and none of them would make any other test in this repo fail.

Two of these assertions exist because the first version of the module got them
wrong and synthesis was happy either way:

* ``add_interface_endpoint`` defaults to ``open=True``, which opens the endpoint's
  security group to the entire VPC CIDR on 443. The explicit sandbox-only ingress
  rule was then decoration, and anything later placed in this VPC would have
  reached the endpoint. See ``test_the_endpoint_is_not_open_to_the_whole_vpc``.
* the deployment Lambda has to actually *receive* the subnet and security-group
  ids, or the sandbox is created with no ``VpcConfig`` and the whole VPC sits
  unused while ``tool_tester`` refuses every test. See
  ``test_the_deployment_lambda_is_told_where_the_sandbox_goes``.
"""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

REGION = "us-east-1"
ACCOUNT = "123456789012"


@pytest.fixture(scope="module")
def template_json() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(region=REGION, account=ACCOUNT),
    )
    return Template.from_stack(stack).to_json()


def _of_type(template_json: dict, type_name: str) -> dict[str, dict]:
    return {lid: r for lid, r in template_json["Resources"].items() if r["Type"] == type_name}


def _sandbox_sg_id(template_json: dict) -> str:
    """The logical id of the security group generated code runs under."""
    for lid in _of_type(template_json, "AWS::EC2::SecurityGroup"):
        if lid.startswith("ToolSandboxSg"):
            return lid
    raise AssertionError("no ToolSandboxSg security group in the template")


def _endpoint_sg_id(template_json: dict) -> str:
    for lid in _of_type(template_json, "AWS::EC2::SecurityGroup"):
        if lid.startswith("ToolSandboxEndpointSg"):
            return lid
    raise AssertionError("no ToolSandboxEndpointSg security group in the template")


# ---------------------------------------------------------------------------
# There is no route off the VPC
# ---------------------------------------------------------------------------


def test_there_is_no_nat_gateway(template_json):
    """The expensive component, and the one that would restore egress silently.

    A NAT gateway is also how someone "fixes" a tool that cannot reach its API, so
    this assertion is here to make that a conversation rather than a commit.
    """
    assert _of_type(template_json, "AWS::EC2::NatGateway") == {}


def test_there_is_no_internet_gateway(template_json):
    """Stronger than a security-group rule: nothing to route to, at all.

    ``PRIVATE_ISOLATED``-only subnet configuration is what produces this. Adding a
    ``PUBLIC`` subnet -- even an unused one -- creates the gateway.
    """
    assert _of_type(template_json, "AWS::EC2::InternetGateway") == {}
    assert _of_type(template_json, "AWS::EC2::VPCGatewayAttachment") == {}


def test_no_route_table_has_a_default_route(template_json):
    """No ``0.0.0.0/0`` and no ``::/0`` anywhere in the VPC."""
    offenders = [
        (lid, r["Properties"])
        for lid, r in _of_type(template_json, "AWS::EC2::Route").items()
        if r["Properties"].get("DestinationCidrBlock") == "0.0.0.0/0"
        or r["Properties"].get("DestinationIpv6CidrBlock") == "::/0"
    ]
    assert not offenders, f"the sandbox VPC has a default route: {offenders}"


def test_no_subnet_assigns_a_public_ip(template_json):
    subnets = _of_type(template_json, "AWS::EC2::Subnet")
    assert subnets, "no subnets synthesized -- the sandbox VPC is missing"
    for lid, r in subnets.items():
        assert not r["Properties"].get("MapPublicIpOnLaunch"), f"{lid} auto-assigns a public IP"


# ---------------------------------------------------------------------------
# Egress is one rule, to one endpoint
# ---------------------------------------------------------------------------


def test_neither_security_group_allows_all_outbound(template_json):
    """``allow_all_outbound=True`` is the CDK default and is the whole defect.

    CDK renders ``allow_all_outbound=False`` as a deliberately unusable placeholder
    rule (ICMP to 255.255.255.255/32) when no real egress rule exists, so the check
    is for the absence of an open CIDR rather than for any particular marker.
    """
    for lid in (_sandbox_sg_id(template_json), _endpoint_sg_id(template_json)):
        rules = template_json["Resources"][lid]["Properties"].get("SecurityGroupEgress", [])
        for rule in rules:
            assert rule.get("CidrIp") != "0.0.0.0/0", f"{lid} allows all outbound IPv4"
            assert rule.get("CidrIpv6") != "::/0", f"{lid} allows all outbound IPv6"


def test_the_sandbox_has_exactly_one_egress_rule_and_it_points_at_the_endpoint(template_json):
    sandbox = _sandbox_sg_id(template_json)
    endpoint = _endpoint_sg_id(template_json)
    inline = template_json["Resources"][sandbox]["Properties"].get("SecurityGroupEgress", [])
    standalone = [
        r["Properties"]
        for r in _of_type(template_json, "AWS::EC2::SecurityGroupEgress").values()
        if r["Properties"].get("GroupId", {}).get("Fn::GetAtt", [None])[0] == sandbox
    ]
    assert inline == [], f"unexpected inline egress on the sandbox group: {inline}"
    assert len(standalone) == 1, f"expected exactly one egress rule, got {standalone}"

    rule = standalone[0]
    assert rule["IpProtocol"] == "tcp"
    assert rule["FromPort"] == rule["ToPort"] == 443
    assert rule["DestinationSecurityGroupId"]["Fn::GetAtt"][0] == endpoint
    # A CIDR destination here would be the same rule shape with a different reach.
    assert "CidrIp" not in rule and "CidrIpv6" not in rule


def test_the_endpoint_is_not_open_to_the_whole_vpc(template_json):
    """``open=True`` -- the constructor default -- opens 443 to the VPC CIDR.

    That single default made the explicit sandbox-only ingress rule decorative:
    anything placed in this VPC afterwards would reach the endpoint regardless of
    which security group it was in.
    """
    endpoint = _endpoint_sg_id(template_json)
    inline = template_json["Resources"][endpoint]["Properties"].get("SecurityGroupIngress", [])
    assert inline == [], f"the endpoint group carries a CIDR-based ingress rule: {inline}"


def test_the_endpoint_ingress_comes_only_from_the_sandbox_group(template_json):
    endpoint = _endpoint_sg_id(template_json)
    sandbox = _sandbox_sg_id(template_json)
    rules = [
        r["Properties"]
        for r in _of_type(template_json, "AWS::EC2::SecurityGroupIngress").values()
        if r["Properties"].get("GroupId", {}).get("Fn::GetAtt", [None])[0] == endpoint
    ]
    assert len(rules) == 1, f"expected one ingress rule on the endpoint group, got {rules}"
    assert rules[0]["SourceSecurityGroupId"]["Fn::GetAtt"][0] == sandbox
    assert rules[0]["FromPort"] == rules[0]["ToPort"] == 443


def test_the_sandbox_accepts_no_inbound_at_all(template_json):
    """Nothing should be able to reach generated code, including us."""
    sandbox = _sandbox_sg_id(template_json)
    inline = template_json["Resources"][sandbox]["Properties"].get("SecurityGroupIngress", [])
    standalone = [
        r["Properties"]
        for r in _of_type(template_json, "AWS::EC2::SecurityGroupIngress").values()
        if r["Properties"].get("GroupId", {}).get("Fn::GetAtt", [None])[0] == sandbox
    ]
    assert inline == [] and standalone == []


# ---------------------------------------------------------------------------
# Logs still work, and the isolation is observable
# ---------------------------------------------------------------------------


def test_the_cloudwatch_logs_endpoint_exists(template_json):
    """One endpoint, and exactly one.

    This docstring used to say the sandbox would "run blind" without the endpoint.
    A live probe disproved that: a sandbox function's own execution logs are
    delivered by the Lambda service from outside the customer VPC, and arrived from
    an invocation that could not reach its own log group at the time. What the
    endpoint genuinely serves is the generated tool's own ``boto3`` Logs calls.

    The count matters more than the existence: a second interface endpoint would be
    a second hole, and the egress rule and endpoint policy below are both written
    for one.
    """
    endpoints = _of_type(template_json, "AWS::EC2::VPCEndpoint")
    logs_eps = [
        p for p in (r["Properties"] for r in endpoints.values()) if str(p.get("ServiceName", "")).endswith(".logs")
    ]
    assert len(logs_eps) == 1, f"expected exactly one CloudWatch Logs endpoint: {endpoints}"
    ep = logs_eps[0]
    assert ep["VpcEndpointType"] == "Interface" if "VpcEndpointType" in ep else True
    # Private DNS is how the SDK inside the function reaches it without any code
    # change; with it off, boto3 would still resolve the public endpoint and hang.
    assert ep["PrivateDnsEnabled"] is True
    assert ep["SecurityGroupIds"][0]["Fn::GetAtt"][0] == _endpoint_sg_id(template_json)


def _logs_endpoint_policy(template_json: dict) -> dict:
    endpoints = _of_type(template_json, "AWS::EC2::VPCEndpoint")
    for props in (r["Properties"] for r in endpoints.values()):
        if str(props.get("ServiceName", "")).endswith(".logs"):
            policy = props.get("PolicyDocument")
            assert policy is not None, (
                "the CloudWatch Logs endpoint has no PolicyDocument, so it ships with the "
                "AWS default of Action/Principal/Resource all '*' -- see ARCC cnt_h4bRcEdetHM9Jv"
            )
            return policy
    raise AssertionError("no CloudWatch Logs endpoint found")


def test_the_logs_endpoint_has_a_policy_at_all(template_json):
    """ARCC ``cnt_h4bRcEdetHM9Jv`` Definition of Done #2.

    An interface endpoint with no explicit policy is created with ``{"Action": "*",
    "Principal": "*", "Resource": "*"}``. This endpoint is the only egress hole in
    the VPC, and a live probe confirmed untrusted code can complete an authenticated
    call through it, so the default is a working path to the whole CloudWatch Logs
    API rather than a latent one.
    """
    policy = _logs_endpoint_policy(template_json)
    statements = policy["Statement"]
    assert statements, "an empty policy is the same as no policy"
    for st in statements:
        assert st.get("Effect") == "Allow"
        assert st.get("Action") != "*", "a wildcard action is the default this test exists to forbid"
        assert st.get("Resource") != "*", "a wildcard resource is the default this test exists to forbid"


def test_the_endpoint_policy_is_scoped_to_the_sandbox_log_groups(template_json):
    """ "Tight restrictions on resource", in ARCC's words.

    Without this the sandbox role could read from and write to every log group in
    the account through the endpoint, which includes the platform's own logs.
    """
    policy = _logs_endpoint_policy(template_json)
    rendered = json.dumps(policy)
    assert "/aws/lambda/AgentCore-ToolTest-*" in rendered, f"policy is not scoped to the sandbox log groups: {rendered}"
    for st in policy["Statement"]:
        for action in st["Action"] if isinstance(st["Action"], list) else [st["Action"]]:
            assert action.startswith("logs:"), f"non-logs action on a logs endpoint: {action}"
        # CreateLogGroup would let untrusted code create groups it then owns. The
        # group is made by the Lambda service outside this VPC, before the code runs.
        assert "logs:CreateLogGroup" not in (st["Action"] if isinstance(st["Action"], list) else [st["Action"]])


def test_the_endpoint_policy_refuses_another_account(template_json):
    """The half a resource restriction cannot cover.

    ARCC ``cnt_h4bRcEdetHM9Jv`` names it directly: *"an attacker could just bring
    their own resource and credentials from another AWS account, connect via the VPC
    endpoint, and use them to pass data out."* Scoping ``Resource`` does nothing
    there, because the resource would be theirs.
    """
    policy = _logs_endpoint_policy(template_json)
    # An env-bound stack resolves ``stack.account`` to the literal; an
    # environment-agnostic one renders ``{"Ref": "AWS::AccountId"}``. Both are the
    # deploying account, and this module is deployed env-bound, so accept either
    # rather than pinning the test to one synthesis mode.
    allowed = (ACCOUNT, {"Ref": "AWS::AccountId"})
    for st in policy["Statement"]:
        condition = st.get("Condition", {})
        assert condition.get("StringEquals", {}).get("aws:PrincipalAccount") in allowed, (
            f"statement is not pinned to this account: {json.dumps(st)}"
        )


def test_a_flow_log_records_all_traffic(template_json):
    """REJECT-only would show the security group working and not what was reached.

    ARCC ``cnt_rkp2f7nJIu5rry`` says to enable flow logs only where a specific need
    is identified; the need here is that the flow log is the only evidence the
    isolation holds, and the volume is a few 443 flows per tool test.
    """
    flow_logs = _of_type(template_json, "AWS::EC2::FlowLog")
    assert len(flow_logs) == 1, f"expected one flow log: {list(flow_logs)}"
    props = next(iter(flow_logs.values()))["Properties"]
    assert props["TrafficType"] == "ALL"
    assert props["ResourceType"] == "VPC"
    assert props["LogDestinationType"] == "cloud-watch-logs"


# ---------------------------------------------------------------------------
# The VPC is actually used
# ---------------------------------------------------------------------------


def test_the_deployment_lambda_is_told_where_the_sandbox_goes(template_json):
    """An unused VPC would be worse than none: every tool test would refuse.

    ``tool_tester`` reads these two variables and, under the default posture, fails
    closed when the subnets are empty. So a VPC that is synthesized but never handed
    to the deployment Lambda turns tool testing off rather than isolating it.
    """
    lambdas = _of_type(template_json, "AWS::Lambda::Function")
    targets = [
        (lid, r["Properties"]["Environment"]["Variables"])
        for lid, r in lambdas.items()
        if "TOOL_SANDBOX_SUBNET_IDS" in r["Properties"].get("Environment", {}).get("Variables", {})
    ]
    assert len(targets) == 1, "exactly one Lambda should carry the sandbox network variables"
    lid, env = targets[0]
    assert lid.startswith("DeploymentLambda"), f"the sandbox variables are on {lid}"

    subnet_ids = env["TOOL_SANDBOX_SUBNET_IDS"]
    # Rendered as a Fn::Join of Refs, one per isolated subnet -- two AZs.
    refs = [p["Ref"] for p in subnet_ids["Fn::Join"][1] if isinstance(p, dict) and "Ref" in p]
    assert len(refs) == 2, f"expected two subnet refs, got {json.dumps(subnet_ids)}"
    for ref in refs:
        assert template_json["Resources"][ref]["Type"] == "AWS::EC2::Subnet"

    sg = env["TOOL_SANDBOX_SECURITY_GROUP_IDS"]
    assert sg["Fn::GetAtt"][0] == _sandbox_sg_id(template_json)


def test_isolation_is_not_switched_off_in_the_environment(template_json):
    """The stack must not ship the opt-out it provisioned the VPC to avoid needing.

    ``TOOL_SANDBOX_REQUIRE_ISOLATION`` unset means enforced, so the assertion is
    that no Lambda sets it to a falsy spelling -- which would leave the VPC in place
    and generated code running outside it.
    """
    falsy = {"0", "false", "no", "off"}
    for lid, r in _of_type(template_json, "AWS::Lambda::Function").items():
        env = r["Properties"].get("Environment", {}).get("Variables", {})
        value = env.get("TOOL_SANDBOX_REQUIRE_ISOLATION")
        if isinstance(value, str):
            assert value.strip().lower() not in falsy, f"{lid} disables sandbox isolation"


def _backend_tool_tester():
    """Import the backend's tool_tester so the two budgets can be compared.

    The invariant this file exists to hold is a *cross-layer* one, and reproducing
    the constant here as a literal is exactly how it broke: 60s in the service and
    120s on the function were each defensible in isolation and wrong together.
    Precedent for reaching into the backend from an infra test:
    test_the_client_secret_grant_is_scoped_and_satisfiable.py.
    """
    import sys
    from pathlib import Path

    backend_src = Path(__file__).resolve().parents[2] / "backend" / "src"
    added = str(backend_src) not in sys.path
    if added:
        sys.path.insert(0, str(backend_src))
    try:
        from app.services import tool_tester

        return tool_tester
    finally:
        if added:
            sys.path.remove(str(backend_src))


def test_the_deployment_lambda_outlives_the_sandbox_it_waits_for(template_json):
    """The host function's timeout has to exceed the wait it performs inside itself.

    This is the defect, found live on acfe2e-p0920 and invisible to every unit test
    because they all mock the AWS call. ``handle_test_tool`` async-invokes this same
    function to run a tool test, so the test's entire budget is this timeout. A
    VPC-attached sandbox function waits on Lambda building a Hyperplane ENI --
    measured at 223.3s, 223.9s, then 6.1s once the (subnet, security-group) mapping
    was warm. The timeout was 120s, so the host died before the sandbox it had just
    created could ever be invoked, and the user was told "Tool testing failed
    unexpectedly".

    Asserted as a relation against the backend's own constant rather than as a
    number, because the two drifting apart is the failure.
    """
    tool_tester = _backend_tool_tester()
    lambdas = _of_type(template_json, "AWS::Lambda::Function")
    deployment = [
        (lid, r)
        for lid, r in lambdas.items()
        if "TOOL_SANDBOX_SUBNET_IDS" in r["Properties"].get("Environment", {}).get("Variables", {})
    ]
    assert len(deployment) == 1
    timeout = deployment[0][1]["Properties"]["Timeout"]

    assert timeout > tool_tester.ACTIVE_WAIT_SECONDS_VPC, (
        f"DeploymentLambda timeout {timeout}s does not cover the "
        f"{tool_tester.ACTIVE_WAIT_SECONDS_VPC}s it waits for the sandbox to become Active"
    )
    # Not merely greater: the wait is followed by the test cases themselves and the
    # cleanup, so a timeout one second longer would still fail on any real tool.
    assert timeout - tool_tester.ACTIVE_WAIT_SECONDS_VPC >= 120, (
        f"only {timeout - tool_tester.ACTIVE_WAIT_SECONDS_VPC}s left for the test cases after waiting for the sandbox"
    )


# ---------------------------------------------------------------------------
# DNS is filtered, not merely unroutable
# ---------------------------------------------------------------------------


def _vpc_logical_id(template_json: dict) -> str:
    for lid in _of_type(template_json, "AWS::EC2::VPC"):
        if lid.startswith("ToolSandboxVpc"):
            return lid
    raise AssertionError("no ToolSandboxVpc in the template")


def _dns_rules(template_json: dict) -> list[dict]:
    groups = _of_type(template_json, "AWS::Route53Resolver::FirewallRuleGroup")
    assert len(groups) == 1, f"expected exactly one DNS firewall rule group: {list(groups)}"
    return next(iter(groups.values()))["Properties"]["FirewallRules"]


def _domain_list(template_json: dict, logical_id: str) -> list[str]:
    lists = _of_type(template_json, "AWS::Route53Resolver::FirewallDomainList")
    assert logical_id in lists, f"{logical_id} missing; have {list(lists)}"
    return lists[logical_id]["Properties"]["Domains"]


def test_dns_queries_are_blocked_by_default(template_json):
    """The channel no security group describes.

    The sandbox never contacts a nameserver itself; it asks the VPC's Route 53
    Resolver, which is not a destination any egress rule can name. Without a DNS
    firewall a tool can encode data into the labels of a query for a domain whose
    nameserver an attacker runs, and the resolver carries it out -- with no TCP
    connection leaving the VPC and nothing for the egress rule to stop.

    That this was open is measured, not assumed: a live sandbox run got
    ``[Errno 99] Cannot assign requested address`` from ``api.open-meteo.com``, and
    EADDRNOTAVAIL is raised while connecting, which means the name resolved first.
    """
    rules = _dns_rules(template_json)
    blocks = [r for r in rules if r["Action"] == "BLOCK"]
    assert blocks, f"no BLOCK rule -- every DNS query is permitted: {rules}"
    for rule in blocks:
        listed = _domain_list(template_json, rule["FirewallDomainListId"]["Fn::GetAtt"][0])
        assert listed == ["*"], f"the BLOCK rule is scoped to {listed}, so it is not a default deny"
        # NODATA or an override would keep the tool waiting; NXDOMAIN surfaces as
        # "name or service not known", which tool_tester already classifies.
        assert rule["BlockResponse"] == "NXDOMAIN", f"BLOCK response is {rule.get('BlockResponse')!r}"


def test_the_allow_rule_is_evaluated_before_the_block_rule(template_json):
    """The one ordering mistake this construct can make silently.

    A lower priority number is evaluated first. With the two inverted, the BLOCK
    rule matches everything and the ALLOW rule is never reached, so the sandbox
    could not resolve the logs endpoint -- and nothing else in this file would fail,
    because a sandbox's own execution logs do not travel through that endpoint.
    """
    rules = _dns_rules(template_json)
    allows = [r for r in rules if r["Action"] == "ALLOW"]
    blocks = [r for r in rules if r["Action"] == "BLOCK"]
    assert allows and blocks, f"expected both an ALLOW and a BLOCK rule: {rules}"
    assert max(r["Priority"] for r in allows) < min(r["Priority"] for r in blocks), (
        f"the BLOCK rule is evaluated before the ALLOW rule: {rules}"
    )


def test_the_dns_allow_list_is_only_the_one_reachable_endpoint(template_json):
    """Resolution is permitted exactly where a packet could actually go.

    ``*.amazonaws.com`` would also be defensible, since the security group permits
    443 to one destination regardless. This is the tighter choice, and the assertion
    exists so that widening it is a deliberate edit rather than a convenience during
    a debugging session.
    """
    rules = _dns_rules(template_json)
    allows = [r for r in rules if r["Action"] == "ALLOW"]
    for rule in allows:
        listed = _domain_list(template_json, rule["FirewallDomainListId"]["Fn::GetAtt"][0])
        assert "*" not in listed, f"the ALLOW list permits everything: {listed}"
        for domain in listed:
            assert domain.endswith(f"logs.{REGION}.amazonaws.com"), f"unexpected permitted domain {domain!r}"
        # ``x`` matches only x and ``*.x`` matches only its subdomains, so the
        # endpoint's own name needs both forms or half of them fails to resolve.
        assert f"logs.{REGION}.amazonaws.com" in listed
        assert f"*.logs.{REGION}.amazonaws.com" in listed


def test_the_rule_group_is_attached_to_the_sandbox_vpc(template_json):
    """An unassociated rule group filters nothing and fails no other test."""
    assocs = _of_type(template_json, "AWS::Route53Resolver::FirewallRuleGroupAssociation")
    assert len(assocs) == 1, f"expected exactly one association: {list(assocs)}"
    props = next(iter(assocs.values()))["Properties"]
    assert props["VpcId"]["Ref"] == _vpc_logical_id(template_json)
    group = next(iter(_of_type(template_json, "AWS::Route53Resolver::FirewallRuleGroup")))
    assert props["FirewallRuleGroupId"]["Fn::GetAtt"][0] == group
    # 100 to 9900; a value outside the range fails at deploy time, not at synth.
    assert 100 < props["Priority"] < 9900, f"association priority {props['Priority']} is out of range"


def _sdk_call(rendered: dict | str) -> dict:
    """Parse one ``AwsCustomResource`` lifecycle call back into a dict.

    The call is serialized as a JSON *string*, but any CDK token inside it makes
    CloudFormation render it as an ``Fn::Join`` over string fragments and ``Ref``
    objects instead. Reading it therefore means re-joining the fragments, substituting
    each ``Ref`` with the logical id it names, and only then parsing -- which is also
    why the assertions compare ``ResourceId`` to a bare logical id rather than to a
    ``{"Ref": ...}``.
    """
    if isinstance(rendered, str):
        return json.loads(rendered)
    parts = rendered["Fn::Join"][1]
    flat = ""
    for part in parts:
        if isinstance(part, str):
            flat += part
        else:
            # Only ``Ref`` appears here. Anything else would mean the construct started
            # emitting a shape this helper silently mangles, so fail loudly instead.
            assert set(part) == {"Ref"}, f"unexpected intrinsic in an SDK call: {part}"
            flat += part["Ref"]
    return json.loads(flat)


def test_the_dns_firewall_does_not_fail_open(template_json):
    """``FirewallFailOpen`` is the whole control, not a tuning knob.

    Enabled, a Resolver-side failure lets every query through and the exfiltration
    channel reopens with no sign in the template that it could. Asserted because the
    safe value here is the AWS default, and an assertion is the only thing that keeps
    a default from being changed by someone debugging a resolution failure.

    Asserted against a ``Custom::DnsFirewallFailClosed`` rather than a
    ``AWS::Route53Resolver::FirewallConfig``, because the latter does not exist.
    CloudFormation rejected it by name -- *"Unrecognized resource types"* -- when this
    test passed against a hand-written ``CfnResource``. That is the failure mode this
    version is shaped against: a template-shape assertion agreeing with a template
    CloudFormation will not accept. So the checks below name the resource type the
    construct actually emits AND the SDK call inside it, since an ``AwsCustomResource``
    with the right type and the wrong parameters would satisfy a type-only check while
    setting nothing.
    """
    configs = [
        r["Properties"] for r in template_json["Resources"].values() if r["Type"] == "Custom::DnsFirewallFailClosed"
    ]
    assert len(configs) == 1, f"expected one fail-closed custom resource, got {len(configs)}"

    for event in ("Create", "Update"):
        assert event in configs[0], f"no {event} call -- the setting would not be re-asserted"
        call = _sdk_call(configs[0][event])
        assert call["service"] == "Route53Resolver"
        assert call["action"] == "updateFirewallConfig"
        assert call["parameters"]["FirewallFailOpen"] == "DISABLED"
        assert call["parameters"]["ResourceId"] == _vpc_logical_id(template_json)

    # Reverting to ENABLED on teardown would reopen the channel while the sandbox is
    # still alive. The absence is deliberate, so it is asserted.
    assert "Delete" not in configs[0], (
        "a Delete call would re-enable fail-open during teardown, reopening DNS egress "
        "for as long as the sandbox VPC still exists"
    )


def test_the_fail_closed_role_can_make_the_call_it_is_for(template_json):
    """``ec2:DescribeVpcs``, which is not discoverable from the Resolver action.

    FOUND BY DEPLOYING. With only the two ``route53resolver`` actions granted, the custom
    resource failed and took the stack into rollback::

        Received response status [FAILED] ... Message returned:
        [RSLVR-02309] You don't have a permission to call ec2:describe-vpcs.

    Resolver resolves the VPC under the *caller's* identity before it will touch the
    config, so the permission lands on this role and appears nowhere in the documentation
    for ``UpdateFirewallConfig``. A grant discovered that way is exactly the kind a later
    reader deletes as unrelated -- an ``ec2`` action on a DNS construct reads like a
    leftover -- and the cost of being wrong is a failed stack update, not a warning. Hence
    an assertion naming it.

    The wildcard resource is asserted too, rather than tolerated: ``ec2:DescribeVpcs``
    declares no resource types, so ``*`` is the only expressible value, and the
    ``ec2:Region`` condition is the only available narrowing. Asserting the condition is
    what stops the statement being quietly widened to every region.
    """
    policies = [
        r["Properties"]
        for lid, r in template_json["Resources"].items()
        if r["Type"] == "AWS::IAM::Policy" and lid.startswith("ToolSandboxDnsFirewallFailClosed")
    ]
    assert len(policies) == 1, f"expected one policy for the fail-closed role, got {len(policies)}"
    statements = policies[0]["PolicyDocument"]["Statement"]

    resolver = [s for s in statements if any("route53resolver:" in a for a in s["Action"])]
    assert resolver, f"no route53resolver grant at all: {statements}"
    assert "route53resolver:UpdateFirewallConfig" in resolver[0]["Action"]

    describe = [
        s
        for s in statements
        if ({s["Action"]} if isinstance(s["Action"], str) else set(s["Action"])) == {"ec2:DescribeVpcs"}
    ]
    assert describe, (
        "no ec2:DescribeVpcs grant -- UpdateFirewallConfig fails with RSLVR-02309 and "
        f"rolls the stack back. Statements: {statements}"
    )
    assert describe[0]["Resource"] == "*"
    assert describe[0].get("Condition") == {"StringEquals": {"ec2:Region": REGION}}, (
        "ec2:DescribeVpcs is wildcard-only and must stay constrained to the deployment region"
    )

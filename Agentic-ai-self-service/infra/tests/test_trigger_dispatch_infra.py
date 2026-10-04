"""Production wiring contract for durable runtime-trigger delivery."""

from __future__ import annotations

import json
import re

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.config import PlatformConfig
from stacks.platform.lambdas import trigger_rule_prefix
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import statements_by_role


@pytest.fixture(scope="module")
def template_json() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TriggerDispatchTestStack",
        environment_name="test",
        project_name="agentcore-workflow",
        env=cdk.Environment(
            region="us-east-1",
            account="123456789012",
        ),
    )
    return Template.from_stack(stack).to_json()


def _of_type(template: dict, type_name: str) -> dict[str, dict]:
    return {
        logical_id: resource for logical_id, resource in template["Resources"].items() if resource["Type"] == type_name
    }


def _queue_ids(template: dict) -> tuple[str, str]:
    queues = _of_type(template, "AWS::SQS::Queue")
    dispatch = [logical_id for logical_id in queues if logical_id.startswith("TriggerDispatchQueue")]
    dead_letter = [logical_id for logical_id in queues if logical_id.startswith("TriggerDispatchDeadLetterQueue")]
    assert len(dispatch) == 1, dispatch
    assert len(dead_letter) == 1, dead_letter
    return dispatch[0], dead_letter[0]


def _deployment_lambda_id(template: dict) -> str:
    matches = [
        logical_id
        for logical_id, resource in _of_type(
            template,
            "AWS::Lambda::Function",
        ).items()
        if resource["Properties"].get("Handler") == "src/app/deployment_handler.handler"
    ]
    assert len(matches) == 1, matches
    return matches[0]


def _deployment_role_statements(template: dict) -> list[dict]:
    role_ids = [
        logical_id
        for logical_id in _of_type(template, "AWS::IAM::Role")
        if logical_id.startswith("DeploymentLambdaRole")
    ]
    assert len(role_ids) == 1, role_ids
    return [statement for _source, statement in statements_by_role(template)[role_ids[0]]]


def _actions(statement: dict) -> set[str]:
    value = statement.get("Action", [])
    return {value} if isinstance(value, str) else set(value)


def test_dispatch_queue_is_encrypted_durable_and_redrives(
    template_json: dict,
) -> None:
    dispatch_id, dead_letter_id = _queue_ids(template_json)
    queues = _of_type(template_json, "AWS::SQS::Queue")
    dispatch = queues[dispatch_id]["Properties"]
    dead_letter = queues[dead_letter_id]["Properties"]

    assert dispatch["SqsManagedSseEnabled"] is True
    assert dead_letter["SqsManagedSseEnabled"] is True
    assert dispatch["VisibilityTimeout"] == 3600
    assert dispatch["MessageRetentionPeriod"] == 4 * 24 * 60 * 60
    assert dead_letter["MessageRetentionPeriod"] == 14 * 24 * 60 * 60
    assert dispatch["RedrivePolicy"] == {
        "deadLetterTargetArn": {
            "Fn::GetAtt": [dead_letter_id, "Arn"],
        },
        "maxReceiveCount": 5,
    }


def test_only_this_stacks_trigger_rules_may_enqueue(
    template_json: dict,
) -> None:
    dispatch_id, _ = _queue_ids(template_json)
    policies = [
        resource["Properties"]
        for resource in _of_type(
            template_json,
            "AWS::SQS::QueuePolicy",
        ).values()
        if {"Ref": dispatch_id} in resource["Properties"]["Queues"]
    ]
    assert len(policies) == 1, policies
    statements = policies[0]["PolicyDocument"]["Statement"]

    allow = [
        statement
        for statement in statements
        if statement.get("Effect") == "Allow" and statement.get("Principal") == {"Service": "events.amazonaws.com"}
    ]
    assert len(allow) == 1, allow
    assert allow[0]["Action"] == "sqs:SendMessage"
    assert allow[0]["Resource"] == {
        "Fn::GetAtt": [dispatch_id, "Arn"],
    }
    assert allow[0]["Condition"]["StringEquals"] == {
        "aws:SourceAccount": "123456789012",
    }
    assert "rule/agentcore-workflow-test-trigger-*" in json.dumps(
        allow[0]["Condition"]["ArnLike"]["aws:SourceArn"],
        sort_keys=True,
    )

    ssl_denies = [
        statement
        for statement in statements
        if statement.get("Effect") == "Deny"
        and statement.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport") == "false"
    ]
    assert len(ssl_denies) == 1, ssl_denies


def test_sqs_invokes_the_deployment_lambda_one_message_at_a_time(
    template_json: dict,
) -> None:
    dispatch_id, _ = _queue_ids(template_json)
    deployment_id = _deployment_lambda_id(template_json)
    mappings = [
        resource["Properties"]
        for resource in _of_type(
            template_json,
            "AWS::Lambda::EventSourceMapping",
        ).values()
        if resource["Properties"].get("EventSourceArn") == {"Fn::GetAtt": [dispatch_id, "Arn"]}
    ]
    assert len(mappings) == 1, mappings
    assert mappings[0]["FunctionName"] == {"Ref": deployment_id}
    assert mappings[0]["BatchSize"] == 1
    assert mappings[0]["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    assert mappings[0]["ScalingConfig"] == {"MaximumConcurrency": 10}


def test_deployment_lambda_receives_the_exact_queue_and_rule_identity(
    template_json: dict,
) -> None:
    dispatch_id, _ = _queue_ids(template_json)
    deployment = _of_type(
        template_json,
        "AWS::Lambda::Function",
    )[_deployment_lambda_id(template_json)]["Properties"]
    variables = deployment["Environment"]["Variables"]

    assert variables["TRIGGER_DISPATCH_QUEUE_ARN"] == {
        "Fn::GetAtt": [dispatch_id, "Arn"],
    }
    assert variables["TRIGGER_DISPATCH_QUEUE_URL"] == {"Ref": dispatch_id}
    assert variables["TRIGGER_RULE_PREFIX"] == "agentcore-workflow-test-trigger"


def test_dynamic_rule_authority_is_prefix_and_tag_bounded(
    template_json: dict,
) -> None:
    statements = _deployment_role_statements(template_json)
    prefix = "rule/agentcore-workflow-test-trigger-*"

    def matching(actions: set[str]) -> list[dict]:
        return [
            statement
            for statement in statements
            if _actions(statement) == actions and prefix in json.dumps(statement["Resource"], sort_keys=True)
        ]

    put_rule = matching({"events:PutRule"})
    put_targets = matching({"events:PutTargets"})
    cleanup = matching({"events:RemoveTargets", "events:DeleteRule"})
    reads = matching({"events:DescribeRule", "events:ListTagsForResource"})
    assert len(put_rule) == 1, put_rule
    assert len(put_targets) == 1, put_targets
    assert len(cleanup) == 1, cleanup
    assert len(reads) == 1, reads
    assert "Condition" not in put_rule[0]
    assert "Condition" not in reads[0]

    resource_tags = {
        "aws:ResourceTag/ManagedBy": "agentcore-flows",
        "aws:ResourceTag/Purpose": "runtime-trigger",
        "aws:ResourceTag/AgentCoreStack": ("agentcore-workflow-test-us-east-1"),
    }
    dispatch_id, _ = _queue_ids(template_json)
    assert put_targets[0]["Condition"] == {
        "StringEquals": resource_tags,
        "ArnEquals": {
            "events:TargetArn": {
                "Fn::GetAtt": [dispatch_id, "Arn"],
            },
        },
    }
    assert cleanup[0]["Condition"] == {
        "StringEquals": resource_tags,
    }

    tagging = [statement for statement in statements if _actions(statement) == {"events:TagResource"}]
    assert len(tagging) == 1, tagging
    assert "rule/agentcore-workflow-test-trigger-*" in json.dumps(tagging[0]["Resource"], sort_keys=True)
    assert tagging[0]["Condition"] == {
        "StringEquals": {
            "aws:RequestTag/ManagedBy": "agentcore-flows",
            "aws:RequestTag/Purpose": "runtime-trigger",
            "aws:RequestTag/AgentCoreStack": ("agentcore-workflow-test-us-east-1"),
        },
        "ForAllValues:StringEquals": {
            "aws:TagKeys": [
                "ManagedBy",
                "Purpose",
                "AgentCoreStack",
                "TriggerId",
                "RuntimeName",
            ]
        },
    }

    sends = [statement for statement in statements if _actions(statement) == {"sqs:SendMessage"}]
    assert any(statement["Resource"] == {"Fn::GetAtt": [dispatch_id, "Arn"]} for statement in sends)


def test_completed_delivery_rows_have_a_ttl_but_trigger_rows_do_not_need_one(
    template_json: dict,
) -> None:
    matches = [
        resource["Properties"]
        for resource in _of_type(
            template_json,
            "AWS::DynamoDB::Table",
        ).values()
        if resource["Properties"].get("TableName") == "agentcore-workflow-test-triggers"
    ]
    assert len(matches) == 1, matches
    assert matches[0]["TimeToLiveSpecification"] == {
        "AttributeName": "ttl",
        "Enabled": True,
    }


def test_webhook_is_the_only_non_health_route_without_a_jwt(
    template_json: dict,
) -> None:
    routes = {
        resource["Properties"]["RouteKey"]: resource["Properties"]
        for resource in _of_type(
            template_json,
            "AWS::ApiGatewayV2::Route",
        ).values()
    }
    hook_key = "POST /hooks/{runtime_name}/{trigger_id}"
    assert hook_key in routes
    assert routes[hook_key].get("AuthorizationType") in (None, "NONE")
    assert "AuthorizerId" not in routes[hook_key]

    unauthenticated = {
        key
        for key, properties in routes.items()
        if not (properties.get("AuthorizationType") == "JWT" and properties.get("AuthorizerId"))
    }
    assert unauthenticated == {"GET /health", hook_key}


def test_webhook_headers_and_route_throttle_are_synthesized(
    template_json: dict,
) -> None:
    api = next(
        iter(
            _of_type(
                template_json,
                "AWS::ApiGatewayV2::Api",
            ).values()
        )
    )["Properties"]
    allowed_headers = set(api["CorsConfiguration"]["AllowHeaders"])
    assert {
        "X-AgentCore-Timestamp",
        "X-AgentCore-Delivery-Id",
        "X-AgentCore-Signature",
    } <= allowed_headers

    stages = _of_type(template_json, "AWS::ApiGatewayV2::Stage")
    assert len(stages) == 1, stages
    route_settings = next(iter(stages.values()))["Properties"]["RouteSettings"]
    assert route_settings["POST /hooks/{runtime_name}/{trigger_id}"] == {
        "ThrottlingBurstLimit": 10,
        "ThrottlingRateLimit": 20,
    }


def test_cloudfront_forwards_webhooks_without_caching(
    template_json: dict,
) -> None:
    distributions = _of_type(
        template_json,
        "AWS::CloudFront::Distribution",
    )
    assert len(distributions) == 1, distributions
    behaviors = next(iter(distributions.values()))["Properties"]["DistributionConfig"]["CacheBehaviors"]
    hook = [behavior for behavior in behaviors if behavior.get("PathPattern") == "/hooks/*"]
    assert len(hook) == 1, hook
    assert hook[0]["ViewerProtocolPolicy"] == "redirect-to-https"
    assert hook[0]["AllowedMethods"] == [
        "GET",
        "HEAD",
        "OPTIONS",
        "PUT",
        "PATCH",
        "POST",
        "DELETE",
    ]
    assert "CachePolicyId" in hook[0]
    assert "OriginRequestPolicyId" in hook[0]


def test_first_dead_letter_message_raises_an_operator_alarm(
    template_json: dict,
) -> None:
    _, dead_letter_id = _queue_ids(template_json)
    alarms = [
        resource["Properties"]
        for resource in _of_type(
            template_json,
            "AWS::CloudWatch::Alarm",
        ).values()
        if resource["Properties"].get("AlarmName") == "agentcore-workflow-test-trigger-dispatch-dlq"
    ]
    assert len(alarms) == 1, alarms
    alarm = alarms[0]
    assert alarm["Namespace"] == "AWS/SQS"
    assert alarm["MetricName"] == "ApproximateNumberOfMessagesVisible"
    assert alarm["Threshold"] == 1
    assert alarm["TreatMissingData"] == "notBreaching"
    assert alarm["Dimensions"] == [
        {
            "Name": "QueueName",
            "Value": {"Fn::GetAtt": [dead_letter_id, "QueueName"]},
        }
    ]
    assert alarm["AlarmActions"] == alarm["OKActions"]


def test_rule_prefix_is_deterministic_safe_and_never_overflows() -> None:
    cfg = PlatformConfig(
        env="environment.with unsafe spaces" * 3,
        project="project/with unsafe spaces" * 4,
        removal_policy=cdk.RemovalPolicy.DESTROY,
        allow_destroy=True,
    )
    first = trigger_rule_prefix(cfg)
    assert first == trigger_rule_prefix(cfg)
    assert len(first) <= 31
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", first)

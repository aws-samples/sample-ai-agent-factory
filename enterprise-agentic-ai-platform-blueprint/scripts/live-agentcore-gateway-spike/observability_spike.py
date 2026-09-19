#!/usr/bin/env python3
"""Reversible AgentCore Gateway rate-limit OTEL observability spike."""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Mapping

from botocore.exceptions import ClientError

from cognito_litellm_spike import CognitoLiteLLMSpike
from gateway_spike import Config, SpikeError, aws_error_code, request_id, validate_config


class GatewayObservabilitySpike(CognitoLiteLLMSpike):
    throttle_positive_twin = "rate_limit_allowed_known_good_model"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.logs = self.session.client("logs")
        self.xray = self.session.client("xray")

    @property
    def resource_policy_name(self) -> str:
        return f"{self.config.prefix}-xray-spans"

    @property
    def logs_source_name(self) -> str:
        return f"{self.config.prefix}-gateway-app-logs"

    @property
    def traces_source_name(self) -> str:
        return f"{self.config.prefix}-gateway-traces"

    @property
    def logs_destination_name(self) -> str:
        return f"{self.config.prefix}-gateway-cwl"

    @property
    def traces_destination_name(self) -> str:
        return f"{self.config.prefix}-gateway-xray"

    def log_group_name(self, gateway_id: str) -> str:
        return (
            "/aws/vendedlogs/bedrock-agentcore/gateway/"
            f"APPLICATION_LOGS/{gateway_id}"
        )

    def indexing_percentage(self) -> float:
        response = self.xray.get_indexing_rules()
        for indexing_rule in response.get("IndexingRules", []):
            if indexing_rule.get("Name") == "Default":
                probabilistic = indexing_rule.get("Rule", {}).get("Probabilistic", {})
                return float(probabilistic.get("DesiredSamplingPercentage", 0.0))
        raise SpikeError("X-Ray Default indexing rule is absent")

    def wait_indexing_percentage(
        self, desired: float, timeout: int = 300
    ) -> None:
        deadline = time.monotonic() + timeout
        last = -1.0
        while time.monotonic() < deadline:
            last = self.indexing_percentage()
            if last == desired:
                return
            time.sleep(5)
        raise SpikeError(
            f"X-Ray indexing percentage did not reach {desired}; last={last}"
        )

    def enable_test_indexing(self) -> None:
        previous = self.indexing_percentage()
        self.save_state(previousIndexingPercentage=previous)
        self.evidence.add(
            "indexing_percentage_captured",
            desiredSamplingPercentage=previous,
        )
        if previous != 100.0:
            response = self.xray.update_indexing_rule(
                Name="Default",
                Rule={
                    "Probabilistic": {"DesiredSamplingPercentage": 100.0}
                },
            )
            self.save_state(changedIndexingPercentage=True)
            self.wait_indexing_percentage(100.0)
            self.evidence.add(
                "indexing_percentage_updated",
                desiredSamplingPercentage=100.0,
                awsRequestId=request_id(response),
            )

    def wait_trace_destination(
        self, desired: str, timeout: int = 600
    ) -> Mapping[str, Any]:
        deadline = time.monotonic() + timeout
        last: Mapping[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.xray.get_trace_segment_destination()
            if last.get("Destination") == desired and last.get("Status") == "ACTIVE":
                return last
            time.sleep(10)
        raise SpikeError(
            f"X-Ray destination did not reach ACTIVE/{desired} within {timeout}s; "
            f"last={dict(last)}"
        )

    def prepare_transaction_search(self) -> None:
        destination = self.xray.get_trace_segment_destination()
        previous = str(destination.get("Destination", "XRay"))
        self.save_state(previousTraceDestination=previous)
        self.evidence.add(
            "trace_destination_captured",
            destination=previous,
            status=destination.get("Status"),
            awsRequestId=request_id(destination),
        )
        self.enable_test_indexing()

        partition = self.session.get_partition_for_region(self.config.region)
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "TransactionSearchXRayAccess",
                    "Effect": "Allow",
                    "Principal": {"Service": "xray.amazonaws.com"},
                    "Action": "logs:PutLogEvents",
                    "Resource": [
                        f"arn:{partition}:logs:{self.config.region}:{self.config.account_id}:log-group:aws/spans:*",
                        f"arn:{partition}:logs:{self.config.region}:{self.config.account_id}:log-group:/aws/application-signals/data:*",
                    ],
                    "Condition": {
                        "ArnLike": {
                            "aws:SourceArn": f"arn:{partition}:xray:{self.config.region}:{self.config.account_id}:*"
                        },
                        "StringEquals": {
                            "aws:SourceAccount": self.config.account_id
                        },
                    },
                }
            ],
        }
        policy_response = self.logs.put_resource_policy(
            policyName=self.resource_policy_name,
            policyDocument=json.dumps(policy),
        )
        self.save_state(createdTraceResourcePolicy=True)
        self.evidence.add(
            "trace_resource_policy_created",
            policyName=self.resource_policy_name,
            awsRequestId=request_id(policy_response),
        )

        if previous != "CloudWatchLogs":
            update = self.xray.update_trace_segment_destination(
                Destination="CloudWatchLogs"
            )
            self.save_state(changedTraceDestination=True)
            self.evidence.add(
                "trace_destination_updated",
                previous=previous,
                destination="CloudWatchLogs",
                awsRequestId=request_id(update),
            )
        self.wait_trace_destination("CloudWatchLogs")

    def configure_gateway_deliveries(self, gateway: Mapping[str, Any]) -> None:
        gateway_id = str(gateway["gatewayId"])
        gateway_arn = str(gateway["gatewayArn"])
        log_group_name = self.log_group_name(gateway_id)
        partition = self.session.get_partition_for_region(self.config.region)
        log_group_arn = (
            f"arn:{partition}:logs:{self.config.region}:{self.config.account_id}:"
            f"log-group:{log_group_name}"
        )

        try:
            create_group = self.logs.create_log_group(
                logGroupName=log_group_name,
                tags=self.config.tags,
            )
            self.save_state(createdLogGroup=True, logGroupName=log_group_name)
            self.evidence.add(
                "gateway_log_group_created",
                logGroupName=log_group_name,
                awsRequestId=request_id(create_group),
            )
        except ClientError as error:
            if aws_error_code(error) != "ResourceAlreadyExistsException":
                raise
            if not self.state.get("createdLogGroup"):
                raise SpikeError(
                    f"Log group {log_group_name} exists without this run's state"
                ) from error
        self.logs.put_retention_policy(logGroupName=log_group_name, retentionInDays=1)

        logs_source = self.logs.put_delivery_source(
            name=self.logs_source_name,
            logType="APPLICATION_LOGS",
            resourceArn=gateway_arn,
        )
        self.save_state(logsSourceName=self.logs_source_name)
        self.evidence.add(
            "gateway_logs_source_created",
            sourceName=self.logs_source_name,
            awsRequestId=request_id(logs_source),
        )

        traces_source = self.logs.put_delivery_source(
            name=self.traces_source_name,
            logType="TRACES",
            resourceArn=gateway_arn,
        )
        self.save_state(tracesSourceName=self.traces_source_name)
        self.evidence.add(
            "gateway_traces_source_created",
            sourceName=self.traces_source_name,
            awsRequestId=request_id(traces_source),
        )

        logs_destination = self.logs.put_delivery_destination(
            name=self.logs_destination_name,
            deliveryDestinationType="CWL",
            deliveryDestinationConfiguration={
                "destinationResourceArn": log_group_arn
            },
            outputFormat="json",
            tags=self.config.tags,
        )
        logs_destination_arn = str(
            logs_destination["deliveryDestination"]["arn"]
        )
        self.save_state(
            logsDestinationName=self.logs_destination_name,
            logsDestinationArn=logs_destination_arn,
        )

        traces_destination = self.logs.put_delivery_destination(
            name=self.traces_destination_name,
            deliveryDestinationType="XRAY",
            tags=self.config.tags,
        )
        traces_destination_arn = str(
            traces_destination["deliveryDestination"]["arn"]
        )
        self.save_state(
            tracesDestinationName=self.traces_destination_name,
            tracesDestinationArn=traces_destination_arn,
        )

        logs_delivery = self.logs.create_delivery(
            deliverySourceName=self.logs_source_name,
            deliveryDestinationArn=logs_destination_arn,
            tags=self.config.tags,
        )
        logs_delivery_id = str(logs_delivery["delivery"]["id"])
        self.save_state(logsDeliveryId=logs_delivery_id)

        traces_delivery = self.logs.create_delivery(
            deliverySourceName=self.traces_source_name,
            deliveryDestinationArn=traces_destination_arn,
            tags=self.config.tags,
        )
        traces_delivery_id = str(traces_delivery["delivery"]["id"])
        self.save_state(tracesDeliveryId=traces_delivery_id)

        self.logs.get_delivery(id=logs_delivery_id)
        self.logs.get_delivery(id=traces_delivery_id)
        self.evidence.add(
            "gateway_deliveries_created",
            logsDeliveryId=logs_delivery_id,
            tracesDeliveryId=traces_delivery_id,
            logsDestinationArn=logs_destination_arn,
            tracesDestinationArn=traces_destination_arn,
        )
        # V2 log deliveries expose no readiness status. Give the delivery
        # control plane time to propagate before generating evidence traffic.
        time.sleep(90)
        self.evidence.add("gateway_delivery_propagation_wait_completed")

    def deploy(self) -> None:
        self.verify_identity()
        self.prepare_transaction_search()
        self.ensure_cognito()
        role_arn = self.ensure_role()
        gateway = self.ensure_gateway(role_arn)
        self.ensure_target(str(gateway["gatewayId"]))
        self.configure_gateway_deliveries(gateway)
        self.evidence.add("observable_gateway_ready", gatewayId=gateway["gatewayId"])

    def ensure_permissive_rate_limit(self, gateway_id: str) -> None:
        if self.state.get("rateLimitId"):
            self.wait_rate_limit(gateway_id)
            return
        response = self.control.create_gateway_rate_limit(
            gatewayIdentifier=gateway_id,
            rateLimitId=self.config.rate_limit_id,
            description="Observe allowed then throttled decisions for one model",
            dimensionKeys=["qualifiedModelId"],
            entries=[
                {
                    "dimensions": {
                        "qualifiedModelId": self.config.model.split("/", 1)[1]
                    },
                    "requests": [{"rate": 100, "period": "minute"}],
                },
                {
                    "dimensions": {"qualifiedModelId": "*"},
                    "requests": [{"rate": 10, "period": "minute"}],
                    "tokens": [{"rate": 10_000, "period": "minute"}],
                },
            ],
            clientToken=self.client_token("observableRateLimit"),
        )
        self.save_state(rateLimitId=response["rateLimitId"])
        self.evidence.add(
            "permissive_rate_limit_created",
            rateLimitId=response["rateLimitId"],
            awsRequestId=request_id(response),
        )
        self.wait_rate_limit(gateway_id)

    def update_zero_rate_limit(self, gateway_id: str) -> None:
        response = self.control.update_gateway_rate_limit(
            gatewayIdentifier=gateway_id,
            rateLimitId=self.config.rate_limit_id,
            description="Block known-good model after allowed OTEL evidence",
            entries=[
                {
                    "dimensions": {
                        "qualifiedModelId": self.config.model.split("/", 1)[1]
                    },
                    "requests": [{"rate": 0, "period": "second"}],
                },
                {
                    "dimensions": {"qualifiedModelId": "*"},
                    "requests": [{"rate": 10, "period": "minute"}],
                    "tokens": [{"rate": 10_000, "period": "minute"}],
                },
            ],
        )
        self.evidence.add(
            "zero_rate_limit_updated",
            rateLimitId=self.config.rate_limit_id,
            awsRequestId=request_id(response),
        )
        self.wait_rate_limit(gateway_id)

    def _invoke_and_record_allowed(self) -> str:
        status, headers, content = self.invoke(stream=False)
        if status != 200:
            raise SpikeError(
                f"Permissive customer rate limit returned HTTP {status}"
            )
        response_id = self.response_request_id(headers)
        if not response_id:
            raise SpikeError("Allowed response did not expose a request ID")
        self.evidence.add(
            "rate_limit_allowed_known_good_model",
            httpStatus=status,
            responseBytes=len(content),
            awsRequestId=response_id,
        )
        return response_id.split(",", 1)[0].strip()

    def _invoke_and_record_throttled(self) -> str:
        status, headers, content = self.invoke(stream=False)
        if status != 429:
            raise SpikeError(f"Expected rate-limit HTTP 429, received {status}")
        response_id = self.response_request_id(headers)
        if not response_id:
            raise SpikeError("Throttled response did not expose a request ID")
        self.evidence.add(
            "rate_limit_throttled_known_good_model",
            httpStatus=status,
            responseBytes=len(content),
            awsRequestId=response_id,
            positiveTwin=self.throttle_positive_twin,
        )
        return response_id.split(",", 1)[0].strip()

    @staticmethod
    def _message_has_decision(
        message: str,
        request_id_value: str,
        decision: str,
        rate_limit_id: str,
    ) -> bool:
        return (
            request_id_value in message
            and "aws.agentcore.gateway.throttle.customer.decision" in message
            and decision in message
            and "aws.agentcore.gateway.throttle.customer.evaluated" in message
            and (decision != "throttled" or rate_limit_id in message)
        )

    def wait_for_otel(
        self,
        allowed_request_id: str,
        throttled_request_id: str,
        start_time_ms: int,
        timeout: int = 360,
    ) -> None:
        deadline = time.monotonic() + timeout
        allowed_event: str | None = None
        throttled_event: str | None = None
        while time.monotonic() < deadline:
            try:
                response = self.logs.filter_log_events(
                    logGroupName="aws/spans",
                    startTime=start_time_ms,
                    limit=1000,
                )
            except ClientError as error:
                if aws_error_code(error) == "ResourceNotFoundException":
                    time.sleep(10)
                    continue
                raise
            for event in response.get("events", []):
                message = str(event.get("message", ""))
                if self._message_has_decision(
                    message,
                    allowed_request_id,
                    "allowed",
                    self.config.rate_limit_id,
                ):
                    allowed_event = str(event.get("eventId", ""))
                if self._message_has_decision(
                    message,
                    throttled_request_id,
                    "throttled",
                    self.config.rate_limit_id,
                ):
                    throttled_event = str(event.get("eventId", ""))
            if allowed_event and throttled_event:
                self.evidence.add(
                    "otel_rate_limit_spans_correlated",
                    allowedLogEventId=allowed_event,
                    throttledLogEventId=throttled_event,
                    allowedAwsRequestId=allowed_request_id,
                    throttledAwsRequestId=throttled_request_id,
                )
                return
            time.sleep(10)
        raise SpikeError(
            "CloudWatch aws/spans did not contain correlated allowed and throttled rate-limit attributes"
        )

    def verify(self) -> None:
        self.verify_identity()
        self.verify_models()
        self.verify_litellm_model(stream=False)
        self.verify_litellm_model(stream=True)
        gateway_id = str(self.state.get("gatewayId", ""))
        if not gateway_id:
            raise SpikeError("Gateway ID is absent from state")
        start_time_ms = int(time.time() * 1000) - 5_000
        self.ensure_permissive_rate_limit(gateway_id)
        time.sleep(35)
        allowed_request_id = self._invoke_and_record_allowed()
        self.update_zero_rate_limit(gateway_id)
        time.sleep(35)
        throttled_request_id = self._invoke_and_record_throttled()
        self.wait_for_otel(
            allowed_request_id,
            throttled_request_id,
            start_time_ms,
        )

    def cleanup_observability(self) -> None:
        for key in ("tracesDeliveryId", "logsDeliveryId"):
            delivery_id = self.state.get(key)
            if not delivery_id:
                continue
            try:
                self.logs.delete_delivery(id=str(delivery_id))
                self.evidence.add("delivery_deleted", deliveryId=delivery_id)
            except ClientError as error:
                if aws_error_code(error) != "ResourceNotFoundException":
                    raise

        for key in ("tracesSourceName", "logsSourceName"):
            source_name = self.state.get(key)
            if not source_name:
                continue
            try:
                self.logs.delete_delivery_source(name=str(source_name))
                self.evidence.add("delivery_source_deleted", sourceName=source_name)
            except ClientError as error:
                if aws_error_code(error) != "ResourceNotFoundException":
                    raise

        for key in ("tracesDestinationName", "logsDestinationName"):
            destination_name = self.state.get(key)
            if not destination_name:
                continue
            try:
                self.logs.delete_delivery_destination(name=str(destination_name))
                self.evidence.add(
                    "delivery_destination_deleted",
                    destinationName=destination_name,
                )
            except ClientError as error:
                if aws_error_code(error) != "ResourceNotFoundException":
                    raise

        log_group_name = self.state.get("logGroupName")
        if log_group_name and self.state.get("createdLogGroup"):
            try:
                self.logs.delete_log_group(logGroupName=str(log_group_name))
                self.evidence.add("gateway_log_group_deleted", logGroupName=log_group_name)
            except ClientError as error:
                if aws_error_code(error) != "ResourceNotFoundException":
                    raise

    def resource_policy_exists(self) -> bool:
        response = self.logs.describe_resource_policies()
        return any(
            policy.get("policyName") == self.resource_policy_name
            for policy in response.get("resourcePolicies", [])
        )

    def restore_transaction_search(self) -> None:
        previous = str(self.state.get("previousTraceDestination", "XRay"))
        previous_indexing = float(
            self.state.get("previousIndexingPercentage", 0.0)
        )
        if self.state.get("changedIndexingPercentage"):
            response = self.xray.update_indexing_rule(
                Name="Default",
                Rule={
                    "Probabilistic": {
                        "DesiredSamplingPercentage": previous_indexing
                    }
                },
            )
            self.wait_indexing_percentage(previous_indexing)
            self.evidence.add(
                "indexing_percentage_restored",
                desiredSamplingPercentage=previous_indexing,
                awsRequestId=request_id(response),
            )
        current = self.xray.get_trace_segment_destination()
        current_destination = str(current.get("Destination", "XRay"))
        if current.get("Status") != "ACTIVE":
            current = self.wait_trace_destination(current_destination)
            current_destination = str(current.get("Destination", current_destination))
        if self.state.get("changedTraceDestination") and current_destination != previous:
            response = self.xray.update_trace_segment_destination(
                Destination=previous
            )
            self.wait_trace_destination(previous)
            self.evidence.add(
                "trace_destination_restored",
                destination=previous,
                awsRequestId=request_id(response),
            )

    def cleanup(self) -> None:
        errors: list[Exception] = []
        state_snapshot = dict(self.state)
        expected_destination = str(
            self.state.get("previousTraceDestination", "XRay")
        )
        expected_indexing = float(
            self.state.get("previousIndexingPercentage", 0.0)
        )
        try:
            self.cleanup_observability()
        except Exception as error:
            errors.append(error)
        # Restore the account-global setting while its captured prior value is
        # still in state. Parent cleanup intentionally clears state after it
        # removes Gateway, IAM, and Cognito resources.
        try:
            self.restore_transaction_search()
        except Exception as error:
            errors.append(error)
        try:
            super().cleanup()
        except Exception as error:
            errors.append(error)
        if state_snapshot.get("createdTraceResourcePolicy"):
            if self.resource_policy_exists():
                self.evidence.add(
                    "trace_resource_policy_operator_cleanup_required",
                    policyName=self.resource_policy_name,
                )
                errors.append(
                    SpikeError(
                        "Operator cleanup required for CloudWatch resource policy "
                        f"{self.resource_policy_name}"
                    )
                )
            else:
                self.evidence.add(
                    "trace_resource_policy_absence_verified",
                    policyName=self.resource_policy_name,
                )
        if errors:
            self.state = state_snapshot
            self.state_store.write(self.state)
            raise SpikeError(
                "Observability cleanup failures: "
                + "; ".join(str(error) for error in errors)
            )
        destination = self.xray.get_trace_segment_destination()
        if destination.get("Destination") != expected_destination:
            raise SpikeError(
                f"Trace destination residue: {destination.get('Destination')} "
                f"!= {expected_destination}"
            )
        actual_indexing = self.indexing_percentage()
        if actual_indexing != expected_indexing:
            raise SpikeError(
                f"Indexing percentage residue: {actual_indexing} "
                f"!= {expected_indexing}"
            )
        self.state = {}
        self.state_store.write(self.state)
        self.evidence.add("observability_zero_residue_verified")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("deploy", "verify", "cleanup", "all"))
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--prefix", default="aiaf-live-20260918")
    parser.add_argument("--model", default="bedrock-mantle/openai.gpt-oss-120b")
    parser.add_argument("--state-file")
    parser.add_argument("--evidence-file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = validate_config(args)
    spike = GatewayObservabilitySpike(config)
    status = "failed"
    try:
        if args.command == "deploy":
            spike.deploy()
            status = "deploy-passed"
        elif args.command == "verify":
            spike.verify()
            status = "verify-passed"
        elif args.command == "cleanup":
            spike.cleanup()
            status = "cleanup-passed"
        else:
            verification_error: Exception | None = None
            try:
                spike.deploy()
                spike.verify()
            except Exception as error:
                verification_error = error
            try:
                spike.cleanup()
            except Exception as cleanup_error:
                if verification_error is not None:
                    raise SpikeError(
                        f"Verification failed: {verification_error}; cleanup failed: {cleanup_error}"
                    ) from cleanup_error
                raise
            if verification_error is not None:
                raise verification_error
            status = "passed"
        spike.evidence.finish(status)
        print(f"Evidence: {config.evidence_path}")
        return 0
    except Exception as error:
        spike.evidence.add(
            "failure",
            errorType=type(error).__name__,
            errorCode=aws_error_code(error) if isinstance(error, ClientError) else None,
            message=str(error),
        )
        spike.evidence.finish("failed")
        print(f"FAIL: {error}", file=sys.stderr)
        print(f"Evidence: {config.evidence_path}", file=sys.stderr)
        return 1
    finally:
        spike.close()


if __name__ == "__main__":
    import sys

    raise SystemExit(main())

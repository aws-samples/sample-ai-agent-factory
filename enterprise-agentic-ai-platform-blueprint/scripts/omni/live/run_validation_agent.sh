#!/usr/bin/env bash
# Launch omni_validation_agent.py under the AWS Distro for OpenTelemetry, the
# way the AgentCore observability guide instruments an agent that is not hosted
# on Runtime: traces go over OTLP/HTTP to the cell's X-Ray OTLP endpoint and
# land in the `aws/spans` log group (Transaction Search must be on).
#
#   bash scripts/omni/live/run_validation_agent.sh --profile <cell-profile> --region eu-west-1 \
#       --service-name omni-validation-agent --log-group /agenticai/omni-validation/omni-validation-agent
#
# Optional: AGENT_PYTHON=<interpreter with strands-agents and aws-opentelemetry-distro>.
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python_bin="${AGENT_PYTHON:-python3}"
region=""; service_name="omni-validation-agent"; log_group=""; profile=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    --region) region="${args[$((i + 1))]}" ;;
    --service-name) service_name="${args[$((i + 1))]}" ;;
    --log-group) log_group="${args[$((i + 1))]}" ;;
    --profile) profile="${args[$((i + 1))]}" ;;
  esac
done
# The OTLP exporter signs with the default credential chain; a named profile must be visible to it.
# botocore reads AWS_DEFAULT_PROFILE before AWS_PROFILE, so set both in case the host pins the first.
if [[ -n "$profile" ]]; then
  export AWS_PROFILE="$profile"
  export AWS_DEFAULT_PROFILE="$profile"
fi
if [[ -z "$region" || -z "$log_group" ]]; then
  echo "usage: run_validation_agent.sh --region <region> --log-group <log group> [--profile <p>] [--service-name <n>] ..." >&2
  exit 2
fi

export OTEL_PYTHON_DISTRO=aws_distro
export OTEL_PYTHON_CONFIGURATOR=aws_configurator
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_TRACES_EXPORTER=otlp
export OTEL_METRICS_EXPORTER=none
# The logs exporter matters: AgentCore Evaluations reads the GenAI prompt/completion LOG EVENTS that
# the distro's LLO handler mirrors out of the spans (Runtime does the same into its runtime log
# group). Without them every sampled trace fails with LogEventMissingException and no result is written.
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="https://xray.${region}.amazonaws.com/v1/traces"
export OTEL_EXPORTER_OTLP_LOGS_ENDPOINT="https://logs.${region}.amazonaws.com/v1/logs"
export OTEL_EXPORTER_OTLP_LOGS_HEADERS="x-aws-log-group=${log_group},x-aws-log-stream=otel/${service_name},x-aws-metric-namespace=agenticai-validation"
export OTEL_RESOURCE_ATTRIBUTES="service.name=${service_name},aws.log.group.names=${log_group},agenticai.validation=omni"
export OTEL_TRACES_SAMPLER=always_on
export AGENT_OBSERVABILITY_ENABLED=true
export OTEL_BSP_SCHEDULE_DELAY=1000
export AWS_REGION="$region"

exec "$(dirname "$python_bin")/opentelemetry-instrument" "$python_bin" "$here/omni_validation_agent.py" "$@"

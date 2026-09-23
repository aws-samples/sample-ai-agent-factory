# syntax=docker/dockerfile:1
#
# ARM64, non-root container image for the real generated-agent reference.
#
# Unlike the inert compatibility handler, this image installs the Strands +
# LiteLLM + MCP clients and runs an agent that actually calls the platform LLM
# Gateway (LiteLLMModel), the platform Tools Gateway (MCPClient), and AgentCore
# Memory. It listens on :8080 and exposes the /invocations and /ping contract
# that BedrockAgentCoreApp implements. No credentials are baked in — the Gateway
# bearer token is provided at runtime via AgentCore Identity M2M — and it runs
# as an unprivileged user. Network egress is governed by the Runtime's
# networkConfiguration at deploy time, not by this image.
#
# The base is pinned to the same AWS-maintained AL2023 Python ARM64 digest that
# the Runtime/Memory compatibility spike scanned to zero Critical/High findings;
# the image must be rebuilt from the exact product revision and re-scanned to
# zero findings before any Runtime consumes it by digest.
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# !!! ADVERSARIAL FIXTURE -- NOT FOR RELEASE !!!
# Break-the-Platform twin for the image scan gate (2026-09-23): the base is
# deliberately pinned to an outdated Debian 12 Python image (October 2023) that
# ECR basic scanning must report with CRITICAL/HIGH findings. The pipeline's
# AgentImageScanGate must refuse this digest, CloudFormation must roll the
# RuntimeMemory stack back, the prior Runtime version must keep serving, and
# production must never be reached. This commit is reverted immediately after
# the evidence is captured; the AL2023 digest-pinned base is the real one.
FROM public.ecr.aws/docker/library/python:3.12.0-slim-bookworm@sha256:be90aa1dbedbeee34dd3c4934a4f93a0227d4cf95a40196896c4c67750b21690

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install exact-pinned runtime dependencies first for layer caching.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --requirement /app/requirements.txt \
    && pip check

# Copy only the agent entrypoint.
COPY agent.py /app/agent.py

# Drop to a numeric, unprivileged user.
USER 10001

# AgentCore Runtime speaks HTTP to the container on 8080.
EXPOSE 8080

ENTRYPOINT ["python", "-u", "agent.py"]
CMD []

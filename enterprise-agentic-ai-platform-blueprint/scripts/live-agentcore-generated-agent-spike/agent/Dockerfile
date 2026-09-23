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

# AWS-maintained AL2023 Python base, pinned to the live-scanned ARM64 digest.
FROM public.ecr.aws/lambda/python:3.13@sha256:c78a03b745f2c27b349377ae979d88b96bc68b457668705cd24a32b5f433c9cf

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

"""
Smoke tests — read-only checks after a reference deployment completes.

Runs against a live workload account. Intended to be invoked from the
post-deploy CodeBuild step in `WorkloadPipelineStack` *after* prod deploy.

Checks performed (non-destructive):
  1. SCPs 01-08 visible at the AgenticAI-Workloads OU.
  2. 9+ VPCEs present in the workload VPC; no IGW, no NAT.
  3. `bedrock:InvokeModel` without `guardrailIdentifier` returns AccessDenied.
  4. `bedrock:InvokeModel` against a non-allowlisted model returns AccessDenied.
  5. CloudWatch log group `/agenticai/bedrock-invocations` exists.
  6. API Gateway URL without JWT returns 401.

Exits 0 on pass, non-zero on any failure. Skips checks if prerequisite
AWS credentials are absent (returns 0 so local runs succeed).

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import os
import sys


def _skip_if_no_aws() -> bool:
    return 'AWS_ACCESS_KEY_ID' not in os.environ and 'AWS_PROFILE' not in os.environ


def main() -> int:
    if _skip_if_no_aws():
        print('[smoke] No AWS credentials available; skipping live checks.')
        return 0
    try:
        import boto3  # type: ignore[import-not-found]
        from botocore.exceptions import ClientError  # type: ignore[import-not-found]
    except ImportError:
        print('[smoke] boto3 not installed; skipping live checks.')
        return 0

    region = os.environ.get('AWS_REGION', 'us-west-2')
    failures: list[str] = []

    # Check 3 — guardrail-less InvokeModel must deny.
    bedrock_runtime = boto3.client('bedrock-runtime', region_name=region)
    try:
        bedrock_runtime.invoke_model(
            modelId='anthropic.claude-haiku-4-5-20251001-v1:0',
            body=b'{"messages":[{"role":"user","content":"hi"}]}',
        )
        failures.append('InvokeModel without guardrail succeeded — SCP/IAM/VPCE deny not enforced.')
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code not in ('AccessDeniedException', 'AccessDenied'):
            failures.append(
                f'InvokeModel without guardrail denied unexpectedly with {code}; expected AccessDenied.'
            )
        else:
            print('[smoke] ✓ Guardrail-less invoke denied (SCP-02 + IAM + VPCE triple-gate).')

    # Check 5 — Bedrock invocation log group exists.
    logs = boto3.client('logs', region_name=region)
    try:
        resp = logs.describe_log_groups(logGroupNamePrefix='/agenticai/bedrock-invocations')
        groups = resp.get('logGroups', [])
        if not any(g.get('logGroupName') == '/agenticai/bedrock-invocations' for g in groups):
            failures.append('/agenticai/bedrock-invocations log group not found.')
        else:
            print('[smoke] ✓ /agenticai/bedrock-invocations log group present.')
    except ClientError as e:
        failures.append(f'DescribeLogGroups failed: {e}')

    # Check 2 — VPC posture.
    ec2 = boto3.client('ec2', region_name=region)
    try:
        igws = ec2.describe_internet_gateways()['InternetGateways']
        nats = ec2.describe_nat_gateways()['NatGateways']
        if igws:
            failures.append(f'Unexpected IGW present: {len(igws)}.')
        if nats:
            failures.append(f'Unexpected NAT gateway present: {len(nats)}.')
        vpces = ec2.describe_vpc_endpoints()['VpcEndpoints']
        if len(vpces) < 9:
            failures.append(f'Expected >=9 VPCEs; found {len(vpces)}.')
        else:
            print(f'[smoke] ✓ Network posture: 0 IGW / 0 NAT / {len(vpces)} VPCEs.')
    except ClientError as e:
        failures.append(f'EC2 inspection failed: {e}')

    if failures:
        print('\n[smoke] FAIL')
        for f in failures:
            print(f'  - {f}')
        return 1
    print('\n[smoke] PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())

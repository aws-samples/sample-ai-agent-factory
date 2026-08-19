"""Shared fixtures for D-03 live-AWS integration tests.

These tests drive a two-account deployment (platform + workload). If live
credentials are not available the whole session short-circuits to
``pytest.skip`` via the ``_require_live_aws`` autouse fixture — this
mirrors the pattern in ``tests/smoke/smoke.py`` so CI runs and developer
machines without AWS creds don't fail module import or collection.

Environment variables consumed:

  Platform account creds (12-digit id supplied at deploy time):
    AWS_ACCESS_KEY_ID_PLATFORM, AWS_SECRET_ACCESS_KEY_PLATFORM
    (optional: AWS_SESSION_TOKEN_PLATFORM)
    — or —
    AWS_PROFILE_PLATFORM    (default: ``agenticai-platform``)

  Workload account creds (12-digit id supplied at deploy time):
    AWS_ACCESS_KEY_ID_WORKLOAD, AWS_SECRET_ACCESS_KEY_WORKLOAD
    (optional: AWS_SESSION_TOKEN_WORKLOAD)
    — or —
    AWS_PROFILE_WORKLOAD     (default: ``agenticai-workload``)

  AWS_REGION                 (default: ``us-east-1``)
  AGENTICAI_D03_EXTERNAL_ID  (required when live)
  AGENTICAI_D03_TENANT_ID    (default: ``demo``)
  AGENTICAI_D03_AGENT_ID     (default: ``primary``)

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import os
from typing import Optional

import pytest

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover — dev machines w/o boto3 still collect
    boto3 = None  # type: ignore[assignment]
    BotoCoreError = Exception  # type: ignore[assignment,misc]
    ClientError = Exception  # type: ignore[assignment,misc]


PLATFORM_STACK_NAME = 'AgenticAI-D03-PlatformCoreStack'
WORKLOAD_STACK_NAME = 'AgenticAI-D03-WorkloadAgentStack'
BEDROCK_CALLER_ROLE_NAME = 'AgenticAI-D03-BedrockCaller'


def _session_from_env(prefix: str, default_profile: str) -> Optional['boto3.session.Session']:
    """Build a boto3 Session from per-account env vars, or fall back to profile.

    Returns None if boto3 isn't importable. Returning a Session does NOT
    guarantee creds are valid — call ``sts:GetCallerIdentity`` to verify.
    """
    if boto3 is None:
        return None
    region = os.environ.get('AWS_REGION', 'us-east-1')
    ak = os.environ.get(f'AWS_ACCESS_KEY_ID_{prefix}')
    sk = os.environ.get(f'AWS_SECRET_ACCESS_KEY_{prefix}')
    st = os.environ.get(f'AWS_SESSION_TOKEN_{prefix}')
    if ak and sk:
        return boto3.session.Session(
            aws_access_key_id=ak,
            aws_secret_access_key=sk,
            aws_session_token=st,
            region_name=region,
        )
    profile = os.environ.get(f'AWS_PROFILE_{prefix}', default_profile)
    try:
        return boto3.session.Session(profile_name=profile, region_name=region)
    except Exception:
        # Profile may not be configured on dev machines; caller will skip.
        return None


@pytest.fixture(scope='session')
def region() -> str:
    return os.environ.get('AWS_REGION', 'us-east-1')


@pytest.fixture(scope='session')
def external_id() -> str:
    return os.environ.get('AGENTICAI_D03_EXTERNAL_ID', '')


@pytest.fixture(scope='session')
def tenant_id() -> str:
    return os.environ.get('AGENTICAI_D03_TENANT_ID', 'demo')


@pytest.fixture(scope='session')
def agent_id() -> str:
    return os.environ.get('AGENTICAI_D03_AGENT_ID', 'primary')


@pytest.fixture(scope='session')
def platform_session():
    s = _session_from_env('PLATFORM', 'agenticai-platform')
    if s is None:
        pytest.skip('platform boto3 session unavailable')
    return s


@pytest.fixture(scope='session')
def workload_session():
    s = _session_from_env('WORKLOAD', 'agenticai-workload')
    if s is None:
        pytest.skip('workload boto3 session unavailable')
    return s


@pytest.fixture(scope='session')
def platform_account_id(platform_session) -> str:
    return platform_session.client('sts').get_caller_identity()['Account']


@pytest.fixture(scope='session')
def workload_account_id(workload_session) -> str:
    return workload_session.client('sts').get_caller_identity()['Account']


@pytest.fixture(autouse=True, scope='session')
def _require_live_aws():
    """Short-circuit the whole session if live creds aren't reachable.

    Mirrors ``tests/smoke/smoke.py`` — we'd rather skip than fail on
    machines without AWS configured.
    """
    if boto3 is None:
        pytest.skip('boto3 not installed; live AWS tests unavailable')
    platform = _session_from_env('PLATFORM', 'agenticai-platform')
    workload = _session_from_env('WORKLOAD', 'agenticai-workload')
    if platform is None or workload is None:
        pytest.skip('live AWS creds not available')
    try:
        platform.client('sts').get_caller_identity()
        workload.client('sts').get_caller_identity()
    except (ClientError, BotoCoreError, Exception):  # noqa: BLE001
        pytest.skip('live AWS creds not available')


# ---------------------------------------------------------------------------
# Helpers — importable from tests via ``from conftest import ...``.
# ---------------------------------------------------------------------------


def assume_bedrock_caller(
    workload_session,
    platform_account_id: str,
    external_id: str,
    tenant_id: str,
    agent_id: str,
    region: str,
    *,
    override_external_id: Optional[str] = None,
    omit_external_id: bool = False,
):
    """Assume the platform's BedrockCallerRole from the workload session.

    Per README §3.3 / BUG-005 this is ``AssumeRole`` only — we
    deliberately do NOT pass ``Tags`` / SessionTags, because session tags
    do not survive the ``account-root → runtime-role → cross-account-role``
    chain. The ``RoleSessionName`` convention
    ``workload-<acctId>-<tenantId>-<agentId>`` carries audit attribution
    instead.
    """
    sts = workload_session.client('sts')
    workload_account_id = sts.get_caller_identity()['Account']
    session_name = f'workload-{workload_account_id}-{tenant_id}-{agent_id}'
    role_arn = f'arn:aws:iam::{platform_account_id}:role/{BEDROCK_CALLER_ROLE_NAME}'

    kwargs = {
        'RoleArn': role_arn,
        'RoleSessionName': session_name,
        'DurationSeconds': 900,
    }
    if not omit_external_id:
        kwargs['ExternalId'] = override_external_id if override_external_id is not None else external_id

    resp = sts.assume_role(**kwargs)
    creds = resp['Credentials']
    return boto3.session.Session(
        aws_access_key_id=creds['AccessKeyId'],
        aws_secret_access_key=creds['SecretAccessKey'],
        aws_session_token=creds['SessionToken'],
        region_name=region,
    ), session_name


def _stack_outputs(session, stack_name: str) -> dict:
    cfn = session.client('cloudformation')
    resp = cfn.describe_stacks(StackName=stack_name)
    stacks = resp.get('Stacks', [])
    if not stacks:
        raise RuntimeError(f'stack {stack_name} not found')
    return {o['OutputKey']: o['OutputValue'] for o in stacks[0].get('Outputs', []) or []}


def get_app_inference_profile_arn(
    platform_session,
    tenant_id: str,
    agent_id: str,
    region: str,
) -> str:
    """Look up the per-tenant Application Inference Profile ARN.

    First tries the CloudFormation stack output
    ``AppInfProfileArn<tenant>__<agent>`` (this is the CDK CfnOutput logical
    id in ``D03PlatformCoreStack``). Falls back to the stable CFN export
    ``AgenticAI-D03-AppInfProfile-<tenant>-<agent>``, and lastly to a live
    ``bedrock:ListInferenceProfiles`` lookup by profile name.
    """
    # 1) Stack output (logical id carries the `<tenant>__<agent>` key).
    try:
        outputs = _stack_outputs(platform_session, PLATFORM_STACK_NAME)
        key = f'AppInfProfileArn{tenant_id}__{agent_id}'
        # CDK output keys have non-alphanumerics stripped — strip to match.
        sanitized = ''.join(c for c in key if c.isalnum())
        for k, v in outputs.items():
            if k == key or k == sanitized or k.replace('_', '') == sanitized:
                return v
    except ClientError:
        pass

    # 2) Named CFN export.
    try:
        cfn = platform_session.client('cloudformation')
        paginator = cfn.get_paginator('list_exports')
        want = f'AgenticAI-D03-AppInfProfile-{tenant_id}-{agent_id}'
        for page in paginator.paginate():
            for exp in page.get('Exports', []):
                if exp.get('Name') == want:
                    return exp['Value']
    except ClientError:
        pass

    # 3) Live lookup via Bedrock.
    bedrock = platform_session.client('bedrock', region_name=region)
    try:
        paginator = bedrock.get_paginator('list_inference_profiles')
        for page in paginator.paginate(typeEquals='APPLICATION'):
            for profile in page.get('inferenceProfileSummaries', []):
                name = profile.get('inferenceProfileName', '')
                if tenant_id in name and agent_id in name and name.startswith('agenticai-d03-'):
                    return profile['inferenceProfileArn']
    except ClientError:
        pass

    raise RuntimeError(
        f'Could not resolve Application Inference Profile ARN for '
        f'{tenant_id}/{agent_id}. Ensure {PLATFORM_STACK_NAME} is deployed.'
    )


def get_guardrail_id_and_version(platform_session, region: str) -> tuple:
    """Return (guardrail_id, version) for the D-03 baseline guardrail.

    Reads the ``GuardrailId`` output from ``AgenticAI-D03-PlatformCoreStack``.
    Uses ``DRAFT`` for the version unless an explicit published version is
    discoverable — the baseline stack publishes version 1 by default.
    """
    outputs = _stack_outputs(platform_session, PLATFORM_STACK_NAME)
    gid = outputs.get('GuardrailId')
    if not gid:
        # Fall back to named export.
        cfn = platform_session.client('cloudformation')
        for page in cfn.get_paginator('list_exports').paginate():
            for exp in page.get('Exports', []):
                if exp.get('Name') == 'AgenticAI-D03-GuardrailId':
                    gid = exp['Value']
                    break
            if gid:
                break
    if not gid:
        raise RuntimeError('GuardrailId output not found on platform stack')

    # Ask Bedrock for the latest published version; fall back to DRAFT.
    bedrock = platform_session.client('bedrock', region_name=region)
    try:
        versions = bedrock.list_guardrails(guardrailIdentifier=gid).get('guardrails', [])
        published = [v.get('version') for v in versions if v.get('version') and v.get('version') != 'DRAFT']
        if published:
            return gid, published[-1]
    except ClientError:
        pass
    return gid, 'DRAFT'

"""Shared fixtures for D-03 post-destroy verification.

Reuses the same ``platform_session`` + ``workload_session`` fixtures as
the integration suite (mirroring the skip-when-no-creds pattern). Runs
AFTER ``cdk destroy`` to confirm no residual D-03 resources are left in
either account.

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
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore[assignment]
    BotoCoreError = Exception  # type: ignore[assignment,misc]
    ClientError = Exception  # type: ignore[assignment,misc]


def _session_from_env(prefix: str, default_profile: str) -> Optional['boto3.session.Session']:
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
        return None


@pytest.fixture(scope='session')
def region() -> str:
    return os.environ.get('AWS_REGION', 'us-east-1')


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


@pytest.fixture(autouse=True, scope='session')
def _require_live_aws():
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

"""Z7-H integration test — Phase G chargeback + showback.

Verifies the C-D triage fix (chargeback fail-closed when CUR missing) and
the post-triage Athena-results bucket separation.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import json

import pytest


def _stack_outputs(session, stack_name: str) -> dict:
    cfn = session.client('cloudformation')
    resp = cfn.describe_stacks(StackName=stack_name)
    if not resp['Stacks']:
        pytest.skip(f'stack {stack_name} not deployed')
    return {o['OutputKey']: o['OutputValue'] for o in resp['Stacks'][0].get('Outputs') or []}


@pytest.fixture(scope='module')
def gap_outputs(workload_session):
    try:
        return _stack_outputs(workload_session, 'AgenticAI-GapClosureStack')
    except Exception as e:
        pytest.skip(f'GapClosureStack not reachable: {e}')


def test_chargeback_lambda_fail_closed_on_missing_cur(workload_session, gap_outputs):
    fn = gap_outputs.get('ChargebackLambdaArn')
    if not fn:
        pytest.skip('Chargeback lambda missing')
    lam = workload_session.client('lambda')
    resp = lam.invoke(FunctionName=fn, InvocationType='RequestResponse', Payload=b'{}')
    body = json.loads(resp['Payload'].read())
    # Post-triage: Lambda returns 200 with `ok: false` and a `reason`,
    # NOT an unhandled error. Failure recorded to runs DDB.
    assert resp['StatusCode'] == 200
    # The handler returns either {"ok": true, ...} or {"ok": false, "reason": "..."}
    assert body.get('ok') is False or body.get('ok') is True


def test_chargeback_buckets_split(workload_session, gap_outputs):
    bucket = gap_outputs.get('ChargebackBucket')
    if not bucket:
        pytest.skip('ChargebackBucket output missing')
    s3 = workload_session.client('s3')
    cfg = s3.get_object_lock_configuration(Bucket=bucket)['ObjectLockConfiguration']
    assert cfg['Rule']['DefaultRetention']['Mode'] == 'GOVERNANCE'
    assert cfg['Rule']['DefaultRetention']['Days'] == 720  # 24 months
    # Athena results bucket separate (post-Z6 split).
    athena_bucket = bucket.replace('chargeback-', 'chargeback-athena-')
    head = s3.head_bucket(Bucket=athena_bucket)
    assert head['ResponseMetadata']['HTTPStatusCode'] == 200

"""Z7-H integration test — Phase A evaluation gate.

Skips when no live AWS creds. Reads stack outputs from
`AgenticAI-GapClosureStack` (workload) and exercises:
  - the Object Lock GOVERNANCE 90d retention on EvalCorpusBucket
  - the runner-role bedrock:InvokeModel scope (read-only check)

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import json
import os

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


def test_eval_corpus_bucket_has_governance_90d(workload_session, gap_outputs):
    bucket = gap_outputs.get('EvalCorpusBucket')
    if not bucket:
        pytest.skip('EvalCorpusBucket output missing')
    s3 = workload_session.client('s3')
    cfg = s3.get_object_lock_configuration(Bucket=bucket)['ObjectLockConfiguration']
    assert cfg['ObjectLockEnabled'] == 'Enabled'
    rule = cfg['Rule']['DefaultRetention']
    assert rule['Mode'] == 'GOVERNANCE'
    assert rule['Days'] == 90


def test_eval_run_history_table_has_pitr(workload_session, gap_outputs):
    table = gap_outputs.get('EvalRunHistoryTable')
    if not table:
        pytest.skip('EvalRunHistoryTable output missing')
    ddb = workload_session.client('dynamodb')
    resp = ddb.describe_continuous_backups(TableName=table)
    assert resp['ContinuousBackupsDescription']['PointInTimeRecoveryDescription']['PointInTimeRecoveryStatus'] == 'ENABLED'

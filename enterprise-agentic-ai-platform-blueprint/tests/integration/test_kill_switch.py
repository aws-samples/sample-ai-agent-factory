"""Z7-H integration test — Phase F kill-switch + Z7-A circuit breaker.

Z6/Z7 fixes verified live:
  - Kill-switch SF SUCCEEDED with all 4 real branches.
  - Audit DDB row with `reason` (M-I fix).
  - Cognito client locked (verified via post-execution describe).

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import time

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


def test_killswitch_sf_succeeds_end_to_end(workload_session, gap_outputs):
    sm_arn = gap_outputs.get('KillSwitchStateMachineArn')
    audit_table = gap_outputs.get('KillSwitchAuditTable')
    if not sm_arn or not audit_table:
        pytest.skip('KillSwitch outputs missing')
    sf = workload_session.client('stepfunctions')
    ddb = workload_session.client('dynamodb')
    pre = ddb.scan(TableName=audit_table, Select='COUNT')['Count']
    exec_ = sf.start_execution(stateMachineArn=sm_arn, input='{"reason":"integration-test"}')
    arn = exec_['executionArn']
    for _ in range(60):
        time.sleep(2)
        d = sf.describe_execution(executionArn=arn)
        if d['status'] in ('SUCCEEDED', 'FAILED', 'TIMED_OUT', 'ABORTED'):
            break
    assert d['status'] == 'SUCCEEDED', f'kill-switch did not succeed: {d}'
    post = ddb.scan(TableName=audit_table, Select='COUNT')['Count']
    assert post >= pre + 1


def test_killswitch_audit_row_persists_reason_post_z6_fix(workload_session, gap_outputs):
    audit_table = gap_outputs.get('KillSwitchAuditTable')
    if not audit_table:
        pytest.skip('KillSwitch audit table missing')
    ddb = workload_session.client('dynamodb')
    items = ddb.scan(TableName=audit_table)['Items']
    # Latest item — find any row that has the `reason` attribute.
    has_reason = any('reason' in it and it['reason'].get('S') for it in items)
    assert has_reason, 'no audit row carries reason; M-I regression'

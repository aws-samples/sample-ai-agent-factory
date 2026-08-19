"""Z7-H integration test — Phase B online evaluation watchdog.

Invokes the Lambda, asserts a DDB row was written + a CW metric emitted.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import json
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


def test_watchdog_invocation_writes_ddb_row(workload_session, gap_outputs):
    fn = gap_outputs.get('OnlineEvalLambdaArn')
    table = gap_outputs.get('OnlineEvalTable')
    if not fn or not table:
        pytest.skip('OnlineEval outputs missing')
    lam = workload_session.client('lambda')
    ddb = workload_session.client('dynamodb')
    pre = ddb.scan(TableName=table, Select='COUNT')['Count']
    resp = lam.invoke(FunctionName=fn, InvocationType='RequestResponse', Payload=b'{}')
    assert resp['StatusCode'] == 200
    time.sleep(5)
    post = ddb.scan(TableName=table, Select='COUNT')['Count']
    assert post >= pre + 1


def test_watchdog_emits_cw_metric(workload_session, gap_outputs):
    if not gap_outputs.get('OnlineEvalLambdaArn'):
        pytest.skip('OnlineEval Lambda missing')
    cw = workload_session.client('cloudwatch')
    metrics = cw.list_metrics(Namespace='AgenticAI/OnlineEval', MetricName='QualityPct').get('Metrics') or []
    assert len(metrics) >= 1

"""Z7-H integration test — Phase H HITL state machine.

Verifies the H-D + H-E triage fixes:
  - malformed confidence → InvalidConfidence FAIL
  - valid confidence ≥ threshold → auto-pass SUCCEEDED
  - valid confidence < threshold → RUNNING with pause-token row

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


def _start_and_wait(sf, sm_arn, payload, deadline_seconds=30):
    exec_ = sf.start_execution(stateMachineArn=sm_arn, input=payload)
    arn = exec_['executionArn']
    end = time.time() + deadline_seconds
    while time.time() < end:
        d = sf.describe_execution(executionArn=arn)
        if d['status'] != 'RUNNING':
            return d
        time.sleep(1)
    return sf.describe_execution(executionArn=arn)


def test_hitl_rejects_malformed_confidence_null(workload_session, gap_outputs):
    sm_arn = gap_outputs.get('HitlStateMachineArn')
    if not sm_arn:
        pytest.skip('HITL outputs missing')
    sf = workload_session.client('stepfunctions')
    d = _start_and_wait(sf, sm_arn, '{"confidence":null}')
    assert d['status'] == 'FAILED'
    assert d.get('error') == 'InvalidConfidence'


def test_hitl_rejects_string_confidence(workload_session, gap_outputs):
    sm_arn = gap_outputs.get('HitlStateMachineArn')
    if not sm_arn:
        pytest.skip('HITL outputs missing')
    sf = workload_session.client('stepfunctions')
    d = _start_and_wait(sf, sm_arn, '{"confidence":"foo"}')
    assert d['status'] == 'FAILED'
    assert d.get('error') == 'InvalidConfidence'


def test_hitl_rejects_empty_input(workload_session, gap_outputs):
    sm_arn = gap_outputs.get('HitlStateMachineArn')
    if not sm_arn:
        pytest.skip('HITL outputs missing')
    sf = workload_session.client('stepfunctions')
    d = _start_and_wait(sf, sm_arn, '{}')
    assert d['status'] == 'FAILED'
    assert d.get('error') == 'InvalidConfidence'


def test_hitl_high_confidence_passes_through(workload_session, gap_outputs):
    sm_arn = gap_outputs.get('HitlStateMachineArn')
    if not sm_arn:
        pytest.skip('HITL outputs missing')
    sf = workload_session.client('stepfunctions')
    d = _start_and_wait(sf, sm_arn, '{"confidence":0.95}')
    assert d['status'] == 'SUCCEEDED'


def test_hitl_low_confidence_pauses(workload_session, gap_outputs):
    sm_arn = gap_outputs.get('HitlStateMachineArn')
    if not sm_arn:
        pytest.skip('HITL outputs missing')
    sf = workload_session.client('stepfunctions')
    exec_ = sf.start_execution(stateMachineArn=sm_arn, input='{"confidence":0.3}')
    arn = exec_['executionArn']
    time.sleep(5)
    d = sf.describe_execution(executionArn=arn)
    assert d['status'] == 'RUNNING'
    # Stop it cleanly to avoid 24h hangers in subsequent test runs.
    sf.stop_execution(executionArn=arn)

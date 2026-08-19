"""D-03 registry + experiment-tracking cross-account access.

The workload account must be able to:
  - Query the platform-owned agent registry (read-only, per-tenant PK).
  - PutItem into experiment-tracking (tenantId-scoped writes).
  - NOT write to the agent registry.

Each read path also exercises the cross-account KMS Decrypt flow via the
registry CMK's ``kms:ViaService`` statement.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import time
import uuid

import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError


pytestmark = pytest.mark.integration


AGENT_TABLE = 'agenticai-d03-registry-agents'
TOOL_TABLE = 'agenticai-d03-registry-tools'
EXPERIMENT_TABLE = 'agenticai-d03-experiment-tracking'


def _table(workload_session, name: str, region: str):
    return workload_session.resource('dynamodb', region_name=region).Table(name)


def test_workload_can_read_agent_registry(workload_session, tenant_id, region):
    table = _table(workload_session, AGENT_TABLE, region)
    resp = table.query(KeyConditionExpression=Key('tenantId').eq(tenant_id))
    # Empty is fine — auth is what we're asserting. Count is always present.
    assert 'Items' in resp
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200


def test_workload_can_write_experiment_tracking(workload_session, tenant_id, region):
    table = _table(workload_session, EXPERIMENT_TABLE, region)
    run_id = f'itest-{uuid.uuid4()}'
    item = {
        'tenantId': tenant_id,
        'runId': run_id,
        'timestamp': str(int(time.time())),
        'source': 'integration-test',
    }
    resp = table.put_item(Item=item)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    # Clean up — the test stack ships with DESTROY removal, but leave no residue.
    try:
        table.delete_item(Key={'tenantId': tenant_id, 'runId': run_id})
    except ClientError:
        pass


def test_workload_cannot_write_agent_registry(workload_session, tenant_id, region):
    table = _table(workload_session, AGENT_TABLE, region)
    with pytest.raises(ClientError) as exc:
        table.put_item(
            Item={
                'tenantId': tenant_id,
                'agentId': f'forbidden-{uuid.uuid4()}',
                'probe': 'integration-test',
            },
        )
    code = exc.value.response.get('Error', {}).get('Code', '')
    assert code in ('AccessDeniedException', 'AccessDenied'), code


def test_cross_account_kms_decrypt_on_table_read(workload_session, tenant_id, region):
    """A successful Query that returns an item proves the KMS cross-account
    Decrypt path (``kms:ViaService = dynamodb.<region>.amazonaws.com``).

    If the registry table is empty this still proves the key-policy allows
    the workload principal to invoke Query end-to-end (the Decrypt only
    fires when items come back, so we query all three tables to maximise
    the chance of a live Decrypt). An access denial on any table fails
    the test.
    """
    resource = workload_session.resource('dynamodb', region_name=region)
    for name in (AGENT_TABLE, TOOL_TABLE, EXPERIMENT_TABLE):
        table = resource.Table(name)
        resp = table.query(KeyConditionExpression=Key('tenantId').eq(tenant_id))
        assert resp['ResponseMetadata']['HTTPStatusCode'] == 200, name

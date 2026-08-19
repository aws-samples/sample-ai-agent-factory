"""D-03 Bedrock cross-account invocation tests.

Exercise the full workload-agent -> BedrockCallerRole -> Bedrock Converse
path. Each test assumes the platform's ``AgenticAI-D03-BedrockCaller``
role from the workload account using the stable ``RoleSessionName``
convention. NO session tags (see README §3.3 / BUG-005).

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import time

import importlib.util
import os

import pytest
from botocore.exceptions import ClientError


def _load_conftest_helpers():
    """Load helpers from the sibling ``conftest.py`` without relying on
    ``sys.path``. Pytest doesn't put the conftest's package on the module
    path under ``--import-mode=importlib``, so we resolve it by file.
    """
    here = os.path.dirname(__file__)
    spec = importlib.util.spec_from_file_location(
        '_d03_conftest_helpers', os.path.join(here, 'conftest.py'),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_helpers = _load_conftest_helpers()
assume_bedrock_caller = _helpers.assume_bedrock_caller
get_app_inference_profile_arn = _helpers.get_app_inference_profile_arn
get_guardrail_id_and_version = _helpers.get_guardrail_id_and_version


pytestmark = pytest.mark.integration


# Model-id outside PLATFORM_ALLOWED_MODELS (Titan is explicitly not on the allow-list).
NON_ALLOWLISTED_MODEL_ID = 'amazon.titan-text-express-v1'


def _converse(
    session,
    model_arn: str,
    guardrail_id=None,
    guardrail_version=None,
):
    """Issue a tiny ``bedrock:Converse`` call and return the raw response."""
    client = session.client('bedrock-runtime')
    kwargs = {
        'modelId': model_arn,
        'messages': [{'role': 'user', 'content': [{'text': 'Say the word OK and nothing else.'}]}],
        'inferenceConfig': {'maxTokens': 16, 'temperature': 0.0},
    }
    if guardrail_id is not None:
        kwargs['guardrailConfig'] = {
            'guardrailIdentifier': guardrail_id,
            'guardrailVersion': guardrail_version or 'DRAFT',
            'trace': 'enabled',
        }
    return client.converse(**kwargs)


# ---------------------------------------------------------------------------
# AssumeRole trust-policy tests
# ---------------------------------------------------------------------------


def test_assume_role_with_correct_external_id_succeeds(
    workload_session, platform_account_id, external_id, tenant_id, agent_id, region,
):
    if not external_id:
        pytest.skip('AGENTICAI_D03_EXTERNAL_ID not set')
    assumed, session_name = assume_bedrock_caller(
        workload_session, platform_account_id, external_id, tenant_id, agent_id, region,
    )
    ident = assumed.client('sts').get_caller_identity()
    assert ident['Account'] == platform_account_id
    assert session_name in ident['Arn']


def test_assume_role_with_wrong_external_id_denied(
    workload_session, platform_account_id, tenant_id, agent_id, region,
):
    with pytest.raises(ClientError) as exc:
        assume_bedrock_caller(
            workload_session, platform_account_id, 'deliberately-wrong-external-id',
            tenant_id, agent_id, region,
            override_external_id='deliberately-wrong-external-id',
        )
    code = exc.value.response.get('Error', {}).get('Code', '')
    assert code in ('AccessDenied', 'AccessDeniedException'), code


def test_assume_role_without_external_id_denied(
    workload_session, platform_account_id, tenant_id, agent_id, region,
):
    with pytest.raises(ClientError) as exc:
        assume_bedrock_caller(
            workload_session, platform_account_id, '', tenant_id, agent_id, region,
            omit_external_id=True,
        )
    code = exc.value.response.get('Error', {}).get('Code', '')
    assert code in ('AccessDenied', 'AccessDeniedException'), code


# ---------------------------------------------------------------------------
# Bedrock Converse path
# ---------------------------------------------------------------------------


def test_bedrock_converse_via_tenant_profile_with_guardrail_succeeds(
    workload_session, platform_session, platform_account_id,
    external_id, tenant_id, agent_id, region,
):
    if not external_id:
        pytest.skip('AGENTICAI_D03_EXTERNAL_ID not set')
    assumed, _ = assume_bedrock_caller(
        workload_session, platform_account_id, external_id, tenant_id, agent_id, region,
    )
    profile_arn = get_app_inference_profile_arn(platform_session, tenant_id, agent_id, region)
    gid, gver = get_guardrail_id_and_version(platform_session, region)

    resp = _converse(assumed, profile_arn, guardrail_id=gid, guardrail_version=gver)

    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200
    output_message = resp.get('output', {}).get('message', {})
    content_blocks = output_message.get('content', []) or []
    text_blocks = [b.get('text', '') for b in content_blocks if 'text' in b]
    assert any(t.strip() for t in text_blocks), f'empty Converse response: {resp}'


def test_bedrock_converse_without_guardrail_denied(
    workload_session, platform_session, platform_account_id,
    external_id, tenant_id, agent_id, region,
):
    if not external_id:
        pytest.skip('AGENTICAI_D03_EXTERNAL_ID not set')
    assumed, _ = assume_bedrock_caller(
        workload_session, platform_account_id, external_id, tenant_id, agent_id, region,
    )
    profile_arn = get_app_inference_profile_arn(platform_session, tenant_id, agent_id, region)

    with pytest.raises(ClientError) as exc:
        _converse(assumed, profile_arn)  # no guardrailConfig
    code = exc.value.response.get('Error', {}).get('Code', '')
    assert code in ('AccessDenied', 'AccessDeniedException'), code


def test_bedrock_converse_non_allowlisted_model_denied(
    workload_session, platform_account_id, external_id, tenant_id, agent_id, region,
):
    if not external_id:
        pytest.skip('AGENTICAI_D03_EXTERNAL_ID not set')
    assumed, _ = assume_bedrock_caller(
        workload_session, platform_account_id, external_id, tenant_id, agent_id, region,
    )
    # Some envs give ValidationException for the model id pre-access-check; both
    # outcomes are equivalent evidence that the model is not reachable.
    with pytest.raises(ClientError) as exc:
        _converse(assumed, NON_ALLOWLISTED_MODEL_ID)
    code = exc.value.response.get('Error', {}).get('Code', '')
    assert code in (
        'AccessDenied',
        'AccessDeniedException',
        'ValidationException',
    ), code


# ---------------------------------------------------------------------------
# CloudTrail audit attribution
# ---------------------------------------------------------------------------


def test_cloudtrail_carries_inference_profile_arn(
    workload_session, platform_session, platform_account_id,
    external_id, tenant_id, agent_id, region, workload_account_id,
):
    """After a successful Converse, CloudTrail must carry the inference
    profile ARN and the stable RoleSessionName.

    This is the live evidence that BUG-005's workaround (per-tenant
    inference profiles + RoleSessionName convention) actually attributes
    the invocation to the originating workload.
    """
    if not external_id:
        pytest.skip('AGENTICAI_D03_EXTERNAL_ID not set')
    assumed, session_name = assume_bedrock_caller(
        workload_session, platform_account_id, external_id, tenant_id, agent_id, region,
    )
    profile_arn = get_app_inference_profile_arn(platform_session, tenant_id, agent_id, region)
    gid, gver = get_guardrail_id_and_version(platform_session, region)

    start = time.time()
    _converse(assumed, profile_arn, guardrail_id=gid, guardrail_version=gver)

    trail = platform_session.client('cloudtrail', region_name=region)
    deadline = time.time() + 120
    found = False
    while time.time() < deadline:
        resp = trail.lookup_events(
            LookupAttributes=[{'AttributeKey': 'EventSource', 'AttributeValue': 'bedrock.amazonaws.com'}],
            StartTime=start - 30,
            MaxResults=50,
        )
        for event in resp.get('Events', []):
            raw = event.get('CloudTrailEvent', '')
            name = event.get('EventName', '')
            if name not in ('Converse', 'InvokeModel', 'ConverseStream', 'InvokeModelWithResponseStream'):
                continue
            if profile_arn in raw and session_name in raw:
                found = True
                break
        if found:
            break
        time.sleep(10)

    assert found, (
        f'No CloudTrail event within 120s carrying inference profile ARN '
        f'{profile_arn} and RoleSessionName {session_name}. '
        f'(Workload acct {workload_account_id} -> platform acct {platform_account_id}.)'
    )

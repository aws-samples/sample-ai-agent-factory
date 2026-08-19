"""Post-``cdk destroy`` residue checks for D-03.

Run this suite AFTER both ``AgenticAI-D03-PlatformCoreStack`` and
``AgenticAI-D03-WorkloadAgentStack`` have been destroyed. Zero residual
resources is the pass condition. KMS keys are warn-only (7-day pending
deletion is expected and surfaced as xfail, not fail).

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import pytest
from botocore.exceptions import ClientError


pytestmark = pytest.mark.teardown


D03_NAME_PREFIX_CFN = 'AgenticAI-D03-'
D03_NAME_PREFIX_IAM = 'AgenticAI-D03-'
D03_NAME_PREFIX_LOWER = 'agenticai-d03-'
D03_KMS_ALIAS_PREFIX = 'alias/agenticai/'


def _active_stack_names(session) -> list:
    cfn = session.client('cloudformation')
    active_statuses = [
        'CREATE_IN_PROGRESS', 'CREATE_COMPLETE',
        'ROLLBACK_IN_PROGRESS', 'ROLLBACK_FAILED', 'ROLLBACK_COMPLETE',
        'UPDATE_IN_PROGRESS', 'UPDATE_COMPLETE', 'UPDATE_COMPLETE_CLEANUP_IN_PROGRESS',
        'UPDATE_ROLLBACK_IN_PROGRESS', 'UPDATE_ROLLBACK_FAILED',
        'UPDATE_ROLLBACK_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS',
        'REVIEW_IN_PROGRESS', 'IMPORT_IN_PROGRESS', 'IMPORT_COMPLETE',
        'IMPORT_ROLLBACK_IN_PROGRESS', 'IMPORT_ROLLBACK_FAILED', 'IMPORT_ROLLBACK_COMPLETE',
        'DELETE_FAILED',
    ]
    names = []
    for page in cfn.get_paginator('list_stacks').paginate(StackStatusFilter=active_statuses):
        for s in page.get('StackSummaries', []):
            names.append(s['StackName'])
    return names


def test_no_d03_cloudformation_stacks(platform_session, workload_session):
    residual = {}
    for label, sess in (('platform', platform_session), ('workload', workload_session)):
        hits = [n for n in _active_stack_names(sess) if n.startswith(D03_NAME_PREFIX_CFN)]
        if hits:
            residual[label] = hits
    assert not residual, f'D-03 stacks still present: {residual}'


def test_no_d03_iam_roles(platform_session, workload_session):
    residual = {}
    for label, sess in (('platform', platform_session), ('workload', workload_session)):
        iam = sess.client('iam')
        hits = []
        for page in iam.get_paginator('list_roles').paginate():
            for r in page.get('Roles', []):
                if r['RoleName'].startswith(D03_NAME_PREFIX_IAM):
                    hits.append(r['RoleName'])
        if hits:
            residual[label] = hits
    assert not residual, f'D-03 IAM roles still present: {residual}'


def test_no_d03_dynamodb_tables(platform_session, workload_session):
    residual = {}
    for label, sess in (('platform', platform_session), ('workload', workload_session)):
        ddb = sess.client('dynamodb')
        hits = []
        for page in ddb.get_paginator('list_tables').paginate():
            for name in page.get('TableNames', []):
                if name.startswith(D03_NAME_PREFIX_LOWER):
                    hits.append(name)
        if hits:
            residual[label] = hits
    assert not residual, f'D-03 DynamoDB tables still present: {residual}'


def test_no_d03_ecr_repos(platform_session, workload_session):
    residual = {}
    for label, sess in (('platform', platform_session), ('workload', workload_session)):
        ecr = sess.client('ecr')
        hits = []
        for page in ecr.get_paginator('describe_repositories').paginate():
            for r in page.get('repositories', []):
                name = r.get('repositoryName', '')
                if name.startswith(D03_NAME_PREFIX_LOWER):
                    hits.append(name)
        if hits:
            residual[label] = hits
    assert not residual, f'D-03 ECR repos still present: {residual}'


def test_no_d03_kms_keys(platform_session, workload_session):
    """KMS keys are warn-only (xfail) — key deletion has a 7-day pending
    window, so ``alias/agenticai/*`` aliases will linger for up to a week
    after ``cdk destroy``. The test xfails (non-strict) if any remain,
    converting a real regression into a visible but non-blocking signal.
    """
    lingering = {}
    for label, sess in (('platform', platform_session), ('workload', workload_session)):
        kms = sess.client('kms')
        hits = []
        try:
            for page in kms.get_paginator('list_aliases').paginate():
                for a in page.get('Aliases', []):
                    if a.get('AliasName', '').startswith(D03_KMS_ALIAS_PREFIX):
                        hits.append(a['AliasName'])
        except ClientError as e:
            hits.append(f'ListAliases-error:{e}')
        if hits:
            lingering[label] = hits
    if lingering:
        pytest.xfail(f'KMS aliases linger during 7-day pending window: {lingering}')


def test_no_d03_bedrock_guardrails(platform_session, region):
    bedrock = platform_session.client('bedrock', region_name=region)
    hits = []
    try:
        paginator = bedrock.get_paginator('list_guardrails')
        for page in paginator.paginate():
            for g in page.get('guardrails', []):
                gid = g.get('id') or g.get('guardrailId')
                arn = g.get('arn') or g.get('guardrailArn')
                if not arn:
                    continue
                try:
                    tags = bedrock.list_tags_for_resource(resourceARN=arn).get('tags', [])
                    if any(t.get('key') == 'deviation' and t.get('value') == 'D-03' for t in tags):
                        hits.append(gid or arn)
                except ClientError:
                    continue
    except ClientError as e:
        pytest.skip(f'list_guardrails unavailable: {e}')
    assert not hits, f'D-03 guardrails still present: {hits}'


def test_no_d03_inference_profiles(platform_session, region):
    bedrock = platform_session.client('bedrock', region_name=region)
    hits = []
    try:
        paginator = bedrock.get_paginator('list_inference_profiles')
        for page in paginator.paginate(typeEquals='APPLICATION'):
            for p in page.get('inferenceProfileSummaries', []):
                name = p.get('inferenceProfileName', '')
                if name.startswith(D03_NAME_PREFIX_LOWER):
                    hits.append(name)
    except ClientError as e:
        pytest.skip(f'list_inference_profiles unavailable: {e}')
    assert not hits, f'D-03 application inference profiles still present: {hits}'

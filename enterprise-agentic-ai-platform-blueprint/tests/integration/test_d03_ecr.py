"""D-03 shared ECR cross-account access.

Workload accounts are pull-only on the platform-owned
``agenticai-d03-agent-base`` repository. Writes are denied; push rights
live only with the platform CI.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import base64

import pytest
from botocore.exceptions import ClientError


pytestmark = pytest.mark.integration


SHARED_REPO = 'agenticai-d03-agent-base'


def test_workload_can_get_auth_token(workload_session, region, platform_account_id):
    ecr = workload_session.client('ecr', region_name=region)
    resp = ecr.get_authorization_token(registryIds=[platform_account_id])
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200
    data = resp.get('authorizationData') or []
    assert data, resp
    token = base64.b64decode(data[0]['authorizationToken']).decode()
    assert token.startswith('AWS:'), 'ECR auth token missing AWS: prefix'


def test_workload_can_batch_get_image_on_shared_repo(
    workload_session, region, platform_account_id,
):
    ecr = workload_session.client('ecr', region_name=region)
    # BatchGetImage against a non-existent tag should succeed at the API
    # layer (returning ``failures[]``) — the IAM check is what we're after,
    # not that the image exists.
    resp = ecr.batch_get_image(
        registryId=platform_account_id,
        repositoryName=SHARED_REPO,
        imageIds=[{'imageTag': 'probe-not-a-real-tag'}],
    )
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200
    assert 'images' in resp and 'failures' in resp


def test_workload_cannot_put_image(workload_session, region, platform_account_id):
    ecr = workload_session.client('ecr', region_name=region)
    with pytest.raises(ClientError) as exc:
        ecr.put_image(
            registryId=platform_account_id,
            repositoryName=SHARED_REPO,
            imageManifest='{"schemaVersion":2,"mediaType":"application/vnd.docker.distribution.manifest.v2+json","config":{"mediaType":"application/vnd.docker.container.image.v1+json","size":0,"digest":"sha256:0000000000000000000000000000000000000000000000000000000000000000"},"layers":[]}',
            imageTag='probe-forbidden',
        )
    code = exc.value.response.get('Error', {}).get('Code', '')
    # ECR returns AccessDeniedException for cross-account writes that IAM
    # denies; some regions surface it as ``RepositoryPolicyNotFoundException``
    # when the resource policy simply doesn't grant the action. Either proves
    # the write path is closed.
    assert code in (
        'AccessDeniedException',
        'AccessDenied',
        'RepositoryPolicyNotFoundException',
    ), code

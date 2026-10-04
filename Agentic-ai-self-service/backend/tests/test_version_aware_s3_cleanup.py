"""F-60: every S3 cleanup path removes each version, not just the current one.

``delete_object`` with no VersionId on a versioned bucket is a successful call that
writes a delete marker, so the object disappears from a plain listing while the agent
source stays readable by VersionId. The target bucket is customer-provisioned
(``deploy_target.validate_artifact_bucket`` never reads its versioning), so all three
deleters are exercised against a fake that models versioning: the teardown dispatcher,
the failure-cleanup dispatcher, and the gateway's spec rollback.
"""

from __future__ import annotations

from unittest.mock import patch

import app.deployment_handler as dh
import pytest
from app.services import gateway_deployer
from app.services.resource_ownership import ResourceDeletionRefused, delete_owned_s3_object, owner_tags
from app.step_handlers.status_update_step import _cleanup_resource, _ResourceRetained

from tests.fake_versioned_s3 import FakeVersionedS3

BUCKET, KEY, REGION, DEP = (
    "agentcore-flows-artifacts-123456789012-us-west-2",
    "deployments/d-1/v/1/code.zip",
    "us-west-2",
    "d-1",
)
ROW = {"type": "s3_object", "id": f"s3://{BUCKET}/{KEY}", "region": REGION}


def ours(deployment_id: str = DEP, region: str = REGION) -> dict[str, str]:
    return {**owner_tags(region), "DeploymentId": deployment_id}


class _Session:
    def __init__(self, s3):
        self._s3 = s3

    def client(self, service, **_kwargs):
        assert service == "s3", service
        return self._s3


def _teardown(s3):
    return dh._delete_managed_resource(ROW, "us-east-1", deployment_id=DEP, target_session=_Session(s3))


def _failure_cleanup(s3):
    with patch("app.services.step_clients.client", return_value=s3):
        _cleanup_resource(ROW, "us-east-1", {"deployment_id": DEP})


def _spec_rollback(s3):
    token = gateway_deployer._GATEWAY_AWS_SESSION.set(_Session(s3))
    try:
        gateway_deployer._delete_spec_s3_object(f"s3://{BUCKET}/{KEY}", REGION, DEP)
    finally:
        gateway_deployer._GATEWAY_AWS_SESSION.reset(token)


PATHS = [_teardown, _failure_cleanup, _spec_rollback]


@pytest.mark.parametrize("versioning", ["Enabled", "Suspended", "Never"])
@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.__name__)
def test_no_version_and_no_marker_survive(path, versioning):
    s3 = FakeVersionedS3(versioning)
    s3.put(BUCKET, KEY, ours())
    if versioning == "Enabled":
        s3.put(BUCKET, KEY, ours())  # a retried upload: two versions of the same key

    path(s3)

    assert s3.data_versions(BUCKET, KEY) == []
    assert s3.markers(BUCKET, KEY) == []


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.__name__)
def test_every_delete_names_a_version(path):
    """The observable difference from the defect: no bare delete_object at all."""
    s3 = FakeVersionedS3("Enabled")
    s3.put(BUCKET, KEY, ours())

    path(s3)

    deletes = [kw for op, kw in s3.calls if op == "DeleteObject"]
    assert deletes and all(kw["VersionId"] for kw in deletes)


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.__name__)
def test_a_legacy_marker_over_an_owned_version_is_cleaned_on_retry(path):
    """What the pre-fix teardown left behind: a marker hiding a readable, tagged version."""
    s3 = FakeVersionedS3("Enabled")
    s3.put(BUCKET, KEY, ours())
    s3.delete_object(Bucket=BUCKET, Key=KEY)

    path(s3)

    assert s3.data_versions(BUCKET, KEY) == []
    assert s3.markers(BUCKET, KEY) == []


def test_teardown_reports_absent_only_when_there_was_nothing():
    assert _teardown(FakeVersionedS3("Enabled")).endswith("already absent")
    s3 = FakeVersionedS3("Enabled")
    s3.put(BUCKET, KEY, ours())
    assert _teardown(s3).endswith("deleted (1 version(s))")


def _state(s3):
    return {k: [dict(e) for e in v] for k, v in s3.objects.items()}


def _flaky_tag_read(s3, failing_version):
    real = s3.get_object_tagging

    def read(**kwargs):
        if kwargs.get("VersionId") == failing_version:
            s3.calls.append(("GetObjectTagging", dict(kwargs)))
            from tests.fake_versioned_s3 import client_error

            raise client_error("AccessDenied", "GetObjectTagging")
        return real(**kwargs)

    s3.get_object_tagging = read


def _foreign_older(s3):
    s3.put(BUCKET, KEY, ours(deployment_id="someone-else"))
    s3.put(BUCKET, KEY, ours())


def _foreign_newer(s3):
    s3.put(BUCKET, KEY, ours())
    s3.put(BUCKET, KEY, ours(deployment_id="someone-else"))


def _one_unreadable_among_ours(s3):
    s3.put(BUCKET, KEY, ours())
    middle = s3.put(BUCKET, KEY, ours())
    s3.put(BUCKET, KEY, ours())
    _flaky_tag_read(s3, middle)


@pytest.mark.parametrize(
    "setup", [_foreign_older, _foreign_newer, _one_unreadable_among_ours], ids=lambda f: f.__name__
)
@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.__name__)
def test_one_unprovable_version_means_zero_deletes(path, setup):
    """Deleting our newer version would promote a foreign one to current: all or nothing."""
    s3 = FakeVersionedS3("Enabled")
    setup(s3)
    before = _state(s3)

    with pytest.raises((ResourceDeletionRefused, _ResourceRetained)):
        path(s3)
    assert [kw for op, kw in s3.calls if op == "DeleteObject"] == []
    assert _state(s3) == before


def test_the_failure_path_records_a_foreign_version_as_retained():
    s3 = FakeVersionedS3("Enabled")
    s3.put(BUCKET, KEY, ours(region="eu-central-1"))  # another stack's owner tag

    with pytest.raises(_ResourceRetained):
        _failure_cleanup(s3)
    assert len(s3.data_versions(BUCKET, KEY)) == 1


def test_markers_over_a_foreign_version_are_not_removed():
    """Removing the marker would resurface an object this deployment does not own."""
    s3 = FakeVersionedS3("Enabled")
    s3.put(BUCKET, KEY, ours(deployment_id="someone-else"))
    s3.delete_object(Bucket=BUCKET, Key=KEY)

    with pytest.raises(ResourceDeletionRefused):
        _teardown(s3)
    assert len(s3.markers(BUCKET, KEY)) == 1


def test_a_sibling_key_sharing_the_prefix_is_untouched():
    s3 = FakeVersionedS3("Enabled")
    s3.put(BUCKET, KEY, ours())
    s3.put(BUCKET, KEY + ".sha256", ours())

    _teardown(s3)

    assert len(s3.data_versions(BUCKET, KEY + ".sha256")) == 1


@pytest.mark.parametrize("denied", ["ListObjectVersions", "GetObjectTagging"])
def test_an_unreadable_proof_deletes_nothing(denied):
    s3 = FakeVersionedS3("Enabled")
    s3.put(BUCKET, KEY, ours())
    s3.deny.add(denied)

    with pytest.raises(ResourceDeletionRefused):
        _teardown(s3)
    assert not [kw for op, kw in s3.calls if op == "DeleteObject"]
    assert len(s3.data_versions(BUCKET, KEY)) == 1


def test_a_denied_version_delete_is_not_reported_as_success():
    s3 = FakeVersionedS3("Enabled")
    s3.put(BUCKET, KEY, ours())
    s3.deny.add("DeleteObject")

    with pytest.raises(Exception, match="AccessDenied"):
        _teardown(s3)
    assert len(s3.data_versions(BUCKET, KEY)) == 1


def test_the_bucket_owner_binds_every_call():
    s3 = FakeVersionedS3("Enabled")
    s3.put(BUCKET, KEY, ours())

    delete_owned_s3_object(s3, BUCKET, KEY, region=REGION, deployment_id=DEP, expected_bucket_owner="210987654321")

    assert s3.calls and all(kw.get("ExpectedBucketOwner") == "210987654321" for _, kw in s3.calls)

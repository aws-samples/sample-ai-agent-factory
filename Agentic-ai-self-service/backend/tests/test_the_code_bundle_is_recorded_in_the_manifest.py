"""An uploaded code bundle must appear in the deployment manifest.

Teardown -- both the failure path (``status_update_step._cleanup_resource``) and the
delete path (``deployment_handler._delete_managed_resource``) -- can only delete what
``created_resources`` records. Nothing in the codebase recorded a code bundle, so every
deploy that uploaded its 18-43MB ``code.zip`` and then failed in a later state left the
object in S3 permanently. Ten such orphans (~420MB) were sitting in the live artifacts
bucket.

Two reasons the obvious alternative -- reconstruct the key at delete time from the
runtime name -- is wrong, and both are why these tests assert on the key the upload
ACTUALLY used rather than on a key the test recomputes:

  * ``agentcore_runtime_name`` is not the key's name component. It carries a ``_<suffix>``
    and a ``[:39]`` truncation that ``friendly_runtime_name`` does not, and the versioned
    branch adds ``/v/<version_id>/``.
  * ``s3:DeleteObject`` on a key that does not exist returns SUCCESS. A reconstructed key
    that is subtly wrong produces a teardown that reports "cleaned" and deletes nothing --
    exactly the failure mode this whole area already had once.

The recorded row is ``{"type": "s3_object", "id": "s3://<bucket>/<key>"}``, the shape
``gateway_step`` already uses for staged connector specs, so both dispatchers' existing
``s3_object`` arms handle it with no new deleter.
"""

from unittest.mock import MagicMock, patch

import pytest

REGION = "us-east-1"
BUCKET = "acf-test-artifacts"


def _config(name: str = "My Agent") -> dict:
    return {
        "name": name,
        "model": {"modelId": "us.anthropic.claude-sonnet-5"},
        "systemPrompt": "Be helpful.",
    }


def _s3_object_rows(store: MagicMock) -> list[dict]:
    return [
        call.args[1]
        for call in store.record_resource.call_args_list
        if len(call.args) >= 2 and isinstance(call.args[1], dict) and call.args[1].get("type") == "s3_object"
    ]


def _run_codegen(event: dict) -> tuple[MagicMock, list[tuple[str, str]]]:
    """Invoke the real handler; return the store and exact ``(bucket, key)`` uploads."""
    from app.step_handlers import codegen_step

    store = MagicMock()
    uploads: list[tuple[str, str]] = []

    def _capture_upload(s3, bucket, key, code, reqs, entrypoint, **kwargs):
        uploads.append((bucket, key))

    with (
        patch.object(codegen_step, "_get_deployment_store", return_value=store),
        patch.object(codegen_step, "_get_env", side_effect=lambda n, d="": {"ARTIFACTS_BUCKET_NAME": BUCKET}.get(n, d)),
        patch.object(codegen_step.step_clients, "client", return_value=MagicMock()),
        patch("boto3.client", return_value=MagicMock()),
        patch.object(codegen_step, "_download_bundle", return_value=b"deps"),
        patch.object(codegen_step, "_provider_bundles", return_value=[]),
        patch("app.services.runtime_deployer.upload_code_to_s3", side_effect=_capture_upload),
    ):
        codegen_step.handler(event, None)

    assert len(uploads) == 1, f"expected exactly one upload, saw {uploads}"
    return store, uploads


def test_the_uploaded_bundle_is_recorded_with_the_key_that_was_actually_used():
    """The recorded id must be byte-identical to the uploaded key.

    Asserting against the *captured* key rather than a key this test rebuilds is the
    whole point: a recorded key that merely looks plausible is a delete that succeeds
    and removes nothing.
    """
    store, uploads = _run_codegen(
        {
            "deployment_id": "d-codegen-1",
            "config": _config(),
            "version_id": "v7",
        }
    )
    rows = _s3_object_rows(store)
    assert len(rows) == 1, f"expected exactly one s3_object row, got {rows}"
    assert uploads[0][0] == BUCKET
    assert rows[0]["id"] == f"s3://{uploads[0][0]}/{uploads[0][1]}"
    assert rows[0]["region"] == REGION


def test_the_versioned_and_unversioned_key_branches_are_both_recorded():
    """Two key shapes reach the upload; a row for only one of them is a leak for the other."""
    versioned_store, versioned_uploads = _run_codegen(
        {"deployment_id": "d-v", "config": _config(), "version_id": "v42"}
    )
    plain_store, plain_uploads = _run_codegen({"deployment_id": "d-p", "config": _config()})
    versioned_bucket, versioned_key = versioned_uploads[0]
    plain_bucket, plain_key = plain_uploads[0]

    assert "/v/v42/" in versioned_key, "the versioned branch did not run — this test proves nothing"
    assert "/v/" not in plain_key, "the unversioned branch did not run — this test proves nothing"

    assert _s3_object_rows(versioned_store)[0]["id"] == f"s3://{versioned_bucket}/{versioned_key}"
    assert _s3_object_rows(plain_store)[0]["id"] == f"s3://{plain_bucket}/{plain_key}"


def test_a_same_account_non_home_bundle_uses_and_records_the_validated_regional_bucket():
    """The upload and manifest must agree on the registered regional bucket.

    Checking only the manifest URI is insufficient: a regression could upload to the
    home bucket and record the regional bucket, making teardown return HTTP 200 while
    deleting an object that never existed and leaving the real bundle behind.
    """
    regional_bucket = "platform-artifacts-eu-west-1"
    store, uploads = _run_codegen(
        {
            "deployment_id": "d-region",
            "config": _config(),
            "target_region": "eu-west-1",
            "target_artifact_bucket": regional_bucket,
        }
    )

    assert uploads[0][0] == regional_bucket
    rows = _s3_object_rows(store)
    assert len(rows) == 1
    assert rows[0]["id"] == f"s3://{uploads[0][0]}/{uploads[0][1]}"
    assert rows[0]["region"] == "eu-west-1"
    assert "account" not in rows[0]


def test_a_cross_account_bundle_records_the_target_bucket_not_the_platform_bucket():
    """Cross-account uploads land in the customer's own bucket.

    The platform bucket has a 90-day expiration on ``deployments/``; a target-account
    bucket is customer-provisioned and has no lifecycle rule this platform controls, so
    it is the case where the manifest row is the ONLY thing that ever removes the object.
    """
    target_bucket = f"agentcore-flows-artifacts-210987654321-{REGION}"
    store, uploads = _run_codegen(
        {
            "deployment_id": "d-xacct",
            "config": _config(),
            "target_account_id": "210987654321",
            "target_region": REGION,
            "target_artifact_bucket": target_bucket,
        }
    )
    assert uploads[0][0] == target_bucket
    rows = _s3_object_rows(store)
    assert len(rows) == 1
    assert rows[0]["id"] == f"s3://{uploads[0][0]}/{uploads[0][1]}"
    assert rows[0]["account"] == "210987654321"
    assert BUCKET not in rows[0]["id"]


def test_cross_account_codegen_refuses_to_reconstruct_an_unvalidated_bucket():
    """A direct/legacy caller cannot bypass target registration by supplying only
    an account id and relying on codegen to guess a globally scoped bucket name."""
    from app.step_handlers import codegen_step

    with (
        patch.object(codegen_step, "_get_deployment_store", return_value=MagicMock()),
        patch.object(
            codegen_step,
            "_get_env",
            side_effect=lambda n, d="": {"ARTIFACTS_BUCKET_NAME": BUCKET}.get(n, d),
        ),
    ):
        with pytest.raises(ValueError, match="validated target_artifact_bucket"):
            codegen_step.handler(
                {
                    "deployment_id": "d-xacct-unvalidated",
                    "config": _config(),
                    "target_account_id": "210987654321",
                    "target_region": REGION,
                },
                None,
            )


def test_nothing_is_recorded_when_no_bucket_resolves():
    """No upload happened, so a manifest row would order a delete for an object that
    never existed — and ``delete_object`` on a missing key returns success, so the log
    would claim a cleanup that did nothing."""
    from app.step_handlers import codegen_step

    store = MagicMock()
    with (
        patch.object(codegen_step, "_get_deployment_store", return_value=store),
        patch.object(codegen_step, "_get_env", side_effect=lambda n, d="": d),
        patch.object(codegen_step.step_clients, "client", return_value=MagicMock()),
        patch("boto3.client", return_value=MagicMock()),
    ):
        codegen_step.handler({"deployment_id": "d-nobucket", "config": _config()}, None)

    assert _s3_object_rows(store) == []


def test_a_recording_failure_does_not_fail_the_deploy():
    """The bundle is already uploaded and the runtime can still launch; losing the
    manifest row must degrade to a leak, never to a failed deploy."""
    from app.step_handlers import codegen_step

    store = MagicMock()
    store.record_resource.side_effect = RuntimeError("DynamoDB is having a day")

    with (
        patch.object(codegen_step, "_get_deployment_store", return_value=store),
        patch.object(codegen_step, "_get_env", side_effect=lambda n, d="": {"ARTIFACTS_BUCKET_NAME": BUCKET}.get(n, d)),
        patch.object(codegen_step.step_clients, "client", return_value=MagicMock()),
        patch("boto3.client", return_value=MagicMock()),
        patch.object(codegen_step, "_download_bundle", return_value=b"deps"),
        patch.object(codegen_step, "_provider_bundles", return_value=[]),
        patch("app.services.runtime_deployer.upload_code_to_s3"),
    ):
        result = codegen_step.handler({"deployment_id": "d-recfail", "config": _config()}, None)

    assert result["s3_bucket"] == BUCKET
    assert result["s3_key"].endswith("code.zip")


def test_the_mcp_server_step_records_its_bundle_too():
    """``mcp_server_step`` uploads a second bundle with a different key shape.

    Source-level assertion rather than a handler invocation: that handler creates an IAM
    role, a Cognito pool, a resource server and a runtime before it returns, so driving
    it end to end here would be testing the mocks. What matters is that the record call
    sits next to the upload and uses the same variable the upload used — a recomputed
    key is the defect, not the fix.
    """
    import ast
    import inspect

    from app.step_handlers import mcp_server_step

    src = inspect.getsource(mcp_server_step.handler)
    tree = ast.parse(src.lstrip())

    manifest_dicts: dict[str, dict[str, ast.AST]] = {}
    recorded_names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Dict)
        ):
            manifest_dicts[node.targets[0].id] = {
                k.value: v
                for k, v in zip(node.value.keys, node.value.values, strict=True)
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "record_resource"):
            continue
        arg = node.args[-1] if node.args else None
        if isinstance(arg, ast.Name):
            recorded_names.add(arg.id)

    recorded_ids = [
        ast.unparse(entry["id"])
        for name, entry in manifest_dicts.items()
        if name in recorded_names and isinstance(entry.get("type"), ast.Constant) and entry["type"].value == "s3_object"
    ]
    assert recorded_ids, "mcp_server_step.handler records no s3_object row — its bundle leaks"
    assert any("mcp_s3_key" in expr for expr in recorded_ids), (
        f"the s3_object row does not reference the mcp_s3_key variable the upload used: {recorded_ids}. "
        "A key rebuilt from the runtime name can differ (sanitize_runtime_name truncates), and "
        "delete_object on a wrong key returns success."
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

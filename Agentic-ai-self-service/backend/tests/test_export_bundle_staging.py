"""Export bundles are temporary, caller-scoped platform artifacts (P0-C).

Both download routes stage a zip in the artifacts bucket behind a one-hour URL.
What this pins, through the real routes:

  * the key is ``<prefix>/<owner hash>/<name>-<128-bit suffix>.zip`` -- the raw
    Cognito sub never enters it, and two exports never share a key, because
    PutObject overwrites silently;
  * the object carries exactly four FIXED tags, never the workload's governance set;
    the workload's tags go on the exported stack's code.zip instead (item 15), and
    because S3 caps an object at 10 tags, a set that cannot fit is REFUSED before
    anything is staged rather than truncated or emitted as an undeployable template;
  * a caller with no identity gets a 401 before anything is generated or written,
    and that 401 is not collapsed into the routes' generic 500;
  * an export needs ``agent:write`` (F-73): a read-only caller can list and invoke
    agents but cannot pull a deployable bundle of one;
  * the Python export refuses ``resourceTags``/``tagProfile`` with an actionable 400
    rather than accepting and discarding them.

ARCC guidance: cnt_AUfPj1lspAXlAO (presigned URLs: short expiry, SigV4),
cnt_sfiNQzRGEcegFL (timeboxed data purged by lifecycle).
"""

from __future__ import annotations

import io
import re
import urllib.parse
import zipfile

import pytest
import yaml
from app.models.deployment_models import RuntimeConfig
from app.services.resource_ownership import owner_sub_hash
from fastapi.testclient import TestClient

MODEL_ID = "us.anthropic.claude-sonnet-5"
SUB = "0a1b2c3d-export-staging-owner"
BUCKET = "acf-test-artifacts"


class _FakeS3:
    def __init__(self):
        self.puts: list[dict] = []
        self.presigns: list[dict] = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        return {}

    def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803 - boto3 names
        self.presigns.append({"op": op, "Params": Params, "ExpiresIn": ExpiresIn})
        return f"https://{Params['Bucket']}.s3.us-east-1.amazonaws.com/{Params['Key']}?X-Amz-Signature=x"


@pytest.fixture
def s3(monkeypatch):
    import app.deployment_handler as dh

    fake = _FakeS3()
    monkeypatch.setenv("ARTIFACTS_BUCKET_NAME", BUCKET)
    monkeypatch.setattr(dh, "_artifacts_s3_client", lambda _region: fake)
    return fake


def _client(sub: str | None = SUB, groups: tuple[str, ...] = ("g-users-default",)) -> TestClient:
    import app.deployment_handler as dh

    claims = {"cognito:groups": list(groups)}
    if sub is not None:
        claims["sub"] = sub
    event = {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}

    async def _inject(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "aws.event": event}
        await dh.deployment_app(scope, receive, send)

    return TestClient(_inject, raise_server_exceptions=False)


def _body(**extra) -> dict:
    return {
        "nodeId": "node-1",
        "config": RuntimeConfig(name="Export Test", model={"modelId": MODEL_ID}).model_dump(mode="json", by_alias=True),
        **extra,
    }


ROUTES = [
    ("/api/generate-cfn-template", "cfn-templates", "cfn-template"),
    ("/api/export-python", "python-exports", "python-export"),
]


@pytest.mark.parametrize(("route", "prefix", "artifact_type"), ROUTES)
def test_an_export_is_keyed_by_the_owner_hash_with_the_fixed_tags(s3, route, prefix, artifact_type):
    response = _client().post(route, json=_body())

    assert response.status_code == 200, response.text[:400]
    assert len(s3.puts) == 1
    put = s3.puts[0]
    assert put["Bucket"] == BUCKET
    assert re.fullmatch(rf"{prefix}/{owner_sub_hash(SUB)}/[a-z0-9-]+-[0-9a-f]{{32}}\.zip", put["Key"]), put["Key"]
    assert SUB not in put["Key"]
    assert dict(urllib.parse.parse_qsl(put["Tagging"])) == {
        "ManagedBy": "agentcore-flows",
        "AgentCoreStack": _stack(),
        "OwnerSubHash": owner_sub_hash(SUB),
        "ArtifactType": artifact_type,
    }
    # the pre-signed URL names the download itself: a browser saves a cross-origin download under the object's name and
    # ignores the anchor's `download` attribute, so the reported `filename` and the URL's disposition must agree
    reported = response.json()["filename"]
    assert reported.endswith(".zip") and "-" in reported
    assert s3.presigns == [
        {
            "op": "get_object",
            "Params": {
                "Bucket": BUCKET,
                "Key": put["Key"],
                "ResponseContentDisposition": f'attachment; filename="{reported}"',
                "ResponseContentType": "application/zip",
            },
            "ExpiresIn": 3600,
        }
    ]
    assert response.json()["download_url"].startswith(f"https://{BUCKET}.s3.")


def _stack() -> str:
    import app.deployment_handler as dh
    from app.services.resource_ownership import stack_id

    return stack_id(dh.config.aws_region)


@pytest.mark.parametrize(("route", "prefix", "artifact_type"), ROUTES)
def test_two_exports_never_share_a_key(s3, route, prefix, artifact_type):
    client = _client()
    for _ in range(2):
        assert client.post(route, json=_body()).status_code == 200
    keys = [p["Key"] for p in s3.puts]
    assert len(keys) == 2 and len(set(keys)) == 2, keys


#: P0-B: an export carrying tags is refused before the store is reached unless it also
#: carries the policy revision those tags were resolved against. The doubles here accept any
#: revision -- the comparison is pinned in ``test_tag_governance_is_fail_closed.py``.
REVISION = "sha256:" + "b" * 64


def _passthrough_store(monkeypatch):
    import app.services.tag_policy_store as tps

    class _Store:
        def ensure_platform_policies(self, _org):
            pass

        def resolve_governance(self, _org, supplied=None, profile_name=None, **_kw):
            return tps.ResolvedGovernance(tags=dict(supplied or {}), policy_revision=REVISION)

    monkeypatch.setattr(tps, "get_tag_policy_store", lambda: _Store())


def test_a_cfn_workload_with_ten_tags_exports_with_four_object_tags_and_all_ten_in_the_template(s3, monkeypatch):
    """The bundle's own tag set is fixed and separate from the workload's (P0-C)."""
    import app.deployment_handler as dh

    _passthrough_store(monkeypatch)
    assert dh.CODE_ZIP_TAG_LIMIT == 10  # the S3 per-object limit; lower it when P0-A adds fixed tags
    workload_tags = {f"Gov{i:02d}": f"v{i}" for i in range(dh.CODE_ZIP_TAG_LIMIT)}

    response = _client().post(
        "/api/generate-cfn-template", json=_body(resourceTags=workload_tags, policyRevision=REVISION)
    )

    assert response.status_code == 200, response.text[:400]
    put = s3.puts[0]
    assert len(urllib.parse.parse_qsl(put["Tagging"])) == 4
    assert not set(workload_tags) & set(dict(urllib.parse.parse_qsl(put["Tagging"])))
    # ...and the workload's own tags are in the template, not lost to make room.
    with zipfile.ZipFile(io.BytesIO(put["Body"])) as zf:
        template_name = next(n for n in zf.namelist() if n.endswith("/template.yaml"))
        template = yaml.load(zf.read(template_name), Loader=_CfnLoader)
    tagged = [
        r
        for r in template["Resources"].values()
        if isinstance(r.get("Properties", {}).get("Tags"), (list, dict)) and "Gov09" in str(r["Properties"]["Tags"])
    ]
    assert tagged, "no resource in the exported template carries the workload's 10th tag"


def test_a_cfn_workload_with_more_than_ten_tags_is_refused_before_anything_is_staged(s3, monkeypatch):
    """Item 15 puts every workload tag on the exported code.zip, and S3 allows 10 per object.

    A set that cannot fit would make the export either undeployable or partially tagged, so
    it is refused with the remedy, and NOTHING is generated or staged (P0-A/P0-C contract).
    """
    import app.deployment_handler as dh

    _passthrough_store(monkeypatch)
    workload_tags = {f"Gov{i:02d}": f"v{i}" for i in range(dh.CODE_ZIP_TAG_LIMIT + 1)}

    response = _client().post(
        "/api/generate-cfn-template", json=_body(resourceTags=workload_tags, policyRevision=REVISION)
    )

    assert response.status_code == 400, response.text[:400]
    detail = response.json()["detail"]
    assert "11 resource tags" in detail and "at most 10" in detail and "Remove tags" in detail
    assert s3.puts == [] and s3.presigns == [], "the refusal must come before staging"


def test_positive_control_exactly_the_limit_is_not_refused(s3, monkeypatch):
    """The guard is > LIMIT, not >= LIMIT: ten tags is the most S3 accepts, and it exports."""
    import app.deployment_handler as dh

    _passthrough_store(monkeypatch)
    tags = {f"Gov{i:02d}": f"v{i}" for i in range(dh.CODE_ZIP_TAG_LIMIT)}
    assert (
        _client().post("/api/generate-cfn-template", json=_body(resourceTags=tags, policyRevision=REVISION)).status_code
        == 200
    )
    assert len(s3.puts) == 1


class _CfnLoader(yaml.SafeLoader):
    pass


_CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


@pytest.mark.parametrize(("route", "_prefix", "_type"), ROUTES)
def test_a_caller_with_no_sub_gets_401_before_anything_is_built_or_written(s3, monkeypatch, route, _prefix, _type):
    import app.services.cfn_template_generator as gen
    import app.services.python_exporter as pyexp

    monkeypatch.setattr(gen.CfnTemplateGenerator, "generate", lambda *_a, **_k: pytest.fail("generated"))
    import app.services.tag_policy_store as tps

    monkeypatch.setattr(pyexp, "build_and_zip", lambda *_a, **_k: pytest.fail("built"))
    monkeypatch.setattr(tps, "get_tag_policy_store", lambda: pytest.fail("tag policy store reached"))

    # resourceTags present, so the CFN route would resolve them if identity came second.
    body = _body(resourceTags={"CostCenter": "cc-1"}) if route.endswith("cfn-template") else _body()
    response = _client(sub=None).post(route, json=body)

    assert response.status_code == 401, response.text[:400]
    assert response.json() == {"detail": "An export needs an authenticated caller"}
    assert s3.puts == [] and s3.presigns == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("resourceTags", {"CostCenter": "cc-1"}), ("tagProfile", "regulated")],
)
def test_the_python_export_refuses_governance_tags_before_building(s3, monkeypatch, field, value):
    import app.services.python_exporter as pyexp

    monkeypatch.setattr(pyexp, "build_and_zip", lambda *_a, **_k: pytest.fail("built"))

    response = _client().post("/api/export-python", json=_body(**{field: value}))

    assert response.status_code == 400, response.text[:400]
    detail = response.json()["detail"]
    assert field in detail
    assert "creates no AWS resources" in detail
    assert f"Remove {field}" in detail
    assert s3.puts == []


def test_with_no_bucket_the_bundle_is_returned_inline_and_no_identity_is_needed(monkeypatch):
    monkeypatch.delenv("ARTIFACTS_BUCKET_NAME", raising=False)

    response = _client(sub=None).post("/api/export-python", json=_body())

    assert response.status_code == 200, response.text[:400]
    assert "zip_base64" in response.json()


@pytest.mark.parametrize(("route", "_prefix", "_type"), ROUTES)
def test_a_read_only_caller_cannot_export(s3, monkeypatch, route, _prefix, _type):
    """F-73: agent:read is what a Chat-only persona would need, so it must not export."""
    import app.services.cfn_template_generator as gen
    import app.services.python_exporter as pyexp
    from app.services.rbac import GROUP_SCOPES

    assert "agent:read" in GROUP_SCOPES["viewer"] and "agent:write" not in GROUP_SCOPES["viewer"]
    monkeypatch.setenv("RBAC_ENFORCE", "true")
    monkeypatch.setattr(gen.CfnTemplateGenerator, "generate", lambda *_a, **_k: pytest.fail("generated"))
    monkeypatch.setattr(pyexp, "build_and_zip", lambda *_a, **_k: pytest.fail("built"))

    response = _client(groups=("viewer",)).post(route, json=_body())

    assert response.status_code == 403, response.text[:400]
    assert "agent:write" in response.json()["detail"]
    assert s3.puts == []


@pytest.mark.parametrize(("route", "_prefix", "_type"), ROUTES)
def test_an_http_error_raised_while_staging_reaches_the_caller_unchanged(s3, monkeypatch, route, _prefix, _type):
    """The routes' broad ``except Exception`` must not turn a deliberate status into a 500."""
    import app.deployment_handler as dh
    from fastapi import HTTPException

    def _refuse(*_a, **_k):
        raise HTTPException(status_code=409, detail="staging refused on purpose")

    monkeypatch.setattr(dh, "_stage_export_bundle", _refuse)

    response = _client().post(route, json=_body())

    assert (response.status_code, response.json()) == (409, {"detail": "staging refused on purpose"})

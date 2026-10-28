"""The live-scenario binding refuses anything that is not the candidate.

Pure tests of ``release/scenarios/_live.py`` with a synthetic sealed assembly and a synthetic
observed stack (no AWS): the deployment binds only when the ``Source*`` outputs, the
canonical template digest and every Lambda ``CodeSha256`` agree with the assembly, each
single disagreement fails, a missing assembly blocks rather than passes, and the provenance
document has exactly the keys ``scripts/release_evidence.py`` accepts.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = PROJECT_ROOT / "release" / "scenarios"
RECORDER = PROJECT_ROOT / "scripts" / "release_evidence.py"


def _load(name: str):
    if str(SCENARIOS) not in sys.path:
        sys.path.insert(0, str(SCENARIOS))
    spec = importlib.util.spec_from_file_location(name, SCENARIOS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


live = _load("_live")
regional_core = _load("regional_core")

COMMIT = "a" * 40
TREE = "b" * 40
REGION = "eu-west-3"
ACCOUNT = "048834207240"


def _asset_bytes(label: str) -> bytes:
    return f"zip-bytes-for-{label}".encode()


def _code_sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _zip_of(files: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture
def sealed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A sealed assembly with one pre-zipped asset and one directory asset, plus the objects
    CDK would have published for them, served from memory instead of S3."""
    assemblies = tmp_path / "assemblies"
    monkeypatch.setattr(live, "ASSEMBLIES_ROOT", assemblies)
    target = assemblies / COMMIT / REGION
    target.mkdir(parents=True)
    template = {
        "Resources": {
            "WorkflowLambda": {"Type": "AWS::Lambda::Function", "Properties": {"Code": {"S3Key": "1111.zip"}}},
            "DeploymentLambda": {"Type": "AWS::Lambda::Function", "Properties": {"Code": {"S3Key": "1111.zip"}}},
            "ProviderLambda": {"Type": "AWS::Lambda::Function", "Properties": {"Code": {"S3Key": "2222.zip"}}},
            "Table": {"Type": "AWS::DynamoDB::Table", "Properties": {}},
        },
        "Outputs": {
            "SourceCommit": {"Value": COMMIT},
            "SourceProjectTree": {"Value": TREE},
            "SourceDirty": {"Value": "false"},
            "CloudFrontUrl": {"Value": "https://d1.cloudfront.net"},
        },
    }
    (target / "acf-production.template.json").write_text(json.dumps(template))
    # 1111: a directory asset (cdk-assets zips it at publish time)
    tree_files = {"app/handler.py": b"def handler(): pass\n", "app/data.json": b"{}\n"}
    for name, data in tree_files.items():
        (target / "asset.1111" / name).parent.mkdir(parents=True, exist_ok=True)
        (target / "asset.1111" / name).write_bytes(data)
    # 2222: a pre-zipped asset (published byte for byte)
    provider_zip = _zip_of({"index.js": b"exports.handler = () => {};\n"})
    (target / "asset.2222.zip").write_bytes(provider_zip)
    published = {"1111.zip": _zip_of(tree_files), "2222.zip": provider_zip}
    monkeypatch.setattr(live, "_published_asset", lambda binding, key, cache_dir: published[key])

    binding = live.RunBinding(tmp_path / "run", COMMIT, TREE, ACCOUNT, REGION)
    observed = live.ObservedStack(
        region=REGION,
        stack_id=f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/acf-production/uuid",
        status="UPDATE_COMPLETE",
        outputs={
            "SourceCommit": COMMIT,
            "SourceProjectTree": TREE,
            "SourceDirty": "false",
            "CloudFrontUrl": "https://d1.cloudfront.net/",
            "UserPoolId": "eu-west-3_pool",
            "UserPoolClientId": "client",
        },
        template_sha256=live.canonical_json_sha256(template),
        lambda_code_sha256={
            "WorkflowLambda": _code_sha256(published["1111.zip"]),
            "DeploymentLambda": _code_sha256(published["1111.zip"]),
            "ProviderLambda": _code_sha256(published["2222.zip"]),
        },
    )
    return binding, observed, target, published


def test_the_candidate_binds_and_the_comparison_is_reported(sealed):
    binding, observed, _, _ = sealed
    comparison = live.bind_deployment(binding, observed)
    assert comparison["problems"] == []
    assert comparison["lambda_functions_compared"] == 3
    assert comparison["template_sha256"]["live"] == comparison["template_sha256"]["assembly"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda o: o.outputs.__setitem__("SourceCommit", "c" * 40), "SourceCommit output"),
        (lambda o: o.outputs.pop("SourceCommit"), "SourceCommit output is <absent>"),
        (lambda o: o.outputs.__setitem__("SourceProjectTree", "d" * 40), "SourceProjectTree output"),
        (lambda o: o.outputs.__setitem__("SourceDirty", "true"), "only a clean synth"),
        (lambda o: setattr(o, "template_sha256", "0" * 64), "live template digest differs"),
        (
            lambda o: o.lambda_code_sha256.__setitem__("ProviderLambda", _code_sha256(b"tampered")),
            "ProviderLambda CodeSha256 is not the published candidate asset",
        ),
        (lambda o: o.lambda_code_sha256.pop("ProviderLambda"), "same Lambda functions"),
        (lambda o: o.lambda_code_sha256.__setitem__("Extra", _code_sha256(b"x")), "same Lambda functions"),
    ],
)
def test_each_single_disagreement_fails_the_binding(sealed, mutate, message):
    binding, observed, _, _ = sealed
    mutate(observed)
    with pytest.raises(live.ScenarioFailed, match=message):
        live.bind_deployment(binding, observed)


def test_a_published_object_whose_content_is_not_the_sealed_asset_fails(sealed, monkeypatch):
    """The live function may carry the published object's exact digest and STILL not be the
    candidate: the published object itself must be the sealed asset, file by file."""
    binding, observed, _, published = sealed
    forged = dict(published)
    forged["1111.zip"] = _zip_of({"app/handler.py": b"def handler(): return 'backdoor'\n", "app/data.json": b"{}\n"})
    monkeypatch.setattr(live, "_published_asset", lambda binding, key, cache_dir: forged[key])
    observed.lambda_code_sha256["WorkflowLambda"] = _code_sha256(forged["1111.zip"])
    observed.lambda_code_sha256["DeploymentLambda"] = _code_sha256(forged["1111.zip"])
    with pytest.raises(live.ScenarioFailed, match="content differs from the sealed asset directory"):
        live.bind_deployment(binding, observed)
    forged["2222.zip"] = _zip_of({"index.js": b"exports.handler = () => { backdoor(); };\n"})
    observed.lambda_code_sha256["ProviderLambda"] = _code_sha256(forged["2222.zip"])
    with pytest.raises(live.ScenarioFailed, match="differs from the sealed zip asset"):
        live.bind_deployment(binding, observed)


def test_a_missing_sealed_assembly_blocks_instead_of_passing(sealed):
    binding, observed, target, _ = sealed
    (target / "acf-production.template.json").unlink()
    with pytest.raises(live.ScenarioBlocked, match="no sealed assembly"):
        live.bind_deployment(binding, observed)


def test_a_missing_asset_in_the_assembly_blocks(sealed):
    binding, observed, target, _ = sealed
    (target / "asset.2222.zip").unlink()
    with pytest.raises(live.ScenarioBlocked, match="lacks the asset for 2222.zip"):
        live.bind_deployment(binding, observed)


def test_provenance_has_exactly_the_keys_the_recorder_accepts(sealed, tmp_path: Path):
    binding, observed, _, _ = sealed
    spec = importlib.util.spec_from_file_location("release_evidence", RECORDER)
    recorder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recorder)
    document = live.write_provenance(tmp_path / "out", binding, observed)
    keys = set(document)
    assert recorder._PROVENANCE_REQUIRED_KEYS <= keys
    assert keys <= recorder._PROVENANCE_REQUIRED_KEYS | recorder._PROVENANCE_OPTIONAL_KEYS
    assert document["endpoint"] == "https://d1.cloudfront.net", "trailing slash normalized"
    assert json.loads((tmp_path / "out" / "provenance").read_text()) == document


def test_run_binding_refuses_a_mismatched_account_or_missing_region(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"source": {"commit": COMMIT, "project_tree": TREE}, "account_id": ACCOUNT})
    )
    good = argparse.Namespace(run_dir=run_dir, region=REGION, account_id=ACCOUNT)
    assert live.RunBinding.from_args(good).region == REGION
    with pytest.raises(live.ScenarioFailed, match="disagrees"):
        live.RunBinding.from_args(argparse.Namespace(run_dir=run_dir, region=REGION, account_id="111111111111"))
    with pytest.raises(live.ScenarioFailed, match="needs --region"):
        live.RunBinding.from_args(argparse.Namespace(run_dir=run_dir, region=None, account_id=ACCOUNT))


def test_profiles_map_to_the_reviewed_phase_plans():
    full = [phase for phase, _, _ in regional_core.phases_for("full")]
    reduced = [phase for phase, _, _ in regional_core.phases_for("reduced")]
    assert full == ["lifecycle", "gateway", "web-search", "features", "triggers"]
    assert reduced == ["lifecycle", "gateway", "features"]
    with pytest.raises(live.ScenarioFailed):
        regional_core.phases_for("excluded")
    selectors = {selector for _, selector, _ in regional_core.phases_for("full")}
    for selector in selectors:
        assert (PROJECT_ROOT / "backend" / selector.split("::")[0]).is_file(), selector


def test_redaction_hides_secrets_and_jwts():
    token = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20
    out = live.redact(f"password=hunter2hunter2 token={token}", "hunter2hunter2")
    assert "hunter2hunter2" not in out and token not in out
    assert "[REDACTED]" in out and "[REDACTED_JWT]" in out

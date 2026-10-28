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


@pytest.fixture
def sealed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assemblies = tmp_path / "assemblies"
    monkeypatch.setattr(live, "ASSEMBLIES_ROOT", assemblies)
    target = assemblies / COMMIT / REGION
    target.mkdir(parents=True)
    template = {
        "Resources": {
            "WorkflowLambda": {"Type": "AWS::Lambda::Function", "Properties": {"Code": {"S3Key": "1111.zip"}}},
            "StreamLambda": {"Type": "AWS::Lambda::Function", "Properties": {"Code": {"S3Key": "2222.zip"}}},
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
    (target / "asset.1111.zip").write_bytes(_asset_bytes("workflow"))
    (target / "asset.2222.zip").write_bytes(_asset_bytes("stream"))
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
            "WorkflowLambda": _code_sha256(_asset_bytes("workflow")),
            "StreamLambda": _code_sha256(_asset_bytes("stream")),
        },
    )
    return binding, observed, target


def test_the_candidate_binds_and_the_comparison_is_reported(sealed):
    binding, observed, _ = sealed
    comparison = live.bind_deployment(binding, observed)
    assert comparison["problems"] == []
    assert comparison["lambda_functions_compared"] == 2
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
            lambda o: o.lambda_code_sha256.__setitem__("StreamLambda", _code_sha256(b"tampered")),
            "StreamLambda code differs",
        ),
        (lambda o: o.lambda_code_sha256.pop("StreamLambda"), "same Lambda functions"),
        (lambda o: o.lambda_code_sha256.__setitem__("Extra", _code_sha256(b"x")), "same Lambda functions"),
    ],
)
def test_each_single_disagreement_fails_the_binding(sealed, mutate, message):
    binding, observed, _ = sealed
    mutate(observed)
    with pytest.raises(live.ScenarioFailed, match=message):
        live.bind_deployment(binding, observed)


def test_a_missing_sealed_assembly_blocks_instead_of_passing(sealed):
    binding, observed, target = sealed
    (target / "acf-production.template.json").unlink()
    with pytest.raises(live.ScenarioBlocked, match="no sealed assembly"):
        live.bind_deployment(binding, observed)


def test_a_missing_asset_in_the_assembly_blocks(sealed):
    binding, observed, target = sealed
    (target / "asset.2222.zip").unlink()
    with pytest.raises(live.ScenarioBlocked, match="lacks asset.2222.zip"):
        live.bind_deployment(binding, observed)


def test_provenance_has_exactly_the_keys_the_recorder_accepts(sealed, tmp_path: Path):
    binding, observed, _ = sealed
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

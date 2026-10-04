"""Matching-principal RegistryReaderRole trust twins, run through the deployed
Workstream validator Lambda (the ONLY principal the reader trust admits).

The reader trust requires the exact ExternalId AND a ``registry-*`` session
name from the ``AgenticAI-D03-*-RegistryValidator`` principal. A user cannot
assume the validator role (Lambda-trusted only), so the twins drive the real
validator function: its handler assumes the reader role with the ExternalId
and session name taken from its environment. We invoke it three times:

  positive              -- unchanged environment; expects the handler to pass
  wrong-external-id     -- REGISTRY_READER_EXTERNAL_ID replaced; expects STS 403 AccessDenied
  wrong-session-name    -- REGISTRY_READER_SESSION_NAME replaced; expects STS 403 AccessDenied

The environment is restored in ``finally`` and re-verified byte-for-byte, and a
final positive invoke proves the restored function still works. Evidence holds
booleans, HTTP codes, error-class names and SHA-256 fingerprints only; the
ExternalId value is never printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import boto3


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def verify_identity(session: boto3.Session, expected_account: str) -> None:
    account = session.client("sts").get_caller_identity()["Account"]
    if account != expected_account:
        raise SystemExit(
            f"credentials belong to account ...{account[-4:]}, expected ...{expected_account[-4:]}; refusing"
        )


def wait_updated(lam, function_name: str) -> None:
    for _ in range(60):
        cfg = lam.get_function_configuration(FunctionName=function_name)
        if cfg.get("LastUpdateStatus") in (None, "Successful"):
            return
        if cfg.get("LastUpdateStatus") == "Failed":
            raise SystemExit("function update failed: " + str(cfg.get("LastUpdateStatusReason")))
        time.sleep(2)
    raise SystemExit("function update did not settle")


def set_env(lam, function_name: str, env: dict[str, str]) -> None:
    lam.update_function_configuration(FunctionName=function_name, Environment={"Variables": env})
    wait_updated(lam, function_name)


def invoke(lam, function_name: str, event: dict) -> dict:
    resp = lam.invoke(FunctionName=function_name, Payload=json.dumps(event).encode("utf-8"))
    payload = resp["Payload"].read().decode("utf-8")
    result = {"statusCode": resp["StatusCode"], "functionError": resp.get("FunctionError")}
    if resp.get("FunctionError"):
        try:
            err = json.loads(payload)
        except json.JSONDecodeError:
            err = {"errorMessage": payload}
        message = str(err.get("errorMessage", ""))
        result["errorType"] = err.get("errorType")
        result["stsHttpStatus"] = 403 if "STS AssumeRole HTTP 403" in message else None
        result["accessDenied"] = "<Code>AccessDenied</Code>" in message
        result["messageFingerprint"] = fingerprint(message)
    else:
        result["payloadFingerprint"] = fingerprint(payload)
        result["payloadIsObject"] = payload.strip().startswith("{")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--function-name", required=True)
    parser.add_argument("--ga-context", required=True, type=Path)
    parser.add_argument("--validation-revision", required=True, help="full 40-hex Git SHA of the deployed revision")
    parser.add_argument("--tenant-id", default="demo")
    parser.add_argument("--agent-id", default="primary")
    parser.add_argument("--evidence-out", required=True, type=Path)
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    verify_identity(session, args.expected_account)
    lam = session.client("lambda")

    context = json.loads(args.ga_context.read_text())
    record = context["records"][0]
    event = {
        "RequestType": "Create",
        "ResourceProperties": {
            "registryId": context["registryId"],
            "recordId": record["recordId"],
            "expectedToolId": record["document"]["toolId"],
            "expectedTargetArn": record["document"]["target"]["arn"],
            "expectedDescriptorSha256": record["descriptorSha256"],
            "validationRevision": args.validation_revision,
            "tenantId": args.tenant_id,
            "agentId": args.agent_id,
        },
    }

    original = lam.get_function_configuration(FunctionName=args.function_name)
    original_env = dict(original["Environment"]["Variables"])
    if not original_env.get("REGISTRY_READER_ROLE_ARN") or not original_env.get("REGISTRY_READER_EXTERNAL_ID"):
        raise SystemExit("validator environment lacks the reader role/ExternalId; refusing")
    evidence: dict = {
        "probe": "registry-reader-trust-twins",
        "region": args.region,
        "functionNameFingerprint": fingerprint(args.function_name),
        "executionRoleName": original["Role"].rsplit("/", 1)[-1],
        "originalEnvFingerprint": fingerprint(json.dumps(original_env, sort_keys=True)),
        "toolId": record["document"]["toolId"],
        "steps": {},
    }
    restored_env_matches = False
    try:
        evidence["steps"]["positive"] = invoke(lam, args.function_name, event)

        wrong_ext = dict(original_env, REGISTRY_READER_EXTERNAL_ID="wrong-external-id-twin")
        set_env(lam, args.function_name, wrong_ext)
        evidence["steps"]["wrongExternalId"] = invoke(lam, args.function_name, event)

        wrong_sess = dict(original_env, REGISTRY_READER_SESSION_NAME="not-a-registry-session")
        set_env(lam, args.function_name, wrong_sess)
        evidence["steps"]["wrongSessionName"] = invoke(lam, args.function_name, event)
    finally:
        set_env(lam, args.function_name, original_env)
        restored = lam.get_function_configuration(FunctionName=args.function_name)
        restored_env_matches = dict(restored["Environment"]["Variables"]) == original_env
        evidence["restoredEnvMatches"] = restored_env_matches
        if restored_env_matches:
            evidence["steps"]["positiveAfterRestore"] = invoke(lam, args.function_name, event)

    steps = evidence["steps"]
    positive_ok = steps["positive"].get("functionError") is None and steps["positive"].get("payloadIsObject") is True
    ext_denied = steps.get("wrongExternalId", {}).get("stsHttpStatus") == 403 and steps["wrongExternalId"].get("accessDenied") is True
    sess_denied = steps.get("wrongSessionName", {}).get("stsHttpStatus") == 403 and steps["wrongSessionName"].get("accessDenied") is True
    after_ok = steps.get("positiveAfterRestore", {}).get("functionError") is None
    evidence["passed"] = bool(positive_ok and ext_denied and sess_denied and restored_env_matches and after_ok)
    args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
    args.evidence_out.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

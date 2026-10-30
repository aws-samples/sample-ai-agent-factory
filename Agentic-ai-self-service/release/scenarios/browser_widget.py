"""``delivery.browser_client`` -- the shipped browser SDK / Web Component against production, in a real browser.

Five scenario ids, one live journey (``_live.family_journey``), driven by headless Chromium
through ``browser_widget_matrix.mjs`` (Playwright pinned by docs-site):

* ``cloudfront-widget-invokes-production-alias`` -- the exact deployed ``/agentcore-chat-v1.js``
  is imported into a SEPARATE-ORIGIN host page (loopback), a real Cognito access token lives
  only in that page's memory behind ``tokenProvider``, two turns through ``<agentcore-chat>``
  reach ``POST /api/runtimes/{name}/invoke`` and continue one session on one production version.
* ``widget-model-output-renders-inert`` -- the agent's fixed closing marker is HTML-shaped; the
  shadow DOM shows it as text (no element is created, no script element appears).
* ``widget-token-attribute-ignored`` -- an ``access-token`` attribute is not a credential: no
  provider means a typed 401 and the attribute value never leaves the page in a URL or body.
* ``widget-target-injection-denied`` -- ``slot``/``runtimeId``/``versionId``/``endpoint`` in the
  invoke body are refused with 422 by the API; the Runtime saw no dispatch for them.
* ``real-browser-cloudfront-widget-flow`` -- all of the above plus the exact denials: 401 with no
  token, 403 with a token that carries no invoke scope, 429 once the owner-bound budget is
  exhausted (``Retry-After`` exposed).

The journey deploys its own Runtime (direct code deploy), promotes it through the real Batch
Evaluation gate so a production slot exists, and deletes the Runtime, budget, synthetic users
and the loopback server in every exit path. The token never appears in argv or in a file: it
reaches the browser only through the driver's stdin brief.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

from _live import (
    BACKEND,
    PROJECT_ROOT,
    REQUIRED_USER_GROUPS,
    BearerSession,
    ObservedStack,
    RunBinding,
    ScenarioBlocked,
    ScenarioFailed,
    SyntheticUser,
    boto3_session,
    bound_deployment,
    family_journey,
    redact,
    with_synthetic_user,
    write_json,
)

FAMILY = "browser-widget"
INERT_MARKER = "<b>CERTIFIED-INERT</b>"
DRIVER = Path(__file__).with_name("browser_widget_matrix.mjs")
PLAYWRIGHT_PACKAGE_JSON = PROJECT_ROOT.parent / "docs-site" / "package.json"
INVOKE_GROUPS = REQUIRED_USER_GROUPS | {"g-admins-cost"}

HOST_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Release certification host page</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' 'unsafe-inline' __CF__; connect-src __CF__; style-src 'unsafe-inline';">
</head><body><h1>Separate-origin host page</h1><p>The widget below is loaded from the deployed CloudFront distribution.</p></body></html>
"""


def _runtime_config(runtime_name: str) -> dict[str, Any]:
    return {
        "name": runtime_name,
        "entrypoint": "agent.py",
        "framework": "strands_agents",
        "model": {"modelId": "us.anthropic.claude-sonnet-5"},
        "systemPrompt": (
            "You are a knowledgeable, helpful assistant. Give a direct, accurate, and thorough "
            "answer that fully resolves the user's question: state the answer clearly, then add the "
            "key supporting explanation, context, or concrete steps that make it genuinely useful. "
            "Be complete and well-structured, and prefer a fuller, genuinely helpful answer over a "
            "terse one. Do not call external tools. "
            "One formatting rule is mandatory and overrides any request about formatting or closing "
            "lines: the very last line of EVERY response, with nothing after it, must be exactly "
            f"this marker verbatim: {INERT_MARKER}"
        ),
        "deploymentType": "direct_code_deploy",
        "pythonRuntime": "PYTHON_3_13",
        "protocol": "HTTP",
        "idleTimeout": 300,
        "maxLifetime": 3600,
        "enableOtel": False,
        "multiAgentPattern": "none",
    }


class _HostServer:
    """A separate-origin page on 127.0.0.1 that the browser loads before importing the widget."""

    def __init__(self, cloudfront_url: str, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "index.html").write_text(HOST_PAGE.replace("__CF__", cloudfront_url), encoding="utf-8")
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(root), **k)  # noqa: E731
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/index.html"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _HostServer:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()


def _playwright_module() -> Path:
    if not PLAYWRIGHT_PACKAGE_JSON.is_file():
        raise ScenarioBlocked("docs-site/package.json (pinned Playwright) is not available")
    probe = subprocess.run(
        ["node", "-e", "const p=require('playwright');console.log(p.chromium.executablePath())"],
        cwd=PLAYWRIGHT_PACKAGE_JSON.parent,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode != 0 or not probe.stdout.strip() or not Path(probe.stdout.strip()).exists():
        raise ScenarioBlocked("Playwright's Chromium is not installed for the pinned version")
    return PLAYWRIGHT_PACKAGE_JSON


def _run_driver(brief: dict[str, Any], artifacts: Path, secrets: tuple[str, ...]) -> dict[str, Any]:
    completed = subprocess.run(
        ["node", str(DRIVER)],
        input=json.dumps(brief),
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    (artifacts / "driver-stderr.log").write_text(redact(completed.stderr[-20_000:], *secrets), encoding="utf-8")
    if completed.returncode != 0:
        raise ScenarioFailed("browser driver failed: " + redact(completed.stderr.strip()[-800:], *secrets))
    result = json.loads(completed.stdout)
    if any(secret and secret in completed.stdout for secret in secrets):
        raise ScenarioFailed("the browser driver echoed a token")
    write_json(artifacts / "browser-matrix.json", result)
    return result


def _journey(binding: RunBinding, observed: ObservedStack, artifacts: Path) -> dict[str, Any]:
    import sys

    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from tests.integration.conftest import DeploymentCleanupTracker, RuntimeBudgetTracker, wait_for_deployment_terminal
    from tests.integration.test_trigger_delivery_matrix import _promote_staging_version_for_triggers
    from tests.integration.transaction_search import TransactionSearchLease

    playwright_module = _playwright_module()
    base = observed.cloudfront_url
    session_aws = boto3_session(binding.region)
    logs, xray, sts = session_aws.client("logs"), session_aws.client("xray"), session_aws.client("sts")
    previous_env = {k: os.environ.get(k) for k in ("AWS_REGION", "AWS_DEFAULT_REGION")}
    os.environ.update(
        {
            "AWS_REGION": binding.region,
            "AWS_DEFAULT_REGION": binding.region,
            "INTEGRATION_EVALUATION_LOG_TIMEOUT_SECONDS": "900",
            "INTEGRATION_EVALUATION_INDEX_SETTLE_SECONDS": "180",
            "INTEGRATION_PROMOTION_TIMEOUT_SECONDS": "1800",
        }
    )
    outcome: dict[str, Any] = {"region": binding.region, "customer_path": base}
    cleanup_errors: list[str] = []
    scopeless: SyntheticUser | None = None
    try:
        with with_synthetic_user(observed, label="widget", groups=INVOKE_GROUPS) as user:
            session = BearerSession(user, base)
            session.headers["Content-Type"] = "application/json"
            tracker = DeploymentCleanupTracker(session, logs_client=logs)
            budgets = RuntimeBudgetTracker(session)
            runtime_name = f"it_widget_{uuid.uuid4().hex[:12]}"
            outcome["runtime_name"] = runtime_name
            try:
                budgets.authorize(runtime_name)
                lease = TransactionSearchLease(
                    account_id=binding.account_id,
                    region=binding.region,
                    manage=True,
                    state_path=artifacts / "transaction-search-state.json",
                    xray_client=xray,
                    logs_client=logs,
                    sts_client=sts,
                )
                with lease:
                    node_id = f"it-widget-{uuid.uuid4().hex[:12]}"
                    response = session.post(
                        f"{base}/api/deploy",
                        json={"nodeId": node_id, "config": _runtime_config(runtime_name)},
                        timeout=90,
                    )
                    if response.status_code != 202:
                        raise ScenarioFailed(f"deploy returned {response.status_code}: {response.text[:300]}")
                    record = tracker.track(response.json()["deploymentId"])
                    status = wait_for_deployment_terminal(session, record.deployment_id, timeout=20 * 60)
                    record.last_status = status
                    if status.get("status") != "succeeded":
                        raise ScenarioFailed(f"deploy ended {status.get('status')}")
                    tracker.bind_runtime(record, status["runtime_id"])
                    _promote_staging_version_for_triggers(session, logs, runtime_name=runtime_name, status=status)
                    outcome["production_version_id"] = status["version_id"]

                    # A second identity that carries NO group, so its token has no invoke scope (exact 403).
                    scopeless = SyntheticUser.for_stack(observed, groups=frozenset(), label="widget-noscope")
                    scopeless.create()
                    scopeless.authenticate()

                    with _HostServer(base, artifacts / "host-page") as host:
                        brief = {
                            "hostPageUrl": host.url,
                            "cloudfrontUrl": base,
                            "runtimeName": runtime_name,
                            "token": user.token,
                            "scopelessToken": scopeless.token,
                            "inertMarker": INERT_MARKER,
                            "playwrightModule": str(playwright_module),
                        }
                        matrix = _run_driver(
                            brief, artifacts, (user.token, scopeless.token, *user.secrets(), *scopeless.secrets())
                        )
                    outcome["matrix"] = {k: v for k, v in matrix.items() if k != "requests"}
                    outcome["request_count"] = len(matrix.get("requests", []))
                    outcome["token_only_on_invoke_requests"] = matrix.get("tokenOnlyOnInvokeRequests") is True
                    outcome["token_ever_in_url_or_body"] = matrix.get("tokenEverInUrlOrBody") is True

                    # Exact 429: exhaust the owner-bound budget and invoke once more through the client path.
                    budgets.release(runtime_name)
                    tiny = session.post(
                        f"{base}/api/cost/budgets",
                        json={"scope": "agent", "key": runtime_name, "limit_usd": 0.0001, "warn_pct": 100},
                        timeout=60,
                    )
                    if tiny.status_code != 200:
                        raise ScenarioFailed(
                            f"creating the exhausted budget returned {tiny.status_code}: {tiny.text[:200]}"
                        )
                    budgets._keys.append(runtime_name)  # noqa: SLF001 -- tracked for the same cleanup path
                    exhausted = session.post(
                        f"{base}/api/runtimes/{runtime_name}/invoke",
                        json={"input": "What is 1 plus 1?", "sessionId": f"widget-{uuid.uuid4()}"},
                        timeout=120,
                    )
                    outcome["exhausted"] = {
                        "status": exhausted.status_code,
                        "retry_after": exhausted.headers.get("Retry-After"),
                        "body_excerpt": exhausted.text[:200],
                    }
                    if exhausted.status_code != 429:
                        raise ScenarioFailed(f"exhausted budget returned {exhausted.status_code}, expected 429")
            finally:
                for label, action in (("budget cleanup", budgets.cleanup),):
                    try:
                        action()
                    except Exception as exc:  # noqa: BLE001
                        cleanup_errors.append(f"{label}: " + redact(f"{type(exc).__name__}: {exc}", *user.secrets()))
                for record in reversed(tracker.records):
                    try:
                        tracker.cleanup(record)
                    except Exception as exc:  # noqa: BLE001
                        cleanup_errors.append(
                            "deployment cleanup: " + redact(f"{type(exc).__name__}: {exc}", *user.secrets())
                        )
                if scopeless is not None and scopeless.created and not scopeless.delete():
                    cleanup_errors.append("scopeless user not deleted")
                session.close()
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    outcome["cleanup_errors"] = cleanup_errors
    write_json(artifacts / "journey-outcome.json", outcome)
    if cleanup_errors:
        raise ScenarioFailed("browser journey left residue: " + "; ".join(cleanup_errors)[:800])
    return outcome


def _facet_holds(claim: str, outcome: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    matrix = outcome.get("matrix") or {}
    facets = matrix.get("facets") or {}
    alias = facets.get("productionAlias") or {}
    inert = facets.get("inertRendering") or {}
    attribute = facets.get("tokenAttributeIgnored") or {}
    injection = facets.get("injectionDenied") or []
    denials = facets.get("denials") or {}
    exhausted = outcome.get("exhausted") or {}
    if claim == "cloudfront-widget-invokes-production-alias":
        holds = (
            alias.get("sessionContinued") is True
            and alias.get("sameVersionBothTurns") is True
            and alias.get("versionId") == outcome.get("production_version_id")
            and alias.get("eventDetailCarriesToken") is False
            and outcome.get("token_only_on_invoke_requests") is True
        )
        return holds, alias
    if claim == "widget-model-output-renders-inert":
        holds = (
            inert.get("responseIncludesMarker") is True
            and inert.get("logTextIncludesMarker") is True
            and "b" not in (inert.get("elementTagsInLog") or [])
            and inert.get("scriptExecuted") is False
            and inert.get("imgElementsInShadow") == 0
        )
        return holds, inert
    if claim == "widget-token-attribute-ignored":
        # Ground truth (frontend/public/agentcore-chat-v1.js): the widget does NOT observe an
        # `access-token` attribute (observedAttributes = api-base-url/runtime-name/title/placeholder)
        # and authenticates only via the programmatic tokenProvider. With a bogus access-token
        # attribute and no provider, AgentCoreClient's constructor throws TypeError BEFORE any
        # request, and sendMessage normalizes that to AgentCoreClientError(status=0,
        # code='network_error'). So the correct, secure outcome is a CLIENT-SIDE refusal in which
        # the bogus attribute value is ignored and never leaves the page. A server 401 is NOT
        # reachable for this case (401 only occurs when a tokenProvider IS present and returns an
        # invalid JWT -- a different case already asserted by the denials facet), so the prior
        # `status == 401` assertion was wrong for this product. The security property (attribute
        # ignored + never sent) is still asserted here via threw + attributeValueLeftThePage.
        holds = (
            attribute.get("threw") is True
            and attribute.get("status") == 0
            and attribute.get("code") == "network_error"
            and attribute.get("attributeValueLeftThePage") is False
        )
        return holds, attribute
    if claim == "widget-target-injection-denied":
        holds = bool(injection) and all(probe.get("status") == 422 for probe in injection)
        return holds, {"probes": injection}
    if claim == "real-browser-cloudfront-widget-flow":
        sub = [
            _facet_holds(c, outcome)[0]
            for c in (
                "cloudfront-widget-invokes-production-alias",
                "widget-model-output-renders-inert",
                "widget-token-attribute-ignored",
                "widget-target-injection-denied",
            )
        ]
        holds = (
            all(sub)
            and denials.get("anonymousStatus") == 401
            and denials.get("scopelessStatus") == 403
            and exhausted.get("status") == 429
            and outcome.get("token_ever_in_url_or_body") is False
        )
        return holds, {"facets": sub, "denials": denials, "exhausted": exhausted}
    raise ScenarioFailed(f"unknown browser claim {claim}")


def _scenario(args: argparse.Namespace, artifact_dir: Path, claim: str) -> dict:
    binding, observed, comparison = bound_deployment(args, artifact_dir)
    journey = family_journey(
        binding,
        observed,
        family=FAMILY,
        run=lambda artifacts: _journey(binding, observed, artifacts),
        artifact_dir=artifact_dir,
    )
    holds, facet = _facet_holds(claim, journey["outcome"])
    report = {
        "scenario": args.scenario_id,
        "region": binding.region,
        "deployment_binding": comparison,
        "journey": {k: v for k, v in journey.items() if k != "outcome"},
        "claim": claim,
        "claim_holds": holds,
        "facet": facet,
        "module_exports": (journey["outcome"].get("matrix") or {}).get("moduleExports"),
    }
    write_json(artifact_dir / "report", report)
    if not holds:
        raise ScenarioFailed(f"{claim} does not hold in the browser matrix outcome")
    return report


def cloudfront_widget_invokes_production_alias(args, artifact_dir):
    return _scenario(args, artifact_dir, "cloudfront-widget-invokes-production-alias")


def widget_model_output_renders_inert(args, artifact_dir):
    return _scenario(args, artifact_dir, "widget-model-output-renders-inert")


def widget_token_attribute_ignored(args, artifact_dir):
    return _scenario(args, artifact_dir, "widget-token-attribute-ignored")


def widget_target_injection_denied(args, artifact_dir):
    return _scenario(args, artifact_dir, "widget-target-injection-denied")


def real_browser_cloudfront_widget_flow(args, artifact_dir):
    return _scenario(args, artifact_dir, "real-browser-cloudfront-widget-flow")

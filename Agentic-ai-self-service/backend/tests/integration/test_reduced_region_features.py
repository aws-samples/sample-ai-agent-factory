"""Reduced-profile Regions refuse the features the reviewed matrix says they lack.

Driven by ``release/agentcore-regions.json``: in a Region where ``web_search`` is false a
Web Search template deployment must be refused synchronously (HTTP 400, the backend's own
message naming the Region and the alternatives) before any deployment row exists; where
``agent_registry`` is false the AWS Agent Registry federation surface must report itself
unavailable and search must return nothing. In a *full* Region the same calls must report
the features available -- the test is a twin, not a one-sided denial.

Runs against a live deployment (``CLOUDFRONT_URL`` + ``API_BEARER_TOKEN``) like the rest of
this directory; ``AWS_REGION`` names the Region the deployment lives in.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from app.services.region_capabilities import feature_unavailable_message

from tests.integration.test_template_deployments import TEMPLATE_CASES, _template_request_payload

REGIONS_MATRIX = Path(__file__).resolve().parents[3] / "release" / "agentcore-regions.json"


def _matrix_entry(region: str) -> dict:
    document = json.loads(REGIONS_MATRIX.read_text(encoding="utf-8"))
    for entry in document["regions"]:
        if entry["region"] == region:
            return entry
    raise AssertionError(f"{region} is not in the release Region matrix")


def _deployment_rows(api_session) -> list[dict]:
    response = api_session.get(f"{api_session.base_url}/api/deployments", timeout=30)
    response.raise_for_status()
    body = response.json()
    assert isinstance(body, list)
    return body


@pytest.mark.integration
def test_aws_agent_registry_surface_matches_the_region_matrix(api_session) -> None:
    region = os.environ["AWS_REGION"]
    entry = _matrix_entry(region)
    config = api_session.get(f"{api_session.base_url}/api/registry/aws-config", timeout=30)
    config.raise_for_status()
    body = config.json()
    assert body["region"] == region
    assert body["region_supported"] is bool(entry["agent_registry"])
    if entry["agent_registry"]:
        assert body.get("unavailable_reason") in (None, "")
        return
    expected = feature_unavailable_message("aws_agent_registry", region)
    assert body == {
        "enabled": False,
        "registry_id": None,
        "available": False,
        "sdk_supported": True,
        "status": None,
        "region": region,
        "region_supported": False,
        "unavailable_reason": expected,
    }
    search = api_session.get(
        f"{api_session.base_url}/api/registry/aws-search", params={"q": "integration probe"}, timeout=30
    )
    search.raise_for_status()
    assert search.json() == {
        "enabled": False,
        "results": [],
        "region": region,
        "region_supported": False,
        "unavailable_reason": expected,
    }


@pytest.mark.integration
def test_web_search_template_is_refused_before_persistence_where_the_matrix_says_so(api_session) -> None:
    region = os.environ["AWS_REGION"]
    entry = _matrix_entry(region)
    if entry["web_search"]:
        pytest.skip(f"{region} supports managed Web Search; the positive path is the gallery deployment test")
    case = next(item for item in TEMPLATE_CASES if item.template_id == "web-search-agent")
    payload = _template_request_payload(case, uuid.uuid4().hex[:16])
    node_id = payload["nodeId"]
    assert "targetRegion" not in payload, "the request must be refused on the DEPLOYED Region, not a request field"
    assert [row for row in _deployment_rows(api_session) if row.get("node_id") == node_id] == []
    response = api_session.post(f"{api_session.base_url}/api/deploy", json=payload, timeout=60)
    assert response.status_code in (400, 422), response.text
    assert feature_unavailable_message("managed_web_search", region) in response.text
    assert [row for row in _deployment_rows(api_session) if row.get("node_id") == node_id] == [], (
        "an unsupported Web Search request created a deployment row"
    )

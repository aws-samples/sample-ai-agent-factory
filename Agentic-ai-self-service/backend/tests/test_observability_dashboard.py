"""Phase 1 Gap 1D — observability dashboard helper unit tests."""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, "src")

from app.services.observability_dashboard import (  # noqa: E402
    build_dashboard_body,
    dashboard_console_url,
    dashboard_name_for_runtime,
    delete_dashboard_for_runtime,
    put_dashboard_for_runtime,
)


def test_dashboard_name_is_safe_and_prefixed():
    assert dashboard_name_for_runtime("my_agent_v1-XXxxYYyy") == "agentcore-my_agent_v1-XXxxYYyy"


def test_dashboard_name_replaces_unsafe_chars():
    name = dashboard_name_for_runtime("my.agent/with:badchars")
    assert "/" not in name
    assert ":" not in name
    assert "." not in name
    assert name.startswith("agentcore-")


def test_dashboard_name_truncated_to_255():
    long_id = "x" * 500
    assert len(dashboard_name_for_runtime(long_id)) <= 255


def test_console_url_includes_region_and_dashboard():
    # Parse the URL and assert on its components (host/scheme/query) rather than
    # substring-matching the host in the raw string — a substring check on an
    # unparsed URL is the py/incomplete-url-substring-sanitization anti-pattern
    # (a host could appear at an arbitrary position), so we verify the netloc
    # exactly via urlparse.
    from urllib.parse import urlparse, parse_qs

    url = dashboard_console_url("us-east-1", "agentcore-myagent")
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "us-east-1.console.aws.amazon.com"
    assert parse_qs(parsed.query).get("region") == ["us-east-1"]
    assert "agentcore-myagent" in parsed.fragment


def test_dashboard_body_is_valid_json():
    body = build_dashboard_body(
        runtime_id="myagent_v1-AbCdEfGh01",
        runtime_name="myagent_v1",
        region="us-east-1",
    )
    parsed = json.loads(body)
    assert "widgets" in parsed
    assert isinstance(parsed["widgets"], list)
    assert len(parsed["widgets"]) >= 5  # header + ≥4 data widgets


def test_dashboard_body_includes_eval_widget_when_log_group_present():
    body = build_dashboard_body(
        runtime_id="myagent_v1-AbCdEfGh01",
        runtime_name="myagent_v1",
        region="us-east-1",
        eval_log_group_name="/aws/bedrock-agentcore/evaluations/results/eval_X",
    )
    parsed = json.loads(body)
    titles = [
        w["properties"].get("title", "")
        for w in parsed["widgets"]
        if w.get("type") == "log"
    ]
    assert any("Evaluator" in t for t in titles)


def test_dashboard_body_omits_eval_widget_by_default():
    body = build_dashboard_body(
        runtime_id="myagent_v1-AbCdEfGh01",
        runtime_name="myagent_v1",
        region="us-east-1",
    )
    parsed = json.loads(body)
    titles = [
        w["properties"].get("title", "")
        for w in parsed["widgets"]
        if w.get("type") == "log"
    ]
    assert not any("Evaluator" in t for t in titles)


def test_put_dashboard_calls_cloudwatch():
    with patch(
        "app.services.observability_dashboard.boto3.client"
    ) as boto_mock:
        cw = MagicMock()
        boto_mock.return_value = cw
        name, url = put_dashboard_for_runtime(
            runtime_id="myagent_v1-AbCdEfGh01",
            runtime_name="myagent_v1",
            region="us-east-1",
        )
    assert name == "agentcore-myagent_v1-AbCdEfGh01"
    from urllib.parse import urlparse

    parsed_url = urlparse(url)
    assert parsed_url.netloc == "us-east-1.console.aws.amazon.com"
    assert parsed_url.path == "/cloudwatch/home"
    cw.put_dashboard.assert_called_once()
    args = cw.put_dashboard.call_args.kwargs
    assert args["DashboardName"] == name
    parsed_body = json.loads(args["DashboardBody"])
    assert "widgets" in parsed_body


def test_delete_dashboard_swallows_not_found():
    with patch(
        "app.services.observability_dashboard.boto3.client"
    ) as boto_mock:
        cw = MagicMock()
        cw.delete_dashboards.side_effect = Exception(
            "DashboardNotFoundError: dashboard does not exist"
        )
        boto_mock.return_value = cw
        ok = delete_dashboard_for_runtime("myagent_v1-AbCdEfGh01", "us-east-1")
    assert ok is True


def test_delete_dashboard_returns_false_on_other_error():
    with patch(
        "app.services.observability_dashboard.boto3.client"
    ) as boto_mock:
        cw = MagicMock()
        cw.delete_dashboards.side_effect = Exception("ThrottlingException")
        boto_mock.return_value = cw
        ok = delete_dashboard_for_runtime("myagent_v1-AbCdEfGh01", "us-east-1")
    assert ok is False

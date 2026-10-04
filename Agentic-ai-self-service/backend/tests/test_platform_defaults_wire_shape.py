"""The platform-defaults wire shape the UI parses, pinned on the producer's side.

Measured live 2026-10-02 (acfe2e-p0925, OTEL not configured): the route answered
``{"enabled": false, "endpoint": null, "sample_rate": null, "service_name_prefix": null}``. The UI's
validator accepted an optional field only when it was absent, so it read those nulls as an unreadable
admin policy and kept Save disabled on the Runtime and Observability configuration modals: no runtime
could be configured from the palette on a platform without OTEL. The frontend test
``frontend/src/hooks/usePlatformObservabilityPolicy.test.ts`` parses these exact bytes; this test keeps
them the bytes the route actually sends.
"""

from __future__ import annotations

import json

import app.services.observability as observability
from app.main import app
from fastapi.testclient import TestClient

LIVE_DISABLED = '{"enabled":false,"endpoint":null,"sample_rate":null,"service_name_prefix":null}'


def _get(monkeypatch, defaults):
    monkeypatch.setattr(observability, "get_platform_observability_defaults_lenient", lambda: defaults)
    return TestClient(app).get("/api/observability/platform-defaults")


def test_the_disabled_response_is_the_bytes_the_ui_test_parses(monkeypatch):
    response = _get(monkeypatch, None)

    assert response.status_code == 200
    assert response.json() == json.loads(LIVE_DISABLED)


def test_an_enabled_policy_never_carries_the_auth_secret(monkeypatch):
    response = _get(
        monkeypatch,
        {
            "otlp_endpoint": "https://otel.example.invalid/v1/traces",
            "sample_rate": 0.25,
            "service_name_prefix": "acf",
            "auth_header_secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:otel-AbCdEf",
        },
    )

    assert response.json() == {
        "enabled": True,
        "endpoint": "https://otel.example.invalid/v1/traces",
        "sample_rate": 0.25,
        "service_name_prefix": "acf",
    }
    assert "secretsmanager" not in response.text

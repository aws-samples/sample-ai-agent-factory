"""The OTEL verification script's documented defaults must be executable."""

import importlib.util
import urllib.request
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify-otel.py"
_SPEC = importlib.util.spec_from_file_location("verify_otel_script", _SCRIPT)
assert _SPEC and _SPEC.loader
verify_otel = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify_otel)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/api/test-runtime",
        "http://127.0.0.1:6006/v1/spans",
        "http://[::1]:6006/v1/spans",
        "https://cloud.langfuse.com/api/public/traces",
    ],
)
def test_safe_open_accepts_https_and_only_loopback_http(monkeypatch, url):
    sentinel = object()
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: sentinel)
    assert verify_otel._safe_open(urllib.request.Request(url), timeout=3) is sentinel


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/api",
        "http://localhost.evil.example/api",
        "ftp://localhost/file",
        "http://user:secret@localhost:8000/api",
        "https://user:secret@example.com/api",
    ],
)
def test_safe_open_rejects_remote_plaintext_spoofs_and_embedded_credentials(monkeypatch, url):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: pytest.fail("urlopen must not run for a rejected URL"),
    )
    with pytest.raises(ValueError):
        verify_otel._safe_open(urllib.request.Request(url), timeout=3)

"""Offline contract tests for ``load_rate_limit_probe.py`` (no AWS calls)."""
from __future__ import annotations

import load_rate_limit_probe as p


def test_classify_429_counts_and_first_index() -> None:
    summary = p.classify_429([{"status": s} for s in (200, 200, 429, 200, 503)])
    assert summary["admitted"] == 3
    assert summary["throttled"] == 1
    assert summary["first429Index"] == 2
    assert summary["otherCodes"] == [503]


def test_classify_429_without_throttles_reports_none() -> None:
    summary = p.classify_429([{"status": 200}] * 3)
    assert summary["throttled"] == 0
    assert summary["first429Index"] is None
    assert summary["otherCodes"] == []


def test_inference_base_derives_sibling_path_and_rejects_non_mcp_url() -> None:
    assert p.inference_base("https://gw.example/mcp") == "https://gw.example/inference/v1"
    assert p.inference_base("https://gw.example/mcp/") == "https://gw.example/inference/v1"
    try:
        p.inference_base("https://gw.example/other")
    except SystemExit as exc:
        assert "must end with /mcp" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("non-/mcp url must be refused")


def test_bearer_refreshes_only_after_max_age(monkeypatch) -> None:
    mints = {"n": 0}

    def fake_mint(secret_arn: str, region: str) -> str:
        mints["n"] += 1
        return f"token-{mints['n']}"

    clock = {"t": 1000.0}
    monkeypatch.setattr(p, "mint_bearer", fake_mint)
    monkeypatch.setattr(p.time, "monotonic", lambda: clock["t"])
    bearer = p.Bearer("arn", "us-west-2", max_age=180.0)
    assert bearer.get() == "token-1"
    clock["t"] += 100
    assert bearer.get() == "token-1"  # still fresh
    clock["t"] += 100
    assert bearer.get() == "token-2"  # re-minted past 180 s
    assert bearer.mints == 2


def test_fingerprint_never_echoes_input() -> None:
    assert "secret" not in p.fingerprint("secret")
    assert len(p.fingerprint("x")) == 16

"""The Cognito auth domain wait asks the zone's authorities, not a resolver that may have cached a miss.

Live, 2026-09-29 (matrix run 17): Cognito reported the new hosted domain ACTIVE within seconds and the
MCP step then polled ``socket.getaddrinfo`` 180 times over nine minutes without one success, while the
record was published -- the Lambda's resolver had cached the first NXDOMAIN for the zone's 900 s
negative TTL (SOA minimum of auth.us-east-1.amazoncognito.com). The same redeploy had passed four
times that day, so the poison window is a race, not a constant. These tests pin the verdict logic of
``authoritative_dns`` with faked wire answers and the step's behaviour on each verdict.
"""

from __future__ import annotations

import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "src")

from app.services import authoritative_dns as ad  # noqa: E402
from app.step_handlers import mcp_server_step  # noqa: E402

NS = ["198.51.100.1", "198.51.100.2", "198.51.100.3", "198.51.100.4"]


def _reply(rcode, answers=()):
    return {"rcode": rcode, "authoritative": True, "answers": list(answers)}


def _fake_query(per_server):
    def _q(server, name, qtype, *, recursion, timeout=3.0):
        assert recursion is False, "the hostname must never be asked recursively"
        outcome = per_server[server]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return _q


def test_every_authority_answering_with_data_is_present(monkeypatch):
    monkeypatch.setattr(ad, "dns_query", _fake_query({s: _reply(0, [(ad.QTYPE_A, "3.94.32.147")]) for s in NS}))
    assert (
        ad.authoritative_answer(
            "x.auth.us-east-1.amazoncognito.com", "auth.us-east-1.amazoncognito.com", nameservers=NS
        )
        == ad.PRESENT
    )


def test_one_nxdomain_makes_the_name_absent_even_if_others_have_it(monkeypatch):
    answers = {s: _reply(0, [(ad.QTYPE_A, "3.94.32.147")]) for s in NS}
    answers[NS[2]] = _reply(ad.RCODE_NXDOMAIN)
    monkeypatch.setattr(ad, "dns_query", _fake_query(answers))
    assert (
        ad.authoritative_answer(
            "x.auth.us-east-1.amazoncognito.com", "auth.us-east-1.amazoncognito.com", nameservers=NS
        )
        == ad.ABSENT
    )


def test_noerror_with_no_data_is_absent(monkeypatch):
    monkeypatch.setattr(ad, "dns_query", _fake_query({s: _reply(0, []) for s in NS}))
    assert (
        ad.authoritative_answer(
            "x.auth.us-east-1.amazoncognito.com", "auth.us-east-1.amazoncognito.com", nameservers=NS
        )
        == ad.ABSENT
    )


def test_unreachable_authorities_are_unknown_not_absent(monkeypatch):
    monkeypatch.setattr(ad, "dns_query", _fake_query({s: OSError("timed out") for s in NS}))
    assert (
        ad.authoritative_answer(
            "x.auth.us-east-1.amazoncognito.com", "auth.us-east-1.amazoncognito.com", nameservers=NS
        )
        == ad.UNKNOWN
    )


def test_a_servfail_from_one_authority_does_not_decide(monkeypatch):
    answers = {s: _reply(0, [(ad.QTYPE_A, "3.94.32.147")]) for s in NS}
    answers[NS[0]] = _reply(2)  # SERVFAIL
    monkeypatch.setattr(ad, "dns_query", _fake_query(answers))
    assert (
        ad.authoritative_answer(
            "x.auth.us-east-1.amazoncognito.com", "auth.us-east-1.amazoncognito.com", nameservers=NS
        )
        == ad.PRESENT
    )


def test_no_nameservers_at_all_is_unknown(monkeypatch):
    monkeypatch.setattr(ad, "zone_nameserver_addresses", lambda *a, **k: [])
    assert (
        ad.authoritative_answer("x.auth.us-east-1.amazoncognito.com", "auth.us-east-1.amazoncognito.com") == ad.UNKNOWN
    )


def test_the_wire_format_round_trips_a_query_and_parses_an_answer():
    qid, packet = ad._build_query("x.auth.us-east-1.amazoncognito.com", ad.QTYPE_A, recursion=False)
    assert packet[2:4] == b"\x00\x00", "recursion desired must be off for authoritative questions"
    assert packet.endswith(b"\x00\x00\x01\x00\x01")
    # a minimal authoritative NOERROR response with one A record, compression pointer to the question
    header = packet[:2] + b"\x84\x00" + b"\x00\x01\x00\x01\x00\x00\x00\x00"
    question = packet[12:]
    answer = b"\xc0\x0c" + b"\x00\x01\x00\x01" + b"\x00\x00\x00\x3c" + b"\x00\x04" + bytes([3, 94, 32, 147])
    data = header + question + answer
    off = 12
    _, off = ad._read_name(data, off)
    assert data[off : off + 4] == b"\x00\x01\x00\x01"
    name, _ = ad._read_name(data, off + 4)
    assert name == "x.auth.us-east-1.amazoncognito.com"


# --- the step's use of the verdicts -------------------------------------------------------------


def _cognito_active():
    c = MagicMock()
    c.describe_user_pool_domain.return_value = {"DomainDescription": {"Status": "ACTIVE"}}
    return c


def test_the_step_waits_while_absent_and_returns_once_present(monkeypatch):
    verdicts = iter([ad.ABSENT, ad.ABSENT, ad.PRESENT])
    asked = []
    monkeypatch.setattr(
        mcp_server_step.authoritative_dns,
        "authoritative_answer",
        lambda host, zone, **k: (asked.append((host, zone)), next(verdicts))[1],
    )
    monkeypatch.setattr(mcp_server_step.time, "sleep", lambda s: None)
    mcp_server_step._wait_for_cognito_domain(
        _cognito_active(), "ac-mcp-x", "us-east-1", deadline_monotonic=time.monotonic() + 600
    )
    assert len(asked) == 3
    assert asked[0] == ("ac-mcp-x.auth.us-east-1.amazoncognito.com", "auth.us-east-1.amazoncognito.com")


def test_the_step_never_consults_the_local_resolver(monkeypatch):
    monkeypatch.setattr(mcp_server_step.authoritative_dns, "authoritative_answer", lambda *a, **k: ad.PRESENT)
    import socket

    def _boom(*a, **k):
        raise AssertionError("getaddrinfo must not be called: its negative cache outlives the deadline")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    monkeypatch.setattr(mcp_server_step.time, "sleep", lambda s: None)
    mcp_server_step._wait_for_cognito_domain(
        _cognito_active(), "ac-mcp-x", "us-east-1", deadline_monotonic=time.monotonic() + 600
    )


def test_unknown_verdicts_fall_back_to_active_plus_settle_not_before(monkeypatch):
    monkeypatch.setattr(mcp_server_step.authoritative_dns, "authoritative_answer", lambda *a, **k: ad.UNKNOWN)
    clock = {"t": 1000.0}
    monkeypatch.setattr(mcp_server_step.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(mcp_server_step.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    waited = mcp_server_step._wait_for_cognito_domain(
        _cognito_active(), "ac-mcp-x", "us-east-1", deadline_monotonic=1000.0 + 600
    )
    # Pinned as a literal, not the constant: a settle of 0 would make the fallback accept a domain the
    # instant Cognito says ACTIVE, which is exactly the state the service could not resolve live.
    assert mcp_server_step._DOMAIN_SETTLE_SECONDS_WHEN_UNVERIFIABLE >= 60.0
    assert 60.0 <= waited < 200.0


def test_absent_until_the_deadline_is_a_deployment_failure(monkeypatch):
    monkeypatch.setattr(mcp_server_step.authoritative_dns, "authoritative_answer", lambda *a, **k: ad.ABSENT)
    monkeypatch.setattr(mcp_server_step.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError) as error:
        mcp_server_step._wait_for_cognito_domain(
            _cognito_active(), "ac-mcp-x", "us-east-1", deadline_monotonic=time.monotonic() + 4.0
        )
    assert "did not become ACTIVE and resolvable" in str(error.value)

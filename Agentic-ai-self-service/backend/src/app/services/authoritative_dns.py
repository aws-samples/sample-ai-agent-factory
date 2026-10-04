"""Ask a zone's authoritative nameservers whether a hostname exists, bypassing every cache.

Why this exists (live, 2026-09-29, matrix run 17): the MCP step created a Cognito hosted auth
domain, Cognito reported it ACTIVE within seconds, and the step then polled
``socket.getaddrinfo`` every 3 s for nine minutes without a single success, although the record
was published. The first lookup ran before the record existed; the Lambda's resolver cached that
NXDOMAIN for the zone's negative TTL -- ``auth.<region>.amazoncognito.com`` has an SOA minimum of
900 s -- which is longer than the step's whole readiness budget. Polling a poisoned resolver can
never recover; only a resolver that has never seen the miss can. The zone's own nameservers keep
no negative cache, so an answer from them is the ground truth every other resolver will converge
to, and a query to them is what the AgentCore service's resolvers will see once *they* look.

Pure standard library (the step bundle ships no DNS library). UDP only, one question, no EDNS.
Recursion is used exactly once, to learn the zone's NS names through the system resolver
(positive answers are safe to cache); the hostname itself is only ever asked of the authorities.
"""

from __future__ import annotations

import logging
import pathlib
import secrets
import socket
import struct

logger = logging.getLogger(__name__)

QTYPE_A = 1
QTYPE_NS = 2
QTYPE_CNAME = 5
RCODE_NOERROR = 0
RCODE_NXDOMAIN = 3

#: Verdicts of :func:`authoritative_answer`.
PRESENT = "present"  # every authority that answered says the name exists
ABSENT = "absent"  # at least one authority says NXDOMAIN (or answered with nothing)
UNKNOWN = "unknown"  # no authority could be reached; the caller must not treat this as either


def _build_query(name: str, qtype: int, *, recursion: bool) -> tuple[int, bytes]:
    qid = secrets.randbelow(65536)
    flags = 0x0100 if recursion else 0x0000
    labels = [label for label in name.rstrip(".").split(".") if label]
    if any(len(label) > 63 for label in labels) or not labels:
        raise ValueError(f"not a valid DNS name: {name!r}")
    qname = b"".join(bytes([len(label)]) + label.encode("ascii") for label in labels) + b"\x00"
    return qid, struct.pack("!HHHHHH", qid, flags, 1, 0, 0, 0) + qname + struct.pack("!HH", qtype, 1)


def _read_name(buf: bytes, off: int) -> tuple[str, int]:
    labels: list[str] = []
    end: int | None = None
    for _ in range(128):
        length = buf[off]
        if length == 0:
            off += 1
            break
        if length & 0xC0 == 0xC0:
            pointer = struct.unpack("!H", buf[off : off + 2])[0] & 0x3FFF
            if end is None:
                end = off + 2
            off = pointer
            continue
        labels.append(buf[off + 1 : off + 1 + length].decode("ascii", errors="replace"))
        off += 1 + length
    else:
        raise ValueError("DNS name too long or looping compression pointer")
    return ".".join(labels), (end if end is not None else off)


def dns_query(server: str, name: str, qtype: int, *, recursion: bool, timeout: float = 3.0) -> dict:
    """One UDP question to ``server``. Returns {rcode, authoritative, answers:[(type, value)]}.

    Raises ``OSError`` (timeout, unreachable) so the caller can tell "no answer" from "answered no".
    """
    qid, packet = _build_query(name, qtype, recursion=recursion)
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (server, 53))
        data, _peer = sock.recvfrom(4096)
    finally:
        sock.close()
    if len(data) < 12:
        raise OSError("short DNS response")
    rid, flags, qdcount, ancount, _ns, _ar = struct.unpack("!HHHHHH", data[:12])
    if rid != qid:
        raise OSError("DNS response id mismatch")
    off = 12
    for _ in range(qdcount):
        _, off = _read_name(data, off)
        off += 4
    answers: list[tuple[int, str]] = []
    for _ in range(ancount):
        _, off = _read_name(data, off)
        rtype, _rclass, _ttl, rdlength = struct.unpack("!HHIH", data[off : off + 10])
        off += 10
        if rtype in (QTYPE_NS, QTYPE_CNAME):
            target, _ = _read_name(data, off)
            answers.append((rtype, target))
        elif rtype == QTYPE_A and rdlength == 4:
            answers.append((rtype, ".".join(str(b) for b in data[off : off + 4])))
        else:
            answers.append((rtype, f"<{rdlength} bytes>"))
        off += rdlength
    return {"rcode": flags & 0xF, "authoritative": bool(flags & 0x0400), "answers": answers}


def system_resolvers(resolv_conf: str = "/etc/resolv.conf") -> list[str]:
    try:
        lines = pathlib.Path(resolv_conf).read_text().splitlines()
    except OSError:
        return []
    out: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            out.append(parts[1].split("%")[0])
    return out


def zone_nameserver_addresses(zone: str, *, resolvers: list[str] | None = None, timeout: float = 3.0) -> list[str]:
    """IPv4 addresses of the zone's NS hosts, learned through the system resolver (positive data only)."""
    for resolver in resolvers if resolvers is not None else system_resolvers():
        try:
            reply = dns_query(resolver, zone, QTYPE_NS, recursion=True, timeout=timeout)
        except (OSError, ValueError) as exc:
            logger.warning("NS lookup for %s via %s failed: %s", zone, resolver, type(exc).__name__)
            continue
        names = [value for rtype, value in reply["answers"] if rtype == QTYPE_NS]
        if not names:
            continue
        addresses: list[str] = []
        for host in names:
            try:
                infos = socket.getaddrinfo(host, 53, socket.AF_INET, socket.SOCK_DGRAM)
            except OSError:
                continue
            if infos:
                addresses.append(infos[0][4][0])
        if addresses:
            return addresses
    return []


def authoritative_answer(host: str, zone: str, *, nameservers: list[str] | None = None, timeout: float = 3.0) -> str:
    """PRESENT when every reachable authority answers NOERROR with data, ABSENT when any says
    NXDOMAIN or answers empty, UNKNOWN when none could be reached. Never consults a recursive
    resolver for ``host`` itself, so a poisoned negative cache cannot influence the verdict."""
    servers = nameservers if nameservers is not None else zone_nameserver_addresses(zone, timeout=timeout)
    if not servers:
        return UNKNOWN
    reached = 0
    for server in servers:
        try:
            reply = dns_query(server, host, QTYPE_A, recursion=False, timeout=timeout)
        except (OSError, ValueError) as exc:
            logger.warning("authoritative query to %s for %s failed: %s", server, host, type(exc).__name__)
            continue
        reached += 1
        if reply["rcode"] == RCODE_NXDOMAIN or (reply["rcode"] == RCODE_NOERROR and not reply["answers"]):
            return ABSENT
        if reply["rcode"] != RCODE_NOERROR:
            # SERVFAIL/REFUSED from one authority: not evidence either way; keep asking the others.
            reached -= 1
            continue
    return PRESENT if reached else UNKNOWN

#!/usr/bin/env python3
"""Does every container image the workshop pins actually exist in its registry?

An unresolvable tag is invisible to cfn-lint and checkov — it only
surfaces as `CannotPullContainerError` after ECS has already been created, which
is 20+ minutes into a deploy. This checks every pin up front, anonymously.

Usage: verify_images.py <repo-root>
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])
ACCEPT = ",".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def curl(url, *args):
    p = subprocess.run(["curl", "-s", "-m", "40", *args, url],
                       capture_output=True, text=True)
    return p.stdout


def status(url, bearer=None):
    args = ["-o", "/dev/null", "-w", "%{http_code}", "-H", f"Accept: {ACCEPT}"]
    if bearer:
        args += ["-H", f"Authorization: Bearer {bearer}"]
    out = curl(url, *args).strip()
    return int(out) if out.isdigit() else 0


def resolve(ref):
    """ref -> (host, repo, tag), applying Docker Hub's implicit defaults."""
    name, _, tag = ref.rpartition(":")
    first = name.split("/")[0]
    if "." in first or ":" in first:
        host, _, repo = name.partition("/")
    else:
        host, repo = "registry-1.docker.io", name
    if host == "registry-1.docker.io" and "/" not in repo:
        repo = f"library/{repo}"                    # postgres -> library/postgres
    return host, repo, tag or "latest"


def exists(ref):
    host, repo, tag = resolve(ref)
    url = f"https://{host}/v2/{repo}/manifests/{tag}"
    st = status(url)
    if st in (401, 403):
        # Follow the registry's own auth challenge instead of hardcoding realms.
        hdrs = curl(url, "-D", "-", "-o", "/dev/null")
        m = re.search(r'realm="([^"]+)"', hdrs, re.I)
        s = re.search(r'service="([^"]+)"', hdrs, re.I)
        if not m:
            return st, "401 with no parsable auth challenge"
        realm, svc = m.group(1), (s.group(1) if s else host)
        sep = "&" if "?" in realm else "?"
        body = curl(f"{realm}{sep}service={svc}&scope=repository:{repo}:pull")
        try:
            d = json.loads(body)
        except ValueError:
            return st, "auth endpoint returned non-JSON"
        tok = d.get("token") or d.get("access_token")
        if not tok:
            return st, "no anonymous pull token"
        st = status(url, tok)
    return st, ""


# Collect pins: literal `Image: repo:tag`, plus !Sub images whose tag comes from a
# parameter Default in the same template.
refs = {}
for y in sorted((ROOT / "static" / "cfn").rglob("*.yaml")):
    text = y.read_text(encoding="utf-8")
    defaults = dict(re.findall(
        r"^  (\w+):\n(?:.*\n)*?    Default: '?([^'\n]+?)'?\s*$", text, re.M))
    for m in re.finditer(r"Image:\s*'?([A-Za-z0-9][^'\s$]*:[\w.\-]+)'?\s*$", text, re.M):
        refs.setdefault(m.group(1), set()).add(y.name)
    for m in re.finditer(r"Image:\s*!Sub\s*'([^']+)'", text):
        sub = re.sub(r"\$\{(\w+)\}", lambda k: defaults.get(k.group(1), "?"), m.group(1))
        if "?" not in sub and "${" not in sub:
            refs.setdefault(sub, set()).add(y.name)

print(f"{len(refs)} distinct pinned image(s) found\n")
bad = []
for ref in sorted(refs):
    st, note = exists(ref)
    if st != 200:
        bad.append((ref, st, note))
    print(f"  [{'OK ' if st == 200 else 'BAD'}] HTTP {st:<3} {ref}"
          + (f"   {note}" if note else ""))
print(f"\nunresolvable pins: {len(bad)}")
for ref, st, note in bad:
    print(f"  BAD {ref}  (HTTP {st}) {note}")
sys.exit(1 if bad else 0)

"""A green test suite on a different dependency version than the container is not evidence.

`scripts/install-agentcore-deps.sh` floated every version in the AgentCore Runtime
dependency bundles, and `backend/pyproject.toml`'s `dev` extra declared neither
`strands-agents` nor any OpenTelemetry package at all -- while
`tests/test_observability.py` imports strands and drives its real `Tracer`, with no skip
guard. So that test was red in any fresh environment and green only on a machine that
happened to have the packages, at whatever version it happened to have them.

Three consequences were measured, not reasoned about:

1. **A signature that differs between the two.** `Tracer.end_model_invoke_span` is
   ``(span, message, usage, stop_reason, error=None)`` in strands-agents 1.9.1, which is
   what the test interpreter here resolved, and ``(span, message, usage, metrics,
   stop_reason)`` in 1.56.0, which is what the built bundle carries. Instrumentation
   written against either one raises `TypeError` against the other, in the deployed
   container, with no source diff -- a bundle rebuild alone is enough.

2. **The cost ledger's field names are owned by OpenTelemetry.** The graph drifted
   1.37.0 -> 1.44.0 and semantic-conventions 0.58b0 -> 0.65b0 between the two
   environments. `cost_tracking.py` parses `gen_ai.usage.input_tokens` and
   `gen_ai.usage.output_tokens` out of the usage log with a CloudWatch Insights query; a
   renamed attribute is a silently empty cost report rather than an error.

3. **`mcp<2` was applied to three of eleven bundles.** That pin exists because the
   generators emit the mcp 1.x API and 2.x renamed both entry points with no alias
   (see `test_mcp_pin_matches_codegen.py`). It was passed to base, strands-mcp and
   mcp-lean -- and NOT to the eight `provider-<extra>.zip` deltas, which resolve mcp
   transitively through ``strands-agents[<extra>]``. Every one of the eight therefore
   carried **mcp 2.1.1**: 62 files on paths that do not exist in 1.30.0, so the delta
   subtraction removed none of them, plus a second `mcp-2.1.1.dist-info` beside the
   pinned `mcp-1.30.0.dist-info`. Measured in `provider-openai.zip`. The merged container
   tree gets mcp 1.30.0 complete, 62 orphan 2.x modules whose siblings are 1.x, and two
   dist-info directories -- which makes `importlib.metadata.version("mcp")` a coin flip.
   It also dragged mcp 2.x's own `httpx2`/`httpcore2`/`mcp_types` dependencies in, and
   `provider-litellm.zip` is 26 MB against `provider-anthropic.zip`'s 2 MB, spent out of
   AgentCore's hard 30-second cold-start budget.

The fix is one pip constraint file passed to EVERY bundle build and mirrored into the
`dev` extra, so there is a single place to bump and one place for these tests to read.

**What each test here is for**, since the point is to be non-vacuous rather than to
restate the file:

- `test_every_pin_is_exact` and `test_the_required_pins_are_all_present` make the rest
  non-vacuous. Every assertion below is over "the packages named in both files", so
  deleting a line from either would otherwise make the comparison trivially true.
- `test_the_dev_extra_and_the_bundle_agree` is the drift check the whole file exists for.
- `test_the_interpreter_running_these_tests_matches_the_pin` is the one that fails on a
  stale machine, and is therefore the one that would have caught the strands signature
  drift. It must never become a skip: an absent package is now a declared-dependency
  violation, and `test_observability.py` is meaningless without it.
- `test_no_bundle_build_escapes_the_constraint_file` is the regression for consequence 3.
  It reads the SCRIPT rather than the artifacts, so it runs in CI where the bundles are
  gitignored.
- `test_no_provider_delta_carries_a_distribution_the_baseline_already_has` is the general
  form of consequence 3, and it is the one that matters most. `mcp` was not the only
  instance: `provider-gemini.zip` also carried `websockets` 16.1.1 over the baseline's
  17.1, a DOWNGRADE, because google-genai caps websockets lower than strands does. Any
  distribution shared between a delta and the baseline can do this, so the invariant is
  stated over all of them rather than over the two that were caught.
- `test_no_built_bundle_carries_an_unpinned_mcp` keeps the named check for `mcp`, which
  the invariant above does not cover for the baseline bundles themselves.
- `test_the_merged_tree_satisfies_every_requirement_its_own_metadata_declares` checks the
  UNION a container imports rather than one zip at a time, including all eight deltas
  together. Its first draft was VACUOUS -- it merged versions into a `dict`, so a delta's
  version overwrote the baseline's and it reported zero violations over artifacts known to
  be broken. It now checks every version a name is present at, because which dist-info
  `importlib.metadata` reads is filesystem enumeration order.
- `test_no_two_provider_deltas_disagree_about_a_distribution` and
  `test_shared_paths_across_provider_deltas_are_byte_identical` close provider-vs-provider
  drift, which the baseline comparison structurally cannot see. Reachable because
  `provider_bundle_keys_for` returns a LIST and `_create_code_zip` merges the extras with
  one dedupe set, first path wins -- so a multi-provider canvas merges several deltas and
  an ordering derived from the canvas decides which bytes win.
- The artifact-reading tests skip when the bundles have not been built -- and only then. A
  partially built directory fails instead of skipping; see `built_bundles`.
"""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import re
import zipfile
from pathlib import Path

import pytest
import tomllib

_REPO_ROOT = Path(__file__).resolve().parents[2]
CONSTRAINTS = _REPO_ROOT / "backend" / "agentcore-deps-constraints.txt"
PYPROJECT = _REPO_ROOT / "backend" / "pyproject.toml"
BUILD_SCRIPT = _REPO_ROOT / "scripts" / "install-agentcore-deps.sh"
DEPS_DIR = _REPO_ROOT / "backend" / "agentcore-deps"

#: Packages that MUST be pinned in the constraint file. Not "everything currently in it":
#: this is the set whose version demonstrably changes behaviour the tests assert on -- the
#: framework whose signature moved, and the OpenTelemetry packages that own the span
#: attribute names the cost ledger parses. A pin removed from here is the defect coming
#: back, so the list is asserted rather than derived.
REQUIRED_PINS = frozenset(
    {
        "strands-agents",
        "strands-agents-tools",
        "bedrock-agentcore",
        "mcp",
        "opentelemetry-api",
        "opentelemetry-sdk",
        "opentelemetry-semantic-conventions",
        "opentelemetry-exporter-otlp-proto-http",
    }
)

#: Pinned packages the TEST environment must also have at the pinned version, because a
#: test imports them directly. Deliberately a subset of REQUIRED_PINS: `mcp` and
#: `bedrock-agentcore` are imported only by generated code that runs in the container, and
#: `strands-agents-tools` only by generated code, so forcing them into every developer's
#: environment would buy nothing. `strands-agents` and the OpenTelemetry trio are driven
#: for real by tests/test_observability.py.
MUST_MATCH_LOCALLY = frozenset(
    {
        "strands-agents",
        "opentelemetry-api",
        "opentelemetry-sdk",
        "opentelemetry-semantic-conventions",
        "opentelemetry-exporter-otlp-proto-http",
    }
)


def _normalize(name: str) -> str:
    """PEP 503 normalization. `strands_agents` and `strands-agents` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


@pytest.fixture(scope="module")
def constraint_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in CONSTRAINTS.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, version = line.partition("==")
        # Anything that is not a bare `name==version` is rejected rather than ignored --
        # a `>=` line silently reintroduces the float this file exists to remove.
        assert version and "=" not in name, f"not an exact pin in {CONSTRAINTS.name}: {raw!r}"
        pins[_normalize(name)] = version.strip()
    return pins


@pytest.fixture(scope="module")
def dev_extra_pins() -> dict[str, str]:
    data = tomllib.loads(PYPROJECT.read_text())
    out: dict[str, str] = {}
    for spec in data["project"]["optional-dependencies"]["dev"]:
        name, sep, version = spec.partition("==")
        if not sep:
            continue
        out[_normalize(name)] = version.strip()
    return out


@pytest.fixture(scope="module")
def build_script() -> str:
    return BUILD_SCRIPT.read_text()


def test_every_pin_is_exact(constraint_pins: dict[str, str]) -> None:
    """Non-vacuity for the parser, and a floor on how much the file actually pins.

    The fixture rejects a non-`==` line, so this asserts the file was not emptied down to
    a couple of entries -- every other test here compares intersections, and an almost
    empty constraint file would make all of them pass.
    """
    assert len(constraint_pins) >= 12, (
        f"{CONSTRAINTS.name} declares only {len(constraint_pins)} pins: "
        f"{sorted(constraint_pins)}. The OpenTelemetry graph alone is 8 packages; a file "
        "this short means transitive resolutions are floating again."
    )


def test_the_required_pins_are_all_present(constraint_pins: dict[str, str]) -> None:
    """The framework and the telemetry graph are pinned, by name."""
    missing = sorted(REQUIRED_PINS - set(constraint_pins))
    assert not missing, (
        f"these packages must be pinned in {CONSTRAINTS.name} and are not: {missing}. "
        "Each one's version changes behaviour a test in this suite asserts on -- a "
        "signature, or the span attribute names the cost ledger parses."
    )


def test_the_dev_extra_and_the_bundle_agree(constraint_pins: dict[str, str], dev_extra_pins: dict[str, str]) -> None:
    """The drift check. A version in both files must be the same version in both.

    This is the whole point of the file: the test environment has to exercise the import
    surface the container gets. It is deliberately a check over the INTERSECTION -- the
    `dev` extra does not need to install `mcp` or the runtime harness, which only
    generated code imports -- and `test_the_dev_extra_declares_what_the_tests_import`
    below is what stops that intersection from shrinking to nothing.
    """
    shared = sorted(set(constraint_pins) & set(dev_extra_pins))
    disagree = {
        name: (constraint_pins[name], dev_extra_pins[name])
        for name in shared
        if constraint_pins[name] != dev_extra_pins[name]
    }
    assert not disagree, (
        "these packages are pinned to different versions in the runtime bundle and in the "
        f"test environment (name: bundle, dev extra): {disagree}. A test passing against "
        "one version says nothing about a container running the other -- that is exactly "
        "how the strands end_model_invoke_span signature change reached production."
    )


def test_the_dev_extra_declares_what_the_tests_import(dev_extra_pins: dict[str, str]) -> None:
    """Without this, the agreement check above can be satisfied by declaring nothing.

    Every name here is imported by a test with no skip guard, so an omission is a red
    suite in a fresh environment rather than a missing nicety.
    """
    missing = sorted(MUST_MATCH_LOCALLY - set(dev_extra_pins))
    assert not missing, (
        f"the `dev` extra in {PYPROJECT.name} must pin {missing}; tests import these "
        "directly. Undeclared, the suite is red on a clean install and green only on a "
        "machine that happens to carry them at some version."
    )


def test_the_interpreter_running_these_tests_matches_the_pin(
    constraint_pins: dict[str, str],
) -> None:
    """The one that fails on a stale machine, which is the point.

    Not a skip when a package is absent: after the change this module documents, an
    absent one is a violated declared dependency, and tests/test_observability.py drives
    strands' real Tracer without a guard. A skip here would restore precisely the
    silence being removed -- install the `dev` extra.
    """
    wrong: dict[str, str] = {}
    for name in sorted(MUST_MATCH_LOCALLY):
        want = constraint_pins[name]
        try:
            got = importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError:
            got = "NOT INSTALLED"
        if got != want:
            wrong[name] = f"want {want}, have {got}"
    assert not wrong, (
        f"the interpreter running this suite does not match {CONSTRAINTS.name}: {wrong}. "
        "Run `pip install -e '.[dev]'` from backend/. Until it matches, every result from "
        "tests/test_observability.py describes a dependency version the deployed "
        "container does not have."
    )


def test_no_bundle_build_escapes_the_constraint_file(build_script: str) -> None:
    """The regression for the eight provider deltas that never got the mcp pin.

    Reads the script rather than the artifacts, because backend/agentcore-deps/ is
    gitignored and so the artifacts do not exist in CI. Asserts the constraint is applied
    in the ONE shared place every bundle goes through, which is what makes "every bundle"
    true by construction instead of by enumerating eleven call sites that can grow a
    twelfth.
    """
    # The single pip invocation all eleven bundles funnel through.
    installs = [m.start() for m in re.finditer(r"^\s*pip3 install", build_script, re.M)]
    assert len(installs) == 1, (
        f"expected exactly one `pip3 install` in {BUILD_SCRIPT.name}, found "
        f"{len(installs)}. A second one is a bundle built outside install_packages, and "
        "therefore outside the constraint file -- pass --constraint there too and raise "
        "this count deliberately."
    )
    # Check the whole of install_packages, not a window after the pip line. The flags are
    # assembled a few lines ABOVE the invocation, and an earlier version of this test read
    # only the 400 characters following `pip3 install` -- so it failed against a script that
    # does pass the constraint, which the build itself had already proven by stopping with
    # `ResolutionImpossible ... The user requested (constraint) websockets==17.1`. A test
    # that reads a byte window instead of the unit is testing the formatting.
    func = re.search(r"^install_packages\(\)\s*\{(?P<body>.*?)^\}", build_script, re.M | re.S)
    assert func, f"{BUILD_SCRIPT.name} no longer defines install_packages()"
    body = func.group("body")
    assert installs[0] >= func.start() and installs[0] < func.end(), (
        "the single pip3 install is no longer inside install_packages(), so the constraint "
        "checked below is not the one every bundle goes through."
    )
    assert "--constraint" in body and "CONSTRAINTS_FILE" in body, (
        'install_packages must pass --constraint "${CONSTRAINTS_FILE}"; without it the '
        "provider-<extra>.zip deltas resolve mcp and the OpenTelemetry graph freely, "
        "which is the defect this module documents."
    )
    # The flags must reach the invocation. Building an array and not expanding it looks
    # exactly like a working constraint and applies nothing.
    assert "${constraint_flags[@]}" in body, (
        "install_packages assembles constraint_flags but the pip3 invocation must expand it; "
        "an unexpanded array is an unpinned build that greps as if it were pinned."
    )
    # And the build must refuse rather than silently float if the file is missing.
    assert 'if [[ ! -f "${CONSTRAINTS_FILE}" ]]' in build_script, (
        f"{BUILD_SCRIPT.name} must fail closed when the constraint file is absent. "
        "pip accepts --constraint on a nonexistent path only as an error, but the path is "
        "built from PROJECT_ROOT, and a silently unpinned rebuild is the whole failure."
    )


def _distributions(zip_path: Path) -> dict[str, str]:
    """Every distribution a bundle carries, normalized name -> version, from its dist-info."""
    found: dict[str, str] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            head = name.split("/", 1)[0]
            if not head.endswith(".dist-info"):
                continue
            dist_name, _, version = head[: -len(".dist-info")].rpartition("-")
            if dist_name and version:
                found[_normalize(dist_name)] = version
    return found


def _expected_bundles(build_script: str) -> set[str]:
    """The eleven zips the script builds, read off the script rather than hardcoded."""
    match = re.search(r"local provider_extras=\((?P<body>[^)]*)\)", build_script)
    assert match, "install-agentcore-deps.sh no longer declares a provider_extras array"
    extras = re.findall(r"[\w-]+", match.group("body"))
    assert len(extras) >= 8, f"parsed only {extras} from provider_extras"
    return {"base.zip", "strands-mcp.zip", "mcp-lean.zip"} | {f"provider-{e}.zip" for e in extras}


@pytest.fixture(scope="module")
def built_bundles(build_script: str) -> dict[str, dict[str, str]]:
    """Every built bundle's distributions, or skip if the bundles were never built.

    A PARTIAL directory must not pass, and this is not hypothetical: run mid-rebuild --
    the script ``rm -rf``s the output directory first -- the artifact check below saw 2 of
    the 11 zips, both of them the already-corrected core bundles, and reported green while
    all eight offending provider deltas simply did not exist yet. A partial set is also a
    defect in its own right rather than only a test artifact, because ``scripts/deploy.sh``
    uploads this directory as it finds it: half-built means a deploy that silently omits
    provider bundles.
    """
    present = {p.name for p in DEPS_DIR.glob("*.zip")}
    if not present:
        pytest.skip(f"no bundles built in {DEPS_DIR}; run scripts/install-agentcore-deps.sh")
    missing = sorted(_expected_bundles(build_script) - present)
    assert not missing, (
        f"{DEPS_DIR.name}/ is partially built -- missing {missing}. Not skipped, because "
        "deploy.sh uploads this directory as it finds it, so a partial set ships a "
        "deployment whose provider bundles are absent; and because passing over a subset "
        "is how this check reported green while every bundle it was written to catch was "
        "simply not on disk yet. Re-run scripts/install-agentcore-deps.sh to completion."
    )
    return {name: _distributions(DEPS_DIR / name) for name in sorted(present)}


def test_no_provider_delta_carries_a_distribution_the_baseline_already_has(
    built_bundles: dict[str, dict[str, str]],
) -> None:
    """The general invariant, which is stronger than pinning the packages seen to break.

    A ``provider-<extra>.zip`` is a DELTA: it is only ever unzipped on top of
    strands-mcp.zip, and it is built by resolving ``strands-agents[<extra>]`` separately
    and then deleting every FILE PATH the baseline already has. So for any distribution
    present in both trees at the SAME version, every path overlaps and the subtraction
    removes it entirely -- meaning a shared distribution appearing in a delta at all *is*
    the proof that it resolved to a different version. That makes "no shared distribution
    in any delta" the exact invariant, with no version arithmetic and nothing to keep in
    sync.

    Two instances were measured before the fix, and the second is why the mcp-specific
    check this replaced was not enough:

    - ``mcp`` 2.1.1 over the baseline's 1.30.0, in ALL EIGHT deltas.
    - ``websockets`` 16.1.1 over the baseline's 17.1, in ``provider-gemini.zip`` only --
      a DOWNGRADE, because google-genai caps websockets lower than strands does. Nothing
      about mcp would have predicted it, and the next one would have been found in
      production.

    What survives in the merged tree is the version-unique modules plus a second
    dist-info, so the package is half one version and half another and
    ``importlib.metadata.version()`` answers whichever dist-info is enumerated first.
    """
    baseline_name = "strands-mcp.zip"
    baseline = built_bundles[baseline_name]
    assert len(baseline) >= 50, (
        f"{baseline_name} lists only {len(baseline)} distributions, which cannot be right "
        "for the full strands+otel+mcp tree -- the comparison below would be vacuous."
    )
    deltas = {name: d for name, d in built_bundles.items() if name.startswith("provider-")}
    assert len(deltas) >= 8, f"expected at least 8 provider deltas, found {sorted(deltas)}"

    offenders: dict[str, dict[str, str]] = {}
    for name, dists in deltas.items():
        shared = {dist: f"delta {ver} vs baseline {baseline[dist]}" for dist, ver in dists.items() if dist in baseline}
        if shared:
            offenders[name] = shared
    assert not offenders, (
        f"these provider deltas carry a distribution the baseline already has, which can "
        f"only mean it resolved to a different version: {offenders}. The two trees merge "
        "into one directory at container start, so the result is a single package tree "
        "holding modules from two versions plus two dist-info directories. Fix it at the "
        "build, not by pinning the individual package: install-agentcore-deps.sh emits the "
        "baseline's own resolution as constraints for the deltas (emit_tree_constraints), "
        "so rebuild rather than adding a pin. If pip now fails on a provider extra, that "
        "extra genuinely cannot accept a baseline version and is not expressible as a "
        "delta at all -- which is a decision to make, not a pin to add."
    )


def test_no_built_bundle_carries_an_unpinned_mcp(
    constraint_pins: dict[str, str], built_bundles: dict[str, dict[str, str]]
) -> None:
    """The named-package check for `mcp`, kept alongside the general invariant above.

    Not redundant: the invariant above compares deltas against the baseline, so it says
    nothing about the baseline itself, and `mcp` is the one distribution where a wrong
    version in ``strands-mcp.zip`` or ``mcp-lean.zip`` is a documented dead container
    rather than a merge artifact (see `test_mcp_pin_matches_codegen.py` -- 2.x renamed both
    entry points the generators emit, with no back-compat alias). strands-agents 1.56.0
    requires ``mcp<2.2,>=1.23.0``, a range that spans that incompatible rename, so the
    framework's own bound does not protect this and an unconstrained resolve lands on 2.x.
    """
    want = constraint_pins["mcp"]
    offenders = {name: dists["mcp"] for name, dists in built_bundles.items() if dists.get("mcp", want) != want}
    assert not offenders, (
        f"these bundles carry an mcp other than the pinned {want} (bundle: version "
        f"present): {offenders}. The generators emit the mcp 1.x API, so a 2.x tree in the "
        "container is an ImportError at startup, surfaced only as AgentCore's misleading "
        '"Runtime initialization time exceeded ... 30s". Rebuild with '
        "scripts/install-agentcore-deps.sh."
    )


# The environment the bundles are BUILT FOR, not the one the tests run on. pip is invoked
# with --platform manylinux2014_aarch64 --python-version 3.13 --implementation cp, so a
# marker must be evaluated against that target: a requirement gated on sys_platform or
# platform_machine would otherwise be judged by this macOS host and silently skipped.
_TARGET_MARKER_ENV = {
    "implementation_name": "cpython",
    "os_name": "posix",
    "platform_machine": "aarch64",
    "platform_python_implementation": "CPython",
    "platform_system": "Linux",
    "python_full_version": "3.13.0",
    "python_version": "3.13",
    "sys_platform": "linux",
}


def _distribution_versions(zip_path: Path) -> dict[str, set[str]]:
    """Like _distributions, but keeps EVERY version seen for a name instead of the last one.

    Needed because the whole defect class here is one distribution present twice. A
    name -> version mapping cannot represent it, and quietly picking a winner is what made
    the first version of the merged-tree check pass over artifacts that were broken.
    """
    found: dict[str, set[str]] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            head = name.split("/", 1)[0]
            if not head.endswith(".dist-info"):
                continue
            dist_name, _, version = head[: -len(".dist-info")].rpartition("-")
            if dist_name and version:
                found.setdefault(_normalize(dist_name), set()).add(version)
    return found


def _requirements(zip_path: Path) -> list[tuple[str, str]]:
    """Every unconditional Requires-Dist in a bundle, as (declaring distribution, raw spec).

    Requirements gated on ``extra == ...`` are dropped: an extra's dependencies are
    installed only when that extra is requested, and a built tree does not record which
    extras were asked for, so treating them as required would flag packages the resolver
    correctly left out. Other markers are evaluated against the aarch64/cp313 target.
    """
    out: list[tuple[str, str]] = []
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            head = name.split("/", 1)[0]
            if not head.endswith(".dist-info") or not name.endswith("/METADATA"):
                continue
            declarer = head[: -len(".dist-info")]
            text = zf.read(name).decode("utf-8", "replace")
            for line in text.splitlines():
                # A blank line ends the RFC 822 headers; everything after it is the long
                # description, where a line could start with "Requires-Dist:" as prose.
                if not line:
                    break
                if line.startswith("Requires-Dist:"):
                    out.append((declarer, line.split(":", 1)[1].strip()))
    return out


def test_the_merged_tree_satisfies_every_requirement_its_own_metadata_declares(
    built_bundles: dict[str, dict[str, str]],
) -> None:
    """Check the UNION that actually loads at runtime, not one zip at a time.

    A provider container unzips strands-mcp.zip and then provider-<extra>.zip into the same
    directory, so the thing Python imports is the merge -- and no single zip's contents can
    tell you whether that merge is coherent. This reads every distribution's own
    ``Requires-Dist`` out of the merged pair and checks the version present satisfies it.

    This is the static form of an import smoke test, and it is deliberately static: the
    wheels are ``manylinux2014_aarch64``, so importing them on a macOS or x86 test host
    fails on the first native extension (measured: ``pydantic_core._pydantic_core``)
    regardless of whether the tree is coherent. A metadata check has no such limit and
    covers all eleven bundles; the executable proof is the live AgentCore deploy and invoke.

    The class of defect this catches is a provider SDK that caps a package the baseline
    resolved above -- exactly what happened with google-genai (``websockets<17.0`` against a
    baseline that freely resolved 17.1). Before the delta constraints existed, that shipped
    as two versions of websockets merged into one tree; with them, the build fails and the
    fix is a baseline pin. Either way this test is what says the shipped artifacts agree.

    A distribution present at more than one version is checked against EVERY version, not
    against one of them. That is not pedantry, it is the only honest model: both dist-info
    directories exist side by side in the merged tree and which one ``importlib.metadata``
    reports depends on filesystem enumeration order, so a requirement is only satisfied if
    it holds whichever wins. Collapsing them to one version -- the obvious `dict` merge --
    makes this check silently vacuous: run that way over the pre-fix artifacts it reported
    ZERO violations, because the delta's websockets 16.1.1 simply overwrote the baseline's
    17.1 and google-genai's ``<17.0`` then looked satisfied.

    Only requirements naming a distribution actually PRESENT in the merged tree are checked.
    An absent one is an optional dependency the resolver deliberately left out, and
    demanding it would turn every unused extra into a failure.

    Measured against the pre-fix artifacts this fails on exactly two merges -- gemini alone
    and all providers at once -- naming ``google_genai-2.24.0 requires 'websockets<17.0,
    >=13.0.0'; present ['16.1.1', '17.1']``. Note what it does NOT catch there: the mcp
    2.1.1-over-1.30.0 leak in all eight deltas passes this check, because no metadata in the
    tree asks for mcp<2 (strands-agents allows ``<2.2,>=1.23.0``, which both versions
    satisfy) -- the incompatibility is in the emitted import lines, not in any Requires-Dist.
    So this test does not subsume
    `test_no_provider_delta_carries_a_distribution_the_baseline_already_has`; the two catch
    different halves and both are needed.
    """
    from packaging.markers import Marker
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.version import InvalidVersion, Version

    baseline_name = "strands-mcp.zip"
    assert baseline_name in built_bundles, sorted(built_bundles)
    baseline_reqs = _requirements(DEPS_DIR / baseline_name)
    assert len(baseline_reqs) >= 50, (
        f"only {len(baseline_reqs)} requirements parsed out of {baseline_name}; the METADATA "
        "files are not being read and every check below would be vacuous."
    )

    # Each provider container is baseline + exactly one delta. The all-providers union is
    # checked too, and it is not a theoretical shape: codegen_step calls
    # provider_bundle_keys_for(canvas_model_providers(config)), which returns a LIST, and
    # runtime_deployer._create_code_zip merges every extra bundle into one tree sharing a
    # single dedupe set. A canvas with several model nodes therefore ships several deltas.
    # The two standalone bundles are checked against themselves, since they unzip alone.
    delta_names = sorted(n for n in built_bundles if n.startswith("provider-"))
    merges: dict[str, list[str]] = {
        "base.zip": ["base.zip"],
        "mcp-lean.zip": ["mcp-lean.zip"],
        baseline_name: [baseline_name],
    }
    for name in delta_names:
        merges[f"{baseline_name} + {name}"] = [baseline_name, name]
    merges["all providers at once"] = [baseline_name, *delta_names]

    violations: dict[str, list[str]] = {}
    for label, members in merges.items():
        merged: dict[str, set[str]] = {}
        reqs: list[tuple[str, str]] = []
        for member in members:
            for dist, versions in _distribution_versions(DEPS_DIR / member).items():
                merged.setdefault(dist, set()).update(versions)
            reqs.extend(baseline_reqs if member == baseline_name else _requirements(DEPS_DIR / member))

        for declarer, raw in reqs:
            try:
                req = Requirement(raw)
            except InvalidRequirement:
                continue
            if req.marker is not None:
                if "extra ==" in str(req.marker):
                    continue
                try:
                    if not Marker(str(req.marker)).evaluate(_TARGET_MARKER_ENV):
                        continue
                except Exception:  # noqa: BLE001 - an unevaluable marker is not a version defect
                    continue
            present = merged.get(_normalize(req.name))
            if not present or not req.specifier:
                continue
            for have in sorted(present):
                try:
                    ok = req.specifier.contains(Version(have), prereleases=True)
                except InvalidVersion:
                    continue
                if not ok:
                    violations.setdefault(label, []).append(
                        f"{declarer} requires {raw!r} but the merged tree has "
                        f"{req.name} {have}" + (f" (present as {sorted(present)})" if len(present) > 1 else "")
                    )

    assert not violations, (
        "the tree a container actually imports does not satisfy its own declared "
        f"requirements: {violations}. This is not a lint nit -- it is the signature of a "
        "delta built against a baseline outside some provider's accepted range, which ships "
        "as two versions of one package merged into a single directory. Fix it by pinning "
        "the shared distribution in backend/agentcore-deps-constraints.txt to a version "
        "every provider extra accepts, then rebuild; do not exempt the provider."
    )


def test_no_two_provider_deltas_disagree_about_a_distribution(
    built_bundles: dict[str, dict[str, str]],
) -> None:
    """Provider-vs-provider drift, which the baseline-vs-delta invariant cannot see.

    That invariant only rejects a distribution the BASELINE already has. A package absent
    from the baseline but pulled in by two different provider extras is invisible to it, and
    can sit at two different versions with every existing check green.

    This is reachable, not hypothetical. ``codegen_step`` calls ``provider_bundle_keys_for``
    on the canvas's model providers and gets a LIST back, and
    ``runtime_deployer._create_code_zip`` merges the extra bundles "in order, sharing one
    dedupe set" -- first bundle wins on a duplicate path. So a canvas with an OpenAI node and
    a Gemini node unzips two deltas over the baseline, and any distribution they disagree
    about lands as a half-and-half tree with two dist-info directories, the same failure the
    mcp 2.1.1 leak produced. It would then manifest only on multi-provider canvases.

    Measured state when this was written: zero cross-provider conflicts, so this pins a
    property that currently holds rather than reporting a live defect. The structural fix is
    that every delta resolves against the baseline's own pins, but that does not constrain a
    package NEITHER the baseline nor the constraint file names, which is precisely the gap.
    """
    deltas = sorted(n for n in built_bundles if n.startswith("provider-"))
    assert len(deltas) >= 8, f"expected at least 8 provider deltas, found {deltas}"

    seen: dict[str, dict[str, set[str]]] = {}
    for name in deltas:
        for dist, versions in _distribution_versions(DEPS_DIR / name).items():
            for version in versions:
                seen.setdefault(dist, {}).setdefault(version, set()).add(name)

    conflicts = {
        dist: {ver: sorted(owners) for ver, owners in by_version.items()}
        for dist, by_version in seen.items()
        if len(by_version) > 1
    }
    assert not conflicts, (
        f"these distributions appear at more than one version across the provider deltas: "
        f"{conflicts}. A multi-provider canvas merges several deltas into one container "
        "(codegen_step -> provider_bundle_keys_for -> _create_code_zip), first path wins, so "
        "the result is one package tree assembled from two versions. Pin the distribution in "
        "backend/agentcore-deps-constraints.txt to a version every provider extra accepts and "
        "rebuild -- the same fix as for a baseline conflict, for the same reason."
    )


def test_shared_paths_across_provider_deltas_are_byte_identical(
    built_bundles: dict[str, dict[str, str]],
) -> None:
    """Same name and same version is not the same file. Compare the bytes.

    ``_create_code_zip``'s dedupe is by path: the first bundle to contribute a path wins and
    every later one is dropped silently. So if two deltas ship the same path with different
    content, which content the container gets depends on the order
    ``provider_bundle_keys_for`` happened to return -- an ordering derived from the canvas,
    not from anything about the packages. That makes the artifact non-deterministic in a way
    no version check can detect, which is why this compares CRCs rather than names.

    Note deliberately what this does NOT claim: the zips are not byte-reproducible builds.
    ``zip -r`` records mtimes, so two builds of an identical tree differ. This compares
    entries WITHIN one build set, where mtime differences do not enter a CRC.
    """
    deltas = sorted(n for n in built_bundles if n.startswith("provider-"))
    assert len(deltas) >= 8, f"expected at least 8 provider deltas, found {deltas}"

    crcs: dict[str, dict[int, set[str]]] = {}
    for name in deltas:
        with zipfile.ZipFile(DEPS_DIR / name) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                crcs.setdefault(info.filename, {}).setdefault(info.CRC, set()).add(name)

    divergent = {
        path: {crc: sorted(owners) for crc, owners in by_crc.items()}
        for path, by_crc in crcs.items()
        if len(by_crc) > 1
    }
    assert not divergent, (
        f"{len(divergent)} path(s) differ in content between provider deltas while sharing a "
        f"name, so which bytes reach the container depends on bundle merge order: "
        f"{dict(list(divergent.items())[:10])}"
    )

"""The exported dependency build runs a pip that can build it, at the platform's own pins.

Measured live 2026-10-01 (Stage 70, the exported bundle deployed the way a recipient
deploys it): ``build-dependency-bundle.sh`` ran the first ``pip3`` on PATH. On a stock Mac
that is the Command Line Tools pip 21.2.4 on Python 3.9.6. It backtracked for 25 minutes
and then crashed with ``RequirementParseError``, and ``deploy.sh`` exited 2 before any
stack existed. With the same flags (aarch64/cp313 target) from a Python 3.9 interpreter:

- pip 21.2.4, 22.0.4, 22.3.1, 23.0.1, 24.0 and 24.1 fail the pinned recipe at once with
  "Package 'bedrock-agentcore' requires a different Python: 3.9.6 not in '>=3.10'". They
  check Requires-Python against the interpreter pip runs on, not the ``--python-version``
  target, and unpinned they backtrack through years of releases instead.
- pip 24.2, 24.3.1, 25.0 and 25.0.1 build the whole bundle in 33-41 seconds.

The same build also floated every version the platform pins. Unpinned, it resolved
strands-agents 1.57.1, bedrock-agentcore 1.24.0, OpenTelemetry 1.45.0 and websockets 17.1,
a set the platform never built or tested. The platform's own bundles carry 1.56.0, 1.23.1,
1.44.0 and 16.1.1.

The script now uses ``pip3`` only when it is 24.2 or newer. Otherwise it builds with a
current pip in a private virtualenv, and otherwise it stops before downloading anything. It
passes the platform's constraint file to the install. The behavioural tests below run the
REAL generated script under bash, ``/bin/bash`` where it exists (3.2 on macOS, the shell
``deploy.sh`` gets there), against fake ``pip3``/``python3.x``/``zip`` programs that record
what they were asked to do.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.cfn_template_generator import (
    DEPENDENCY_BUNDLE_CONSTRAINTS,
    CfnTemplateGenerator,
)

CONSTRAINTS_FILE = Path(__file__).resolve().parents[1] / "agentcore-deps-constraints.txt"
BASH = "/bin/bash" if os.path.exists("/bin/bash") else (shutil.which("bash") or "bash")
CANDIDATES = ("python3.13", "python3.12", "python3.11", "python3.10", "python3")
#: The only system tools the sandbox exposes, linked one by one, so no real pip3 or python3 in
#: /usr/bin can answer for a fake.
SYSTEM_TOOLS = (
    "awk",
    "basename",
    "cat",
    "cp",
    "cut",
    "dirname",
    "du",
    "find",
    "mkdir",
    "mktemp",
    "od",
    "rm",
    "sort",
    "touch",
)


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(lines) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, version = line.partition("==")
        assert sep and version and "=" not in name, f"not an exact pin: {raw!r}"
        pins[_normalize(name)] = version.strip()
    return pins


def _config(**overrides) -> RuntimeConfig:
    values = {
        "name": "pinned_bundle_probe",
        "model": {"modelId": "us.anthropic.claude-sonnet-5"},
        "systemPrompt": "You are helpful.",
    }
    values.update(overrides)
    return RuntimeConfig(**values)


def _bundles():
    generator = CfnTemplateGenerator()
    strands = generator.generate(DeployRequest(nodeId="pins-strands", config=_config()))
    # Model-free, as the standalone FastMCP template requires (it refuses model fields).
    lean_config = RuntimeConfig.model_validate({"name": "pins_lean", "protocol": "MCP", "enableOtel": False})
    lean = generator.generate(DeployRequest(nodeId="pins-lean", config=lean_config, templateId="mcp-server-runtime"))
    chain = generator.generate(
        DeployRequest.model_validate(
            {
                "nodeId": "pins-chain",
                "config": {
                    "name": "pins_chain",
                    "model": {"modelId": "us.anthropic.claude-sonnet-5"},
                    "modelProvider": "bedrock",
                    "protocol": "HTTP",
                    "enableOtel": False,
                },
                "templateId": "mcp-server-gateway-target",
                "gatewayConfig": {"gateway_provider": "agentcore", "targetType": "lambda"},
                "mcpServerConfig": {"name": "order_tools", "tools": []},
            }
        )
    )
    return {
        "default": strands.build_bundle_sh,
        "mcp-server-runtime": lean.build_bundle_sh,
        "gateway-target-client": chain.build_bundle_sh,
        "gateway-target-mcp-server": chain.build_mcp_bundle_sh,
    }


BUNDLES = _bundles()


# ---------------------------------------------------------------------------
# The pins
# ---------------------------------------------------------------------------


def test_the_exported_pins_are_exactly_the_platforms_pins():
    """One list, mirrored. The generator cannot read the file: the Lambda asset excludes it."""
    exported = _pins(DEPENDENCY_BUNDLE_CONSTRAINTS)
    platform = _pins(CONSTRAINTS_FILE.read_text().splitlines())
    assert len(platform) >= 12, "the constraint file was emptied; this comparison would be vacuous"
    assert exported == platform


@pytest.mark.parametrize("kind", sorted(BUNDLES))
def test_every_recipe_installs_through_the_pins(kind):
    script = BUNDLES[kind]
    assert script, f"{kind} has no build script"
    heredoc = script.split("<<'CONSTRAINTS'\n", 1)[1].split("\nCONSTRAINTS\n", 1)[0]
    assert heredoc.splitlines() == list(DEPENDENCY_BUNDLE_CONSTRAINTS)

    installs = [line for line in script.splitlines() if re.match(r"\s*(run_pip|pip3|pip) install\b", line)]
    assert installs == ["run_pip install \\"], f"{kind}: expected the single pinned install, found {installs}"
    block = script[script.index("run_pip install \\") :].split("\n\n", 1)[0]
    assert '--constraint "$PIP_WORK/constraints.txt"' in block
    for flag in ("--platform manylinux2014_aarch64", "--python-version 3.13", "--only-binary=:all:"):
        assert flag in block


# ---------------------------------------------------------------------------
# The pip the build runs
# ---------------------------------------------------------------------------

# 20 bytes: ELF magic, then e_machine (offset 18) = 0xb7 0x00, little-endian AArch64.
_ELF_AARCH64 = r"\177ELF\002\001\001\000\000\000\000\000\000\000\000\000\000\000\267\000"

_FAKE_PIP_BODY = r"""
log_dir="__LOG__"
version_file="__VERSION_FILE__"
name="__NAME__"
if [ "$1" = "--version" ]; then
    echo "pip $(cat "$version_file") from /fake/$name (python 3.x)"
    exit 0
fi
if [ "$1" != "install" ]; then
    echo "fake pip: unexpected $*" >&2
    exit 2
fi
shift
target=""
constraint=""
upgrade=""
while [ $# -gt 0 ]; do
    case "$1" in
        --target) target="$2"; shift ;;
        --constraint) constraint="$2"; shift ;;
        pip\>=*) upgrade="$1" ;;
    esac
    shift
done
if [ -n "$upgrade" ]; then
    echo "$upgrade" >> "$log_dir/$name.upgrades"
    if [ "__UPGRADES__" = "yes" ]; then
        echo "25.0" > "$version_file"
    fi
    exit 0
fi
echo "install" >> "$log_dir/$name.installs"
[ -n "$constraint" ] && cp "$constraint" "$log_dir/$name.constraints"
mkdir -p "$target/pkg"
printf '__ELF__' > "$target/pkg/_native.so"
exit 0
"""


def _write(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _fake_pip(path: Path, name: str, version: str, log: Path, *, upgrades: bool, as_python: bool) -> None:
    version_file = path.parent / f".{name}.version"
    version_file.write_text(version + "\n")
    body = (
        _FAKE_PIP_BODY.replace("__LOG__", str(log))
        .replace("__VERSION_FILE__", str(version_file))
        .replace("__NAME__", name)
        .replace("__UPGRADES__", "yes" if upgrades else "no")
        .replace("__ELF__", _ELF_AARCH64)
    )
    if as_python:
        # `python -m pip ...`: drop the two leading words, then behave as pip.
        body = (
            'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then shift 2; else echo "fake python: $*" >&2; exit 2; fi\n' + body
        )
    _write(path, "#!/bin/sh\n" + body)


def _sandbox(tmp_path: Path, *, pip3: str | None, venvs: dict[str, tuple[str, bool]]) -> tuple[dict, Path]:
    """A PATH whose pip3 and python3.x are fakes; every other tool is the system's.

    *venvs* maps a candidate interpreter to (pip version its venv starts with, whether
    `pip install "pip>=..."` upgrades it). A candidate absent from *venvs* still shadows
    any real one and fails `-m venv`, so no real interpreter is ever reached.
    """
    fakebin = tmp_path / "fakebin"
    sysbin = tmp_path / "sysbin"
    log = tmp_path / "log"
    for directory in (fakebin, sysbin, log):
        directory.mkdir()
    for tool in SYSTEM_TOOLS:
        real = shutil.which(tool, path="/usr/bin:/bin")
        assert real, f"the sandbox needs the system {tool}"
        (sysbin / tool).symlink_to(real)
    if pip3 is not None:
        _fake_pip(fakebin / "pip3", "pip3", pip3, log, upgrades=False, as_python=False)
    for candidate in CANDIDATES:
        if candidate in venvs:
            start, upgrades = venvs[candidate]
            template = tmp_path / f"{candidate}.venv-python"
            _fake_pip(template, f"venv-{candidate}", start, log, upgrades=upgrades, as_python=True)
            _write(
                fakebin / candidate,
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    echo "$*" >> "{log}/{candidate}.calls"
                    if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
                        mkdir -p "$3/bin" && cp "{template}" "$3/bin/python" && exit 0
                    fi
                    exit 2
                    """
                ),
            )
        else:
            _write(
                fakebin / candidate,
                f'#!/bin/sh\necho "$*" >> "{log}/{candidate}.calls"\necho "No module named venv" >&2\nexit 1\n',
            )
    _write(
        fakebin / "zip",
        '#!/bin/sh\nfor a in "$@"; do case "$a" in *.zip) echo fake > "$a" ;; esac; done\ncat > /dev/null\n',
    )
    temp = tmp_path / "tmp"
    temp.mkdir()
    env = {"PATH": f"{fakebin}:{sysbin}", "HOME": str(tmp_path), "TMPDIR": str(temp), "LANG": "C"}
    return env, log


def _run(tmp_path: Path, env: dict) -> subprocess.CompletedProcess:
    script = tmp_path / "build-dependency-bundle.sh"
    script.write_text(BUNDLES["default"])
    return subprocess.run(
        [BASH, str(script), str(tmp_path / "out.zip")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _calls(log: Path, name: str) -> list[str]:
    path = log / name
    return path.read_text().splitlines() if path.exists() else []


@pytest.mark.parametrize("version", ["24.2", "24.10", "25.0.1", "100.0"])
def test_a_recent_pip3_is_used_as_is(tmp_path, version):
    env, log = _sandbox(tmp_path, pip3=version, venvs={})

    result = _run(tmp_path, env)

    assert result.returncode == 0, result.stderr
    assert _calls(log, "pip3.installs") == ["install"]
    assert _pins((log / "pip3.constraints").read_text().splitlines()) == _pins(DEPENDENCY_BUNDLE_CONSTRAINTS)
    assert not any((log / f"{candidate}.calls").exists() for candidate in CANDIDATES)
    assert (tmp_path / "out.zip").exists()


@pytest.mark.parametrize("version", ["21.2.4", "24.1", "9.0", "abc"])
def test_an_old_pip3_is_replaced_by_a_current_pip_in_a_private_virtualenv(tmp_path, version):
    env, log = _sandbox(tmp_path, pip3=version, venvs={"python3.13": ("24.0", True)})

    result = _run(tmp_path, env)

    assert result.returncode == 0, result.stderr
    assert "building with pip 25.0 in a temporary virtualenv" in result.stdout
    assert _calls(log, "pip3.installs") == [], "the old pip3 must never run the build"
    assert _calls(log, "venv-python3.13.upgrades") == ["pip>=24.2"]
    assert _calls(log, "venv-python3.13.installs") == ["install"]
    assert _pins((log / "venv-python3.13.constraints").read_text().splitlines()) == _pins(DEPENDENCY_BUNDLE_CONSTRAINTS)


def test_a_missing_pip3_is_replaced_the_same_way(tmp_path):
    env, log = _sandbox(tmp_path, pip3=None, venvs={"python3": ("21.2.4", True)})

    result = _run(tmp_path, env)

    assert result.returncode == 0, result.stderr
    assert _calls(log, "venv-python3.installs") == ["install"]
    # Every better candidate was tried first and refused to make a virtualenv.
    for candidate in ("python3.13", "python3.12", "python3.11", "python3.10"):
        assert _calls(log, f"{candidate}.calls") and _calls(log, f"{candidate}.calls")[0].startswith("-m venv")


def test_a_virtualenv_whose_pip_stays_old_is_not_used(tmp_path):
    env, log = _sandbox(
        tmp_path,
        pip3="21.2.4",
        venvs={"python3.13": ("21.2.4", False), "python3.12": ("23.0", True)},
    )

    result = _run(tmp_path, env)

    assert result.returncode == 0, result.stderr
    assert _calls(log, "venv-python3.13.installs") == []
    assert _calls(log, "venv-python3.12.installs") == ["install"]


def test_without_any_capable_pip_the_build_stops_before_downloading(tmp_path):
    env, log = _sandbox(tmp_path, pip3="21.2.4", venvs={})

    result = _run(tmp_path, env)

    assert result.returncode == 1
    assert "needs pip 24.2 or newer" in result.stderr
    assert "pip 21.2.4" in result.stderr, "the error must name the pip it refused"
    assert not any(path.name.endswith(".installs") for path in log.iterdir())
    assert not (tmp_path / "out.zip").exists()


def test_the_build_leaves_no_scratch_behind(tmp_path):
    env, _log = _sandbox(tmp_path, pip3="21.2.4", venvs={"python3.13": ("24.0", True)})

    result = _run(tmp_path, env)

    assert result.returncode == 0, result.stderr
    assert list((tmp_path / "tmp").iterdir()) == [], "the build dir or the private virtualenv survived"

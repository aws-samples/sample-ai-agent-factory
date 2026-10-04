"""Pytest configuration for CDK stack tests.

Ensures tests run from the infra/ directory so that:
1. Relative Docker asset paths (e.g., ``../backend``) resolve correctly
2. The ``stacks`` package is importable
"""

import os
import shutil
import subprocess
import sys
import tempfile

import aws_cdk as cdk
import pytest

# Resolve the infra/ directory (parent of tests/)
_INFRA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Ensure stacks package is importable regardless of where pytest is invoked
if _INFRA_DIR not in sys.path:
    sys.path.insert(0, _INFRA_DIR)

# Change to infra/ so ContainerImage.from_asset("../backend") resolves
os.chdir(_INFRA_DIR)


# --- Bash >= 4 for the deploy-script tests -----------------------------------------------------------------------
# Several tests load functions out of scripts/deploy.sh with `source <(awk ...)` and drive them under `bash`. macOS'
# /bin/bash is 3.2, which silently defines NOTHING from a process substitution, so under a system-first PATH (the
# certifier's hermetic phase environment) every such driver fails with "command not found". The scripts themselves
# target Bash 4+ (Linux). Resolve one explicitly, put it first on PATH for the whole session, or fail loudly.


def _bash_major(path: str) -> int:
    try:
        out = subprocess.run(
            [path, "-c", 'printf %s "${BASH_VERSINFO[0]}"'], capture_output=True, text=True, timeout=10
        )
        return int(out.stdout.strip() or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0


def _bash4_on_path() -> str:
    candidates = [shutil.which("bash"), "/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/bin/bash"]
    for cand in candidates:
        if cand and os.path.isfile(cand) and _bash_major(cand) >= 4:
            return cand
    raise SystemExit(
        "infra tests need Bash >= 4 (process substitution in `source <(...)`); none found among " + str(candidates)
    )


_BASH4 = _bash4_on_path()
if shutil.which("bash") != _BASH4:  # expose it under the plain name every driver uses; children inherit os.environ
    _shim_dir = tempfile.mkdtemp(prefix="infra-tests-bash4-")
    os.symlink(_BASH4, os.path.join(_shim_dir, "bash"))
    os.environ["PATH"] = _shim_dir + os.pathsep + os.environ.get("PATH", "")


# --- CDK synth output never leaks into TMPDIR --------------------------------------------------------------------
# A ``cdk.App()`` built without ``outdir`` synthesizes into ``mkdtemp(realpath(TMPDIR)/cdk.out)``. CDK removes that
# directory only when the jsii kernel (the node child) exits gracefully; a run whose kernel is killed (timeout,
# ENOSPC, orphan sweep) leaves EVERY synth of that process behind (measured: SIGKILL of the kernel leaks, a normal
# exit does not). With staged Lambda assets a PlatformStack synth is ~200 MB, the suite synthesizes dozens of times
# per run, and after ~25 certifications ~1000 such directories filled the disk (ENOSPC inside jsii while copying
# backend/agentcore-deps/mcp-lean.zip). Every App the suite creates is therefore routed into one directory under
# pytest's basetemp and removed at the end of the scope that created it: the test body, the test module (module-
# scoped fixtures build their App BEFORE the function-scoped fixture below records its start index, so their
# output survives the module), or the session. Explicit ``outdir=`` arguments are respected and left alone.

_CDK_OUTDIRS: list[str] = []  # every outdir handed to an App this session, in creation order
_ORIGINAL_APP_INIT = cdk.App.__init__


def _remove_synth_output_created_since(start: int) -> None:
    for outdir in _CDK_OUTDIRS[start:]:
        if os.path.isdir(outdir):
            shutil.rmtree(outdir)


@pytest.fixture(scope="session", autouse=True)
def _route_cdk_synth_output_under_basetemp(tmp_path_factory: pytest.TempPathFactory):
    base = tmp_path_factory.mktemp("cdk-synth")

    def routed_init(self, *args, outdir: str | None = None, **kwargs) -> None:
        if outdir is None:
            outdir = tempfile.mkdtemp(prefix="cdk.out", dir=str(base))
            _CDK_OUTDIRS.append(outdir)
        _ORIGINAL_APP_INIT(self, *args, outdir=outdir, **kwargs)

    cdk.App.__init__ = routed_init
    try:
        yield base
    finally:
        cdk.App.__init__ = _ORIGINAL_APP_INIT
        _remove_synth_output_created_since(0)
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(scope="module", autouse=True)
def _remove_module_synth_output():
    start = len(_CDK_OUTDIRS)
    yield
    _remove_synth_output_created_since(start)


@pytest.fixture(autouse=True)
def _remove_test_synth_output():
    start = len(_CDK_OUTDIRS)
    yield
    _remove_synth_output_created_since(start)

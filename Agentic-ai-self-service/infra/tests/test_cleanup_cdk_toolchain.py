"""cleanup.sh must install and verify the pinned CDK before deleting anything.

A teardown is often run from a fresh checkout or after ``node_modules`` was
removed. Depending on a prior deploy makes the recovery command fail exactly
when it is most needed; falling back to a global/npx CDK makes the synthesized
destroy plan machine-dependent. These tests execute the real shell function
with a scratch checkout and exercise the real ``main`` ordering without making
AWS calls.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CLEANUP_SH = REPO / "scripts" / "cleanup.sh"
PIN = json.loads((REPO / "infra" / "package.json").read_text())["devDependencies"]["aws-cdk"]


def _scratch_checkout(tmp_path: Path, *, lock_pin: str | None = None) -> Path:
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    (root / "infra").mkdir()
    shutil.copy2(CLEANUP_SH, root / "scripts" / "cleanup.sh")
    (root / "infra" / "package.json").write_text(
        json.dumps(
            {
                "name": "cleanup-toolchain-test",
                "private": True,
                "devDependencies": {"aws-cdk": PIN},
            }
        )
    )
    resolved = PIN if lock_pin is None else lock_pin
    (root / "infra" / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "cleanup-toolchain-test",
                "lockfileVersion": 3,
                "requires": True,
                "packages": {
                    "": {
                        "name": "cleanup-toolchain-test",
                        "devDependencies": {"aws-cdk": resolved},
                    },
                    "node_modules/aws-cdk": {
                        "version": resolved,
                    },
                },
            }
        )
    )
    # The real function invokes pip because cdk destroy executes app.py. An
    # empty requirements file proves the call without touching the test env.
    (root / "infra" / "requirements.txt").write_text("")
    return root


def _stub_toolchain(tmp_path: Path, *, cdk_version: str = PIN) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "npm.calls"
    npm = bin_dir / "npm"
    npm.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\\n' "$*" >> "${{NPM_CALLS}}"
            mkdir -p node_modules/.bin
            cat > node_modules/.bin/cdk <<'STUB'
            #!/usr/bin/env bash
            printf '%s (build test)\\n' '{cdk_version}'
            STUB
            chmod +x node_modules/.bin/cdk
            """
        )
    )
    npm.chmod(0o755)
    return bin_dir, calls


def _run_install(
    root: Path,
    bin_dir: Path,
    calls: Path,
    *,
    cdk_bin: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    driver = root / "run-install.sh"
    override = f'export CDK_BIN="{cdk_bin}"\n' if cdk_bin is not None else ""
    driver.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            {override}source "{root / "scripts" / "cleanup.sh"}"
            PROJECT_PYTHON="{os.fspath(Path(os.sys.executable))}"
            install_cdk_dependencies_for_cleanup
            """
        )
    )
    driver.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "NPM_CALLS": os.fspath(calls),
    }
    return subprocess.run(
        ["bash", os.fspath(driver)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_a_fresh_checkout_installs_and_uses_the_exact_locked_cli(tmp_path: Path) -> None:
    root = _scratch_checkout(tmp_path)
    bin_dir, calls = _stub_toolchain(tmp_path)
    assert not (root / "infra" / "node_modules").exists(), "precondition: no prior deploy/install"

    result = _run_install(root, bin_dir, calls)

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text().splitlines() == ["ci --ignore-scripts --no-audit --no-fund --loglevel=error"]
    installed = root / "infra" / "node_modules" / ".bin" / "cdk"
    assert installed.is_file() and os.access(installed, os.X_OK)
    assert PIN in result.stdout


def test_a_lockfile_that_disagrees_with_package_json_fails_before_npm(tmp_path: Path) -> None:
    root = _scratch_checkout(tmp_path, lock_pin="0.0.1")
    bin_dir, calls = _stub_toolchain(tmp_path)

    result = _run_install(root, bin_dir, calls)

    assert result.returncode != 0
    assert not calls.exists(), "invalid committed metadata must fail before npm executes"
    assert "invalid or inconsistent" in result.stderr


def test_an_installed_cli_with_the_wrong_version_is_rejected(tmp_path: Path) -> None:
    root = _scratch_checkout(tmp_path)
    bin_dir, calls = _stub_toolchain(tmp_path, cdk_version="0.0.1")

    result = _run_install(root, bin_dir, calls)

    assert result.returncode != 0
    assert calls.exists(), "the mismatch is measured after the locked install"
    assert "repository pins" in result.stderr
    assert "0.0.1" in result.stderr


def _run_main_with_stubs(
    tmp_path: Path,
    *,
    install_succeeds: bool,
    stack_exists: bool = True,
) -> subprocess.CompletedProcess[str]:
    calls = tmp_path / "main.calls"
    driver = tmp_path / "run-main.sh"
    install_result = "return 0" if install_succeeds else "return 42"
    stack_value = "true" if stack_exists else "false"
    driver.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            source "{CLEANUP_SH}"
            record() {{ printf '%s\\n' "$1" >> "{calls}"; }}
            validate_cleanup_options() {{ :; }}
            check_prerequisites() {{ :; }}
            check_aws_credentials() {{ STACK_OWNER_ID="arn:test"; }}
            check_stack_exists() {{ STACK_EXISTS={stack_value}; }}
            confirm_destroy() {{ :; }}
            install_cdk_dependencies_for_cleanup() {{ record install; {install_result}; }}
            prepare_retained_gateway_auth_target() {{ record prepare; }}
            cleanup_deployment_resources() {{ record dynamic; }}
            sweep_orphan_resources() {{ record sweep; }}
            run_cdk_destroy() {{ record destroy; }}
            verify_resources_removed() {{ record verify; }}
            delete_retained_gateway_auth_resources() {{ record retained; }}
            print_summary() {{ record summary; }}
            main
            """
        )
    )
    driver.chmod(0o755)
    return subprocess.run(
        ["bash", os.fspath(driver)],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_main_installs_before_every_destructive_path(tmp_path: Path) -> None:
    result = _run_main_with_stubs(tmp_path, install_succeeds=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "main.calls").read_text().splitlines() == [
        "install",
        "prepare",
        "dynamic",
        "sweep",
        "destroy",
        "verify",
        "retained",
        "summary",
    ]


def test_an_install_failure_leaves_every_destructive_path_untouched(tmp_path: Path) -> None:
    result = _run_main_with_stubs(tmp_path, install_succeeds=False)

    assert result.returncode != 0
    assert (tmp_path / "main.calls").read_text().splitlines() == ["install"]


def test_an_already_absent_stack_does_not_require_the_cdk_toolchain(tmp_path: Path) -> None:
    result = _run_main_with_stubs(
        tmp_path,
        install_succeeds=False,
        stack_exists=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "main.calls").read_text().splitlines() == [
        "prepare",
        "sweep",
        "retained",
        "summary",
    ]


def test_the_shipping_script_has_no_global_or_npx_cdk_fallback() -> None:
    source = CLEANUP_SH.read_text()
    main = source[source.index("main() {") :]

    assert 'CDK_BIN="${CDK_BIN:-${PROJECT_ROOT}/infra/node_modules/.bin/cdk}"' in source
    assert '"${CDK_BIN}" destroy' in source
    assert "npx cdk" not in source
    assert "npm install" not in source
    assert "npm ci --ignore-scripts --no-audit --no-fund --loglevel=error" in source
    assert main.index("install_cdk_dependencies_for_cleanup") < main.index("cleanup_deployment_resources")

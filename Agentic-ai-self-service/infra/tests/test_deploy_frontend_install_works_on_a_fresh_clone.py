"""deploy.sh must be able to build the frontend on a machine that has never seen this lock.

Why this exists: ``build_frontend`` installed the frontend with ``npm ci --offline``.
On a machine whose npm cache had never seen ``frontend/package-lock.json`` -- every
fresh clone, and the first us-east-1 production create on 2026-10-28 -- npm failed
with ``ENOTCACHED`` *after* the CloudFormation stack had been created, leaving a
platform with an API and no UI. The customer-facing failure was silent in the sense
that mattered: the stack was green.

``--offline`` was never the security boundary. The install already runs under
``env -i`` (no ``AWS_*`` name reaches npm), with user and global npm config disabled,
and with ``--ignore-scripts`` (no dependency lifecycle script runs, with or without
credentials). ``--prefer-offline`` keeps the cache as the first source and lets npm
fetch a package the cache lacks from the registry -- verified against the lock's
integrity hash in both cases. These assertions pin that the boundary stayed and the
cache requirement went.

The function is asserted textually, as its siblings in
``test_deploy_cloudfront_invalidation.py`` are, and the install flags are also
executed through npm's own argument parser so a typo cannot pass as a flag.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DEPLOY_SH = _ROOT / "scripts" / "deploy.sh"


def _extract_fn(name: str) -> str:
    src = _DEPLOY_SH.read_text()
    start = src.index(f"{name}() {{")
    end = src.index("\n}\n", start) + len("\n}\n")
    return src[start:end]


def _install_line() -> str:
    fn = _extract_fn("build_frontend")
    lines = [line.strip() for line in fn.splitlines() if "npm ci" in line and not line.strip().startswith("#")]
    assert len(lines) == 1, f"exactly one frontend install, got {lines!r}"
    return lines[0]


class TestTheInstallDoesNotRequireAWarmCache:
    def test_the_cache_is_preferred_but_never_required(self):
        line = _install_line()
        assert "--prefer-offline" in line
        # The exact old flag, as a whole word: ``--prefer-offline`` contains ``offline`` but not ``--offline``.
        assert re.search(r"(?<![\w-])--offline(?![\w-])", line) is None

    def test_the_credential_boundary_is_unchanged(self):
        fn = _extract_fn("build_frontend")
        line = _install_line()
        assert "--ignore-scripts" in line
        assert line.startswith('"${fe_env[@]}" npm ci')
        assert 'env -i "PATH=${PATH}" "HOME=${fe_home}"' in fn
        assert "NPM_CONFIG_USERCONFIG=/dev/null" in fn
        assert "NPM_CONFIG_GLOBALCONFIG=/var/empty/npmrc" in fn
        assert '"NPM_CONFIG_CACHE=${NPM_CONFIG_CACHE:-${HOME}/.npm}"' in fn

    def test_the_build_still_runs_in_the_same_stripped_environment(self):
        fn = _extract_fn("build_frontend")
        assert '"${fe_env[@]}" "${vite_env[@]}" npm run build' in fn
        assert '"${fe_env[@]}" "${vite_env[@]}" npm run verify:production-build' in fn

    @pytest.mark.skipif(shutil.which("npm") is None, reason="npm is not installed on this host")
    def test_npm_accepts_every_flag_on_the_install_line(self, tmp_path):
        """A misspelt flag would be taken by npm as a package name and the install would
        fail (or install the wrong thing). Parse the flags with npm itself, in a throwaway
        empty project, in dry-run mode, under the same stripped environment."""
        line = _install_line()
        flags = [part for part in line.split()[3:] if part.startswith("--")]
        assert flags, line
        (tmp_path / "package.json").write_text('{"name": "flag-probe", "version": "0.0.0", "private": true}\n')
        (tmp_path / "package-lock.json").write_text(
            '{"name": "flag-probe", "version": "0.0.0", "lockfileVersion": 3, "requires": true, '
            '"packages": {"": {"name": "flag-probe", "version": "0.0.0"}}}\n'
        )
        home = tmp_path / "home"
        home.mkdir()
        proc = subprocess.run(
            [
                "env",
                "-i",
                f"PATH={pathlib.Path(shutil.which('npm')).parent}:/usr/bin:/bin",
                f"HOME={home}",
                "LANG=C.UTF-8",
                "CI=1",
                "NPM_CONFIG_USERCONFIG=/dev/null",
                "NPM_CONFIG_GLOBALCONFIG=/var/empty/npmrc",
                f"NPM_CONFIG_CACHE={home / '.npm'}",
                "npm",
                "ci",
                *flags,
                "--dry-run",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert "Unknown" not in proc.stderr and "ENOTCACHED" not in proc.stderr

"""Executable fail-closed tests for the exact-byte CDK safety gate in deploy.sh.

`run_cdk_deploy` in scripts/deploy.sh must:

  1. strict-synth ONCE into a pinned assembly and, on synth failure, abort BEFORE
     running diff or deploy (fail-closed);
  2. abort if strict synth produced no template for the stack;
  3. run `cdk diff` on the frozen assembly and, if diff itself FAILS (non-zero
     exit), abort BEFORE deploy -- a real regression: with `|| true` a diff that
     exited 42 still reached deploy;
  4. on success, invoke synth -> diff -> deploy in that order, with diff and
     deploy consuming the IDENTICAL `--app` assembly and context applied ONLY to
     the synth (so synth/diff/deploy cannot drift).

These run the REAL function body extracted from deploy.sh against a stubbed
`npx`, so they exercise the shipped control flow, not a paraphrase of it.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"

STACK = "teststack"


def _run_gate(
    tmp_path: Path, *, synth_mode: str, diff_rc: int, hasher_mode: str = "real", run_tag: str = "r1"
) -> tuple[int, str, list[str]]:
    """Drive the real run_cdk_deploy with a stubbed npx.

    synth_mode: "ok" (synth 0 + writes template/assets/manifest), "fail" (synth
                non-zero), "notemplate" (synth 0 but writes no template),
                "nomanifest" (synth 0, template+assets but NO manifest.json),
                "symlink" (synth 0, the template is a SYMLINK to a real file),
                "hardlink" (synth 0, a second hard link to the assets manifest).
    diff_rc:    exit code the stub returns for `cdk diff`.
    hasher_mode: kept for call compatibility; the receipts are produced by the Python
                stable-reader now, so a degenerate PATH hasher cannot influence them.
    run_tag:    distinguishes the calls file when two runs share one project dir.
    Returns (function_rc, calls_text, calls_lines).
    """
    project_root = tmp_path / "proj"
    (project_root / "infra").mkdir(parents=True, exist_ok=True)
    calls_file = tmp_path / f"calls-{run_tag}.txt"

    driver = tmp_path / "driver.sh"
    driver.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail

            # --- stubs for the helpers run_cdk_deploy calls ---
            log_info()    {{ :; }}
            log_success() {{ :; }}
            log_error()   {{ echo "ERR: $*" >&2; }}

            # --- the variables run_cdk_deploy reads ---
            STACK_NAME={STACK}
            AWS_REGION=us-east-1
            ENVIRONMENT_NAME=t
            PROJECT_NAME=p
            COGNITO_USERS=
            CLOUDFRONT_WEB_ACL_ARN=
            RBAC_ENFORCE=true
            OTEL_ENDPOINT=
            OTEL_AUTH_SECRET_ARN=
            OTEL_SAMPLE_RATE=1.0
            OTEL_SERVICE_NAME_PREFIX=
            PROJECT_ROOT={project_root}

            CALLS={calls_file}
            SYNTH_MODE={synth_mode}
            DIFF_RC={diff_rc}
            HASHER_MODE={hasher_mode}

            # --- optional degenerate hasher, first on PATH (the gate prefers sha256sum) ---
            mkdir -p "{tmp_path}/bin"; rm -f "{tmp_path}/bin/sha256sum"
            if [ "$HASHER_MODE" = "empty" ]; then printf '#!/bin/sh\nexit 0\n' > "{tmp_path}/bin/sha256sum"; fi
            if [ "$HASHER_MODE" = "garbage" ]; then printf '#!/bin/sh\necho notahash\n' > "{tmp_path}/bin/sha256sum"; fi
            if [ -f "{tmp_path}/bin/sha256sum" ]; then chmod +x "{tmp_path}/bin/sha256sum"; export PATH="{tmp_path}/bin:$PATH"; fi

            # --- stub cdk binary via CDK_BIN: record every call verbatim, behave per mode ---
            mkdir -p "{tmp_path}/cdkbin"
            cat > "{tmp_path}/cdkbin/cdk" <<'STUB'
#!/bin/bash
echo "cdk $*" >> "$CALLS"
if [ "${{1:-}}" = "synth" ]; then
  if [ "$SYNTH_MODE" = "fail" ]; then exit 7; fi
  out=""; prev=""
  for a in "$@"; do
    if [ "$prev" = "--output" ]; then out="$a"; fi
    prev="$a"
  done
  if [ -n "$out" ]; then
    mkdir -p "$out"
    if [ "$SYNTH_MODE" != "notemplate" ]; then
      echo '{{}}' > "$out/{STACK}.template.json"
      echo '{{}}' > "$out/{STACK}.assets.json"
      if [ "$SYNTH_MODE" != "nomanifest" ]; then
        echo '{{}}' > "$out/manifest.json"
      fi
      if [ "$SYNTH_MODE" = "symlink" ]; then
        mv "$out/{STACK}.template.json" "$out/real.template.json.bak"
        ln -s "$out/real.template.json.bak" "$out/{STACK}.template.json"
      fi
      if [ "$SYNTH_MODE" = "hardlink" ]; then
        ln "$out/{STACK}.assets.json" "$out/second.assets.json"
      fi
    fi
  fi
  exit 0
fi
if [ "${{1:-}}" = "diff" ]; then exit "$DIFF_RC"; fi
if [ "${{1:-}}" = "deploy" ]; then echo "DEPLOY_CALLED" >> "$CALLS"; exit 0; fi
exit 0
STUB
            chmod +x "{tmp_path}/cdkbin/cdk"
            export CALLS SYNTH_MODE DIFF_RC
            export CDK_BIN="{tmp_path}/cdkbin/cdk"

            # import ONLY the function definition (not main) from the real script
            source <(awk '/^(pinned_payload_tool|verify_pinned_assembly|cdk_synth_pinned|cdk_diff_pinned|cdk_deploy_pinned|run_cdk_deploy)\\(\\)/,/^}}/' "{DEPLOY_SH}")

            run_cdk_deploy
            """
        ).lstrip()
    )

    proc = subprocess.run(
        ["bash", str(driver)],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": os.environ["PATH"]},
    )
    calls_text = calls_file.read_text() if calls_file.exists() else ""
    calls_lines = [ln for ln in calls_text.splitlines() if ln.strip()]
    return proc.returncode, calls_text, calls_lines


def test_the_extracted_function_is_the_real_one():
    """Guard: the awk extraction must capture run_cdk_deploy AND the three pinned functions it delegates to
    (cdk_synth_pinned / cdk_diff_pinned / cdk_deploy_pinned -- split so certified mode can re-derive the frozen
    identity before every AWS mutation boundary), or every
    other test would vacuously 'pass' on an empty body."""
    body = subprocess.run(
        [
            "awk",
            r"/^(pinned_payload_tool|verify_pinned_assembly|cdk_synth_pinned|cdk_diff_pinned|cdk_deploy_pinned|run_cdk_deploy)\(\)/,/^}/",
            str(DEPLOY_SH),
        ],
        capture_output=True,
        text=True,
    ).stdout
    assert "cdk synth" not in body or "npx" not in body, "the gate must not fall back to `npx cdk`"
    assert '"${CDK_BIN}" synth' in body and '"${CDK_BIN}" diff' in body and '"${CDK_BIN}" deploy' in body, body
    assert "npx cdk" not in body, "every cdk invocation must go through the pinned CDK_BIN"
    # The `|| true` swallow on diff must be gone.
    diff_line = next(ln for ln in body.splitlines() if '"${CDK_BIN}" diff' in ln)
    assert "|| true" not in diff_line, "cdk diff must not swallow its exit code"
    # The digest capture must be fail-closed and portable, not `... || true` + shasum-only.
    assert 'assembly-hashes.txt" 2>&1 || true' not in body, "digest capture must not swallow failures"
    assert "pinned_payload_tool record-all" in body, (
        "artifact and payload receipts must be produced by the Python stable-reader tool as ONE transaction"
    )
    assert "sha256sum" not in body, "no pathname hasher may remain: receipts come from stable O_NOFOLLOW reads"
    assert "manifest.json" in body, "the cloud-assembly manifest must be digested"
    inv, rec = body.index("pinned_payload_tool invalidate"), body.index("pinned_payload_tool record-all")
    assert inv < rec, "prior-run receipts must be invalidated (both files, fsynced) BEFORE any artifact is read"
    # published atomically AND no-clobber by the Python tool: link(2) from an exclusive 0600 temp; never mv -f
    assert "os.link(tmpname, name, src_dir_fd=dfd, dst_dir_fd=dfd)" in body and "os.fchmod(fd, 0o600)" in body, (
        "receipts: link(2) from an exclusive 0600 temp, both relative to the bound directory handle"
    )
    assert 'mv -f -- "${hashes_tmp}"' not in body, "a receipt must never be published with mv -f (clobbers a winner)"


def test_strict_synth_failure_aborts_before_diff_and_deploy(tmp_path):
    rc, _text, calls = _run_gate(tmp_path, synth_mode="fail", diff_rc=0)
    assert rc != 0, "a strict-synth failure must fail the gate"
    joined = "\n".join(calls)
    assert "cdk synth" in joined
    assert "cdk diff" not in joined, "diff must NOT run after a synth failure"
    assert "DEPLOY_CALLED" not in joined, "deploy must NOT run after a synth failure"


def test_missing_template_aborts_before_diff_and_deploy(tmp_path):
    rc, _text, calls = _run_gate(tmp_path, synth_mode="notemplate", diff_rc=0)
    assert rc != 0, "synth producing no template must fail the gate"
    joined = "\n".join(calls)
    assert "cdk diff" not in joined
    assert "DEPLOY_CALLED" not in joined


def test_diff_failure_aborts_before_deploy(tmp_path):
    # peer-65 regression: a diff exiting 42 must NOT reach deploy.
    rc, _text, calls = _run_gate(tmp_path, synth_mode="ok", diff_rc=42)
    assert rc != 0, "a diff execution failure must fail the gate"
    joined = "\n".join(calls)
    assert "cdk synth" in joined and "cdk diff" in joined
    assert "DEPLOY_CALLED" not in joined, "deploy must NOT run after a diff failure"


def test_success_runs_synth_then_diff_then_deploy_sharing_one_assembly(tmp_path):
    rc, _text, calls = _run_gate(tmp_path, synth_mode="ok", diff_rc=0)
    assert rc == 0, f"clean synth + clean diff must deploy; calls={calls}"

    synth_i = next(i for i, ln in enumerate(calls) if "cdk synth" in ln)
    diff_i = next(i for i, ln in enumerate(calls) if "cdk diff" in ln)
    deploy_i = next(i for i, ln in enumerate(calls) if "cdk deploy" in ln)
    assert synth_i < diff_i < deploy_i, f"order must be synth<diff<deploy: {calls}"

    synth_line = calls[synth_i]
    diff_line = calls[diff_i]
    deploy_line = calls[deploy_i]

    # diff and deploy must consume the identical --app assembly path.
    def app_arg(line: str) -> str:
        toks = line.split()
        return toks[toks.index("--app") + 1]

    assert "--app" in diff_line and "--app" in deploy_line
    assert app_arg(diff_line) == app_arg(deploy_line), "diff/deploy must share one assembly"
    assert app_arg(diff_line).endswith("/infra/cdk.out.preflight")

    # synth carries the shared -c context; diff and deploy must NOT (no drift).
    assert "-c environment_name=t" in synth_line
    assert "-c " not in diff_line, "diff must not take context; it reads the frozen assembly"
    assert "-c " not in deploy_line, "deploy must not take context; it reads the frozen assembly"
    # deploy must be scoped to the one stack.
    assert "--exclusively" in deploy_line


def test_digest_step_hashes_template_manifest_and_assets_and_is_nonempty(tmp_path):
    """The evidence the ledger cites must actually exist: one digest line each for the
    stack template, the cloud-assembly manifest, and the asset manifest, in a nonempty
    file the deploy step consumed."""
    rc, _text, calls = _run_gate(tmp_path, synth_mode="ok", diff_rc=0)
    assert rc == 0, f"clean run must deploy; calls={calls}"
    hashes_path = tmp_path / "proj" / ".cdk-gate" / "assembly-hashes.txt"
    assert hashes_path.exists(), "digest file was not written"
    hashes = hashes_path.read_text()
    assert hashes.strip(), "digest file must be nonempty"
    assert f"{STACK}.template.json" in hashes, hashes
    assert "manifest.json" in hashes, "the cloud-assembly manifest must be digested"
    assert f"{STACK}.assets.json" in hashes, "asset manifests must be digested"
    # every digest line is a real 64-hex sha256, not an error message swallowed into the file
    digest_lines = [ln for ln in hashes.splitlines() if not ln.startswith("reviewed assembly:")]
    assert len(digest_lines) == 3, digest_lines
    for ln in digest_lines:
        assert len(ln.split()[0]) == 64, f"not a sha256 line: {ln!r}"


def test_a_missing_manifest_aborts_before_deploy(tmp_path):
    # fail-closed: an incomplete assembly (no manifest.json) must never ship.
    rc, _text, calls = _run_gate(tmp_path, synth_mode="nomanifest", diff_rc=0)
    assert rc != 0, "a missing cloud-assembly manifest must fail the gate"
    joined = "\n".join(calls)
    assert "cdk synth" in joined
    assert "cdk diff" not in joined, "diff must NOT run when a digest target is missing"
    assert "DEPLOY_CALLED" not in joined, "deploy must NOT run when a digest target is missing"


HEX64 = re.compile(r"^[0-9a-f]{64}  \S")


def _hashes(tmp_path):
    return tmp_path / "proj" / ".cdk-gate" / "assembly-hashes.txt"


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_a_symlinked_or_hardlinked_artifact_aborts_before_diff_and_deploy(tmp_path, mode):
    """The receipts are produced by stable O_NOFOLLOW single-link reads (the bash `sha256sum` hasher and its
    degenerate-output mutants are gone). A template that is a symlink, or an asset manifest with a second hard
    link, is refused BEFORE any receipt is published, so neither diff nor deploy can run."""
    rc, _t, calls = _run_gate(tmp_path, synth_mode=mode, diff_rc=0)
    assert rc != 0, f"synth_mode={mode} must fail the gate"
    joined = "\n".join(calls)
    assert "cdk synth" in joined
    assert "cdk diff" not in joined and "DEPLOY_CALLED" not in joined
    assert not _hashes(tmp_path).exists(), "no citable digest file may exist after a refused artifact"
    gate_dir = tmp_path / "proj" / ".cdk-gate"
    assert not list(gate_dir.glob(".assembly-hashes.*")), "no half-written temp file either"


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_a_failed_second_run_does_not_leave_the_first_runs_digests_as_evidence(tmp_path, mode):
    """Peer 6b: run 1 succeeds and leaves valid digests; run 2's assembly carries a refused artifact and the
    gate exits before the final write. Without an up-front invalidation, run 1's file would
    still be there looking citable for run 2. The canonical file must be ABSENT after run 2."""
    rc1, _t, calls1 = _run_gate(tmp_path, synth_mode="ok", diff_rc=0, run_tag="r1")
    assert rc1 == 0 and "DEPLOY_CALLED" in "\n".join(calls1)
    first = _hashes(tmp_path).read_text()
    assert len([ln for ln in first.splitlines() if HEX64.match(ln)]) == 3, first

    rc2, _t, calls2 = _run_gate(tmp_path, synth_mode=mode, diff_rc=0, run_tag="r2")
    assert rc2 != 0
    assert "cdk diff" not in "\n".join(calls2) and "DEPLOY_CALLED" not in "\n".join(calls2)
    assert not _hashes(tmp_path).exists(), "run 1's digests survived a failed run 2 -- stale evidence"


def test_every_digest_line_is_one_sha256_over_our_own_target_path(tmp_path):
    rc, _t, _c = _run_gate(tmp_path, synth_mode="ok", diff_rc=0)
    assert rc == 0
    lines = _hashes(tmp_path).read_text().splitlines()
    assert lines[0].startswith("reviewed assembly: ")
    digests = lines[1:]
    assert len(digests) == 3, digests  # template + manifest + assets
    for ln in digests:
        assert HEX64.match(ln), ln
        # the recorded path is the gate's own target path, inside the pinned assembly
        assert "/infra/cdk.out.preflight/" in ln.split("  ", 1)[1], ln

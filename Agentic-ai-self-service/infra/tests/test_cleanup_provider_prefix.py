"""cleanup.sh must strip the TYPE prefix off a recorded credential-provider name.

Why this exists: ``gateway_result.connector_credential_providers`` records each
provider as ``"TYPE:name"`` (``API_KEY:`` / ``OAUTH:``) — the shape
``_record_gateway_resources`` partitions on in ``step_handlers/gateway_step.py``.
cleanup.sh passed that entry straight to ``--name``, and the ':' violates the
provider-name pattern ``[a-zA-Z0-9\\-_]+``, so BOTH deletes failed with
ValidationException. Every call there is suffixed ``2>/dev/null || true``, so the
teardown reported success while the provider — and the credential inside it —
survived. Verified against real AWS on 2026-09-04: with the raw entry the provider
was still listed afterwards; with the stripped name it was deleted.

The bash expansion is executed rather than transcribed, because the whole defect
was a string that *looked* like a name and wasn't.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

_CLEANUP_SH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cleanup.sh"


def _cleanup_sh() -> str:
    return _CLEANUP_SH.read_text()


def _strip_expr() -> str:
    """Pull the actual prefix-stripping expansion out of cleanup.sh."""
    m = re.search(r'cp_name="(\$\{cp_entry[^"]*\})"', _cleanup_sh())
    assert m, "prefix-stripping assignment not found — did cleanup.sh change shape?"
    return m.group(1)


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("API_KEY:mcp-litellm-proxy-deepwiki-e34565a10e", "mcp-litellm-proxy-deepwiki-e34565a10e"),
        ("OAUTH:acc-salesforce-7f3a", "acc-salesforce-7f3a"),
        # Legacy bare names predate the type prefix and must pass through untouched.
        ("acc-legacy-name", "acc-legacy-name"),
        ("harness-gw-outbound", "harness-gw-outbound"),
    ],
)
def test_the_shipped_expansion_yields_a_deletable_name(entry: str, expected: str) -> None:
    expr = _strip_expr()
    out = subprocess.run(
        ["bash", "-c", f'cp_entry="$1"; printf "%s" "{expr}"', "_", entry],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out == expected
    # The API rejects anything outside this pattern, which is what made the
    # unstripped entry a silent no-op rather than a loud failure.
    assert re.fullmatch(r"[a-zA-Z0-9\-_]+", out), f"{out!r} would be rejected by the provider API"


def test_both_deletes_use_the_stripped_name_not_the_raw_entry() -> None:
    """Stripping is pointless if either delete still passes ``cp_entry``."""
    src = _cleanup_sh()
    block = src[src.index("for cp_entry in ${conn_providers}") :]
    block = block[: block.index("\n    done")]
    for api in ("delete-api-key-credential-provider", "delete-oauth2-credential-provider"):
        assert api in block, f"{api} disappeared from the provider-cleanup loop"
    assert '--name "${cp_name}"' in block
    assert '--name "${cp_entry}"' not in block


def test_both_provider_namespaces_are_still_attempted() -> None:
    """The recorded TYPE is a hint only.

    Deleting an API-key provider through the OAuth2 API reports success WITHOUT
    deleting it, so cleanup cannot branch on the prefix — it must try both.
    """
    src = _cleanup_sh()
    block = src[src.index("for cp_entry in ${conn_providers}") :]
    block = block[: block.index("\n    done")]
    assert block.count('--name "${cp_name}"') == 2, "cleanup must attempt BOTH provider namespaces"

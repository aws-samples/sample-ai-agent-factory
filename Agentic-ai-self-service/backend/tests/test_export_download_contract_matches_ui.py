"""The export API and the UI's download parser agree on the response contract -- pinned.

Found live: the API started shipping ``artifact_manifest_sha256`` beside the archive digest,
while ``frontend/src/utils/downloadArtifact.ts`` required an EXACT key set without it, so the
deployed UI rejected every real "Download Python" / "Download CloudFormation" response with
its own integrity error. Each layer's tests were green against their own fixtures.

This test reads the key list the UI parser accepts straight out of the TypeScript source and
compares it with what the API handler emits for a real generated archive, for both response
shapes (presigned URL and inline base64). A change to either side alone fails here, offline.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.deployment_handler import _export_integrity_fields

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PARSER = PROJECT_ROOT / "frontend" / "src" / "utils" / "downloadArtifact.ts"
_KEY_LIST = re.compile(r"hasExactKeys\(record,\s*\[(?P<keys>[^\]]+)\]\)")


def _ui_accepted_keys() -> set[str]:
    source = PARSER.read_text(encoding="utf-8")
    match = _KEY_LIST.search(source)
    assert match, "downloadArtifact.ts no longer pins the response keys with hasExactKeys(record, [...])"
    literal_keys = {item.strip().strip("'\"") for item in match.group("keys").split(",") if "'" in item or '"' in item}
    assert "sourceKey" in match.group("keys"), "the parser must accept exactly one source key"
    return literal_keys


def _api_response_keys(source_key: str) -> set[str]:
    import io
    import json
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "agent-root/artifact-manifest.json", json.dumps({"schema": 1, "artifact_type": "python", "files": []})
        )
        archive.writestr("agent-root/agent.py", "print('agent')\n")
    body = buffer.getvalue()
    response = {"filename": "agent-root-python.zip", source_key: "...", **_export_integrity_fields(body)}
    return set(response)


def test_api_export_response_keys_are_exactly_what_the_ui_parser_accepts():
    ui_keys = _ui_accepted_keys()
    for source_key in ("download_url", "zip_base64"):
        assert _api_response_keys(source_key) == ui_keys | {source_key}, (
            f"API emits {sorted(_api_response_keys(source_key))}, UI accepts {sorted(ui_keys | {source_key})}"
        )


def test_the_manifest_digest_is_part_of_the_contract_and_is_a_sha256_hex():
    assert "artifact_manifest_sha256" in _ui_accepted_keys()
    fields = _export_integrity_fields(_zip_with_manifest())
    assert re.fullmatch(r"[0-9a-f]{64}", fields["artifact_manifest_sha256"])
    assert re.fullmatch(r"[0-9a-f]{64}", fields["archive_sha256"])
    assert isinstance(fields["archive_bytes"], int) and fields["archive_bytes"] > 0


def _zip_with_manifest() -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("root/artifact-manifest.json", "{}")
    return buffer.getvalue()


def test_ui_parser_source_still_validates_the_manifest_digest_shape():
    """The key may be accepted and yet unchecked; pin that the parser validates its shape."""
    source = PARSER.read_text(encoding="utf-8")
    assert "record.artifact_manifest_sha256 !== 'string'" in source
    assert "SHA256_HEX_PATTERN.test(record.artifact_manifest_sha256)" in source
    assert "artifactManifestSha256: record.artifact_manifest_sha256" in source

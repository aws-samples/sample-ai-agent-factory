"""Property tests for _create_code_zip() with bundled dependencies.

Tests cover correctness Property 4 from the design document:

- Property 4: Code zip structure with bundled dependencies

For any non-empty agent_code, any entrypoint, and any valid deps_bundle,
the resulting zip contains the entrypoint, all non-cache bundle entries,
and no ``requirements.txt``.

**Validates: Requirements 4.4, 5.2, 5.3, 5.4**
"""

import io
import sys
import zipfile

sys.path.insert(0, "src")

from app.services.runtime_deployer import _create_code_zip
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Non-empty agent code strings
_agent_code = st.text(min_size=1, max_size=500)

# Entrypoint filenames (valid Python filenames)
_entrypoint = st.from_regex(r"[a-z][a-z0-9_]{0,20}\.py", fullmatch=True)

# Normal file paths (no cache markers)
_normal_paths = st.from_regex(
    r"[a-z][a-z0-9_/]{0,60}\.(py|txt|json|cfg|so|pyd)",
    fullmatch=True,
)

# Paths containing __pycache__
_pycache_paths = _normal_paths.map(
    lambda p: p.rsplit("/", 1)[0] + "/__pycache__/" + p.rsplit("/", 1)[-1] if "/" in p else "__pycache__/" + p
)

# Paths ending in .pyc
_pyc_paths = _normal_paths.map(lambda p: p.rsplit(".", 1)[0] + ".pyc")

# Mix of all path types for bundle contents
_mixed_paths = st.lists(
    st.one_of(_normal_paths, _pycache_paths, _pyc_paths),
    min_size=1,
    max_size=30,
    unique=True,
)


def _build_zip_bytes(file_paths: list[str]) -> bytes:
    """Create an in-memory zip containing the given file paths with dummy content."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in file_paths:
            zf.writestr(path, f"content-of-{path}")
    buf.seek(0)
    return buf.read()


def _is_cache_entry(name: str) -> bool:
    """Return True if *name* is a cache entry that should be excluded."""
    return "__pycache__" in name or name.endswith(".pyc")


# ---------------------------------------------------------------------------
# Property 4: Code zip structure with bundled dependencies
# ---------------------------------------------------------------------------


class TestCodeZipStructureWithBundledDeps:
    """**Validates: Requirements 4.4, 5.2, 5.3, 5.4**

    For any non-empty agent_code, any entrypoint filename, and any valid
    deps_bundle bytes, the zip produced by
    ``_create_code_zip(agent_code, "", entrypoint, deps_bundle)``
    satisfies:

    1. Contains the entrypoint file with the correct agent_code content.
    2. Contains all non-cache entries from the bundle.
    3. Does NOT contain ``requirements.txt``.
    4. Does NOT contain any ``__pycache__`` or ``.pyc`` entries.
    """

    @given(
        agent_code=_agent_code,
        entrypoint=_entrypoint,
        bundle_paths=_mixed_paths,
    )
    @settings(max_examples=200)
    def test_entrypoint_present_with_correct_content(self, agent_code, entrypoint, bundle_paths):
        """**Validates: Requirements 5.2, 5.4**

        The zip always contains the entrypoint file with the exact
        agent_code content.
        """
        # Filter out bundle paths that collide with the entrypoint so the
        # merge step cannot overwrite the agent code entry.
        safe_paths = [p for p in bundle_paths if p != entrypoint]
        if not safe_paths:
            safe_paths = ["pkg/__init__.py"]
        deps_bundle = _build_zip_bytes(safe_paths)
        zip_bytes = _create_code_zip(agent_code, "", entrypoint, deps_bundle)

        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            assert entrypoint in zf.namelist(), (
                f"Entrypoint {entrypoint!r} missing from zip. Zip contains: {sorted(zf.namelist())}"
            )
            content = zf.read(entrypoint).decode()
            assert content == agent_code, f"Entrypoint content mismatch: expected {agent_code!r}, got {content!r}"

    @given(
        agent_code=_agent_code,
        entrypoint=_entrypoint,
        bundle_paths=_mixed_paths,
    )
    @settings(max_examples=200)
    def test_all_non_cache_bundle_entries_present(self, agent_code, entrypoint, bundle_paths):
        """**Validates: Requirements 4.4, 5.2**

        Every non-cache entry from the deps_bundle appears in the
        resulting zip.
        """
        deps_bundle = _build_zip_bytes(bundle_paths)
        expected = [p for p in bundle_paths if not _is_cache_entry(p)]

        zip_bytes = _create_code_zip(agent_code, "", entrypoint, deps_bundle)

        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            names = set(zf.namelist())
            for entry in expected:
                assert entry in names, (
                    f"Non-cache bundle entry {entry!r} missing from zip. Zip contains: {sorted(names)}"
                )

    @given(
        agent_code=_agent_code,
        entrypoint=_entrypoint,
        bundle_paths=_mixed_paths,
    )
    @settings(max_examples=200)
    def test_no_requirements_txt(self, agent_code, entrypoint, bundle_paths):
        """**Validates: Requirements 5.3**

        When requirements_txt is empty, the zip must NOT contain
        ``requirements.txt``.
        """
        deps_bundle = _build_zip_bytes(bundle_paths)
        zip_bytes = _create_code_zip(agent_code, "", entrypoint, deps_bundle)

        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            assert "requirements.txt" not in zf.namelist(), (
                "Zip should not contain requirements.txt when requirements_txt is empty"
            )

    @given(
        agent_code=_agent_code,
        entrypoint=_entrypoint,
        bundle_paths=_mixed_paths,
    )
    @settings(max_examples=200)
    def test_no_pycache_or_pyc_entries(self, agent_code, entrypoint, bundle_paths):
        """**Validates: Requirements 4.4, 5.2**

        The resulting zip must NOT contain any entries with
        ``__pycache__`` in the path or ending in ``.pyc``.
        """
        deps_bundle = _build_zip_bytes(bundle_paths)
        zip_bytes = _create_code_zip(agent_code, "", entrypoint, deps_bundle)

        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            for name in zf.namelist():
                assert "__pycache__" not in name, f"Zip contains __pycache__ entry: {name!r}"
                assert not name.endswith(".pyc"), f"Zip contains .pyc entry: {name!r}"

"""Property tests for _merge_deps_into_zip() cache file exclusion.

Tests cover correctness Property 2 from the design document:

- Property 2: Cache file exclusion during merge

For any valid zip bytes as bundle_bytes (including zips with ``__pycache__``
directories and ``.pyc`` files), after merge the target zip contains no such
entries.  All non-cache entries from the bundle ARE present in the target zip.

**Validates: Requirements 1.3, 4.5, 5.5**
"""

import io
import sys
import zipfile

sys.path.insert(0, "src")

from app.services.runtime_deployer import _merge_deps_into_zip
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

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

# Mix of all path types
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
# Property 2: Cache file exclusion during merge
# ---------------------------------------------------------------------------


class TestCacheFileExclusionDuringMerge:
    """**Validates: Requirements 1.3, 4.5, 5.5**

    For any valid zip bytes as bundle_bytes (including zips with
    ``__pycache__`` directories and ``.pyc`` files), after calling
    ``_merge_deps_into_zip(target_zf, bundle_bytes)``:

    1. The target zip contains NO entries with ``__pycache__`` in the path.
    2. The target zip contains NO entries ending in ``.pyc``.
    3. All non-cache entries from the bundle ARE present in the target zip.
    """

    @given(file_paths=_mixed_paths)
    @settings(max_examples=200)
    def test_no_pycache_or_pyc_entries_after_merge(self, file_paths):
        """**Validates: Requirements 1.3, 4.5, 5.5**

        After merging any bundle into a target zip, the target must contain
        zero entries with ``__pycache__`` in the path or ending in ``.pyc``.
        """
        bundle_bytes = _build_zip_bytes(file_paths)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as target_zf:
            _merge_deps_into_zip(target_zf, bundle_bytes)

        buf.seek(0)
        with zipfile.ZipFile(buf, "r") as result_zf:
            for name in result_zf.namelist():
                assert "__pycache__" not in name, f"Target zip contains __pycache__ entry: {name!r}"
                assert not name.endswith(".pyc"), f"Target zip contains .pyc entry: {name!r}"

    @given(file_paths=_mixed_paths)
    @settings(max_examples=200)
    def test_all_non_cache_entries_preserved(self, file_paths):
        """**Validates: Requirements 1.3, 4.5, 5.5**

        Every non-cache entry from the bundle must appear in the target zip
        after merge, with its original content intact.
        """
        bundle_bytes = _build_zip_bytes(file_paths)
        expected_entries = [p for p in file_paths if not _is_cache_entry(p)]

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as target_zf:
            _merge_deps_into_zip(target_zf, bundle_bytes)

        buf.seek(0)
        with zipfile.ZipFile(buf, "r") as result_zf:
            result_names = set(result_zf.namelist())
            for entry in expected_entries:
                assert entry in result_names, (
                    f"Non-cache entry {entry!r} missing from target zip. Target contains: {sorted(result_names)}"
                )
                content = result_zf.read(entry).decode()
                assert content == f"content-of-{entry}", f"Content mismatch for {entry!r}: {content!r}"

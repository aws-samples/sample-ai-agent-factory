"""Wrapper to run pytest with clean sys.path (avoids vendored pydantic stubs).

The backend/ directory contains vendored pydantic/pydantic_core stubs for Lambda
packaging (pure-Python, no compiled extensions). These shadow the real pydantic
from site-packages when pytest adds the rootdir to sys.path.

This wrapper temporarily renames the vendored dirs, runs pytest, then restores them.
"""

import os
import sys

backend_dir = os.path.dirname(os.path.abspath(__file__))
src_dir = os.path.join(backend_dir, "src")

VENDORED = [
    ("pydantic", "_vendored_pydantic"),
    ("pydantic_core", "_vendored_pydantic_core"),
]


def _hide_vendored():
    for orig, hidden in VENDORED:
        orig_path = os.path.join(backend_dir, orig)
        hidden_path = os.path.join(backend_dir, hidden)
        if os.path.isdir(orig_path) and not os.path.isdir(hidden_path):
            os.rename(orig_path, hidden_path)


def _restore_vendored():
    for orig, hidden in VENDORED:
        orig_path = os.path.join(backend_dir, orig)
        hidden_path = os.path.join(backend_dir, hidden)
        if os.path.isdir(hidden_path) and not os.path.isdir(orig_path):
            os.rename(hidden_path, orig_path)


if __name__ == "__main__":
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    _hide_vendored()
    try:
        import pytest

        code = pytest.main(sys.argv[1:])
    finally:
        _restore_vendored()
    sys.exit(code)

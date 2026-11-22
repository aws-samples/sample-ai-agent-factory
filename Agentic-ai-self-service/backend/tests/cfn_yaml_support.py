"""Parse exported CloudFormation YAML in tests without PyYAML's ``yaml.load`` entry point.

The export tests read the generated ``template.yaml`` back to assert on its resources.
CloudFormation's short-form intrinsics (``!Ref``, ``!Sub``, ``!GetAtt`` ...) are YAML
tags, so the default safe loader rejects them and the tests used custom loaders. Each
of those loaders was already safe (a ``SafeLoader`` subclass, or ``BaseLoader``, which
only ever constructs strings, lists and dicts), but they were driven through
``yaml.load``, the entry point that also accepts arbitrary-object loaders and that
AppSec scanners flag as a remote-code-execution sink (ACAT UnsafeYAMLLoad).

The two parsers below drive their loader directly. That is exactly what ``yaml.load``
does internally (construct the loader, ``get_single_data()``, ``dispose()``), so the
parsed documents are unchanged; what changes is that no code path can hand an unsafe
loader to the parser, and the repository no longer contains the flagged call.
"""

from __future__ import annotations

import yaml


class _IntrinsicsDroppedLoader(yaml.SafeLoader):
    """A SafeLoader that reads every ``!``-tagged intrinsic as ``None``.

    Used where a test asserts on plain values (tags, names) and does not care what the
    intrinsics resolve to.
    """


_IntrinsicsDroppedLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _parse_single_document(loader: yaml.BaseLoader | yaml.SafeLoader) -> object:
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def parse_template_as_strings(template_yaml: str) -> dict:
    """Parse a template with ``BaseLoader``: every scalar is a string, intrinsics are plain values.

    ``BaseLoader`` has no type resolution and no object constructors, so ``!Ref X`` becomes
    the string ``"X"`` and ``!GetAtt [A, B]`` becomes ``["A", "B"]``; a number or boolean
    stays the text it was written as.
    """
    document = _parse_single_document(yaml.BaseLoader(template_yaml))
    if not isinstance(document, dict):
        raise AssertionError(f"the exported template is not a mapping: {type(document).__name__}")
    return document


def parse_template_without_intrinsics(template_yaml: bytes | str) -> dict:
    """Parse a template with a SafeLoader whose ``!``-tagged intrinsics read as ``None``."""
    document = _parse_single_document(_IntrinsicsDroppedLoader(template_yaml))
    if not isinstance(document, dict):
        raise AssertionError(f"the exported template is not a mapping: {type(document).__name__}")
    return document

"""Keep generated and shipped source importable on the declared Python floor.

PEP 701 (3.12) made a backslash inside an f-string replacement field legal, so
``f'{"\\n".join(xs)}'`` compiles on every interpreter this repo is tested on and is
a SyntaxError on 3.11 -- ``code_generator.py`` shipped two of them and the module no
longer imported there.

The guard itself must run on Python 3.11: asking that interpreter's AST parser to parse
the forbidden syntax makes the scanner fail before it can report the defect. Python
3.11's tokenizer deliberately leaves an f-string as one STRING token, so inspect that
token and scan only replacement-field expressions. Backslashes in literal f-string
text and format-spec text remain valid and are ignored.
"""

from __future__ import annotations

import ast
import io
import tokenize
from collections.abc import Callable
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
MODULES = sorted(SRC.rglob("*.py"))


def _fstring_body(token_text: str) -> str | None:
    prefix_end = 0
    while prefix_end < len(token_text) and token_text[prefix_end] in "rRuUbBfF":
        prefix_end += 1
    if "f" not in token_text[:prefix_end].lower():
        return None

    for quote in ('"""', "'''", '"', "'"):
        if token_text.startswith(quote, prefix_end) and token_text.endswith(quote):
            return token_text[prefix_end + len(quote) : -len(quote)]
    return None


def _consume_quoted(
    text: str,
    start: int,
    mark_backslash: Callable[[int], None],
) -> int:
    quote = next(
        (candidate for candidate in ('"""', "'''", '"', "'") if text.startswith(candidate, start)),
        "",
    )
    if not quote:
        return start + 1

    cursor = start + len(quote)
    while cursor < len(text):
        if text.startswith(quote, cursor):
            return cursor + len(quote)
        if text[cursor] == "\\":
            mark_backslash(cursor)
            cursor += 2
        else:
            cursor += 1
    return cursor


def _scan_replacement_field(
    body: str,
    start: int,
    token_start_line: int,
) -> tuple[int, list[int]]:
    """Scan one ``{...}``, returning its end and incompatible field line(s)."""

    lines: list[int] = []
    field_was_flagged = False
    expected_closers: list[str] = []
    in_expression = True
    cursor = start

    def mark_backslash(offset: int) -> None:
        nonlocal field_was_flagged
        if not field_was_flagged:
            lines.append(token_start_line + body.count("\n", 0, offset))
            field_was_flagged = True

    while cursor < len(body):
        character = body[cursor]

        if in_expression:
            if character in {'"', "'"}:
                cursor = _consume_quoted(body, cursor, mark_backslash)
                continue
            if character == "#":
                while cursor < len(body) and body[cursor] != "\n":
                    if body[cursor] == "\\":
                        mark_backslash(cursor)
                    cursor += 1
                continue
            if character == "\\":
                mark_backslash(cursor)
                cursor += 2
                continue
            if character in "([{":
                expected_closers.append({"(": ")", "[": "]", "{": "}"}[character])
                cursor += 1
                continue
            if expected_closers and character == expected_closers[-1]:
                expected_closers.pop()
                cursor += 1
                continue
            if not expected_closers and character == "}":
                return cursor + 1, lines
            if not expected_closers and character == ":":
                in_expression = False
                cursor += 1
                continue
            if not expected_closers and character == "!" and not body.startswith("!=", cursor):
                in_expression = False
                cursor += 1
                continue
        else:
            # A format specification is f-string literal text, except that it
            # can contain nested replacement fields of its own.
            if character == "{":
                if body.startswith("{{", cursor):
                    cursor += 2
                    continue
                cursor, nested_lines = _scan_replacement_field(
                    body,
                    cursor + 1,
                    token_start_line,
                )
                lines.extend(nested_lines)
                continue
            if character == "}":
                return cursor + 1, lines

        cursor += 1

    return cursor, lines


def _pep701_only_fields_from_tokens(source: str) -> list[int]:
    lines: list[int] = []
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for token in tokens:
        if token.type != tokenize.STRING:
            continue
        body = _fstring_body(token.string)
        if body is None:
            continue

        cursor = 0
        while cursor < len(body):
            if body.startswith("{{", cursor) or body.startswith("}}", cursor):
                cursor += 2
                continue
            if body[cursor] == "{":
                cursor, field_lines = _scan_replacement_field(
                    body,
                    cursor + 1,
                    token.start[0],
                )
                lines.extend(field_lines)
                continue
            cursor += 1
    return lines


def _pep701_only_fields(source: str) -> list[int]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Python 3.11 reaches this branch for the exact PEP 701 construct the
        # guard exists to find. Its tokenizer still returns the whole f-string
        # as one STRING token, allowing the replacement fields to be inspected.
        return _pep701_only_fields_from_tokens(source)

    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for value in node.values:
            if not isinstance(value, ast.FormattedValue):
                continue
            segment = ast.get_source_segment(source, value.value) or ""
            if "\\" in segment:
                lines.append(value.lineno)
    return lines


def test_the_scan_sees_the_defect_it_guards_against():
    assert _pep701_only_fields('x = f"{chr(10).join(a)}"\ny = f"{\'\\\\n\'.join(a)}"\n') == [2]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('x = f"line\\\\n{value}"\n', []),
        ('x = f"{{literal}} and {{more}}: {value}"\n', []),
        ('x = f"{value:{width}}"\n', []),
        ("x = f\"{mapping['brace}']}\"\n", []),
        ("x = f\"{value:{'\\\\t'.join(parts)}}\"\n", [1]),
        (
            'x = f"""\n{\'\\\\n\'.join(parts)}\n{value}\n"""\n',
            [2],
        ),
        (
            "x = f\"{'\\\\n'.join(first)} {'\\\\t'.join(second)}\"\n",
            [1, 1],
        ),
    ],
)
def test_the_scan_distinguishes_fields_from_literal_and_format_text(
    source: str,
    expected: list[int],
) -> None:
    assert _pep701_only_fields(source) == expected


@pytest.mark.parametrize("module", MODULES, ids=lambda p: str(p.relative_to(SRC)))
def test_no_backslash_inside_an_fstring_field(module):
    assert _pep701_only_fields(module.read_text()) == []

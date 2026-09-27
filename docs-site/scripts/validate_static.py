#!/usr/bin/env python3
"""Fail-closed validation for the repository documentation artifact."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from xml.etree import ElementTree

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SITE_ROOT = REPOSITORY_ROOT / "docs-site"
DIST_ROOT = SITE_ROOT / "dist"
SOURCE_SVG = REPOSITORY_ROOT / "assets" / "repository-atlas-journey.svg"
DIST_SVG = DIST_ROOT / SOURCE_SVG.name

TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".svg", ".txt"}
DENIED_PATTERNS = {
    "12-digit account identifier": re.compile(r"(?<!\d)\d{12}(?!\d)"),
    "local Unix user path": re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+"),
    "local Windows user path": re.compile(r"[A-Za-z]:[\\\\/]Users[\\\\/]", re.IGNORECASE),
    "private evidence marker": re.compile(r"private-evidence|tasks/(?:lessons|todo)\.md", re.IGNORECASE),
    "internal domain": re.compile(
        r"(?:[A-Za-z0-9-]+\.)*(?:a2z\.com|corp\.amazon\.com|amazon\.dev|aws\.dev)"
        r"|(?:w|code|issues|sim|i|cti|phonetool|pipelines|build|apollo)\.amazon\.com"
        r"|quip-amazon\.com",
        re.IGNORECASE,
    ),
}


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].lower()


def validate_source_svg() -> None:
    try:
        root = ElementTree.parse(SOURCE_SVG).getroot()
    except (OSError, ElementTree.ParseError) as error:
        fail(f"invalid source SVG: {error}")

    ids: set[str] = set()
    title_found = False
    description_found = False

    if root.attrib.get("role") != "img":
        fail("source SVG must declare role=img")

    for element in root.iter():
        element_name = local_name(element.tag)
        title_found = title_found or element_name == "title"
        description_found = description_found or element_name == "desc"
        if element_name in {"script", "foreignobject"}:
            fail(f"active SVG element found: {element_name}")

        identifier = element.attrib.get("id")
        if identifier:
            if identifier in ids:
                fail(f"duplicate SVG id: {identifier}")
            ids.add(identifier)

        for attribute, value in element.attrib.items():
            attribute_name = local_name(attribute)
            if attribute_name.startswith("on"):
                fail(f"SVG event handler found: {attribute_name}")
            if attribute_name == "href" and value.strip().lower().startswith(("http://", "https://", "//", "data:")):
                fail(f"external SVG reference found: {value}")

    if not title_found or not description_found:
        fail("source SVG must contain title and description elements")


def validate_single_source() -> None:
    duplicate = SITE_ROOT / "public" / SOURCE_SVG.name
    if duplicate.exists():
        fail(f"duplicate SVG source exists: {duplicate}")
    if not DIST_SVG.is_file():
        fail(f"built SVG is missing: {DIST_SVG}")
    if SOURCE_SVG.read_bytes() != DIST_SVG.read_bytes():
        fail("built SVG differs from the repository source")


def validate_generated_tree() -> None:
    if not DIST_ROOT.is_dir():
        fail("dist does not exist; run npm run build first")

    for generated_name in (
        "vite.config.js",
        "vite.config.d.ts",
        "vitest.config.js",
        "vitest.config.d.ts",
        "tsconfig.node.tsbuildinfo",
    ):
        if (SITE_ROOT / generated_name).exists():
            fail(f"generated configuration artifact exists: {generated_name}")

    for path in DIST_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix == ".map":
            fail(f"source map found in dist: {path.relative_to(DIST_ROOT)}")
        if path.suffix not in TEXT_SUFFIXES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in DENIED_PATTERNS.items():
            if pattern.search(content):
                fail(f"{label} found in {path.relative_to(DIST_ROOT)}")


def validate_source_contracts() -> None:
    for path in (SITE_ROOT / "src").rglob("*.tsx"):
        content = path.read_text(encoding="utf-8")
        if "dangerouslySetInnerHTML" in content:
            fail(f"raw HTML injection found in {path.relative_to(SITE_ROOT)}")
        if re.search(r"\bfetch\s*\(", content):
            fail(f"runtime fetch found in {path.relative_to(SITE_ROOT)}")


def validate_repository_readme() -> None:
    readme = REPOSITORY_ROOT / "README.md"
    content = readme.read_text(encoding="utf-8")

    for label, pattern in DENIED_PATTERNS.items():
        if pattern.search(content):
            fail(f"{label} found in README.md")

    for raw_target in re.findall(r"\[[^]]+\]\(([^)]+)\)", content):
        if raw_target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target = raw_target.split("#", 1)[0]
        if target and not (REPOSITORY_ROOT / target).exists():
            fail(f"broken README.md link: {raw_target}")


if __name__ == "__main__":
    validate_source_svg()
    validate_single_source()
    validate_generated_tree()
    validate_source_contracts()
    validate_repository_readme()
    print("Static artifact validation passed.")

#!/usr/bin/env python3
"""Fail-closed validation for the repository documentation artifact.

Checks, in order: the source SVG, single-source SVG copying, the generated tree
(no source maps, no denied strings, no externally hosted asset, no browser
network API in the shipped code), the prerendered pages (sitemap, 404, llms.txt,
titles, canonical, Open Graph, hash shim, the six redirect stubs), the source
contracts (no raw HTML, no runtime fetch) and the repository README links.
Every failure names the file. Standard library only.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SITE_ROOT = REPOSITORY_ROOT / "docs-site"
DIST_ROOT = SITE_ROOT / "dist"
SOURCE_SVG = REPOSITORY_ROOT / "assets" / "repository-atlas-journey.svg"
DIST_SVG = DIST_ROOT / SOURCE_SVG.name
BASE_PATH = "/sample-ai-agent-factory/"

TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".svg", ".txt", ".webmanifest", ".xml"}

ACCOUNT_ID_PATTERN = re.compile(r"(?<!\d)\d{12}(?!\d)")
# Example account IDs used throughout AWS documentation. Any other 12-digit run is an error,
# with one narrow exemption: the Blueprint "AWS services" diagram
# (enterprise-agentic-ai-platform-blueprint/assets/*.svg, emitted by Vite as dist/assets/*.svg)
# labels its example accounts with a single repeated digit (444444444444, 555555555555, ...).
# That exemption applies only to SVG files directly under dist/assets, never to HTML, JS or the README.
ALLOWED_EXAMPLE_ACCOUNT_IDS = {"111122223333", "123456789012", "444455556666", "777788889999"}
REPEATED_DIGIT_ID_PATTERN = re.compile(r"^(\d)\1{11}$")

# Externally hosted assets. The site must load everything from its own origin; only <link> relations
# that describe the page (canonical, sitemap, alternate) may point at another host.
ASSET_SCAN_SUFFIXES = {".css", ".html", ".js", ".svg"}
EXTERNAL_URL = r"(?:https?:)?//"
EXTERNAL_ASSET_PATTERNS = {
    "external script": re.compile(r"<script\b[^>]*\bsrc\s*=\s*[\"']\s*" + EXTERNAL_URL, re.IGNORECASE),
    "external image": re.compile(
        r"<(?:img|image)\b[^>]*\b(?:src|href|xlink:href)\s*=\s*[\"']\s*" + EXTERNAL_URL, re.IGNORECASE
    ),
    "embedded frame or plugin (iframe, object, embed)": re.compile(r"<(?:iframe|object|embed)\b", re.IGNORECASE),
    "resource hint to another host (preconnect, dns-prefetch)": re.compile(
        r"\brel\s*=\s*[\"'][^\"']*\b(?:preconnect|dns-prefetch)\b", re.IGNORECASE
    ),
    # CSS functions are matched case-sensitively and on one line: JavaScript bundles contain
    # `new URL("http://...")` (a parser default, not a request), which must not count.
    "external CSS url()": re.compile(r"\burl\([ \t]*[\"']?[ \t]*" + EXTERNAL_URL),
    "external CSS @import": re.compile(r"@import[ \t]+(?:url\([ \t]*)?[\"']?[ \t]*" + EXTERNAL_URL),
}
LINK_TAG_PATTERN = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
EXTERNAL_URL_PATTERN = re.compile(r"^\s*" + EXTERNAL_URL, re.IGNORECASE)
EXTERNAL_LINK_RELS_ALLOWED = {"canonical", "sitemap", "alternate"}

# Browser network APIs that must not appear in shipped JavaScript or inline scripts: the site makes
# no runtime requests (fetch is already banned at source level by SOURCE_CONTRACT_PATTERNS).
NETWORK_SCAN_SUFFIXES = {".html", ".js"}
DIST_NETWORK_PATTERNS = {
    "XMLHttpRequest": re.compile(r"\bXMLHttpRequest\b"),
    "navigator.sendBeacon": re.compile(r"\bnavigator\s*\.\s*sendBeacon\b"),
    "WebSocket constructor": re.compile(r"\bnew\s+WebSocket\b"),
    "EventSource constructor": re.compile(r"\bnew\s+EventSource\b"),
}

DENIED_PATTERNS = {
    "local Unix user path": re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+"),
    "local Windows user path": re.compile(r"[A-Za-z]:[\\\\/]Users[\\\\/]", re.IGNORECASE),
    "private evidence marker": re.compile(r"private-evidence|tasks/(?:lessons|todo)\.md", re.IGNORECASE),
    "internal domain": re.compile(
        r"(?:[A-Za-z0-9-]+\.)*(?:a2z\.com|corp\.amazon\.com|amazon\.dev|aws\.dev)"
        r"|(?:w|code|issues|sim|i|cti|phonetool|pipelines|build|apollo)\.amazon\.com"
        r"|quip-amazon\.com",
        re.IGNORECASE,
    ),
    "other repository reference": re.compile(r"sample-agentcore-lowcode-nocode", re.IGNORECASE),
}

SOURCE_CONTRACT_PATTERNS = {
    "raw HTML injection (dangerouslySetInnerHTML)": re.compile(r"dangerouslySetInnerHTML"),
    "runtime fetch": re.compile(r"\bfetch\s*\("),
    "innerHTML assignment": re.compile(r"innerHTML\s*="),
    "rehype-raw import": re.compile(r"rehype-raw"),
}
BANNED_PACKAGES = {"rehype-raw"}

HEAD_PATTERN = re.compile(r"<head[^>]*>(.*?)</head>", re.IGNORECASE | re.DOTALL)
TITLE_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
CANONICAL_PATTERN = re.compile(r"<link[^>]+rel=[\"']canonical[\"']", re.IGNORECASE)
OG_TITLE_PATTERN = re.compile(r"<meta[^>]+property=[\"']og:title[\"']", re.IGNORECASE)
NOINDEX_PATTERN = re.compile(
    r"<meta[^>]+name=[\"']robots[\"'][^>]+content=[\"'][^\"']*noindex"
    r"|<meta[^>]+content=[\"'][^\"']*noindex[^\"']*[\"'][^>]+name=[\"']robots[\"']",
    re.IGNORECASE,
)
# Every prerendered page carries the inline hash shim behind this comment. Full pages (sitemap pages
# and 404.html) must test the legacy "#/" prefix; redirect stubs instead call location.replace on load.
HASH_SHIM_COMMENT = "<!-- hash-shim -->"
HASH_SHIM_TEST = "indexOf('#/')"
STUB_REDIRECT_CALL = "location.replace("
META_TAG_PATTERN = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
REFRESH_CONTENT_PATTERN = re.compile(r"^\s*0\s*;\s*url\s*=\s*(\S+)\s*$", re.IGNORECASE)
# Old hash-router routes. Each keeps a redirect stub at dist/<old>/index.html whose meta refresh,
# canonical and script all point at the new route. Exactly these stubs may exist outside the sitemap.
REDIRECT_STUBS = {
    "choose-a-path": "start/which-project/",
    "how-it-works": "concepts/agent-factory/",
    "capabilities": "concepts/capability-contracts/",
    "architecture": "concepts/architecture/",
    "security": "reference/security/",
    "getting-started": "start/",
}


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].lower()


def is_allowed_account_id(value: str, *, allow_repeated_digit: bool = False) -> bool:
    if value in ALLOWED_EXAMPLE_ACCOUNT_IDS:
        return True
    return allow_repeated_digit and REPEATED_DIGIT_ID_PATTERN.match(value) is not None


def allows_repeated_digit_ids(path: Path) -> bool:
    """Only the diagram SVGs Vite copies to dist/assets may use repeated-digit placeholder accounts."""
    return path.suffix == ".svg" and path.parent == DIST_ROOT / "assets"


def find_denied(content: str, *, allow_repeated_digit: bool = False) -> list[str]:
    """Return labels of denied content found in a text blob (empty when clean)."""
    findings: list[str] = []
    for match in ACCOUNT_ID_PATTERN.finditer(content):
        if not is_allowed_account_id(match.group(0), allow_repeated_digit=allow_repeated_digit):
            findings.append(f"12-digit account identifier ({match.group(0)})")
            break
    for label, pattern in DENIED_PATTERNS.items():
        if pattern.search(content):
            findings.append(label)
    return findings


def attribute_value(tag: str, name: str) -> str | None:
    match = re.search(r"\b" + re.escape(name) + r"\s*=\s*([\"'])(.*?)\1", tag, re.IGNORECASE | re.DOTALL)
    return match.group(2) if match else None


def find_external_assets(content: str) -> list[str]:
    """Return descriptions of resources a page or stylesheet would load from another host."""
    findings = [label for label, pattern in EXTERNAL_ASSET_PATTERNS.items() if pattern.search(content)]
    for tag in LINK_TAG_PATTERN.findall(content):
        href = attribute_value(tag, "href")
        if href is None or not EXTERNAL_URL_PATTERN.match(href):
            continue
        rels = set((attribute_value(tag, "rel") or "").lower().split())
        if not rels or not rels <= EXTERNAL_LINK_RELS_ALLOWED:
            findings.append(f"external <link rel=\"{' '.join(sorted(rels))}\"> to {href.strip()}")
    return findings


def find_network_apis(content: str) -> list[str]:
    return [label for label, pattern in DIST_NETWORK_PATTERNS.items() if pattern.search(content)]


def refresh_target(head: str) -> str | None:
    """The url of a <meta http-equiv="refresh" content="0; url=..."> tag, in either attribute order."""
    for tag in META_TAG_PATTERN.findall(head):
        if (attribute_value(tag, "http-equiv") or "").strip().lower() != "refresh":
            continue
        match = REFRESH_CONTENT_PATTERN.match(attribute_value(tag, "content") or "")
        if match:
            return match.group(1)
    return None


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
        "dist-ssr",
    ):
        if (SITE_ROOT / generated_name).exists():
            fail(f"generated build artifact exists and must be removed: {generated_name}")

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
        relative = f"dist/{path.relative_to(DIST_ROOT)}"
        for label in find_denied(content, allow_repeated_digit=allows_repeated_digit_ids(path)):
            fail(f"{label} found in {relative}")
        if path.suffix in ASSET_SCAN_SUFFIXES:
            for label in find_external_assets(content):
                fail(f"{label} found in {relative}; every asset must be served from the site itself")
        if path.suffix in NETWORK_SCAN_SUFFIXES:
            for label in find_network_apis(content):
                fail(f"{label} found in {relative}; the site must not make runtime network requests")


def sitemap_locations(sitemap: Path) -> list[str]:
    try:
        root = ElementTree.parse(sitemap).getroot()
    except ElementTree.ParseError as error:
        fail(f"invalid dist/sitemap.xml: {error}")
    locations = [element.text.strip() for element in root.iter() if local_name(element.tag) == "loc" and element.text]
    if not locations:
        fail("dist/sitemap.xml contains no <loc> entries")
    return locations


def page_for_location(location: str) -> Path:
    parts = urlsplit(location)
    if parts.scheme != "https" or not parts.netloc:
        fail(f"sitemap <loc> must be an absolute https URL: {location}")
    if not parts.path.startswith(BASE_PATH):
        fail(f"sitemap <loc> is outside the site base path {BASE_PATH}: {location}")
    if not parts.path.endswith("/"):
        fail(f"sitemap <loc> must end with a trailing slash: {location}")
    if parts.query or parts.fragment:
        fail(f"sitemap <loc> must not carry a query or fragment: {location}")
    return DIST_ROOT / parts.path[len(BASE_PATH):] / "index.html"


def validate_prerender() -> None:
    """Every route is a real HTML page with unique title, canonical, Open Graph and the hash shim,
    and the only pages outside the sitemap are the six redirect stubs for the old hash routes."""
    if not DIST_ROOT.is_dir():
        fail("dist does not exist; run npm run build first")

    for name in ("404.html", "sitemap.xml", "llms.txt"):
        if not (DIST_ROOT / name).is_file():
            fail(f"prerender output is missing: dist/{name}")

    locations = sitemap_locations(DIST_ROOT / "sitemap.xml")
    sitemap_pages: set[Path] = set()
    for location in locations:
        page = page_for_location(location)
        if not page.is_file():
            fail(f"sitemap <loc> {location} has no prerendered page at dist/{page.relative_to(DIST_ROOT)}")
        sitemap_pages.add(page)
    if len(sitemap_pages) != len(locations):
        fail("dist/sitemap.xml lists the same URL more than once")

    llms = (DIST_ROOT / "llms.txt").read_text(encoding="utf-8")
    if not llms.strip():
        fail("dist/llms.txt is empty")
    for location in locations:
        if location not in llms:
            fail(f"dist/llms.txt does not list the sitemap URL {location}")

    titles: dict[str, Path] = {}
    found_stubs: set[str] = set()
    pages = sorted(DIST_ROOT.rglob("index.html")) + [DIST_ROOT / "404.html"]
    for page in pages:
        relative = f"dist/{page.relative_to(DIST_ROOT)}"
        content = page.read_text(encoding="utf-8")
        is_stub = page.name != "404.html" and page not in sitemap_pages

        head_match = HEAD_PATTERN.search(content)
        if not head_match:
            fail(f"{relative} has no <head> element")
        head = head_match.group(1)

        found_titles = TITLE_PATTERN.findall(head)
        if len(found_titles) != 1:
            fail(f"{relative} must have exactly one <title> in <head>, found {len(found_titles)}")
        title = re.sub(r"\s+", " ", found_titles[0]).strip()
        if not title:
            fail(f"{relative} has an empty <title>")
        if title in titles:
            fail(f"duplicate <title> {title!r} in {relative} and dist/{titles[title].relative_to(DIST_ROOT)}")
        titles[title] = page

        if HASH_SHIM_COMMENT not in content:
            fail(f"{relative} is missing the hash shim comment {HASH_SHIM_COMMENT}")
        if is_stub:
            if STUB_REDIRECT_CALL not in content:
                fail(f"{relative} redirect stub does not call {STUB_REDIRECT_CALL})")
        elif HASH_SHIM_TEST not in content:
            fail(f"{relative} hash shim does not test for legacy hashes ({HASH_SHIM_TEST})")

        if page.name == "404.html":
            if not NOINDEX_PATTERN.search(head):
                fail(f"{relative} must carry a robots noindex meta tag")
            continue

        if not CANONICAL_PATTERN.search(head):
            fail(f"{relative} is missing <link rel=\"canonical\">")

        if not is_stub:
            if not OG_TITLE_PATTERN.search(head):
                fail(f"{relative} is missing <meta property=\"og:title\">")
            continue

        # Outside the sitemap only the known redirect stubs may exist, and each must send the
        # browser (meta refresh) to the exact new route, which must be a prerendered sitemap page.
        stub_dir = page.parent.relative_to(DIST_ROOT).as_posix()
        if stub_dir not in REDIRECT_STUBS:
            fail(f"{relative} is not listed in dist/sitemap.xml and is not a known redirect stub")
        target = refresh_target(head)
        if target is None:
            fail(f"{relative} redirect stub has no <meta http-equiv=\"refresh\" content=\"0; url=...\">")
        expected_target = BASE_PATH + REDIRECT_STUBS[stub_dir]
        if target != expected_target:
            fail(f"{relative} redirects to {target}, expected {expected_target}")
        target_page = DIST_ROOT / REDIRECT_STUBS[stub_dir] / "index.html"
        if not target_page.is_file():
            fail(f"{relative} redirects to {target} but dist/{target_page.relative_to(DIST_ROOT)} does not exist")
        if target_page not in sitemap_pages:
            fail(f"{relative} redirects to {target}, which is not listed in dist/sitemap.xml")
        found_stubs.add(stub_dir)

    missing_stubs = sorted(set(REDIRECT_STUBS) - found_stubs)
    if missing_stubs:
        fail(f"redirect stubs are missing for the old routes: {', '.join(missing_stubs)}")


def source_contract_files() -> list[Path]:
    files: list[Path] = []
    for pattern in ("*.ts", "*.tsx", "*.mjs"):
        files.extend((SITE_ROOT / "src").rglob(pattern))
    files.extend((SITE_ROOT / "scripts").glob("*.mjs"))
    files.extend((SITE_ROOT / "tests").rglob("*.ts"))
    files.append(SITE_ROOT / "index.html")
    files.extend(SITE_ROOT.glob("vite.config.*"))
    files.extend(SITE_ROOT.glob("vitest.config.*"))
    return sorted(path for path in files if path.is_file())


def validate_source_contracts() -> None:
    """No raw HTML rendering and no runtime network requests anywhere in the site source,
    including the HTML shell, the build scripts and the browser tests."""
    for path in source_contract_files():
        content = path.read_text(encoding="utf-8")
        for label, pattern in SOURCE_CONTRACT_PATTERNS.items():
            if pattern.search(content):
                fail(f"{label} found in {path.relative_to(SITE_ROOT)}")

    package_json = SITE_ROOT / "package.json"
    try:
        manifest = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"cannot read docs-site/package.json: {error}")
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        for name in manifest.get(section, {}):
            if name in BANNED_PACKAGES:
                fail(f"banned package {name} listed in docs-site/package.json {section}")


def validate_repository_readme() -> None:
    readme = REPOSITORY_ROOT / "README.md"
    content = readme.read_text(encoding="utf-8")

    for label in find_denied(content):
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
    validate_prerender()
    validate_source_contracts()
    validate_repository_readme()
    print("Static artifact validation passed.")

#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Fail when a CLI walkthrough and its notebook write different source files.

Why this exists
---------------
Several modules ship the same lesson twice: a CLI walkthrough in ``content/``
that writes a file with ``cat > path << 'PYEOF'``, and a notebook in ``source/``
that writes the same file from an embedded string. Participants are told to
follow *either* path, so the two copies have to agree — but nothing enforced it.

That cost us a real bug. ``travel_agent.py`` was fixed in
``02-deploy-fast.ipynb`` to resolve the region from ``AWS_REGION`` (AgentCore
Runtime sets that one; ``AWS_DEFAULT_REGION`` is not guaranteed) and the fix was
never ported to ``content/module-4/deploy/index.en.md``. The notebook path
worked in every region; the CLI path — the primary path — silently used
``us-west-2`` Memory and Code Interpreter no matter where the participant
deployed. Both files parsed, both deployed, and no test compared them.

What parity means here
----------------------
For each pair in ``PAIRS``, the file body extracted from the content page must
equal the body extracted from the notebook, after normalising typographic dashes
(prose in ``content/`` uses em/en dashes; the notebooks use ASCII) and trailing
whitespace. Both bodies must also parse as Python.

Add a pair here whenever a new page and notebook write the same file.

Guardrail parity
----------------
``GUARDRAIL_PAIRS`` covers a second, narrower kind of drift the file comparison
above cannot see: the two paths do not write a *file*, they each call
``CreateGuardrail`` — one via ``aws bedrock create-guardrail --content-policy-config
'<json>'``, the other via ``boto3`` keyword arguments. The bodies are different
languages, so they can never be compared literally.

That gap hid a live one. ``content/module-3b/step-7`` configured only the PII
policy while ``07-guardrails.ipynb`` configured PII *and* five content filters, so
a CLI participant attached a materially weaker guardrail to the response
interceptor than a notebook participant did — while the page's own
``--description`` claimed it screened "PII and harmful content".

Rather than parse two languages, this compares the set of Bedrock policy **type
tokens** (``HATE``, ``CREDIT_DEBIT_CARD_NUMBER``, ...). Those are uppercase string
literals in both a JSON blob and a Python list comprehension, so a plain scan
finds them either way. It will not catch a differing *strength* or *action*, which
is a deliberate limit: the check stays robust instead of becoming a parser.

Usage:
  verify-walkthrough-parity.py [repo-root]
"""
import ast
import difflib
import json
import pathlib
import re
import sys

# Each entry is (written filename, content page, notebook, notebook variable).
# The notebook checked is the one under source/; assets/ is covered separately by
# verify-assets-parity.py. Both connect pages write gateway.py, and they are
# deliberately DIFFERENT files (MCP path vs AgentCore path) — each is compared
# only against the notebook for its own path.
PAIRS = [
    (
        "travel_agent.py",
        "content/module-4/deploy/index.en.md",
        "source/module-4b-fast/notebooks/02-deploy-fast.ipynb",
        "TRAVEL_AGENT_PY",
    ),
    (
        "config.yaml",
        "content/module-4/deploy/index.en.md",
        "source/module-4b-fast/notebooks/02-deploy-fast.ipynb",
        "CONFIG_YAML",
    ),
    (
        "gateway.py",
        "content/module-4/connect-gateway-mcp/index.en.md",
        "source/module-4b-fast/notebooks/04a-connect-gateway-mcp.ipynb",
        "GATEWAY_PY",
    ),
    (
        "gateway.py",
        "content/module-4/connect-gateway-agentcore/index.en.md",
        "source/module-4b-fast/notebooks/04b-connect-gateway-agentcore.ipynb",
        "GATEWAY_PY",
    ),
]


# Each entry is (guardrail name, content page, notebook). The two paths create the
# same named guardrail, so whichever the participant runs first wins and the other
# reuses it -- which is exactly why their policies have to agree.
GUARDRAIL_PAIRS = [
    (
        "workshop-content-filter",
        "content/module-2/step-5/index.en.md",
        "source/module-2-llm-gateway/notebooks/step-5-guardrails.ipynb",
    ),
    (
        "workshop-tool-guardrail",
        "content/module-3a/tg-guardrails/index.en.md",
        "source/module-4a-tools-gateway/notebooks/06-bedrock-guardrails.ipynb",
    ),
    (
        "workshop-tool-output-guardrail",
        "content/module-3b/step-7/index.en.md",
        "source/module-3b-agentcore/notebooks/07-guardrails.ipynb",
    ),
]

# Bedrock content-filter and PII-entity type tokens. Extend when a workshop
# guardrail starts using one that is not listed -- an unlisted token is invisible
# to this check, so the list is the check's coverage.
GUARDRAIL_TYPES = (
    # contentPolicyConfig
    "HATE", "INSULTS", "MISCONDUCT", "PROMPT_ATTACK", "SEXUAL", "VIOLENCE",
    # sensitiveInformationPolicyConfig
    "ADDRESS", "AGE", "AWS_ACCESS_KEY", "AWS_SECRET_KEY",
    "CREDIT_DEBIT_CARD_CVV", "CREDIT_DEBIT_CARD_EXPIRY",
    "CREDIT_DEBIT_CARD_NUMBER", "DRIVER_ID", "EMAIL", "INTERNATIONAL_BANK_ACCOUNT_NUMBER",
    "IP_ADDRESS", "LICENSE_PLATE", "MAC_ADDRESS", "NAME", "PASSWORD", "PHONE",
    "PIN", "SWIFT_CODE", "URL", "USERNAME", "US_BANK_ACCOUNT_NUMBER",
    "US_BANK_ROUTING_NUMBER", "US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER",
    "US_PASSPORT_NUMBER", "US_SOCIAL_SECURITY_NUMBER", "VEHICLE_IDENTIFICATION_NUMBER",
)

_TYPE_RE = re.compile(r"""["'](%s)["']""" % "|".join(GUARDRAIL_TYPES))


def guardrail_types(path):
    """Policy type tokens a page or notebook configures, language-agnostically.

    Matches only quoted tokens so that prose ("Email addresses", "the NAME
    entity") cannot inflate the set. A notebook is decoded to its cell sources
    first — reading the ``.ipynb`` as raw text finds ``\\"HATE\\"`` with the quotes
    escaped for JSON, which no quote-anchored pattern will match.
    """
    if path.suffix == ".ipynb":
        nb = json.loads(path.read_text())
        text = "\n".join(
            "".join(c["source"]) for c in nb["cells"] if c.get("cell_type") == "code"
        )
    else:
        text = path.read_text()
    return set(_TYPE_RE.findall(text))


def normalise(text):
    """Dash style differs by medium and is not a behavioural difference."""
    for fancy, plain in (("—", "--"), ("–", "-"), ("→", "->")):
        text = text.replace(fancy, plain)
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def from_page(path, filename):
    """Body of the `cat > ...<filename> << 'HEREDOC'` block on a content page."""
    text = path.read_text()
    pattern = (
        r"cat > (?:\S*/)?" + re.escape(filename) + r" << '(\w+)'\n(.*?)\n\1\b"
    )
    matches = re.findall(pattern, text, re.S)
    if len(matches) != 1:
        raise LookupError(
            f"{path}: expected exactly one heredoc writing {filename}, "
            f"found {len(matches)}"
        )
    return matches[0][1]


def from_notebook(path, var):
    """Body of the embedded triple-quoted string a notebook writes a file from.

    The convention is ``VAR = r'''<body>'''`` (the ``r`` prefix is optional —
    config.yaml has no escapes to protect). A renamed constant fails loudly
    rather than silently matching nothing.
    """
    nb = json.loads(path.read_text())
    bodies = []
    for cell in nb["cells"]:
        if cell.get("cell_type") != "code":
            continue
        found = re.findall(
            re.escape(var) + r" = r?'''(.*?)'''", "".join(cell["source"]), re.S
        )
        bodies.extend(found)
    if len(bodies) != 1:
        raise LookupError(
            f"{path}: expected exactly one `{var} = '''...'''`, "
            f"found {len(bodies)}"
        )
    return bodies[0]


def main(argv):
    root = pathlib.Path(argv[1] if len(argv) > 1 else ".").resolve()
    problems = []
    for filename, page_rel, nb_rel, var in PAIRS:
        page, nb = root / page_rel, root / nb_rel
        missing = [c for c in (page, nb) if not c.is_file()]
        if missing:
            problems += [f"{c.relative_to(root)}: missing" for c in missing]
            continue
        try:
            page_body = from_page(page, filename)
            nb_body = from_notebook(nb, var)
        except LookupError as exc:
            problems.append(str(exc))
            continue

        if filename.endswith(".py"):
            for label, body in ((page_rel, page_body), (nb_rel, nb_body)):
                try:
                    ast.parse(body)
                except SyntaxError as exc:
                    problems.append(f"{label}: {filename} does not parse: {exc}")

        if normalise(page_body) != normalise(nb_body):
            diff = difflib.unified_diff(
                normalise(page_body).splitlines(),
                normalise(nb_body).splitlines(),
                page_rel,
                nb_rel,
                lineterm="",
            )
            problems.append(
                f"{filename}: CLI walkthrough and notebook disagree\n"
                + "\n".join(f"    {line}" for line in diff)
            )
        else:
            print(f"OK  {filename}: {page_rel} == {nb_rel}")

    for name, page_rel, nb_rel in GUARDRAIL_PAIRS:
        page, nb = root / page_rel, root / nb_rel
        missing = [c for c in (page, nb) if not c.is_file()]
        if missing:
            problems += [f"{c.relative_to(root)}: missing" for c in missing]
            continue
        page_types, nb_types = guardrail_types(page), guardrail_types(nb)
        if not page_types or not nb_types:
            problems.append(
                f"{name}: no guardrail policy types found in "
                f"{page_rel if not page_types else nb_rel} — either the guardrail "
                f"moved or it uses a type missing from GUARDRAIL_TYPES"
            )
        elif page_types != nb_types:
            only_page = sorted(page_types - nb_types)
            only_nb = sorted(nb_types - page_types)
            detail = []
            if only_page:
                detail.append(f"only on the page: {', '.join(only_page)}")
            if only_nb:
                detail.append(f"only in the notebook: {', '.join(only_nb)}")
            problems.append(
                f"{name}: the two paths build different guardrails "
                f"({'; '.join(detail)})\n"
                f"    page:     {page_rel}\n"
                f"    notebook: {nb_rel}"
            )
        else:
            print(f"OK  {name}: {len(page_types)} policy types match across both paths")

    if problems:
        print("\nwalkthrough parity FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nFix by porting the change to BOTH copies. Participants are told "
            "to follow either path, so a fix in one is a bug in the other.",
            file=sys.stderr,
        )
        return 1
    print("walkthrough parity OK — every dual-path file matches.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

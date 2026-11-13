#!/usr/bin/env python3
"""Write schemas/agent-spec-v2.schema.json from the Pydantic model, or check that it is current.

    backend/.venv/bin/python scripts/export_agent_spec_schema.py          # write
    backend/.venv/bin/python scripts/export_agent_spec_schema.py --check  # exit 1 if stale

``backend/tests/test_agent_spec_v2.py`` runs the check, so CI fails when the model changes and the
committed schema does not. The file is the contract for every non-Python consumer (the TypeScript
types the frontend will generate from it, external validators, the docs).
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "agent-spec-v2.schema.json"


def render() -> str:
    sys.path.insert(0, str(ROOT / "backend" / "src"))
    from app.schemas.agent_spec_v2 import json_schema

    return json.dumps(json_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        current = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else ""
        if current != text:
            print(f"{SCHEMA_PATH.relative_to(ROOT)} is stale; run scripts/export_agent_spec_schema.py", file=sys.stderr)
            return 1
        print("schema current")
        return 0
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {SCHEMA_PATH.relative_to(ROOT)} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

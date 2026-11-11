#!/usr/bin/env python3
"""Export the live FastAPI OpenAPI schema to docs/api-openapi.json.

Run from the project root (``parking-system``)::

    .venv/Scripts/python.exe tools/export_openapi.py
    .venv/Scripts/python.exe tools/export_openapi.py --check

``--check`` verifies that every path/method in the existing file is present in
the live schema and that the live schema covers the expected current routes.
Exit code 0 means the snapshot is current; 1 means it is stale.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

METHODS = ("get", "post", "put", "delete", "patch")


def live_schema() -> dict:
    from server.main import app

    return app.openapi()


def operations(schema: dict) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for path, item in schema.get("paths", {}).items():
        for method in item:
            if method.lower() in METHODS:
                out.add((method.upper(), path))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "docs" / "api-openapi.json"))
    parser.add_argument("--check", action="store_true",
                        help="compare the existing snapshot with the live schema")
    args = parser.parse_args()
    out = Path(args.out)
    schema = live_schema()
    live = operations(schema)
    if not live:
        print("ERROR: live OpenAPI schema has no operations", file=sys.stderr)
        return 1
    if args.check:
        if not out.exists():
            print(f"STALE: {out} does not exist", file=sys.stderr)
            return 1
        old = json.loads(out.read_text(encoding="utf-8"))
        old_ops = operations(old)
        missing = sorted(old_ops - live)
        new = sorted(live - old_ops)
        print(f"live paths={len(schema.get('paths', {}))} operations={len(live)}")
        print(f"snapshot paths={len(old.get('paths', {}))} operations={len(old_ops)}")
        if missing:
            print("snapshot has operations absent from live app:")
            for m, p in missing:
                print(f"  {m} {p}")
        if new:
            print("live app has operations absent from snapshot:")
            for m, p in new:
                print(f"  {m} {p}")
        if missing or new:
            print("STALE: regenerate with tools/export_openapi.py")
            return 1
        print("OK: snapshot matches live app")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"wrote {out} paths={len(schema.get('paths', {}))} operations={len(live)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

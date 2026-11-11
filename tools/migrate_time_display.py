"""Explain P1-04 time display migration without modifying the database."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.core.config import load_config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()
    cfg = load_config(args.config, force=True)
    print(json.dumps({
        "database": str(cfg.paths.db_file),
        "stored_time": "naive UTC; unchanged",
        "api_display_timezone": cfg.server.timezone,
        "migration_required": False,
        "action": "Deploy the new code; do not shift historical SQLite timestamps.",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

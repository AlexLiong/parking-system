#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
TASK_PYTHON="$TASK_ROOT/.venv/bin/python"
if [[ ! -x "$TASK_PYTHON" ]]; then TASK_PYTHON=python3; fi
exec "$TASK_PYTHON" "$TASK_ROOT/tools/ci.py" "$@"

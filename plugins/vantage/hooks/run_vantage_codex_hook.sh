#!/bin/sh

python_path="${VANTAGE_CODEX_PYTHON:-}"
if [ -z "$python_path" ]; then
  python_path="$(command -v python3 2>/dev/null || true)"
fi

if [ -z "$python_path" ] || [ ! -x "$python_path" ]; then
  exit 0
fi

if [ "$python_path" = "/usr/bin/python3" ] && command -v xcode-select >/dev/null 2>&1; then
  if ! xcode-select -p >/dev/null 2>&1; then
    exit 0
  fi
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
launcher="${script_dir}/run_vantage_codex_hook.py"
[ -f "$launcher" ] || exit 0
exec "$python_path" -I "$launcher"

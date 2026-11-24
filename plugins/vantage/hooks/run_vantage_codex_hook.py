#!/usr/bin/env python3
"""Run the newest installed Vantage Codex hook."""

import json
import os
import pathlib
import re
import runpy
import sys


SEMVER = re.compile(
    r"^(?:v)?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def version_key(value):
    match = SEMVER.fullmatch(value)
    if not match:
        return None
    major, minor, patch, prerelease = match.groups()
    prerelease_key = ()
    if prerelease is not None:
        prerelease_key = tuple(
            (0, int(part)) if part.isdigit() else (1, part)
            for part in prerelease.split(".")
        )
    return (
        int(major),
        int(minor),
        int(patch),
        prerelease is None,
        prerelease_key,
    )


def declared_version(plugin_root):
    for manifest in (
        plugin_root / "plugin.json",
        plugin_root / ".codex-plugin" / "plugin.json",
    ):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        value = payload.get("version") if isinstance(payload, dict) else None
        key = version_key(value) if isinstance(value, str) else None
        if key is not None:
            return key
    return None


def plugin_roots(env):
    codex_home = pathlib.Path(env.get("CODEX_HOME") or "~/.codex").expanduser()
    roots = []
    explicit_root = env.get("VANTAGE_CODEX_PLUGIN_ROOT", "").strip()
    if explicit_root:
        roots.append(pathlib.Path(explicit_root).expanduser())

    for cache_root in (
        codex_home / "plugins" / "cache" / "workspace-directory" / "vantage",
        codex_home / "plugins" / "cache" / "vantage-codex-plugin" / "vantage",
        codex_home / "plugins" / "cache" / "vantage-codex-plugin",
    ):
        try:
            roots.extend(path for path in cache_root.iterdir() if path.is_dir())
        except OSError:
            continue
    return roots


def newest_hook(env=os.environ):
    candidates = []
    for plugin_root in plugin_roots(env):
        hook = plugin_root / "hooks" / "vantage_codex_hook.py"
        version = declared_version(plugin_root)
        if version is not None and hook.is_file():
            candidates.append((version, str(plugin_root), hook))
    if not candidates:
        return None
    return max(candidates)[2]


def main():
    if sys.version_info < (3, 9):
        if sys.argv[1:]:
            print("Vantage telemetry requires Python 3.9 or newer.", file=sys.stderr)
            return 4
        return 0
    hook = newest_hook()
    if hook is None:
        if sys.argv[1:]:
            print("Vantage attribution hook is not installed.", file=sys.stderr)
            return 1
        return 0
    runpy.run_path(str(hook), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())

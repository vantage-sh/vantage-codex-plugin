#!/usr/bin/env python3
"""Vantage Codex attribution hook. Run directly with Python 3.9+.
SQLite captures evidence. A detached copy of this script exports OTLP/gRPC.
"""

from __future__ import annotations

import argparse
import datetime
import errno
import hashlib
import json
import os
from functools import lru_cache
import re
import random
import platform
import shlex
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

if os.name == "nt":
    import msvcrt
else:
    import fcntl

COMMON = (
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "Stop",
    "SessionEnd",
)
GIT_TOOLS = {
    "Bash", "Edit", "Write", "MultiEdit", "exec_command", "apply_patch", "shell", "shell_command",
}
GIT_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
ORIGIN_HEAD = "refs/remotes/origin/HEAD"
GIT_LAYOUT_OVERRIDES = {
    "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
}


@dataclass(frozen=True)
class Agent:
    turn_field: str
    events: tuple[str, ...]
    child_owner_field: str | None = None

    def session(self, payload):
        return identifier(
            payload.get(self.child_owner_field) or payload.get("session_id")
        )


AGENTS = {
    "codex": Agent(
        "turn_id", COMMON + ("PostModelRequest", "SubagentStart"), "agent_id"
    ),
    "claude_code": Agent("prompt_id", COMMON),
}


def identifier(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[\w.:/-]{1,180}", value, re.ASCII)
        else None
    )


def run_git(cwd, *args):
    try:
        return subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=0.5,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            check=False,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None


def git_output(cwd, *args):
    result = run_git(cwd, *args)
    return result.stdout.strip() if result and result.returncode == 0 else ""


def git_config(cwd):
    result = run_git(
        cwd, "config", "--null", "--get-regexp",
        r"^(user\.(name|email)|include\.path|includeif\..*\.path)$",
    )
    if not result or result.returncode not in (0, 1):
        return None, False
    fields = dict(entry.split("\n", 1) for entry in result.stdout.split("\0") if "\n" in entry)
    email = fields.get("user.email", "")
    name = fields.get("user.name", "").strip()
    if len(email) > 254 or not re.fullmatch(r"[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+", email):
        email = ""
    if len(name.encode()) > 256 or not name.isprintable():
        name = ""
    return (email, name), not any(key.startswith("include") for key in fields)


def repository_name(remote):
    if "://" not in remote:
        remote = re.sub(r"^([\w.-]+@)?([\w.-]+):", r"ssh://\2/", remote)
    try:
        url = urlsplit(remote)
        if url.hostname and url.scheme in {"https", "http", "ssh", "git"}:
            repo = url.path.strip("/").removesuffix(".git")
            parts = repo.split("/")
            if len(parts) >= 2 and re.fullmatch(r"[\w./-]+", repo) and not any(part in {"", ".", ".."} for part in parts):
                return f"{url.hostname}/{repo}"
    except ValueError:
        pass
    return ""


def git_layout(cwd):
    # Let Git handle explicit environment overrides and nonstandard repositories.
    if GIT_LAYOUT_OVERRIDES.intersection(os.environ) or any(key.startswith("GIT_CONFIG") for key in os.environ):
        return None
    path = Path(cwd).resolve()
    if not path.is_dir():
        return None
    for directory in (path, *path.parents):
        marker = directory / ".git"
        if marker.is_dir():
            git_dir = marker.resolve()
        elif marker.is_file():
            pointer = marker.read_text().strip()
            if not pointer.startswith("gitdir: "):
                return None
            git_dir = (directory / pointer.removeprefix("gitdir: ")).resolve()
        else:
            continue
        common_file = git_dir / "commondir"
        common_dir = (
            (git_dir / common_file.read_text().strip()).resolve()
            if common_file.is_file() else git_dir
        )
        return git_dir, common_dir
    return None


def file_stamp(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def git_head_stamp(git_dir, common_dir):
    if (common_dir / "reftable").exists():
        return None
    head = git_dir / "HEAD"
    stamp = file_stamp(head)
    if stamp is None or head.is_symlink():
        return None
    value = head.read_text().strip()
    if GIT_OID.fullmatch(value):
        return ["", stamp]
    if not value.startswith("ref: refs/heads/"):
        return None
    ref = Path(value.removeprefix("ref: "))
    if ".." in ref.parts:
        return None
    loose = common_dir / ref
    ref_stamp = file_stamp(loose)
    if ref_stamp is not None and not GIT_OID.fullmatch(loose.read_text().strip()):
        return None
    return [
        ref.as_posix().removeprefix("refs/heads/"), stamp, ref_stamp,
        file_stamp(common_dir / "packed-refs") if ref_stamp is None else None,
    ]


def git_config_stamp(git_dir, common_dir):
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    files = [
        common_dir / "config", git_dir / "config.worktree",
        Path.home() / ".gitconfig", xdg / "git" / "config", Path("/etc/gitconfig"),
        common_dir / ORIGIN_HEAD,
    ]
    executable = shutil.which("git")
    if executable:
        files.append(Path(executable).resolve().parent.parent / "etc/gitconfig")
    return [file_stamp(path) for path in files]


def git_default_ref(cwd, layout):
    if layout and not (layout[1] / "reftable").exists():
        try:
            path = layout[1] / ORIGIN_HEAD
            ref = path.read_text().strip() if path.is_file() else ""
            if not ref or GIT_OID.fullmatch(ref):
                return "", True
            if ref.startswith("ref: refs/remotes/origin/"):
                target = ref.removeprefix("ref: ")
                if ".." not in Path(target).parts:
                    target_file = layout[1] / target
                    if not target_file.is_file() or GIT_OID.fullmatch(target_file.read_text().strip()):
                        return target.removeprefix("refs/remotes/"), True
        except (OSError, ValueError):
            pass
    return git_output(cwd, "symbolic-ref", "--short", "-q", ORIGIN_HEAD), False


def git_observation(db, cwd, salt, *, observe=True):
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return {}, None
    layout = head_stamp = config_stamp = None
    cached = {}
    try:
        layout = git_layout(cwd)
        if layout:
            head_stamp = git_head_stamp(*layout)
            config_stamp = git_config_stamp(*layout)
            key = hashlib.sha256(salt + os.fsencode(layout[0])).hexdigest()
            row = db.execute("SELECT state FROM git_worktrees WHERE worktree=?", (key,)).fetchone()
            if row:
                cached = json.loads(row[0])
    except (OSError, ValueError, RuntimeError):
        layout = head_stamp = config_stamp = None

    if not layout and not observe:
        return {}, git_config(cwd)[0]

    same_head = head_stamp is not None and head_stamp == cached.get("head_stamp")
    if same_head:
        facts = cached["facts"]
    else:
        # Git still resolves the commit, including packed refs and linked worktrees.
        result = run_git(
            cwd, "rev-parse", "--absolute-git-dir",
            "--git-common-dir", "HEAD", "--symbolic-full-name", "HEAD",
        )
        fields = result.stdout.splitlines() if result else []
        if len(fields) < 2 or not os.path.isabs(fields[0]) or not fields[1]:
            return {}, git_config(cwd)[0]
        git_dir, common_dir = fields[:2]
        try:
            if layout != (Path(git_dir).resolve(), (Path(cwd) / common_dir).resolve()):
                layout = head_stamp = config_stamp = None
        except (OSError, RuntimeError):
            layout = head_stamp = config_stamp = None
        worktree = hashlib.sha256(salt + os.fsencode(git_dir)).hexdigest()
        head = fields[2] if result.returncode == 0 and len(fields) >= 3 and GIT_OID.fullmatch(fields[2]) else ""
        symbol = fields[3] if len(fields) == 4 else ""
        if symbol == "HEAD" or symbol.startswith("refs/heads/"):
            branch = symbol.removeprefix("refs/heads/") if symbol != "HEAD" else ""
        elif head_stamp is not None and not head:
            branch = head_stamp[0]
        else:
            branch = git_output(cwd, "symbolic-ref", "--short", "-q", "HEAD")
        facts = {"worktree": worktree, "branch": branch, "head": head}
        if result.returncode != 0 and fields[2:] != ["HEAD"]:
            head_stamp = None

    same_config = config_stamp is not None and config_stamp == cached.get("config_stamp")
    if same_config:
        identity, remote, default_ref = cached["metadata"]
        identity = tuple(identity)
    else:
        identity, cache_config = git_config(cwd)
        remote_result = run_git(cwd, "remote", "get-url", "origin")
        remote = repository_name(remote_result.stdout.strip()) if remote_result and remote_result.returncode == 0 else ""
        default_ref, cache_default = git_default_ref(cwd, layout)
        if not cache_config or not cache_default or remote_result is None:
            config_stamp = None

    if layout and not (same_head and same_config):
        try:
            if head_stamp != git_head_stamp(*layout):
                head_stamp = None
            if config_stamp != git_config_stamp(*layout):
                config_stamp = None
        except (OSError, ValueError):
            head_stamp = config_stamp = None
        if head_stamp is not None or config_stamp is not None:
            state = {"head_stamp": head_stamp, "facts": facts,
                     "config_stamp": config_stamp, "metadata": [identity, remote, default_ref]}
            db.execute("INSERT OR REPLACE INTO git_worktrees VALUES (?,?)", (key, json.dumps(state)))

    facts = dict(facts)
    if default_ref.startswith("origin/") and facts["branch"]:
        facts["branch_is_default"] = facts["branch"] == default_ref.removeprefix("origin/")
    if remote:
        facts["repository"] = remote
    return facts, identity


REQUEST = "vantage.request_correlation"
TURN = "vantage.turn_attribution"
HOME = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser().resolve()
PLUGIN_MANIFEST = Path(__file__).resolve().parents[1] / "plugin.json"
try:
    PLUGIN_VERSION = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))["version"]
except (OSError, ValueError, TypeError, KeyError):
    PLUGIN_VERSION = "unknown"
if not isinstance(PLUGIN_VERSION, str) or not PLUGIN_VERSION:
    PLUGIN_VERSION = "unknown"
USER_AGENT = f"vantage-codex-plugin/{PLUGIN_VERSION}"
DEFAULT_CLASSIFIER_ENDPOINT = "https://api.staging.vantage.sh/plugins/classifier"
MAX_CLASSIFIER_BYTES = 64 * 1024
CLASSIFIER_TIMEOUT = 2
CONNECTION = HOME / "vantage" / "connection.json"
ENDPOINT = None
STATE = None
TOKEN = None
OPT_IN = None
SOURCE = "tag"


def codex_home(env):
    configured = str(env.get("CODEX_HOME", "")).strip()
    return Path(configured or "~/.codex").expanduser()


def classifier_directory(env):
    return codex_home(env) / "vantage"


def classifier_token_path(env):
    return classifier_directory(env) / "classifier_read_token"


def classifier_markdown_path(env):
    return classifier_directory(env) / "classifier.md"


def classifier_etag_path(env):
    return classifier_directory(env) / "classifier.etag"


def classifier_endpoint(env):
    return (
        str(env.get("VANTAGE_CLASSIFIER_ENDPOINT", "")).strip()
        or DEFAULT_CLASSIFIER_ENDPOINT
    )


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def refresh_classifier(env, stderr=sys.stderr):
    cached = classifier_markdown_path(env).is_file()
    try:
        try:
            token = classifier_token_path(env).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return cached
        if not token:
            return cached

        endpoint = classifier_endpoint(env)
        parsed = urllib.parse.urlparse(endpoint)
        if not https_or_loopback(parsed):
            raise ValueError(
                f"refusing to send the classifier credential over {parsed.scheme}"
            )

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
        }
        try:
            etag = classifier_etag_path(env).read_text(encoding="utf-8").strip()
            if etag:
                headers["If-None-Match"] = etag
        except (OSError, UnicodeError):
            pass

        request = urllib.request.Request(endpoint, headers=headers, method="GET")
        opener = urllib.request.build_opener(NoRedirectHandler)
        try:
            response = opener.open(request, timeout=CLASSIFIER_TIMEOUT)
        except urllib.error.HTTPError as error:
            if error.code == 304:
                return cached
            raise

        with response:
            raw = response.read(MAX_CLASSIFIER_BYTES + 1)
            if len(raw) > MAX_CLASSIFIER_BYTES:
                raise ValueError("classifier response is too large")
            payload = json.loads(raw.decode("utf-8"))
            markdown = payload.get("classifier_markdown") if isinstance(payload, dict) else None
            if not isinstance(markdown, str) or not markdown.strip():
                raise ValueError("classifier response is empty")
            write_private_file(classifier_markdown_path(env), markdown.encode("utf-8"))
            new_etag = response.headers.get("ETag", "").strip()
            if new_etag:
                write_private_file(classifier_etag_path(env), new_etag.encode("utf-8"))
        return True
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
        print(
            f"[vantage_codex_hook] classifier refresh failed: {type(error).__name__}",
            file=stderr,
        )
        return cached


def write_private_file(path, content):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def https_or_loopback(parsed):
    return parsed.scheme == "https" or (
        parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    )


def load_connection():
    global STATE, TOKEN, OPT_IN, ENDPOINT
    connection = json.loads(CONNECTION.read_text())
    TOKEN = connection["token"]
    ENDPOINT = connection["endpoint"]
    if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{8,512}", TOKEN) or not https_or_loopback(urlsplit(ENDPOINT)):
        raise ValueError("invalid ingest connection")
    binding = hashlib.sha256((ENDPOINT + "\0" + TOKEN).encode()).hexdigest()
    STATE = CONNECTION.parent / "outbox" / binding
    OPT_IN = connection["since_ns"]


def connect():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(STATE / "state.sqlite", timeout=5, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            agent TEXT, session TEXT, home TEXT, offset INTEGER DEFAULT 0,
            PRIMARY KEY(agent, session));
        CREATE TABLE IF NOT EXISTS turns (
            agent TEXT, session TEXT, turn TEXT, revision INTEGER, state TEXT,
            PRIMARY KEY(agent, session, turn));
        CREATE TABLE IF NOT EXISTS prompt_contexts (
            agent TEXT, session TEXT, turn TEXT, context TEXT, classified INTEGER DEFAULT 0,
            PRIMARY KEY(agent, session, turn));
        CREATE INDEX IF NOT EXISTS prompt_context_session ON prompt_contexts(agent, session);
        CREATE TABLE IF NOT EXISTS outbox (
            id TEXT PRIMARY KEY, timestamp INTEGER, payload TEXT);
        CREATE TABLE IF NOT EXISTS git_identity (
            agent TEXT, session TEXT, turn TEXT, at_ns INTEGER, email TEXT NOT NULL,
            PRIMARY KEY(agent, session, turn, at_ns));
        CREATE TABLE IF NOT EXISTS git_names (
            agent TEXT, session TEXT, turn TEXT, at_ns INTEGER, name TEXT NOT NULL,
            PRIMARY KEY(agent, session, turn, at_ns));
        CREATE TABLE IF NOT EXISTS git_worktrees (worktree TEXT PRIMARY KEY, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS install (id INTEGER PRIMARY KEY, salt BLOB NOT NULL);
        INSERT OR IGNORE INTO install VALUES (1,randomblob(16));
        CREATE TABLE IF NOT EXISTS diagnostics (name TEXT PRIMARY KEY, count INTEGER);
        CREATE TABLE IF NOT EXISTS cli_versions (agent TEXT, session TEXT, version TEXT,
            PRIMARY KEY(agent, session));
        CREATE TABLE IF NOT EXISTS turn_models (agent TEXT, session TEXT, turn TEXT, model TEXT,
            PRIMARY KEY(agent, session, turn, model));
        CREATE TABLE IF NOT EXISTS sender (id INTEGER PRIMARY KEY, failures INTEGER DEFAULT 0,
            retry_at REAL DEFAULT 0, last_ack REAL DEFAULT 0, discovered REAL DEFAULT 0);
        INSERT OR IGNORE INTO sender(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS recovery_queue (session TEXT PRIMARY KEY, generation INTEGER);
        CREATE TABLE IF NOT EXISTS rollout_paths (session TEXT PRIMARY KEY, path TEXT);
        CREATE TABLE IF NOT EXISTS versions_at (agent TEXT, session TEXT, at_ns INTEGER, version TEXT,
            PRIMARY KEY(agent,session,at_ns));
    """)
    return db


@contextmanager
def write(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def observed_identity(db, key, at_ns):
    row = db.execute(
        "SELECT email FROM git_identity WHERE agent=? AND session=? AND turn=? "
        "AND at_ns<=? ORDER BY at_ns DESC LIMIT 1",
        (*key, at_ns),
    ).fetchone()
    name = db.execute(
        "SELECT name FROM git_names WHERE agent=? AND session=? AND turn=? "
        "AND at_ns<=? ORDER BY at_ns DESC LIMIT 1",
        (*key, at_ns),
    ).fetchone()
    return (row[0] if row else ""), (name[0] if name else "")


@lru_cache(maxsize=1)
def device_facts():
    return {"os.type": platform.system().lower(), "os.version": platform.release(),
            "host.arch": platform.machine()}


def enqueue(db, kind, key, attrs, timestamp=None, event_id=None):
    agent, session, turn = key
    event_id = event_id or str(uuid.uuid4())
    timestamp = timestamp if timestamp is not None else time.time_ns()
    email, name = observed_identity(db, key, timestamp)
    version = db.execute("SELECT version FROM versions_at WHERE agent=? AND session=? AND at_ns<=? "
                         "ORDER BY at_ns DESC LIMIT 1", (*key[:2], timestamp)).fetchone()
    models = [row[0] for row in db.execute("SELECT model FROM turn_models WHERE agent=? AND session=? AND turn=?", key)]
    turn_model = models[0] if len(models) == 1 else "multiple" if models else "unknown"
    summary = kind == TURN
    payload = {
        "git.identity.source": "git_config" if email or name else "unknown",
        "agent.version": version[0] if version else "unknown",
        "model.source": ("turn" if models else "unknown") if summary else ("request" if attrs.get("model") else "unknown"),
        "model": turn_model if summary else "unknown",
        **({key: "unknown" for key in ("os.type", "os.version", "host.arch")}
           if SOURCE == "rollout" else device_facts()),
        "vantage.capture.source": SOURCE,
        "vantage.plugin.version": PLUGIN_VERSION,
        "vantage.schema.version": "1",
        "event.name": kind,
        "event_id": event_id,
        "agent": agent,
        "session.id": session,
        "turn.id": turn,
        **attrs,
    }
    if name:
        payload["git.user.name"] = name
    if email:
        payload["git.user.email"] = email
    db.execute(
        "INSERT OR IGNORE INTO outbox VALUES (?,?,?)",
        (event_id, timestamp, json.dumps(payload)),
    )


def snapshot(
    db, key, *, facts=None, changes=None, tags=None, tag_source="manual", timestamp=None
):
    row = db.execute(
        "SELECT revision,state FROM turns WHERE agent=? AND session=? AND turn=?", key
    ).fetchone()
    revision, state = (
        (row[0], json.loads(row[1])) if row else (0, {"tags": {}, "git": []})
    )
    previous = json.dumps(state, sort_keys=True)
    if facts and (not state["git"] or state["git"][-1]["facts"] != facts):
        state["git"].append({"at_ns": time.time_ns(), "facts": facts})
    for k, v in (changes or {}).items():
        if (
            k in ("first_seen_ns", "first_source", "started_ns", "ended_ns")
            and k in state
        ):
            continue
        state[k] = v
    for name, value in (tags or {}).items():
        if value is None:
            state["tags"].pop(name, None)
            state.setdefault("tag_sources", {}).pop(name, None)
        else:
            state["tags"][name] = value
            state.setdefault("tag_sources", {})[name] = tag_source
    encoded = json.dumps(state, sort_keys=True)
    while len(encoded.encode()) > 64 * 1024 and len(state["git"]) > 2:
        del state["git"][1]
        state["omitted_git_observations"] = state.get("omitted_git_observations", 0) + 1
        encoded = json.dumps(state, sort_keys=True)
    if row and previous == encoded:
        return
    db.execute(
        "INSERT OR REPLACE INTO turns VALUES (?,?,?,?,?)", (*key, revision + 1, encoded)
    )
    enqueue(
        db, TURN, key, {"revision": revision + 1, "state": encoded}, timestamp=timestamp
    )


def correlation(
    db, key, usage, source, *, status, model=None, response=None, **options
):
    attrs = {"source": source, "status": status}
    if model:
        attrs["model"] = model
    if response:
        attrs["response_id"] = response
    for name in (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    ):
        value = (usage or {}).get(name)
        if type(value) is int and 0 <= value < 2**63:
            attrs["fp." + name] = value
    enqueue(db, REQUEST, key, attrs, **options)


def project(db, session, offset, line):
    entry = json.loads(line)
    if entry.get("type") == "session_meta":
        meta = entry.get("payload", {})
        version = identifier(meta.get("cli_version"))
        if version and meta.get("id") == session:
            when = datetime.datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
            db.execute("INSERT OR REPLACE INTO versions_at VALUES ('codex',?,?,?)",
                       (session, int(when.timestamp() * 1e9), version))
        return
    if entry.get("type") != "token_usage_record":
        return
    p = entry["payload"]
    turn = identifier(p.get("turn_id"))
    if p.get("thread_id") != session or not turn:
        return
    when = datetime.datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))  # noqa: FURB162 -- Python 3.9
    key = ("codex", session, turn)
    ns = int(when.timestamp() * 1e9)
    if OPT_IN and ns < OPT_IN:
        return
    response = identifier(p.get("response_id"))
    event_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"codex:{session}:{response or offset}")
    )
    snapshot(
        db, key, changes={"first_seen_ns": ns, "first_source": "rollout"}, timestamp=ns
    )
    correlation(
        db,
        key,
        p["usage"],
        "rollout",
        status="completed",
        response=response,
        model=identifier(p.get("model")),
        timestamp=ns,
        event_id=event_id,
    )


def recover(db, targets=None, *, after_chunk=None):
    sessions = db.execute("SELECT session,home,offset FROM sessions WHERE agent='codex'")
    by_home = {str(HOME): {}} if OPT_IN else {}
    for session, home, offset in sessions:
        by_home.setdefault(home, {})[session] = offset
    for home, offsets in by_home.items():
        if targets is None:
            paths = (path for directory in ("sessions", "archived_sessions")
                     for path in (Path(home) / directory).rglob("*.jsonl"))
        else:
            paths = [Path(row[0]) for session in targets if session in offsets
                     for row in db.execute("SELECT path FROM rollout_paths WHERE session=?", (session,))]
        for path in paths:
            # Codex file names end with the 36-character owning thread UUID.
            session = path.stem[-36:]
            if session not in offsets:
                # Discover only threads created after setup for this binding.
                # A UUIDv7 timestamp is independent of file copy or archive times.
                try:
                    thread = uuid.UUID(session)
                except ValueError:
                    continue
                if not OPT_IN or thread.version != 7 or (thread.int >> 80) * 1_000_000 < OPT_IN:
                    continue
                db.execute("INSERT OR IGNORE INTO sessions(agent,session,home) VALUES ('codex',?,?)", (session, home))
                offsets[session] = 0
            offset = offsets[session]
            try:
                with path.open("rb") as stream:
                    stream.seek(offset if path.stat().st_size >= offset else 0)
                    while True:
                        lines = []
                        for _ in range(128):
                            start = stream.tell()
                            line = stream.readline()
                            if not line.endswith(b"\n"):
                                stream.seek(start)
                                break
                            lines.append((start, line))
                        if not lines:
                            break
                        with write(db):
                            for start, line in lines:
                                try:
                                    project(db, session, start, line)
                                except (
                                    ValueError,
                                    KeyError,
                                    TypeError,
                                    AttributeError,
                                    OverflowError,
                                ):
                                    # Count malformed completed lines without recording their contents.
                                    db.execute(
                                        "INSERT INTO diagnostics VALUES ('invalid_rollout',1) ON CONFLICT(name) DO UPDATE SET count=count+1"
                                    )
                            db.execute(
                                "UPDATE sessions SET offset=? WHERE agent='codex' AND session=?",
                                (stream.tell(), session),
                            )
                        offsets[session] = stream.tell()
                        if after_chunk:
                            after_chunk(db)
            except OSError:
                db.execute("INSERT INTO diagnostics VALUES ('rollout_io_errors',1) "
                           "ON CONFLICT(name) DO UPDATE SET count=count+1")
                continue
            db.execute("INSERT OR REPLACE INTO rollout_paths VALUES (?,?)", (session, str(path)))


# The sender only produces OTLP logs with scalar attributes. curl owns the
# HTTP/2 session and verifies transport errors. All protobuf field numbers
# below come from opentelemetry-proto's logs/v1 and common/v1 schemas.
def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def message(number, payload):
    return varint(number << 3 | 2) + varint(len(payload)) + payload


def string(number, value):
    return message(number, str(value).encode("utf-8"))


def attribute(key, value):
    if type(value) is int:
        encoded = b"\x18" + varint(value & ((1 << 64) - 1))  # AnyValue.int_value
    else:
        encoded = string(1, value)
    return string(1, key) + message(2, encoded)


def encode_logs(service, records):
    resource = message(1, attribute("service.name", service))
    scope_logs = bytearray(message(1, string(1, service)))
    for timestamp, attrs in records:
        log = bytearray(
            b"\x09" + struct.pack("<Q", timestamp)
        )  # fixed64 time_unix_nano
        log.extend(message(5, string(1, attrs["event.name"])))
        for key, value in attrs.items():
            log.extend(message(6, attribute(key, value)))
        scope_logs.extend(message(2, log))
    return message(1, message(1, resource) + message(2, scope_logs))


def require_ack(headers, frame):
    # gRPC can return an HTTP 200 for a failed RPC. Also require the final
    # grpc-status and an empty ExportLogsServiceResponse (possibly containing
    # an empty partial_success submessage, as emitted by the Collector).
    fields = headers.lower().splitlines()
    # HTTPS proxies may prepend their CONNECT response.
    fields = fields[next((i for i in reversed(range(len(fields))) if fields[i].startswith(b"http/")), 0):]
    statuses = [
        line.partition(b":")[2].strip()
        for line in fields
        if line.startswith(b"grpc-status:")
    ]
    if (
        not fields
        or not re.fullmatch(rb"http/2 200\s*", fields[0])
        or statuses != [b"0"]
    ):
        raise RuntimeError("collector did not acknowledge the gRPC export")
    if frame not in (b"\x00\x00\x00\x00\x00", b"\x00\x00\x00\x00\x02\x0a\x00"):
        raise RuntimeError(
            "collector returned a partial or unrecognized response; batch retained"
        )


def export(service, records, *, token=None, endpoint=None):
    body = encode_logs(service, records)
    frame = b"\x00" + struct.pack(">I", len(body)) + body
    url = f"{endpoint or ENDPOINT}/opentelemetry.proto.collector.logs.v1.LogsService/Export"
    # Keep the credential in stdin. curl writes HTTP/2 trailers with the headers.
    with tempfile.TemporaryDirectory(prefix="vantage-export-") as directory:
        directory = Path(directory)
        (directory / "request.bin").write_bytes(frame)
        auth = f"authorization: Bearer {token or TOKEN}\n".encode()
        args = [
            os.environ.get("VANTAGE_CODEX_CURL") or ("curl.exe" if os.name == "nt" else "curl"),
            "--disable",
            "--silent",
            "--show-error",
            "--fail",
            "--http2",
            "--header",
            "@-",
            "--max-time",
            "5",
            "--header",
            "content-type: application/grpc",
            "--header",
            "te: trailers",
            "--dump-header",
            "headers.bin",
            "--data-binary",
            "@request.bin",
            url,
        ]
        reply = subprocess.run(
            args,
            cwd=directory,
            input=auth,
            stdout=subprocess.PIPE,
            check=True,
            timeout=7,
        )
        require_ack((directory / "headers.bin").read_bytes(), reply.stdout)


def drain(db, *, single=False):
    while rows := db.execute(
        "SELECT id,timestamp,payload FROM outbox ORDER BY rowid LIMIT 64"
    ).fetchall():
        export("vantage-agent-hook", [(ts, json.loads(data)) for _, ts, data in rows])
        with write(db):
            db.executemany("DELETE FROM outbox WHERE id=?", [(row[0],) for row in rows])
            db.execute("UPDATE sender SET failures=0,retry_at=0,last_ack=? WHERE id=1", (time.time(),))
        if single:
            return


def schedule_recovery(db, session):
    db.execute("INSERT INTO recovery_queue VALUES (?,1) ON CONFLICT(session) "
               "DO UPDATE SET generation=generation+1", (session,))


def send_batch_if_due(db):
    failures, retry_at = db.execute("SELECT failures,retry_at FROM sender WHERE id=1").fetchone()
    if retry_at > time.time():
        return
    try:
        drain(db, single=True)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        delay = min(300, 5 * 2 ** min(failures, 6) * random.uniform(0.8, 1.2))
        db.execute("UPDATE sender SET failures=failures+1,retry_at=? WHERE id=1",
                   (time.time() + delay,))


def try_lock(lock):
    try:
        if os.name == "nt":
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return False
        raise
    return True


def release_lock(lock):
    if os.name == "nt":
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(lock, fcntl.LOCK_UN)


def run_sender(db, lock):
    while True:
        requests = db.execute("SELECT session,generation FROM recovery_queue").fetchall()
        send_batch_if_due(db)
        if requests:
            targets = {session for session, _ in requests}
            missing_path = db.execute("SELECT 1 FROM recovery_queue q LEFT JOIN rollout_paths p "
                                      "USING(session) WHERE p.path IS NULL LIMIT 1").fetchone()
            discovered = db.execute("SELECT discovered FROM sender").fetchone()[0]
            discover = bool(missing_path) or time.time() - discovered >= 300
            recover(db, None if discover else targets, after_chunk=send_batch_if_due)
            with write(db):
                if discover:
                    db.execute("UPDATE sender SET discovered=? WHERE id=1", (time.time(),))
                db.executemany("DELETE FROM recovery_queue WHERE session=? AND generation=?", requests)
        # Serialize the idle decision with hook writes. A writer that commits
        # after this check will find the lock available when it kicks the sender.
        with write(db):
            pending = db.execute("SELECT EXISTS(SELECT 1 FROM recovery_queue) OR "
                                 "(retry_at<=? AND EXISTS(SELECT 1 FROM outbox)) FROM sender",
                                 (time.time(),)).fetchone()[0]
            if not pending:
                release_lock(lock)
                return


def send(db):
    with (STATE / "worker.lock").open("a+b") as lock:
        if try_lock(lock):
            run_sender(db, lock)


def launch(db, recover_files=False, *, foreground=False, force=False):
    if recover_files:
        schedule_recovery(db, "*")
    if force and foreground:
        db.execute("UPDATE sender SET retry_at=0 WHERE id=1")
    runnable = db.execute("SELECT EXISTS(SELECT 1 FROM recovery_queue) OR "
                          "(retry_at<=? AND EXISTS(SELECT 1 FROM outbox)) FROM sender",
                          (time.time(),)).fetchone()[0]
    if not runnable:
        return
    if foreground:
        send(db)
        return
    with (STATE / "worker.lock").open("a+b") as lock:
        if not try_lock(lock):
            return
        release_lock(lock)
    detached = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
        if os.name == "nt" else {"start_new_session": True}
    )
    subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).resolve()), "send", "--binding", STATE.name],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **detached,
    )


def installed_version():
    try:
        result = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            return identifier(result.stdout.strip().removeprefix("codex-cli "))
    except (OSError, UnicodeError, subprocess.SubprocessError):
        pass
    return None


def attribution_context(facts, cwd, classifier, salt):
    worktree = facts.get("worktree")
    if worktree:
        branch = facts.get("branch")
        location = [worktree, "branch" if branch else "detached", branch or facts.get("head")]
    elif isinstance(cwd, str) and os.path.isabs(cwd):
        location = ["directory", os.path.realpath(cwd)]
    else:
        return None
    try:
        contents = classifier.read_bytes()
    except FileNotFoundError:
        contents = b""
        location.append("missing-classifier")
    except OSError:
        return None
    digest = hashlib.sha256(salt + json.dumps(location).encode())
    digest.update(contents)
    return digest.hexdigest()


def prompt_attribution(db, key, context, changes):
    """Seed a turn once. Only compare with the last submitted prompt in this session."""
    current = db.execute(
        "SELECT classified FROM prompt_contexts WHERE agent=? AND session=? AND turn=?", key
    ).fetchone()
    if current:
        if current[0]:
            state = db.execute("SELECT state FROM turns WHERE agent=? AND session=? AND turn=?", key).fetchone()
            return json.loads(state[0])["tags"]
        return None

    previous = db.execute(
        "SELECT context, classified, state FROM prompt_contexts JOIN turns USING(agent, session, turn) "
        "WHERE agent=? AND session=? ORDER BY prompt_contexts.rowid DESC LIMIT 1", key[:2]
    ).fetchone()
    inherited = None
    existing = db.execute("SELECT state FROM turns WHERE agent=? AND session=? AND turn=?", key).fetchone()
    if existing and json.loads(existing[0]).get("tags"):
        inherited = json.loads(existing[0])["tags"]
    elif context and previous and previous[0] == context and previous[1]:
        state = json.loads(previous[2])
        inherited = state["tags"]
        changes.update(tags=inherited, tag_sources=state.get("tag_sources", {}))
    db.execute("INSERT INTO prompt_contexts VALUES (?,?,?,?,?)", (*key, context, inherited is not None))
    return inherited


def handle(db, name, payload):
    global SOURCE
    agent = AGENTS[name]
    event = payload.get("hook_event_name")
    session = agent.session(payload)
    turn = identifier(payload.get(agent.turn_field))
    if not session or event not in agent.events:
        return
    if event == "PostToolUse" and payload.get("tool_name") not in GIT_TOOLS:
        return
    if event == "SessionStart":
        refresh_classifier(os.environ)
    SOURCE = event
    model = identifier(payload.get("model"))
    version = identifier(payload.get("cli_version") or payload.get("agent_version"))
    if not version and name == "codex" and (event == "SessionStart" or not db.execute(
        "SELECT 1 FROM cli_versions WHERE agent=? AND session=?", (name, session)).fetchone()):
        version = installed_version() or "unknown"
    key = (name, session, turn)
    now = time.time_ns()
    home = str(Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().resolve())
    first = (
        turn
        and not db.execute(
            "SELECT 1 FROM turns WHERE agent=? AND session=? AND turn=?", key
        ).fetchone()
    )
    observe = turn and (
        first or event in {"PostToolUse", "UserPromptSubmit", "Stop", "SessionEnd"}
    )
    identity = facts = None
    if turn:
        salt = db.execute("SELECT salt FROM install WHERE id=1").fetchone()[0]
        facts, identity = git_observation(db, payload.get("cwd"), salt, observe=observe)
    if turn and event == "UserPromptSubmit":
        classifier = Path(os.environ.get("VANTAGE_CLASSIFIER_FILE", Path(home) / "vantage/classifier.md")).expanduser()
        context = attribution_context(facts, payload.get("cwd"), classifier, salt)
    with write(db):
        db.execute(
            "INSERT OR IGNORE INTO sessions(agent,session,home) VALUES (?,?,?)",
            (name, session, home),
        )
        if version:
            db.execute("INSERT OR REPLACE INTO versions_at VALUES (?,?,?,?)", (name, session, now, version))
            db.execute("INSERT OR REPLACE INTO cli_versions VALUES (?,?,?)", (name, session, version))
        if turn:
            changes = {"first_seen_ns": now, "first_source": event}
            if model:
                db.execute("INSERT OR IGNORE INTO turn_models VALUES (?,?,?,?)", (*key, model))
                changes["models"] = [r[0] for r in db.execute(
                    "SELECT model FROM turn_models WHERE agent=? AND session=? AND turn=? ORDER BY model", key)]
            if identity is not None and identity != observed_identity(db, key, now):
                email, git_name = identity
                db.execute("INSERT OR REPLACE INTO git_names VALUES (?,?,?,?,?)", (*key, now, git_name))
                db.execute(
                    "INSERT OR REPLACE INTO git_identity VALUES (?,?,?,?,?)",
                    (*key, now, email),
                )
                changes["identity_observed_ns"] = now
            if event == "UserPromptSubmit":
                changes["started_ns"] = now
                inherited = prompt_attribution(db, key, context, changes)
            if event in ("Stop", "SessionEnd"):
                changes["ended_ns"] = now
            if agent.child_owner_field and session != payload.get("session_id"):
                parent = identifier(payload.get("session_id"))
                if parent:
                    changes["parent_session"] = parent
            snapshot(
                db,
                key,
                facts=facts if observe else None,
                changes=changes,
                timestamp=now,
            )
            if event == "PostModelRequest":
                correlation(
                    db,
                    key,
                    payload.get("token_usage"),
                    "model_hook",
                    timestamp=now,
                    status=payload.get("status", "unknown"),
                    model=identifier(payload.get("model")),
                )
        if event in {"SessionStart", "Stop", "SessionEnd"}:
            schedule_recovery(db, "*" if event == "SessionStart" else session)
    if turn and event == "UserPromptSubmit":
        rule = (f"read {shlex.quote(str(classifier))}" if classifier.is_file()
                else "infer known PR, ticket, and purpose")
        update = (f"call the vantage_local MCP tool set_turn_attribution with "
                  f"session_id={session}, turn_id={turn}, and tags containing the complete "
                  "classification as a JSON object ({} clears all dimensions)")
        if inherited is None:
            message = f"For this turn, {rule} and {update}."
        else:
            message = (f"Current attribution: {json.dumps(inherited, ensure_ascii=True)}. "
                       f"Only if the task or attribution changed, {rule} and {update}.")
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": event,
                        "additionalContext": message,
                    }
                }
            )
        )
    launch(db)


def main():
    global SOURCE
    if os.name != "nt":
        os.umask(0o077)
    try:
        load_connection()
    except (OSError, ValueError, KeyError):
        if len(sys.argv) == 1:
            return
        raise SystemExit("Run Vantage telemetry setup first.")
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("hook").add_argument("agent", choices=AGENTS)
    tag = sub.add_parser("tag")
    tag.add_argument("agent", choices=AGENTS)
    tag.add_argument("session")
    tag.add_argument("turn")
    tag.add_argument(
        "--source", choices=["manual", "inferred", "explicit"], default="manual"
    )
    group = tag.add_mutually_exclusive_group(required=True)
    group.add_argument("--set", "--tag", action="append", metavar="NAME=VALUE")
    group.add_argument("--replace", nargs="*", metavar="NAME=VALUE")
    group.add_argument("--clear", metavar="NAME")
    for action in ("recover", "flush", "status"):
        sub.add_parser(action).add_argument("--binding")
    sender = sub.add_parser("send")
    sender.add_argument("--binding", required=True)
    args = parser.parse_args(sys.argv[1:] or ["hook", "codex"])
    if getattr(args, "binding", None) and args.binding != STATE.name:
        return
    if args.action == "hook":
        # Coding clients must be able to continue even if capture is unavailable.
        try:
            db = connect()
            try:
                handle(db, args.agent, json.load(sys.stdin))
            finally:
                db.close()
        except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error, subprocess.SubprocessError) as err:
            print(f"Vantage hook skipped ({type(err).__name__})", file=sys.stderr)
        return
    db = connect()
    try:
        if args.action == "tag":
            key = (args.agent, args.session, args.turn)
            tags = {}
            assignments = args.replace if args.replace is not None else args.set or [args.clear]
            for assignment in assignments:
                name, sep, value = assignment.partition("=")
                if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", name):
                    parser.error("invalid classifier dimension name")
                if args.clear and sep:
                    parser.error("--clear expects a dimension name")
                if args.clear is None and (
                    not sep
                    or not 1 <= len(value) <= 512
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)
                ):
                    parser.error("invalid classifier value")
                tags[name] = None if args.clear else value
            with write(db):
                row = db.execute(
                    "SELECT state FROM turns WHERE agent=? AND session=? AND turn=?",
                    key,
                ).fetchone()
                if not row:
                    parser.error("unknown turn")
                previous = json.loads(row[0])["tags"]
                final_names = set(previous) | tags.keys() if args.set else tags.keys()
                if len(final_names) > 32:
                    parser.error("at most 32 dimensions per turn")
                if args.replace is not None:
                    tags.update({name: None for name in previous if name not in tags})
                snapshot(db, key, tags=tags, tag_source=args.source)
                db.execute("UPDATE prompt_contexts SET classified=1 WHERE agent=? AND session=? AND turn=?", key)
            launch(db, False, force=True)
        elif args.action == "status":
            for table in ("sessions", "turns", "outbox"):
                print(table, db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            failures, retry_at, last_ack = db.execute("SELECT failures,retry_at,last_ack FROM sender").fetchone()
            print("consecutive_send_failures", failures)
            print("retry_in_seconds", max(0, round(retry_at-time.time())))
            print("last_ack_unix_seconds", int(last_ack))
            print("pending_recoveries", db.execute("SELECT count(*) FROM recovery_queue").fetchone()[0])
            for name, count in db.execute("SELECT name,count FROM diagnostics"):
                print(name, count)
        else:
            SOURCE = "rollout"
            if args.action == "send":
                send(db)
            else:
                launch(db, args.action == "recover", foreground=True, force=True)
    finally:
        db.close()


if __name__ == "__main__":
    main()

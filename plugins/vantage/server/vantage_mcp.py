#!/usr/bin/env python3
"""Dependency-free local MCP tools for Vantage telemetry setup."""

import json
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass


PROTOCOL_VERSION = "2025-06-18"
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_STATUS_WAIT_SECONDS = 45
PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_MANIFEST = PLUGIN_ROOT / "plugin.json"
SETUP_SCRIPT = (
    PLUGIN_ROOT
    / "skills/setup-vantage-telemetry/scripts/configure_vantage_telemetry.py"
)


MISSING_REQUIREMENTS_EXIT = 6
# Fixed messages keyed by the IDs the setup script reports. The server never
# returns the script's own output, so only these known IDs reach the agent.
REQUIREMENT_MESSAGES = {
    "python_version": "Install Python 3.9 or newer (or set VANTAGE_CODEX_PYTHON to one).",
    "sqlite": "Install a Python 3 build that includes the sqlite3 module.",
    "curl_missing": "Install an HTTP/2-enabled curl or set VANTAGE_CODEX_CURL to its full path.",
    "curl_http2": "Install an HTTP/2-enabled curl and set VANTAGE_CODEX_CURL to its full path if needed.",
}
WARNING_MESSAGES = {
    "git_attribution": (
        "Install Git for repository, worktree, branch, and commit attribution. "
        "Usage reporting and classifier tags can still work."
    ),
}


def notice_ids(stderr, prefix, messages):
    ids = {}
    for line in (stderr or "").splitlines():
        if line.startswith(prefix):
            notice = line[len(prefix):].split(" ", 1)[0]
            if notice in messages:
                ids[notice] = None
    return ids


def plugin_version():
    try:
        payload = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        version = payload.get("version")
        return version if isinstance(version, str) and version else "unknown"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "unknown"


@dataclass(frozen=True)
class SetupOutcome:
    status: str
    message: str
    restart_required: bool
    is_error: bool = False


_ACTIVE_SETUP = None
_LAST_SETUP_OUTCOME = None


def configuration_command(action):
    command = [sys.executable, str(SETUP_SCRIPT)]
    if action == "remove":
        command.append("--remove")
    return command


def outcome_from_completed(action, completed):
    if completed.returncode == 0:
        output = completed.stdout.lower()
        if action == "remove":
            if "nothing to remove" in output or "no changes were made" in output:
                return SetupOutcome(
                    "not_configured",
                    "No plugin-managed Vantage telemetry configuration was found.",
                    False,
                )
            return SetupOutcome(
                "removed",
                "Vantage telemetry was removed. Restart Codex to apply the change.",
                True,
            )
        already_connected = "already configured" in output
        status = "already_connected" if already_connected else "connected"
        connection = "already connected" if already_connected else "connected"
        warnings = notice_ids(completed.stderr, "warning:", WARNING_MESSAGES)
        warning_text = "".join("\n\n" + WARNING_MESSAGES[warning] for warning in warnings)
        return SetupOutcome(
            status,
            f"Vantage telemetry is {connection}. Review and trust the Vantage hooks in "
            "/hooks or Settings > Hooks. Restart Codex, then start a new chat. "
            "After updating the Vantage plugin, rerun setup to refresh the copied hook scripts."
            + warning_text,
            True,
        )

    if completed.returncode == MISSING_REQUIREMENTS_EXIT:
        problems = notice_ids(completed.stderr, "requirement:", REQUIREMENT_MESSAGES)
        steps = " ".join("- %s" % REQUIREMENT_MESSAGES[problem] for problem in problems)
        return SetupOutcome(
            "missing_requirements",
            "Vantage telemetry can't be set up until these are installed: %s "
            "Then run setup again." % (steps or "- Check that Python 3.9+ and curl with HTTP/2 are installed."),
            False,
            True,
        )

    messages = {
        1: "Codex telemetry configuration could not be read safely.",
        2: "Vantage sign-in or credential verification did not complete. Start setup again.",
        3: "Existing OpenTelemetry settings prevent automatic setup. Review your Codex telemetry configuration and retry.",
        4: "Vantage telemetry setup requires Python 3.9 or newer.",
        5: "Vantage telemetry is connected, but its Codex hooks could not be installed safely. Review ~/.codex/hooks.json and retry setup.",
    }
    return SetupOutcome(
        "error",
        messages.get(
            completed.returncode,
            "Vantage telemetry setup could not be completed.",
        ),
        False,
        True,
    )


def pending_outcome(message=None):
    return SetupOutcome(
        "pending",
        message
        or "Vantage telemetry setup is still waiting for sign-in to finish in your browser.",
        False,
    )


def check_configuration(wait_seconds=MAX_STATUS_WAIT_SECONDS):
    global _ACTIVE_SETUP, _LAST_SETUP_OUTCOME

    if _ACTIVE_SETUP is None:
        if _LAST_SETUP_OUTCOME is not None:
            return _LAST_SETUP_OUTCOME
        return SetupOutcome(
            "not_started",
            "No Vantage telemetry setup is running. Start setup first.",
            False,
        )

    try:
        stdout, stderr = _ACTIVE_SETUP.communicate(timeout=wait_seconds)
    except subprocess.TimeoutExpired:
        return pending_outcome()
    except OSError:
        _ACTIVE_SETUP = None
        _LAST_SETUP_OUTCOME = SetupOutcome(
            "error",
            "Vantage telemetry setup status could not be read.",
            False,
            True,
        )
        return _LAST_SETUP_OUTCOME

    completed = subprocess.CompletedProcess(
        configuration_command("connect"),
        _ACTIVE_SETUP.returncode,
        stdout,
        stderr,
    )
    _ACTIVE_SETUP = None
    _LAST_SETUP_OUTCOME = outcome_from_completed("connect", completed)
    return _LAST_SETUP_OUTCOME


def start_configuration():
    global _ACTIVE_SETUP, _LAST_SETUP_OUTCOME

    if _ACTIVE_SETUP is not None:
        outcome = check_configuration(wait_seconds=0)
        if outcome.status != "pending":
            return outcome
        return pending_outcome("Vantage telemetry setup is already running. Complete sign-in in your browser.")

    _LAST_SETUP_OUTCOME = None
    try:
        _ACTIVE_SETUP = subprocess.Popen(
            configuration_command("connect"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError:
        return SetupOutcome(
            "error",
            "Vantage telemetry setup could not be started.",
            False,
            True,
        )

    return pending_outcome(
        "Vantage sign-in is opening in your default browser. Complete sign-in there, "
        "then check setup status."
    )


def remove_configuration():
    global _ACTIVE_SETUP

    if _ACTIVE_SETUP is not None:
        outcome = check_configuration(wait_seconds=0)
        if outcome.status == "pending":
            return SetupOutcome(
                "error",
                "Vantage telemetry setup is still running. Finish setup before removing it.",
                False,
                True,
            )

    try:
        completed = subprocess.run(
            configuration_command("remove"),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return SetupOutcome(
            "error",
            "Removing Vantage telemetry timed out.",
            False,
            True,
        )
    except OSError:
        return SetupOutcome(
            "error",
            "Vantage telemetry setup could not be started.",
            False,
            True,
        )

    return outcome_from_completed("remove", completed)


def run_tool_action(action, wait_seconds=MAX_STATUS_WAIT_SECONDS):
    if action == "start":
        return start_configuration()
    if action == "status":
        return check_configuration(wait_seconds=wait_seconds)
    if action == "remove":
        return remove_configuration()
    return SetupOutcome(
        "error",
        "Unsupported Vantage telemetry action.",
        False,
        True,
    )


def tool_definitions():
    empty_input = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    output = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": [
                    "pending",
                    "not_started",
                    "cancelled",
                    "connected",
                    "already_connected",
                    "removed",
                    "not_configured",
                    "missing_requirements",
                    "error",
                ],
            },
            "restart_required": {"type": "boolean"},
        },
        "required": ["status", "restart_required"],
        "additionalProperties": False,
    }
    return [
        {
            "name": "connect_vantage_telemetry",
            "title": "Connect Vantage telemetry",
            "description": (
                "Start Vantage sign-in in the default browser and return immediately. "
                "Use check_vantage_telemetry_setup until setup finishes. Credentials and "
                "authorization URLs are never returned."
            ),
            "inputSchema": empty_input,
            "outputSchema": output,
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": True,
            },
        },
        {
            "name": "check_vantage_telemetry_setup",
            "title": "Check Vantage telemetry setup",
            "description": (
                "Wait up to forty-five seconds for the active Vantage sign-in to finish and "
                "return its current status. No credentials or authorization URLs are returned."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "wait_seconds": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": MAX_STATUS_WAIT_SECONDS,
                        "default": MAX_STATUS_WAIT_SECONDS,
                    }
                },
                "additionalProperties": False,
            },
            "outputSchema": output,
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        },
        {
            "name": "remove_vantage_telemetry",
            "title": "Remove Vantage telemetry",
            "description": (
                "Remove only the Codex telemetry configuration managed by the Vantage plugin."
            ),
            "inputSchema": empty_input,
            "outputSchema": output,
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": True,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        },
        {
            "name": "set_turn_attribution",
            "description": "Replace the classifier tags for the Codex turn identified by the Vantage hook.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "turn_id": {"type": "string"},
                    "tags": {"type": "object", "additionalProperties": {"type": "string"}, "maxProperties": 32},
                },
                "required": ["session_id", "turn_id", "tags"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
        },
    ]


def set_turn_attribution(arguments):
    if set(arguments) != {"session_id", "turn_id", "tags"}:
        raise ValueError("expected session_id, turn_id, and tags")
    ids = [arguments["session_id"], arguments["turn_id"]]
    if not all(isinstance(i, str) and re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.:/-]{0,179}", i) for i in ids):
        raise ValueError("invalid session or turn identifier")
    tags = arguments["tags"]
    if not isinstance(tags, dict) or len(tags) > 32:
        raise ValueError("tags must be an object with at most 32 dimensions")
    assignments = []
    for key, value in tags.items():
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", key) or not isinstance(value, str):
            raise ValueError("invalid classifier dimension")
        if not 1 <= len(value) <= 512 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("invalid classifier value")
        assignments.append(f"{key}={value}")
    codex_home = pathlib.Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
    launcher = codex_home / "vantage" / "hooks" / "run_vantage_codex_hook.py"
    command = [sys.executable, "-I", str(launcher),
               "tag", "codex", *ids, "--source", "inferred", "--replace", *assignments]
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
        saved = completed.returncode == 0
        errors = completed.stderr.rstrip().splitlines()
        last_error = errors[-1] if errors else b""
    except subprocess.TimeoutExpired:
        saved = False
        last_error = b"timeout"
    except OSError:
        saved = False
        last_error = b""
    if saved:
        message = "Turn attribution saved."
    elif last_error == b"unknown turn" or last_error.endswith(b": error: unknown turn"):
        message = "Vantage could not find that turn. Use the session and turn IDs from the current Vantage hook."
    elif last_error == b"timeout":
        message = "Saving turn attribution timed out."
    elif last_error in {b"Run Vantage telemetry setup first.", b"Vantage attribution hook is not installed."} or not launcher.is_file():
        message = "Set up Vantage telemetry before saving attribution."
    else:
        message = "Could not save turn attribution. Check local telemetry setup."
    return {"content": [{"type": "text", "text": message}], "isError": not saved}


def call_tool(params, runner=run_tool_action):
    if not isinstance(params, dict):
        raise ValueError("tool parameters must be an object")
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    if name == "set_turn_attribution":
        return set_turn_attribution(arguments)
    actions = {
        "connect_vantage_telemetry": "start",
        "check_vantage_telemetry_setup": "status",
        "remove_vantage_telemetry": "remove",
    }
    action = actions.get(name)
    if action is None:
        raise LookupError("unknown tool")

    wait_seconds = MAX_STATUS_WAIT_SECONDS
    if action == "status":
        if set(arguments) - {"wait_seconds"}:
            raise ValueError("status only accepts wait_seconds")
        wait_seconds = arguments.get("wait_seconds", MAX_STATUS_WAIT_SECONDS)
        if (
            isinstance(wait_seconds, bool)
            or not isinstance(wait_seconds, (int, float))
            or wait_seconds < 0
            or wait_seconds > MAX_STATUS_WAIT_SECONDS
        ):
            raise ValueError("wait_seconds must be between 0 and 45")
    elif arguments:
        raise ValueError("this tool does not accept arguments")

    outcome = runner(action, wait_seconds)
    structured = {
        "status": outcome.status,
        "restart_required": outcome.restart_required,
    }
    return {
        "content": [{"type": "text", "text": outcome.message}],
        "structuredContent": structured,
        "isError": outcome.is_error,
    }


def result(message_id, payload):
    return {"jsonrpc": "2.0", "id": message_id, "result": payload}


def error(message_id, code, message):
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": code, "message": message},
    }


def handle_message(message, runner=run_tool_action):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")

    message_id = message.get("id")
    method = message.get("method")
    if message_id is None:
        if method == "notifications/cancelled":
            stop_active_setup(cancelled=True)
        return None

    if method == "initialize":
        return result(
            message_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "vantage-local",
                    "version": plugin_version(),
                },
            },
        )
    if method == "ping":
        return result(message_id, {})
    if method == "tools/list":
        return result(message_id, {"tools": tool_definitions()})
    if method == "tools/call":
        try:
            return result(message_id, call_tool(message.get("params"), runner=runner))
        except ValueError as exception:
            return error(message_id, -32602, str(exception))
        except LookupError:
            return error(message_id, -32601, "Unknown tool")
    return error(message_id, -32601, "Method not found")


def write_message(stream, message):
    encoded = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
    stream.write(encoded)
    stream.flush()


def stop_active_setup(cancelled=False):
    global _ACTIVE_SETUP, _LAST_SETUP_OUTCOME

    if _ACTIVE_SETUP is None:
        return
    process = _ACTIVE_SETUP
    if cancelled and process.poll() is not None:
        check_configuration(wait_seconds=0)
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    _ACTIVE_SETUP = None
    if cancelled:
        _LAST_SETUP_OUTCOME = SetupOutcome(
            "cancelled",
            "Vantage telemetry setup was cancelled.",
            False,
        )


def serve(input_stream=None, output_stream=None, runner=run_tool_action):
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout.buffer

    try:
        while True:
            line = input_stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_LINE_BYTES:
                while line and not line.endswith(b"\n"):
                    line = input_stream.readline(MAX_LINE_BYTES + 1)
                write_message(output_stream, error(None, -32700, "Request too large"))
                continue
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                write_message(output_stream, error(None, -32700, "Parse error"))
                continue
            response = handle_message(message, runner=runner)
            if response is not None:
                write_message(output_stream, response)
    finally:
        stop_active_setup()


def main():
    python_path = os.environ.get("VANTAGE_CODEX_PYTHON")
    if python_path and os.path.realpath(python_path) != os.path.realpath(sys.executable):
        command = [python_path, "-I", str(pathlib.Path(__file__).resolve())]
        environment = os.environ.copy()
        del environment["VANTAGE_CODEX_PYTHON"]
        try:
            return subprocess.call(command, env=environment)
        except OSError:
            print("Vantage could not start the Python interpreter set by VANTAGE_CODEX_PYTHON.", file=sys.stderr)
            return 4
    if sys.version_info < (3, 9):
        print("Vantage telemetry requires Python 3.9 or newer.", file=sys.stderr)
        return 4
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Configure Codex OpenTelemetry log export to Vantage."""

import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import secrets
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


BEGIN = "# BEGIN Vantage telemetry (managed by the Vantage plugin; rerun setup to change it)"
END = "# END Vantage telemetry"
INGEST_ENDPOINT = os.environ.get("VANTAGE_CODEX_INGEST_ENDPOINT", "https://ingest-staging.vantage.sh/v1/logs")
CLAIM_ENDPOINT = os.environ.get(
    "VANTAGE_CODEX_CLAIM_ENDPOINT", "https://api.staging.vantage.sh/plugins/codex_telemetry_setup/claim"
)
AUTHORIZATION_ENDPOINT = os.environ.get(
    "VANTAGE_CODEX_AUTHORIZATION_ENDPOINT", "https://console-staging.vantage.sh/codex/telemetry/authorize"
)
AUTHORIZATION_TIMEOUT = 60 * 60
POLL_INTERVAL = 2
MAX_POLL_INTERVAL = 10
REQUEST_TIMEOUT = max(1, min(300, float(os.environ.get("VANTAGE_CODEX_REQUEST_TIMEOUT", "15"))))
MAX_CLASSIFIER_BYTES = 64 * 1024
MANAGED_KEYS = (
    "exporter",
    "log_user_prompt",
    "log_agent_responses",
    "log_guardian_assessments",
    "tool_result",
)
TABLE = re.compile(r"^\s*\[")
OTEL_TABLE = re.compile(r"^\s*\[\s*otel\s*\]\s*(?:#.*)?$")
OTEL_CONFIG = re.compile(r"^\s*(?:\[+\s*otel(?:\.|\s*\])|otel(?:\.|\s*=))")
TOKEN = re.compile(r'authorization\s*=\s*"Bearer ([A-Za-z0-9._~+/=\-]{8,512})"')
PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[3]
try:
    PLUGIN_VERSION = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))["version"]
except (OSError, ValueError, TypeError, KeyError):
    PLUGIN_VERSION = "unknown"
if not isinstance(PLUGIN_VERSION, str) or not PLUGIN_VERSION:
    PLUGIN_VERSION = "unknown"
USER_AGENT = "vantage-codex-plugin/%s" % PLUGIN_VERSION
BUNDLED_HOOKS_DIR = PLUGIN_ROOT / "hooks"
HOOK_SCRIPT_NAMES = ("run_vantage_codex_hook.sh", "run_vantage_codex_hook.py")
MIN_PYTHON = (3, 9)
MISSING_REQUIREMENTS_EXIT = 6
REQUIREMENT_MESSAGES = {
    "python_version": "Python 3.9 or newer is required. Install a newer Python 3, or set VANTAGE_CODEX_PYTHON to one.",
    "sqlite": "Python's sqlite3 module is missing. Install a Python 3 build that includes SQLite.",
    "curl_missing": "curl is unavailable. Install an HTTP/2-enabled curl or set VANTAGE_CODEX_CURL to its full path.",
    "curl_http2": "Install an HTTP/2-enabled curl and set VANTAGE_CODEX_CURL to its full path if needed.",
}
WARNING_MESSAGES = {
    "git_attribution": (
        "Install Git for repository, worktree, branch, and commit attribution. "
        "Usage reporting and classifier tags can still work."
    ),
}


def curl_supports_http2(curl, run=subprocess.run):
    try:
        completed = run([curl, "-V"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    for line in (completed.stdout or "").splitlines():
        if line.lower().startswith("features:"):
            return "HTTP2" in line.split()
    return False


def requirement_problems(*, version=None, os_name=None, which=shutil.which, run=subprocess.run):
    """Return the IDs of missing prerequisites, in the order a user should fix them."""
    version = tuple(sys.version_info[:2] if version is None else version)
    os_name = os.name if os_name is None else os_name
    problems = []
    if version < MIN_PYTHON:
        problems.append("python_version")
    try:
        import sqlite3  # noqa: F401 - the attribution outbox needs it
    except ImportError:
        problems.append("sqlite")
    curl = os.environ.get("VANTAGE_CODEX_CURL") or ("curl.exe" if os_name == "nt" else "curl")
    if not which(curl):
        problems.append("curl_missing")
    elif not curl_supports_http2(curl, run=run):
        problems.append("curl_http2")
    return problems


def requirement_warnings(run=subprocess.run):
    try:
        completed = run(["git", "--version"], capture_output=True, text=True, timeout=0.5, check=False)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return ["git_attribution"]
    if completed.returncode != 0 or not completed.stdout.startswith("git version "):
        return ["git_attribution"]
    return []


def report_requirement_problems(problems, stream=None):
    stream = stream or sys.stderr
    print("Vantage telemetry setup needs a few things first:", file=stream)
    for problem in problems:
        # The MCP server reads only the ID before the first space.
        print("requirement:%s %s" % (problem, REQUIREMENT_MESSAGES[problem]), file=stream)


def codex_home_path():
    configured = os.environ.get("CODEX_HOME")
    return pathlib.Path(configured or "~/.codex").expanduser()


def config_path():
    return str(codex_home_path() / "config.toml")


def user_hooks_path():
    return codex_home_path() / "hooks.json"


def managed_hooks_dir():
    return codex_home_path() / "vantage" / "hooks"


def classifier_directory():
    return codex_home_path() / "vantage"


def classifier_token_path():
    return classifier_directory() / "classifier_read_token"


def classifier_markdown_path():
    return classifier_directory() / "classifier.md"


def classifier_etag_path():
    return classifier_directory() / "classifier.etag"


def hook_handler(event_name):
    hooks_dir = managed_hooks_dir()
    shell_script = hooks_dir / "run_vantage_codex_hook.sh"
    python_script = hooks_dir / "run_vantage_codex_hook.py"
    handler = {
        "type": "command",
        "command": "sh %s" % shlex.quote(str(shell_script)),
        "commandWindows": subprocess.list2cmdline(["python3", "-I", str(python_script)]),
        "timeout": 3 if event_name == "SessionEnd" else 30,
        "statusMessage": (
            "Loading Vantage attribution"
            if event_name == "SessionStart"
            else "Adding Vantage usage tags"
        ),
    }
    if event_name == "Stop":
        handler["async"] = True
    return handler


def is_managed_hook(handler):
    if not isinstance(handler, dict):
        return False
    expected = hook_handler("SessionStart")
    python_script = str(managed_hooks_dir() / "run_vantage_codex_hook.py")
    legacy_windows = (
        subprocess.list2cmdline(["python", "-I", python_script]),
        subprocess.list2cmdline(["py", "-3", "-I", python_script]),
    )
    return (
        handler.get("command") == expected["command"]
        or handler.get("commandWindows") in (expected["commandWindows"], *legacy_windows)
    )


def remove_managed_hook_handlers(groups):
    if not isinstance(groups, list):
        raise ValueError("a Vantage hook event is not a list")

    result = []
    changed = False
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            result.append(group)
            continue
        remaining = [handler for handler in group["hooks"] if not is_managed_hook(handler)]
        if len(remaining) == len(group["hooks"]):
            result.append(group)
            continue
        changed = True
        if remaining:
            updated = dict(group)
            updated["hooks"] = remaining
            result.append(updated)
    return result, changed


def load_user_hooks():
    path = user_hooks_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("%s could not be read safely" % path) from error
    if not isinstance(payload, dict):
        raise ValueError("%s must contain a JSON object" % path)
    hooks = payload.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ValueError("the hooks value in %s must be a JSON object" % path)
    return payload


def write_file(path, content, mode=0o600):
    path = pathlib.Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".%s." % path.name)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def write_user_hooks(payload):
    content = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    write_file(user_hooks_path(), content)


def classifier_bundle(payload):
    if not isinstance(payload, dict):
        raise ValueError("Vantage did not return classifier configuration.")

    token = payload.get("classifier_read_token")
    markdown = payload.get("classifier_markdown")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Vantage did not return a classifier credential.")
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError("Vantage did not return a classifier.")
    encoded = markdown.encode("utf-8")
    if len(encoded) > MAX_CLASSIFIER_BYTES:
        raise ValueError("The Vantage classifier is too large.")
    return {"token": token.strip(), "markdown": encoded}


def save_classifier(bundle):
    write_file(classifier_token_path(), bundle["token"].encode("utf-8"))
    write_file(classifier_markdown_path(), bundle["markdown"])
    classifier_etag_path().unlink(missing_ok=True)


def remove_classifier_files():
    changed = False
    for path in (classifier_token_path(), classifier_markdown_path(), classifier_etag_path()):
        try:
            path.unlink()
            changed = True
        except FileNotFoundError:
            pass
    try:
        classifier_directory().rmdir()
    except OSError:
        pass
    return changed


def copy_managed_hook_scripts():
    destination = managed_hooks_dir()
    for name in HOOK_SCRIPT_NAMES:
        source = BUNDLED_HOOKS_DIR / name
        write_file(destination / name, source.read_bytes(), mode=0o700)


HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostModelRequest", "SubagentStart",
               "PostToolUse", "Stop", "SessionEnd")
POST_TOOL_MATCHER = r"^(Bash|Edit|Write|MultiEdit|exec_command|apply_patch|shell|shell_command)$"


def save_connection(token):
    path = classifier_directory() / "connection.json"
    endpoint = INGEST_ENDPOINT.removesuffix("/v1/logs").rstrip("/")
    if path.exists():
        existing = json.loads(path.read_text())
        if existing.get("endpoint") == endpoint and existing.get("token") == token:
            return
    write_file(path, json.dumps({"endpoint": endpoint, "token": token, "since_ns": time.time_ns()}).encode())


def install_user_hooks():
    payload = load_user_hooks()
    hooks = dict(payload.get("hooks") or {})
    if "PreToolUse" in hooks:
        remaining, changed = remove_managed_hook_handlers(hooks["PreToolUse"])
        if changed:
            if remaining:
                hooks["PreToolUse"] = remaining
            else:
                del hooks["PreToolUse"]
    for event_name in HOOK_EVENTS:
        groups, _changed = remove_managed_hook_handlers(hooks.get(event_name, []))
        group = {"hooks": [hook_handler(event_name)]}
        if event_name == "PostToolUse":
            group["matcher"] = POST_TOOL_MATCHER
        groups.append(group)
        hooks[event_name] = groups
    payload["hooks"] = hooks

    copy_managed_hook_scripts()
    write_user_hooks(payload)
    return user_hooks_path()


def remove_managed_hook_scripts():
    changed = False
    directory = managed_hooks_dir()
    for name in HOOK_SCRIPT_NAMES:
        path = directory / name
        try:
            path.unlink()
            changed = True
        except FileNotFoundError:
            pass
    for path in (directory, directory.parent):
        try:
            path.rmdir()
        except OSError:
            pass
    return changed


def remove_user_hooks():
    path = user_hooks_path()
    payload = load_user_hooks()
    hooks = dict(payload.get("hooks") or {})
    changed = False
    for event_name in (*HOOK_EVENTS, "PreToolUse"):
        if event_name not in hooks:
            continue
        groups, event_changed = remove_managed_hook_handlers(hooks[event_name])
        changed = changed or event_changed
        if groups:
            hooks[event_name] = groups
        else:
            hooks.pop(event_name, None)

    if changed:
        if hooks:
            payload["hooks"] = hooks
        else:
            payload.pop("hooks", None)
        if payload:
            write_user_hooks(payload)
        else:
            path.unlink(missing_ok=True)
    return remove_managed_hook_scripts() or changed


def install_user_hooks_or_report():
    try:
        path = install_user_hooks()
    except (OSError, UnicodeError, ValueError) as error:
        print("Vantage telemetry is configured, but its hooks could not be installed: %s" % error, file=sys.stderr)
        return False
    print("Installed or refreshed Vantage hooks in %s." % path)
    return True


def remove_managed_block(text):
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip().startswith("# BEGIN Vantage telemetry")]
    ends = [index for index, line in enumerate(lines) if line.strip().startswith(END)]
    if not starts and not ends:
        return lines, False
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise ValueError("the Vantage telemetry block markers are invalid")
    del lines[starts[0] : ends[0] + 1]
    return lines, True


def otel_section(lines):
    headers = [index for index, line in enumerate(lines) if OTEL_TABLE.match(line)]
    if len(headers) > 1:
        raise ValueError("config.toml has more than one [otel] section")
    if not headers:
        return None, None
    start = headers[0]
    end = next((index for index in range(start + 1, len(lines)) if TABLE.match(lines[index])), len(lines))
    return start, end


def inspect_otel(lines):
    start, end = otel_section(lines)
    conflicts = set()
    has_environment = False

    for index, line in enumerate(lines):
        if start is not None and start < index < end:
            for key in MANAGED_KEYS:
                if re.match(r"^\s*%s\s*=" % re.escape(key), line):
                    conflicts.add(key)
            has_environment = has_environment or bool(re.match(r"^\s*environment\s*=", line))
            continue

        for key in MANAGED_KEYS:
            pattern = r"^\s*(?:otel\.%s\s*=|\[+\s*otel\.%s(?:\.|\s*\]))" % (key, key)
            if re.match(pattern, line):
                conflicts.add(key)
        has_environment = has_environment or bool(re.match(r"^\s*otel\.environment\s*=", line))

    if start is None and any(OTEL_CONFIG.match(line) for line in lines):
        conflicts.add("existing otel configuration")
    return start, has_environment, sorted(conflicts)


def vantage_exporter_token(lines):
    start, end = otel_section(lines)
    candidates = []

    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or INGEST_ENDPOINT not in line:
            continue

        in_otel = start is not None and start < index < end
        assignment = r"^\s*exporter\s*=" if in_otel else r"^\s*otel\.exporter\s*="
        if not re.match(assignment, line):
            continue

        match = TOKEN.search(line)
        if match:
            candidates.append(match.group(1))

    return candidates[0] if len(set(candidates)) == 1 else None


def has_vantage_endpoint(lines):
    return any(INGEST_ENDPOINT in line and not line.lstrip().startswith("#") for line in lines)


def remove_managed_settings(lines):
    start, end = otel_section(lines)
    result = []

    for index, line in enumerate(lines):
        in_otel = start is not None and start < index < end
        if in_otel and any(re.match(r"^\s*%s\s*=" % re.escape(key), line) for key in MANAGED_KEYS):
            continue
        if any(re.match(r"^\s*otel\.%s\s*=" % re.escape(key), line) for key in MANAGED_KEYS):
            continue
        result.append(line)

    return result


def managed_block(token, include_environment, *, environment="staging"):
    lines = [BEGIN]
    if include_environment:
        lines.append('environment = "%s"' % environment)
    lines += [
        'exporter = { otlp-http = { endpoint = "%s", protocol = "binary", headers = { authorization = "Bearer %s" } } }'
        % (INGEST_ENDPOINT, token),
        "log_user_prompt = false",
        "log_agent_responses = false",
        "log_guardian_assessments = false",
        "tool_result = { max_bytes = 0 }",
        END,
    ]
    return lines


def open_authorization_url(authorization_url):
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["open", authorization_url],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return False
        return result.returncode == 0

    try:
        return webbrowser.open(authorization_url, new=1)
    except webbrowser.Error:
        return False


def browser_sign_in():
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    authorization_expires_at = int(time.time()) + AUTHORIZATION_TIMEOUT
    query = urllib.parse.urlencode(
        {
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "authorization_expires_at": authorization_expires_at,
        }
    )
    authorization_url = "%s?%s" % (AUTHORIZATION_ENDPOINT, query)

    if not open_authorization_url(authorization_url):
        raise ValueError("Vantage sign-in could not be opened in your browser.")
    return code_verifier, authorization_expires_at


def wait_for_setup_credentials(code_verifier, authorization_expires_at):
    remaining = authorization_expires_at - time.time()
    if remaining <= 0:
        raise ValueError("Vantage sign-in timed out. Start setup again.")

    deadline = time.monotonic() + remaining
    poll_interval = POLL_INTERVAL
    while True:
        if time.monotonic() >= deadline:
            raise ValueError("Vantage sign-in timed out. Start setup again.")

        request = urllib.request.Request(
            CLAIM_ENDPOINT,
            data=json.dumps(
                {
                    "code_verifier": code_verifier,
                    "authorization_expires_at": authorization_expires_at,
                }
            ).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status == 202:
                    if time.monotonic() >= deadline:
                        raise ValueError("Vantage sign-in timed out. Start setup again.")
                    time.sleep(poll_interval)
                    poll_interval = min(MAX_POLL_INTERVAL, poll_interval * 2)
                    continue
        except urllib.error.HTTPError as error:
            if error.code in (429, 503):
                if time.monotonic() >= deadline:
                    raise ValueError("Vantage could not finish setup. Start setup again.") from error
                time.sleep(poll_interval)
                poll_interval = min(MAX_POLL_INTERVAL, poll_interval * 2)
                continue
            try:
                detail = json.loads(error.read().decode("utf-8")).get(
                    "errors", ["Setup could not be completed."]
                )[0]
            except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
                detail = "Setup could not be completed."
            raise ValueError(detail) from error
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise ValueError("Vantage could not be reached to complete setup.") from error

        token = payload.get("plaintext_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Vantage did not return an ingest credential.")
        return {"ingest_token": token.strip(), "classifier": classifier_bundle(payload)}


def verify_attribution_transport(token):
    try:
        hook = runpy.run_path(str(BUNDLED_HOOKS_DIR / "vantage_codex_hook.py"))
        hook["export"]("vantage-agent-hook", [], token=token,
                       endpoint=INGEST_ENDPOINT.removesuffix("/v1/logs").rstrip("/"))
    except (ImportError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        message = "Could not verify the authenticated gRPC logs endpoint."
        if os.name == "nt":
            message += " Use an HTTP/2-enabled curl.exe and set VANTAGE_CODEX_CURL to its full path."
        raise ValueError(message) from error


def verify_ingest_token(token):
    deadline = time.monotonic() + 5
    while True:
        request = urllib.request.Request(
            INGEST_ENDPOINT,
            data=json.dumps({"resourceLogs": []}).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer %s" % token,
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT):
                verify_attribution_transport(token)
                return
        except urllib.error.HTTPError as error:
            if error.code in (401, 403) and time.monotonic() < deadline:
                time.sleep(0.25)
                continue
            if error.code in (401, 403):
                raise ValueError("Vantage rejected the new ingest credential.") from error
            raise ValueError("Vantage could not verify the new ingest credential.") from error
        except (OSError, urllib.error.URLError) as error:
            raise ValueError("Vantage could not be reached to verify the new ingest credential.") from error


def write_config(path, lines):
    content = ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")
    write_file(path, content)


def print_next_steps():
    print("Next steps:")
    print("1. Review and trust the Vantage hooks in Settings > Hooks.")
    print("2. Restart Codex.")
    print("3. Start a new conversation.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remove", action="store_true", help="remove the Vantage-managed block")
    parser.add_argument("--check-requirements", action="store_true", help="check prerequisites only")
    args = parser.parse_args()

    if not args.remove:
        problems = requirement_problems()
        if problems:
            report_requirement_problems(problems)
            return MISSING_REQUIREMENTS_EXIT
        for warning in requirement_warnings():
            print("warning:%s %s" % (warning, WARNING_MESSAGES[warning]), file=sys.stderr)
        if args.check_requirements:
            print("All Vantage telemetry requirements are installed.")
            return 0

    path = config_path()
    original = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            original = handle.read()

    try:
        lines, configured = remove_managed_block(original)
        otel_header, has_environment, conflicts = inspect_otel(lines)
        existing_vantage_token = vantage_exporter_token(lines)
        existing_vantage_endpoint = has_vantage_endpoint(lines)
    except ValueError as error:
        print("Could not read %s: %s" % (path, error), file=sys.stderr)
        return 1

    if args.remove:
        try:
            removed_hooks = remove_user_hooks()
            (classifier_directory() / "connection.json").unlink(missing_ok=True)
            removed_classifier = remove_classifier_files()
        except (OSError, UnicodeError, ValueError) as error:
            print("Could not remove Vantage files safely: %s" % error, file=sys.stderr)
            return 5
        if not configured:
            if existing_vantage_token:
                if removed_hooks or removed_classifier:
                    print("Removed Vantage hooks and classifier files.")
                    print(
                        "The Vantage configuration in %s is not managed by the plugin, so its telemetry settings were left unchanged."
                        % path
                    )
                else:
                    print("The Vantage configuration in %s is not managed by the plugin. No changes were made." % path)
            elif removed_hooks or removed_classifier:
                print("Removed Vantage hooks and classifier files.")
            else:
                print("Nothing to remove: no Vantage block in %s" % path)
            return 0
        write_config(path, lines)
        print("Removed Vantage telemetry from %s. Restart Codex to apply." % path)
        return 0

    # Reauthorize when a managed exporter targets a different environment.
    managed_token = vantage_exporter_token(original.splitlines()) if configured else None
    if configured and managed_token:
        if not classifier_token_path().is_file() or not classifier_markdown_path().is_file():
            print(
                "Vantage telemetry is configured, but its classifier files are missing. Remove Vantage telemetry and connect again.",
                file=sys.stderr,
            )
            return 2
        if not install_user_hooks_or_report():
            return 5
        save_connection(managed_token)
        print("Vantage telemetry is already configured in %s." % path)
        print_next_steps()
        return 0

    if existing_vantage_token:
        lines = remove_managed_settings(lines)
        try:
            otel_header, has_environment, remaining_conflicts = inspect_otel(lines)
        except (OSError, UnicodeError, ValueError) as error:
            print("Could not safely normalize %s: %s" % (path, error), file=sys.stderr)
            return 3
        if remaining_conflicts or has_vantage_endpoint(lines):
            print(
                "Could not safely normalize the existing Vantage exporter in %s. No changes were made." % path,
                file=sys.stderr,
            )
            return 3
        try:
            code_verifier, authorization_expires_at = browser_sign_in()
            credentials = wait_for_setup_credentials(code_verifier, authorization_expires_at)
            token = credentials["ingest_token"]
            verify_ingest_token(token)
            save_classifier(credentials["classifier"])
            save_connection(token)
        except (OSError, UnicodeError, ValueError) as error:
            print("Could not complete Vantage setup: %s" % error, file=sys.stderr)
            return 2
        block = managed_block(token, not has_environment)
        if otel_header is None:
            lines += ([""] if lines else []) + ["[otel]"] + block
        else:
            lines[otel_header + 1 : otel_header + 1] = block
        write_config(path, lines)
        if not install_user_hooks_or_report():
            return 5
        print("Refreshed Vantage telemetry in %s." % path)
        print_next_steps()
        return 0
    if conflicts or existing_vantage_endpoint:
        if existing_vantage_endpoint:
            print(
                "The Vantage exporter in %s is incomplete or uses an unsupported format. No changes were made." % path,
                file=sys.stderr,
            )
        elif conflicts:
            message = "Automatic setup cannot replace existing OpenTelemetry settings in %s: %s. No changes were made."
            print(message % (path, ", ".join(conflicts)), file=sys.stderr)
        print("Next step: remove or change the conflicting OpenTelemetry settings, then run this command again.", file=sys.stderr)
        return 3
    try:
        code_verifier, authorization_expires_at = browser_sign_in()
        credentials = wait_for_setup_credentials(code_verifier, authorization_expires_at)
        token = credentials["ingest_token"]
        verify_ingest_token(token)
        save_classifier(credentials["classifier"])
        save_connection(token)
    except (OSError, UnicodeError, ValueError) as error:
        print("Could not complete Vantage setup: %s" % error, file=sys.stderr)
        return 2

    block = managed_block(token, not has_environment)
    if otel_header is None:
        lines += ([""] if lines else []) + ["[otel]"] + block
    else:
        lines[otel_header + 1 : otel_header + 1] = block
    write_config(path, lines)
    if not install_user_hooks_or_report():
        return 5
    print("Configured Vantage telemetry in %s." % path)
    print_next_steps()
    return 0


if __name__ == "__main__":
    sys.exit(main())

import hashlib
import importlib.util
import io
import json
import os
import pathlib
import runpy
import shutil
import sqlite3
import subprocess
import tempfile
import sys
import tempfile
import unittest
from contextlib import closing
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[4]
PLUGIN_ROOT = ROOT / "plugins/vantage"
SCRIPT = PLUGIN_ROOT / "server/vantage_mcp.py"
MCP_SERVER = json.loads((PLUGIN_ROOT / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["vantage_local"]
SPEC = importlib.util.spec_from_file_location("vantage_mcp", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
sys.path.insert(0, str(ROOT / "plugins/vantage/hooks"))
from vantage_codex_hook import PLUGIN_VERSION as HOOK_VERSION, try_lock


def request(message_id, method, params=None):
    payload = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


class FakeProcess:
    def __init__(self, returncode=0, stdout="", stderr="", pending=False):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.pending = pending
        self.communicate_timeouts = []
        self.terminated = False

    def communicate(self, timeout=None):
        self.communicate_timeouts.append(timeout)
        if self.pending:
            raise subprocess.TimeoutExpired([], timeout)
        return self.stdout, self.stderr

    def poll(self):
        return None if self.pending else self.returncode

    def terminate(self):
        self.terminated = True
        self.pending = False

    def kill(self):
        self.terminated = True
        self.pending = False

    def wait(self, timeout=None):
        if self.pending:
            raise subprocess.TimeoutExpired([], timeout)
        return self.returncode


class VantageMcpTest(unittest.TestCase):
    def setUp(self):
        MODULE._ACTIVE_SETUP = None
        MODULE._LAST_SETUP_OUTCOME = None

    def test_server_runs_over_stdio_without_extra_output(self):
        messages = [
            request(1, "initialize", {"protocolVersion": MODULE.PROTOCOL_VERSION}),
            request(2, "tools/list"),
        ]

        environment = os.environ.copy()
        environment.pop("PLUGIN_ROOT", None)
        environment.pop("VANTAGE_CODEX_PYTHON", None)
        args = [arg.replace("${PLUGIN_ROOT}", str(PLUGIN_ROOT)) for arg in MCP_SERVER["args"]]
        completed = subprocess.run(
            [MCP_SERVER["command"], *args],
            input="".join(json.dumps(message) + "\n" for message in messages),
            capture_output=True,
            check=False,
            env=environment,
            cwd=PLUGIN_ROOT,
            text=True,
            timeout=5,
        )

        self.assertEqual(0, completed.returncode)
        self.assertEqual("", completed.stderr)
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([1, 2], [response["id"] for response in responses])
        self.assertEqual(
            "connect_vantage_telemetry",
            responses[1]["result"]["tools"][0]["name"],
        )
        self.assertIn("set_turn_attribution", {tool["name"] for tool in responses[1]["result"]["tools"]})

    def test_mcp_reports_a_missing_python_override(self):
        environment = os.environ.copy()
        environment.pop("PLUGIN_ROOT", None)
        environment.update(
            {
                "VANTAGE_CODEX_PYTHON": str(ROOT / "missing-python"),
            }
        )

        completed = subprocess.run(
            [sys.executable, "-I", str(SCRIPT)],
            capture_output=True, check=False, env=environment, text=True, timeout=5,
        )
        self.assertEqual(4, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertIn("VANTAGE_CODEX_PYTHON", completed.stderr)

    def test_negotiates_legacy_protocol_and_lists_deterministic_tools(self):
        messages = [
            request(1, "server/discover"),
            request(2, "initialize", {"protocolVersion": "2026-07-28"}),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            request(3, "tools/list"),
        ]
        input_stream = io.BytesIO(
            b"".join((json.dumps(message) + "\n").encode("utf-8") for message in messages)
        )
        output_stream = io.BytesIO()

        MODULE.serve(input_stream, output_stream)

        responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(-32601, responses[0]["error"]["code"])
        self.assertEqual(MODULE.PROTOCOL_VERSION, responses[1]["result"]["protocolVersion"])
        self.assertEqual("vantage-local", responses[1]["result"]["serverInfo"]["name"])
        self.assertEqual(3, responses[2]["id"])
        tools = responses[2]["result"]["tools"]
        self.assertEqual(
            [
                "connect_vantage_telemetry",
                "check_vantage_telemetry_setup",
                "remove_vantage_telemetry",
                "set_turn_attribution",
            ],
            [tool["name"] for tool in tools],
        )
        self.assertFalse(tools[0]["annotations"]["readOnlyHint"])
        self.assertTrue(tools[1]["annotations"]["readOnlyHint"])
        self.assertTrue(tools[2]["annotations"]["destructiveHint"])

    def test_set_turn_attribution_replaces_and_clears_tags(self):
        for tags, assignments in [({"ticket": "DEMO-1"}, ["ticket=DEMO-1"]), ({}, [])]:
            with mock.patch.object(subprocess, "run", return_value=FakeProcess()) as run:
                result = MODULE.call_tool({"name": "set_turn_attribution", "arguments": {
                    "session_id": "s", "turn_id": "t", "tags": tags,
                }})
            self.assertFalse(result["isError"])
            self.assertEqual(run.call_args.args[0][3:], [
                "tag", "codex", "s", "t", "--source", "inferred", "--replace", *assignments,
            ])

    def test_mcp_uses_the_same_installed_hook_that_created_the_turn(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, CODEX_HOME=tmp, VANTAGE_CODEX_PLUGIN_ROOT=""
        ):
            home = pathlib.Path(tmp)
            vantage = home / "vantage"
            plugin_hooks = MODULE.PLUGIN_ROOT / "hooks"
            launcher = vantage / "hooks" / "run_vantage_codex_hook.py"
            launcher.parent.mkdir(parents=True)
            shutil.copyfile(plugin_hooks / launcher.name, launcher)
            cache = home / "plugins" / "cache" / "vantage-codex-plugin" / "vantage"
            older, newer = cache / "older", cache / "newer"
            for root, version in ((older, "0.5.0"), (newer, "0.5.1")):
                (root / "hooks").mkdir(parents=True)
                (root / "plugin.json").write_text(json.dumps({"version": version}))
            writer = "vantage_codex_hook.py"
            (older / "hooks" / writer).write_text("raise SystemExit('stale hook')\n")
            shutil.copyfile(plugin_hooks / writer, newer / "hooks" / writer)
            endpoint, token = "http://127.0.0.1:1", "fixture-token"
            binding = hashlib.sha256((endpoint + "\0" + token).encode()).hexdigest()
            outbox = vantage / "outbox" / binding
            outbox.mkdir(parents=True)
            (vantage / "connection.json").write_text(json.dumps({
                "endpoint": endpoint, "token": token, "since_ns": 1,
            }))
            # Keep the test offline while exercising the same hook and writer as the server.
            with (outbox / "worker.lock").open("a+b") as lock, mock.patch.object(MODULE, "PLUGIN_ROOT", older):
                self.assertTrue(try_lock(lock))
                subprocess.run([sys.executable, "-I", str(launcher)],
                               input=json.dumps({"hook_event_name": "UserPromptSubmit",
                                                 "session_id": "s", "turn_id": "t"}),
                               capture_output=True, text=True, check=True)
                for turn, tags, success in [("t", {"ticket": "DEMO-1"}, True),
                                            ("missing", {}, False), ("t", {}, True)]:
                    args = {"session_id": "s", "turn_id": turn, "tags": tags}
                    result = MODULE.call_tool({"name": "set_turn_attribution", "arguments": args})
                    self.assertEqual(result["isError"], not success)
                    with closing(sqlite3.connect(outbox / "state.sqlite")) as db:
                        rows = db.execute("SELECT state FROM turns").fetchall()
                    self.assertEqual(len(rows), 1)
                    if success:
                        self.assertEqual(json.loads(rows[0][0])["tags"], tags)
                    else:
                        self.assertIn("could not find that turn", result["content"][0]["text"])
                (vantage / "connection.json").unlink()
                result = MODULE.set_turn_attribution({"session_id": "s", "turn_id": "t", "tags": {}})
                self.assertTrue(result["isError"])
                self.assertIn("Set up Vantage telemetry", result["content"][0]["text"])
                with closing(sqlite3.connect(outbox / "state.sqlite")) as db:
                    versions = {
                        json.loads(payload)["vantage.plugin.version"]
                        for (payload,) in db.execute("SELECT payload FROM outbox")
                    }
                self.assertEqual(versions, {"0.5.1"})

    def test_attribution_error_does_not_expose_subprocess_output(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, CODEX_HOME=tmp):
            launcher = pathlib.Path(tmp) / "vantage/hooks/run_vantage_codex_hook.py"
            launcher.parent.mkdir(parents=True)
            launcher.touch()
            completed = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"private telemetry details")
            with mock.patch.object(subprocess, "run", return_value=completed):
                result = MODULE.set_turn_attribution({"session_id": "s", "turn_id": "t", "tags": {}})
            self.assertTrue(result["isError"])
            self.assertNotIn("private telemetry details", result["content"][0]["text"])

    def test_set_turn_attribution_rejects_invalid_arguments_before_writing(self):
        valid = {"session_id": "s", "turn_id": "t", "tags": {}}
        for invalid in [{"session_id": "--help"}, {"tags": {"x=y": "a"}}, {"tags": {"x": 2}},
                        {"tags": {"x": "two\nlines"}}, {"binding": "another"}]:
            with self.subTest(invalid=invalid), mock.patch.object(subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    MODULE.set_turn_attribution(dict(valid, **invalid))
                run.assert_not_called()

    def test_connect_tool_returns_only_sanitized_output(self):
        secret = "vntg_ti_PRIVATE_CREDENTIAL"
        authorization_url = "https://console.vantage.sh/codex/telemetry/authorize?secret=value"
        process = FakeProcess(
            stdout="Configured with %s at %s" % (secret, authorization_url),
            stderr="code_verifier=private-verifier",
        )

        with mock.patch.object(MODULE.subprocess, "Popen", return_value=process):
            started = MODULE.handle_message(
                request(
                    1,
                    "tools/call",
                    {"name": "connect_vantage_telemetry", "arguments": {}},
                )
            )
            response = MODULE.handle_message(
                request(
                    2,
                    "tools/call",
                    {
                        "name": "check_vantage_telemetry_setup",
                        "arguments": {"wait_seconds": 0},
                    },
                )
            )

        serialized = json.dumps(response)
        self.assertEqual("pending", started["result"]["structuredContent"]["status"])
        self.assertEqual("connected", response["result"]["structuredContent"]["status"])
        self.assertIn("Restart Codex", serialized)
        self.assertIn("trust the Vantage hooks", serialized)
        self.assertIn("rerun setup to refresh the copied hook scripts", serialized)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(authorization_url, serialized)
        self.assertNotIn("private-verifier", serialized)

    def test_existing_connection_still_requests_a_restart(self):
        process = FakeProcess(
            stdout="Vantage telemetry is already configured.",
            stderr="",
        )

        with mock.patch.object(MODULE.subprocess, "Popen", return_value=process):
            MODULE.handle_message(
                request(
                    1,
                    "tools/call",
                    {"name": "connect_vantage_telemetry", "arguments": {}},
                )
            )
            response = MODULE.handle_message(
                request(
                    2,
                    "tools/call",
                    {
                        "name": "check_vantage_telemetry_setup",
                        "arguments": {"wait_seconds": 0},
                    },
                )
            )

        result = response["result"]
        self.assertEqual("already_connected", result["structuredContent"]["status"])
        self.assertTrue(result["structuredContent"]["restart_required"])
        self.assertIn("Restart Codex", result["content"][0]["text"])
        self.assertIn("trust the Vantage hooks", result["content"][0]["text"])
        self.assertIn(
            "rerun setup to refresh the copied hook scripts",
            result["content"][0]["text"],
        )

    def test_connect_returns_immediately_and_does_not_start_duplicate_setup(self):
        process = FakeProcess(pending=True)

        with mock.patch.object(MODULE.subprocess, "Popen", return_value=process) as popen:
            first = MODULE.handle_message(
                request(
                    1,
                    "tools/call",
                    {"name": "connect_vantage_telemetry", "arguments": {}},
                )
            )
            second = MODULE.handle_message(
                request(
                    2,
                    "tools/call",
                    {"name": "connect_vantage_telemetry", "arguments": {}},
                )
            )

        self.assertEqual("pending", first["result"]["structuredContent"]["status"])
        self.assertEqual("pending", second["result"]["structuredContent"]["status"])
        popen.assert_called_once()
        self.assertEqual([0], process.communicate_timeouts)

    def test_status_wait_is_bounded_and_reports_pending(self):
        process = FakeProcess(pending=True)
        MODULE._ACTIVE_SETUP = process

        response = MODULE.handle_message(
            request(
                1,
                "tools/call",
                {
                    "name": "check_vantage_telemetry_setup",
                    "arguments": {"wait_seconds": 45},
                },
            )
        )

        self.assertEqual("pending", response["result"]["structuredContent"]["status"])
        self.assertEqual([45], process.communicate_timeouts)

    def test_cancellation_notification_stops_active_setup(self):
        process = FakeProcess(pending=True)
        MODULE._ACTIVE_SETUP = process

        response = MODULE.handle_message(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 1, "reason": "user cancelled"},
            }
        )
        status = MODULE.handle_message(
            request(
                2,
                "tools/call",
                {
                    "name": "check_vantage_telemetry_setup",
                    "arguments": {"wait_seconds": 0},
                },
            )
        )

        self.assertIsNone(response)
        self.assertTrue(process.terminated)
        self.assertIsNone(MODULE._ACTIVE_SETUP)
        self.assertEqual("cancelled", status["result"]["structuredContent"]["status"])

    def test_late_cancellation_preserves_completed_setup_result(self):
        process = FakeProcess(stdout="Configured Vantage telemetry.")
        MODULE._ACTIVE_SETUP = process

        MODULE.handle_message(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 1},
            }
        )
        status = MODULE.handle_message(
            request(
                2,
                "tools/call",
                {
                    "name": "check_vantage_telemetry_setup",
                    "arguments": {"wait_seconds": 0},
                },
            )
        )

        self.assertFalse(process.terminated)
        self.assertEqual("connected", status["result"]["structuredContent"]["status"])

    def test_remove_tool_passes_only_the_remove_flag(self):
        completed = subprocess.CompletedProcess([], 0, stdout="Removed", stderr="")

        with mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
            response = MODULE.handle_message(
                request(
                    1,
                    "tools/call",
                    {"name": "remove_vantage_telemetry", "arguments": {}},
                )
            )

        command = run.call_args.args[0]
        self.assertEqual(sys.executable, command[0])
        self.assertEqual(str(MODULE.SETUP_SCRIPT), command[1])
        self.assertEqual("--remove", command[2])
        self.assertEqual("removed", response["result"]["structuredContent"]["status"])

    def test_remove_reports_unmanaged_configuration_as_unchanged(self):
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout="The Vantage configuration is not managed by the plugin. No changes were made.",
            stderr="",
        )

        with mock.patch.object(MODULE.subprocess, "run", return_value=completed):
            response = MODULE.handle_message(
                request(
                    1,
                    "tools/call",
                    {"name": "remove_vantage_telemetry", "arguments": {}},
                )
            )

        result = response["result"]
        self.assertEqual("not_configured", result["structuredContent"]["status"])
        self.assertFalse(result["structuredContent"]["restart_required"])

    def test_remove_reports_removed_when_hooks_were_removed_but_configuration_is_unmanaged(self):
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                "Removed Vantage hooks from ~/.codex/hooks.json.\n"
                "The Vantage configuration is not managed by the plugin, so its telemetry settings were left unchanged."
            ),
            stderr="",
        )

        with mock.patch.object(MODULE.subprocess, "run", return_value=completed):
            response = MODULE.handle_message(
                request(
                    1,
                    "tools/call",
                    {"name": "remove_vantage_telemetry", "arguments": {}},
                )
            )

        result = response["result"]
        self.assertEqual("removed", result["structuredContent"]["status"])
        self.assertTrue(result["structuredContent"]["restart_required"])

    def test_failed_setup_does_not_return_captured_output(self):
        process = FakeProcess(
            returncode=2,
            stdout="https://private.example/authorize",
            stderr="Bearer secret-token",
        )

        with mock.patch.object(MODULE.subprocess, "Popen", return_value=process):
            MODULE.handle_message(
                request(
                    1,
                    "tools/call",
                    {"name": "connect_vantage_telemetry", "arguments": {}},
                )
            )
            response = MODULE.handle_message(
                request(
                    2,
                    "tools/call",
                    {
                        "name": "check_vantage_telemetry_setup",
                        "arguments": {"wait_seconds": 0},
                    },
                )
            )

        serialized = json.dumps(response)
        self.assertTrue(response["result"]["isError"])
        self.assertNotIn("private.example", serialized)
        self.assertNotIn("secret-token", serialized)

    def test_hook_install_failure_is_actionable_and_sanitized(self):
        process = FakeProcess(
            returncode=5,
            stdout="Vantage telemetry is configured.",
            stderr="private hook parsing details",
        )

        with mock.patch.object(MODULE.subprocess, "Popen", return_value=process):
            MODULE.handle_message(
                request(1, "tools/call", {"name": "connect_vantage_telemetry", "arguments": {}})
            )
            response = MODULE.handle_message(
                request(
                    2,
                    "tools/call",
                    {"name": "check_vantage_telemetry_setup", "arguments": {"wait_seconds": 0}},
                )
            )

        serialized = json.dumps(response)
        self.assertTrue(response["result"]["isError"])
        self.assertIn("hooks could not be installed safely", serialized)
        self.assertNotIn("private hook parsing details", serialized)

    def test_tools_reject_arguments_and_unknown_names(self):
        arguments = MODULE.handle_message(
            request(
                1,
                "tools/call",
                {"name": "connect_vantage_telemetry", "arguments": {"token": "no"}},
            )
        )
        unknown = MODULE.handle_message(
            request(2, "tools/call", {"name": "unknown", "arguments": {}})
        )
        wait = MODULE.handle_message(
            request(
                3,
                "tools/call",
                {
                    "name": "check_vantage_telemetry_setup",
                    "arguments": {"wait_seconds": 46},
                },
            )
        )

        self.assertEqual(-32602, arguments["error"]["code"])
        self.assertEqual(-32601, unknown["error"]["code"])
        self.assertEqual(-32602, wait["error"]["code"])

    def test_plugin_versions_stay_in_sync(self):
        manifest = json.loads(
            (ROOT / "plugins/vantage/plugin.json").read_text(encoding="utf-8")
        )
        setup = runpy.run_path(str(MODULE.SETUP_SCRIPT))
        self.assertEqual(manifest["version"], HOOK_VERSION)
        self.assertEqual(manifest["version"], setup["PLUGIN_VERSION"])
        self.assertEqual(manifest["version"], MODULE.plugin_version())

        self.assertEqual("stdio", MCP_SERVER["type"])
        self.assertEqual("python3", MCP_SERVER["command"])
        self.assertEqual(["-I", "${PLUGIN_ROOT}/server/vantage_mcp.py"], MCP_SERVER["args"])
        self.assertEqual("${PLUGIN_ROOT}", MCP_SERVER["cwd"])


class RequirementOutcomeTest(unittest.TestCase):
    def test_maps_requirement_ids_to_fixed_messages(self):
        completed = subprocess.CompletedProcess(
            ["setup"], 6, stdout="",
            stderr="Vantage telemetry setup needs a few things first:\n"
                   "requirement:curl_http2 anything\nrequirement:bogus Bearer secret-token\n",
        )
        outcome = MODULE.outcome_from_completed("connect", completed)
        self.assertEqual("missing_requirements", outcome.status)
        self.assertTrue(outcome.is_error)
        self.assertIn("HTTP/2", outcome.message)
        self.assertNotIn("secret-token", outcome.message)
        self.assertNotIn("bogus", outcome.message)

    def test_requirement_ids_match_the_setup_script(self):
        script = ROOT / "plugins/vantage/skills/setup-vantage-telemetry/scripts/configure_vantage_telemetry.py"
        spec = importlib.util.spec_from_file_location("configure_for_ids", script)
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        self.assertEqual(set(setup.REQUIREMENT_MESSAGES), set(MODULE.REQUIREMENT_MESSAGES))
        self.assertEqual(setup.WARNING_MESSAGES, MODULE.WARNING_MESSAGES)
        self.assertEqual(setup.MISSING_REQUIREMENTS_EXIT, MODULE.MISSING_REQUIREMENTS_EXIT)

    def test_git_warning_is_returned_with_success_using_fixed_text(self):
        for stdout, status in [("configured", "connected"), ("already configured", "already_connected")]:
            with self.subTest(status=status):
                completed = subprocess.CompletedProcess(
                    ["setup"], 0, stdout=stdout,
                    stderr="warning:git_attribution untrusted token\nwarning:unknown untrusted output\n",
                )
                outcome = MODULE.outcome_from_completed("connect", completed)
                self.assertEqual(status, outcome.status)
                self.assertFalse(outcome.is_error)
                self.assertIn("Install Git for repository", outcome.message)
                self.assertNotIn("untrusted", outcome.message)

    def test_status_enum_includes_missing_requirements(self):
        schema = MODULE.tool_definitions()[0]["outputSchema"]
        self.assertIn("missing_requirements", schema["properties"]["status"]["enum"])

    def test_mcp_rejects_python_older_than_3_9(self):
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, VANTAGE_CODEX_PYTHON=""), \
                mock.patch.object(sys, "version_info", (3, 8)), mock.patch.object(sys, "stderr", stderr), \
                mock.patch.object(MODULE, "serve") as serve:
            self.assertEqual(4, MODULE.main())
        serve.assert_not_called()
        self.assertIn("Python 3.9 or newer", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

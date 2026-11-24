import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import urllib.parse
import unittest
from unittest import mock


SCRIPT = (
    pathlib.Path(__file__).parents[4]
    / "plugins/vantage/skills/setup-vantage-telemetry/scripts/configure_vantage_telemetry.py"
)
SPEC = importlib.util.spec_from_file_location("configure_vantage_telemetry", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Response:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ConfigureVantageTelemetryTest(unittest.TestCase):
    def setUp(self):
        # Setup tests should not depend on the machine's curl or Python build.
        patcher = mock.patch.object(MODULE, "requirement_problems", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(MODULE, "requirement_warnings", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_normal_home_installs_one_hook_and_shared_staging_connection(self):
        profile_variable = "USERPROFILE" if os.name == "nt" else "HOME"
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {profile_variable: home, "CODEX_HOME": ""}), mock.patch.object(sys, "argv", ["setup"]):
            stderr = io.StringIO()
            with mock.patch.object(MODULE, "requirement_warnings", return_value=["git_attribution"]), \
                    contextlib.redirect_stderr(stderr), \
                    mock.patch.object(MODULE, "browser_sign_in", return_value=("verifier", 1)), mock.patch.object(
                MODULE, "wait_for_setup_credentials", return_value=self.setup_credentials()
            ), mock.patch.object(MODULE, "verify_ingest_token"):
                self.assertEqual(MODULE.main(), 0)
            self.assertIn("warning:git_attribution", stderr.getvalue())
            root = pathlib.Path(home) / ".codex"
            config = (root / "config.toml").read_text()
            connection = root / "vantage/connection.json"
            settings = json.loads(connection.read_text())
            self.assertEqual("https://ingest-staging.vantage.sh", settings["endpoint"])
            self.assertIn(settings["endpoint"] + "/v1/logs", config)
            self.assertIn("Bearer " + settings["token"], config)
            if os.name != "nt":
                self.assertEqual(0o600, connection.stat().st_mode & 0o777)
            self.assertEqual(MODULE.main(), 0)
            self.assertEqual(settings, json.loads(connection.read_text()))
            self.assertEqual(set(MODULE.HOOK_EVENTS), set(MODULE.load_user_hooks()["hooks"]))
            with mock.patch.object(sys, "argv", ["setup", "--remove"]):
                self.assertEqual(MODULE.main(), 0)
            self.assertFalse(connection.exists())
            self.assertNotIn("exporter =", (root / "config.toml").read_text())

    def setup_payload(self, ingest_token="secret-token"):
        return {
            "plaintext_token": ingest_token,
            "classifier_read_token": "classifier-token",
            "classifier_markdown": "# Current classifier\n",
        }

    def setup_credentials(self, ingest_token="new-token"):
        return {
            "ingest_token": ingest_token,
            "classifier": {
                "token": "classifier-token",
                "markdown": b"# Current classifier\n",
            },
        }

    def authorization_expires_at(self):
        return int(MODULE.time.time()) + MODULE.AUTHORIZATION_TIMEOUT

    def run_main(self, config="", configured_assets=False):
        with tempfile.TemporaryDirectory() as codex_home:
            path = pathlib.Path(codex_home) / "config.toml"
            if config:
                path.write_text(config, encoding="utf-8")
            if configured_assets:
                vantage = pathlib.Path(codex_home) / "vantage"
                vantage.mkdir()
                (vantage / "classifier_read_token").write_text(
                    "classifier-token", encoding="utf-8"
                )
                (vantage / "classifier.md").write_text(
                    "# Current classifier\n", encoding="utf-8"
                )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {"CODEX_HOME": codex_home}):
                with mock.patch.object(sys, "argv", [str(SCRIPT)]):
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        status = MODULE.main()

            result = path.read_text(encoding="utf-8") if path.exists() else ""
            return status, stdout.getvalue(), stderr.getvalue(), result

    def test_browser_sign_in_uses_no_localhost_callback(self):
        with mock.patch.object(MODULE.time, "time", return_value=1_000):
            with mock.patch.object(MODULE.sys, "platform", "linux"):
                with mock.patch.object(MODULE.webbrowser, "open", return_value=True) as browser_open:
                    verifier, authorization_expires_at = MODULE.browser_sign_in()

        authorization_url = browser_open.call_args.args[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(authorization_url).query)
        expected_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")

        self.assertEqual([expected_challenge], query["code_challenge"])
        self.assertEqual(["S256"], query["code_challenge_method"])
        self.assertEqual([str(authorization_expires_at)], query["authorization_expires_at"])
        self.assertEqual(1_000 + MODULE.AUTHORIZATION_TIMEOUT, authorization_expires_at)
        self.assertNotIn("redirect_uri", query)
        self.assertNotIn("state", query)

    def test_browser_sign_in_uses_native_open_on_macos(self):
        completed = mock.Mock(returncode=0)

        with mock.patch.object(MODULE.sys, "platform", "darwin"):
            with mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
                with mock.patch.object(MODULE.webbrowser, "open") as browser_open:
                    MODULE.browser_sign_in()

        command = run.call_args.args[0]
        self.assertEqual("open", command[0])
        self.assertTrue(command[1].startswith(MODULE.AUTHORIZATION_ENDPOINT + "?"))
        self.assertEqual(MODULE.subprocess.DEVNULL, run.call_args.kwargs["stdout"])
        self.assertEqual(MODULE.subprocess.DEVNULL, run.call_args.kwargs["stderr"])
        browser_open.assert_not_called()

    def test_browser_sign_in_does_not_print_the_authorization_url(self):
        stdout = io.StringIO()

        with mock.patch.object(MODULE, "open_authorization_url", return_value=False):
            with contextlib.redirect_stdout(stdout):
                with self.assertRaisesRegex(ValueError, "could not be opened"):
                    MODULE.browser_sign_in()

        self.assertEqual("", stdout.getvalue())

    def test_waits_until_vantage_returns_setup_credentials(self):
        responses = [
            Response(202, {"status": "pending"}),
            Response(201, self.setup_payload()),
        ]

        with mock.patch.object(MODULE.urllib.request, "urlopen", side_effect=responses) as urlopen:
            with mock.patch.object(MODULE.time, "sleep") as sleep:
                authorization_expires_at = self.authorization_expires_at()
                credentials = MODULE.wait_for_setup_credentials(
                    "v" * 64, authorization_expires_at
                )

        self.assertEqual("secret-token", credentials["ingest_token"])
        self.assertEqual("classifier-token", credentials["classifier"]["token"])
        self.assertEqual(b"# Current classifier\n", credentials["classifier"]["markdown"])
        sleep.assert_called_once_with(MODULE.POLL_INTERVAL)
        request = urlopen.call_args_list[0].args[0]
        request_payload = json.loads(request.data)
        self.assertEqual(authorization_expires_at, request_payload["authorization_expires_at"])
        self.assertEqual(MODULE.USER_AGENT, request.get_header("User-agent"))

    def test_verifies_ingest_token_with_the_plugin_user_agent(self):
        with mock.patch.object(
            MODULE.urllib.request,
            "urlopen",
            return_value=Response(200, {}),
        ) as urlopen:
            with mock.patch.object(MODULE, "verify_attribution_transport") as grpc:
                MODULE.verify_ingest_token("secret-token")
                grpc.assert_called_once_with("secret-token")

        request = urlopen.call_args.args[0]
        self.assertEqual(MODULE.USER_AGENT, request.get_header("User-agent"))

    def test_backs_off_while_authorization_is_pending(self):
        responses = [
            Response(202, {"status": "pending"}),
            Response(202, {"status": "pending"}),
            Response(202, {"status": "pending"}),
            Response(201, self.setup_payload()),
        ]

        with mock.patch.object(MODULE.urllib.request, "urlopen", side_effect=responses):
            with mock.patch.object(MODULE.time, "sleep") as sleep:
                credentials = MODULE.wait_for_setup_credentials(
                    "v" * 64, self.authorization_expires_at()
                )

        self.assertEqual("secret-token", credentials["ingest_token"])
        self.assertEqual(
            [mock.call(2), mock.call(4), mock.call(8)],
            sleep.call_args_list,
        )

    def test_retries_rate_limited_polling_with_backoff(self):
        rate_limited = MODULE.urllib.error.HTTPError(
            MODULE.CLAIM_ENDPOINT,
            429,
            "Too Many Requests",
            {},
            None,
        )
        responses = [rate_limited, Response(201, self.setup_payload())]

        with mock.patch.object(MODULE.urllib.request, "urlopen", side_effect=responses):
            with mock.patch.object(MODULE.time, "sleep") as sleep:
                credentials = MODULE.wait_for_setup_credentials(
                    "v" * 64, self.authorization_expires_at()
                )

        self.assertEqual("secret-token", credentials["ingest_token"])
        sleep.assert_called_once_with(MODULE.POLL_INTERVAL)

    def test_retries_classifier_issuance_failure_with_backoff(self):
        unavailable = MODULE.urllib.error.HTTPError(
            MODULE.CLAIM_ENDPOINT,
            503,
            "Service Unavailable",
            {},
            None,
        )
        responses = [unavailable, Response(201, self.setup_payload())]

        with mock.patch.object(MODULE.urllib.request, "urlopen", side_effect=responses):
            with mock.patch.object(MODULE.time, "sleep") as sleep:
                credentials = MODULE.wait_for_setup_credentials(
                    "v" * 64, self.authorization_expires_at()
                )

        self.assertEqual("secret-token", credentials["ingest_token"])
        sleep.assert_called_once_with(MODULE.POLL_INTERVAL)

    def test_default_command_stops_when_plugin_configuration_exists(self):
        config = "[otel]\n%s\n" % "\n".join(MODULE.managed_block("existing-token", True))

        with mock.patch.object(MODULE, "browser_sign_in") as browser_sign_in:
            status, stdout, stderr, result = self.run_main(config, configured_assets=True)

        self.assertEqual(0, status)
        self.assertIn("already configured", stdout)
        self.assertIn("Next steps:", stdout)
        self.assertEqual("", stderr)
        self.assertEqual(config, result)
        browser_sign_in.assert_not_called()

    def test_configured_telemetry_requires_classifier_files(self):
        config = "[otel]\n%s\n" % "\n".join(MODULE.managed_block("existing-token", True))

        status, stdout, stderr, result = self.run_main(config)

        self.assertEqual(2, status)
        self.assertEqual("", stdout)
        self.assertIn("classifier files are missing", stderr)
        self.assertEqual(config, result)

    def test_saves_classifier_credential_and_markdown_privately(self):
        with tempfile.TemporaryDirectory() as codex_home:
            with mock.patch.dict(os.environ, {"CODEX_HOME": codex_home}):
                MODULE.save_classifier(self.setup_credentials()["classifier"])
                token_path = MODULE.classifier_token_path()
                markdown_path = MODULE.classifier_markdown_path()

            self.assertEqual("classifier-token", token_path.read_text(encoding="utf-8"))
            self.assertEqual("# Current classifier\n", markdown_path.read_text(encoding="utf-8"))
            if os.name != "nt":
                self.assertEqual(0o600, token_path.stat().st_mode & 0o777)
                self.assertEqual(0o600, markdown_path.stat().st_mode & 0o777)

    def test_default_command_refreshes_an_existing_vantage_exporter(self):
        config = """[otel]
log_user_prompt = true
log_agent_responses = true
log_guardian_assessments = true
tool_result = { max_bytes = 1048576 }
exporter = { otlp-http = { endpoint = "https://ingest-staging.vantage.sh/v1/logs", headers = { authorization = "Bearer existing-token" } } }
trace_exporter = "none"
"""

        with mock.patch.object(
            MODULE,
            "browser_sign_in",
            return_value=("verifier", self.authorization_expires_at()),
        ):
            with mock.patch.object(
                MODULE,
                "wait_for_setup_credentials",
                return_value=self.setup_credentials(),
            ):
                with mock.patch.object(MODULE, "verify_ingest_token"):
                    status, stdout, stderr, result = self.run_main(config)

        self.assertEqual(0, status)
        self.assertIn("Refreshed Vantage telemetry", stdout)
        self.assertIn("Next steps:", stdout)
        self.assertEqual("", stderr)
        self.assertIn('authorization = "Bearer new-token"', result)
        self.assertNotIn("existing-token", result)
        self.assertIn("# BEGIN Vantage telemetry", result)
        self.assertIn("log_user_prompt = false", result)
        self.assertIn("log_agent_responses = false", result)
        self.assertIn("log_guardian_assessments = false", result)
        self.assertIn("tool_result = { max_bytes = 0 }", result)
        self.assertNotIn("log_user_prompt = true", result)
        self.assertNotIn("log_agent_responses = true", result)
        self.assertIn('trace_exporter = "none"', result)

    def test_default_command_configures_telemetry(self):
        with mock.patch.object(
            MODULE,
            "browser_sign_in",
            return_value=("verifier", self.authorization_expires_at()),
        ):
            with mock.patch.object(
                MODULE,
                "wait_for_setup_credentials",
                return_value=self.setup_credentials(),
            ):
                with mock.patch.object(MODULE, "verify_ingest_token"):
                    status, stdout, stderr, result = self.run_main()

        self.assertEqual(0, status)
        self.assertIn("Configured Vantage telemetry", stdout)
        self.assertIn("Next steps:", stdout)
        self.assertEqual("", stderr)
        self.assertIn("[otel]", result)
        self.assertIn('authorization = "Bearer new-token"', result)

    def test_default_command_reports_a_conflicting_exporter(self):
        config = """[otel]
exporter = { otlp-http = { endpoint = "https://example.com/v1/logs" } }
"""

        with mock.patch.object(MODULE, "browser_sign_in") as browser_sign_in:
            status, stdout, stderr, result = self.run_main(config)

        self.assertEqual(3, status)
        self.assertEqual("", stdout)
        self.assertIn("No changes were made", stderr)
        self.assertIn("run this command again", stderr)
        self.assertEqual(config, result)
        browser_sign_in.assert_not_called()

    def test_installs_user_hooks_without_replacing_existing_hooks(self):
        with tempfile.TemporaryDirectory() as codex_home:
            hooks_path = pathlib.Path(codex_home) / "hooks.json"
            existing = {
                "hooks": {
                    "PostToolUse": [{"hooks": [{"type": "command", "command": "echo existing"}]}],
                    "SessionStart": [{"hooks": [{"type": "command", "command": "echo start"}]}],
                }
            }
            with mock.patch.dict(os.environ, {"CODEX_HOME": codex_home}):
                previous = json.loads(json.dumps(existing))
                unrelated_pre_hook = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo pre"}]}
                legacy_windows = MODULE.subprocess.list2cmdline([
                    "python", "-I", str(MODULE.managed_hooks_dir() / "run_vantage_codex_hook.py"),
                ])
                previous["hooks"]["PreToolUse"] = [unrelated_pre_hook, {
                    "hooks": [{"type": "command", "commandWindows": legacy_windows}],
                }]
                previous["hooks"]["PostToolUse"].append({"hooks": [MODULE.hook_handler("PostToolUse")]})
                legacy_py = MODULE.subprocess.list2cmdline([
                    "py", "-3", "-I", str(MODULE.managed_hooks_dir() / "run_vantage_codex_hook.py"),
                ])
                previous["hooks"]["SessionEnd"] = [{
                    "hooks": [{"type": "command", "commandWindows": legacy_py, "timeout": 30}],
                }]
                hooks_path.write_text(json.dumps(previous), encoding="utf-8")
                MODULE.install_user_hooks()
                installed = pathlib.Path(codex_home) / "vantage" / "hooks"
                for name in MODULE.HOOK_SCRIPT_NAMES:
                    (installed / name).write_bytes(b"stale hook script")
                MODULE.install_user_hooks()
                payload = json.loads(hooks_path.read_text(encoding="utf-8"))
                self.assertEqual([unrelated_pre_hook], payload["hooks"]["PreToolUse"])
                self.assertEqual(existing["hooks"]["PostToolUse"], payload["hooks"]["PostToolUse"][:1])
                matcher = payload["hooks"]["PostToolUse"][-1]["matcher"]
                self.assertRegex("Bash", matcher)
                self.assertRegex("apply_patch", matcher)
                self.assertNotRegex("mcp__example__read", matcher)
                self.assertTrue(payload["hooks"]["PostToolUse"][-1]["hooks"][0]["commandWindows"].startswith("python3 -I "))
                self.assertEqual("echo start", payload["hooks"]["SessionStart"][0]["hooks"][0]["command"])
                for event_name in MODULE.HOOK_EVENTS:
                    managed = [
                        handler
                        for group in payload["hooks"][event_name]
                        for handler in group.get("hooks", [])
                        if MODULE.is_managed_hook(handler)
                    ]
                    self.assertEqual(1, len(managed))
                    if event_name == "SessionEnd":
                        self.assertEqual(3, managed[0]["timeout"])
                        self.assertEqual(1, len(payload["hooks"][event_name]))
                self.assertIn(
                    "run_vantage_codex_hook.py",
                    json.dumps(payload["hooks"]),
                )
                self.assertIn("-I", json.dumps(payload["hooks"]))
                self.assertNotIn(
                    "vantage/hooks/vantage_codex_hook.py",
                    json.dumps(payload["hooks"]),
                )

            for name in MODULE.HOOK_SCRIPT_NAMES:
                destination = installed / name
                self.assertEqual(
                    (MODULE.BUNDLED_HOOKS_DIR / name).read_bytes(),
                    destination.read_bytes(),
                )
                if os.name != "nt":
                    self.assertTrue(destination.stat().st_mode & 0o100)

    def test_remove_user_hooks_preserves_unrelated_hooks(self):
        with tempfile.TemporaryDirectory() as codex_home:
            hooks_path = pathlib.Path(codex_home) / "hooks.json"
            with mock.patch.dict(os.environ, {"CODEX_HOME": codex_home}):
                MODULE.install_user_hooks()
                payload = json.loads(hooks_path.read_text(encoding="utf-8"))
                payload["hooks"]["Stop"].insert(
                    0,
                    {"hooks": [{"type": "command", "command": "echo keep-me"}]},
                )
                hooks_path.write_text(json.dumps(payload), encoding="utf-8")

                self.assertTrue(MODULE.remove_user_hooks())

            payload = json.loads(hooks_path.read_text(encoding="utf-8"))
            self.assertEqual("echo keep-me", payload["hooks"]["Stop"][0]["hooks"][0]["command"])
            self.assertNotIn("SessionStart", payload["hooks"])
            for name in MODULE.HOOK_SCRIPT_NAMES:
                self.assertFalse((pathlib.Path(codex_home) / "vantage" / "hooks" / name).exists())

    def test_remove_reports_removed_hooks_when_configuration_is_unmanaged(self):
        config = """[otel]
exporter = { otlp-http = { endpoint = "https://ingest-staging.vantage.sh/v1/logs", headers = { authorization = "Bearer existing-token" } } }
"""

        with tempfile.TemporaryDirectory() as codex_home:
            config_path = pathlib.Path(codex_home) / "config.toml"
            hooks_path = pathlib.Path(codex_home) / "hooks.json"
            config_path.write_text(config, encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.dict(os.environ, {"CODEX_HOME": codex_home}):
                MODULE.install_user_hooks()
                with mock.patch.object(sys, "argv", [str(SCRIPT), "--remove"]):
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        status = MODULE.main()

            self.assertEqual(0, status)
            self.assertIn("Removed Vantage hooks", stdout.getvalue())
            self.assertIn("telemetry settings were left unchanged", stdout.getvalue())
            self.assertNotIn("No changes were made", stdout.getvalue())
            self.assertEqual("", stderr.getvalue())
            self.assertEqual(config, config_path.read_text(encoding="utf-8"))
            self.assertFalse(hooks_path.exists())

    def test_invalid_user_hooks_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as codex_home:
            hooks_path = pathlib.Path(codex_home) / "hooks.json"
            hooks_path.write_text("{invalid", encoding="utf-8")

            with mock.patch.dict(os.environ, {"CODEX_HOME": codex_home}):
                with self.assertRaisesRegex(ValueError, "could not be read safely"):
                    MODULE.install_user_hooks()

            self.assertEqual("{invalid", hooks_path.read_text(encoding="utf-8"))
            self.assertFalse((pathlib.Path(codex_home) / "vantage").exists())

class RequirementChecksTest(unittest.TestCase):
    @staticmethod
    def curl_version(features):
        completed = mock.Mock(stdout="curl 8.5.0\nProtocols: http https\nFeatures: %s\n" % features)
        return mock.Mock(return_value=completed)

    def test_ready_machine_has_no_problems(self):
        problems = MODULE.requirement_problems(
            version=(3, 11), os_name="posix", which=lambda _: "/usr/bin/curl",
            run=self.curl_version("alt-svc HTTP2 HTTPS-proxy IPv6 SSL"),
        )
        self.assertEqual([], problems)

    def test_reports_old_python(self):
        problems = MODULE.requirement_problems(
            version=(3, 8), os_name="posix", which=lambda _: "/usr/bin/curl",
            run=self.curl_version("HTTP2"),
        )
        self.assertEqual(["python_version"], problems)

    def test_reports_missing_curl(self):
        problems = MODULE.requirement_problems(version=(3, 11), os_name="posix", which=lambda _: None)
        self.assertEqual(["curl_missing"], problems)

    def test_reports_curl_without_http2(self):
        problems = MODULE.requirement_problems(
            version=(3, 11), os_name="posix", which=lambda _: "/usr/bin/curl",
            run=self.curl_version("alt-svc IPv6 SSL"),
        )
        self.assertEqual(["curl_http2"], problems)

    def test_reports_curl_that_cannot_run(self):
        problems = MODULE.requirement_problems(
            version=(3, 11), os_name="posix", which=lambda _: "/usr/bin/curl",
            run=mock.Mock(side_effect=OSError("broken")),
        )
        self.assertEqual(["curl_http2"], problems)

    def test_windows_checks_the_configured_curl(self):
        executable = r"C:\Tools\curl.exe"
        which = mock.Mock(return_value=executable)
        run = self.curl_version("HTTP2")
        with mock.patch.dict(os.environ, VANTAGE_CODEX_CURL=executable):
            problems = MODULE.requirement_problems(version=(3, 11), os_name="nt", which=which, run=run)
        self.assertEqual([], problems)
        which.assert_called_once_with(executable)
        self.assertEqual([executable, "-V"], run.call_args.args[0])

    def test_git_warning_uses_version_and_handles_missing_git(self):
        for stdout, expected in [
            ("git version 2.30.9\n", []),
            ("git version 2.31.0\n", []),
            ("git version 2.39.5 (Apple Git-154)\n", []),
            ("git version 2.52.0.windows.1\n", []),
        ]:
            with self.subTest(stdout=stdout):
                run = mock.Mock(return_value=mock.Mock(stdout=stdout, returncode=0))
                self.assertEqual(expected, MODULE.requirement_warnings(run=run))
        self.assertEqual(["git_attribution"], MODULE.requirement_warnings(run=mock.Mock(side_effect=FileNotFoundError)))

    def test_setup_stops_before_sign_in_when_requirements_are_missing(self):
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as codex_dir, mock.patch.dict(os.environ, CODEX_HOME=codex_dir), \
                mock.patch.object(sys, "argv", ["setup"]), \
                mock.patch.object(MODULE, "requirement_problems", return_value=["curl_missing"]), \
                mock.patch.object(MODULE, "browser_sign_in") as sign_in, contextlib.redirect_stderr(stderr):
            self.assertEqual(MODULE.MISSING_REQUIREMENTS_EXIT, MODULE.main())
            self.assertFalse((pathlib.Path(codex_dir) / "config.toml").exists())
        sign_in.assert_not_called()
        self.assertIn("requirement:curl_missing ", stderr.getvalue())

    def test_remove_works_even_when_requirements_are_missing(self):
        with tempfile.TemporaryDirectory() as codex_dir, mock.patch.dict(os.environ, CODEX_HOME=codex_dir), \
                mock.patch.object(sys, "argv", ["setup", "--remove"]), \
                mock.patch.object(MODULE, "requirement_problems", return_value=["curl_missing"]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, MODULE.main())

    def test_check_requirements_flag_reports_success_without_sign_in(self):
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", ["setup", "--check-requirements"]), \
                mock.patch.object(MODULE, "requirement_problems", return_value=[]), \
                mock.patch.object(MODULE, "browser_sign_in") as sign_in, contextlib.redirect_stdout(stdout):
            self.assertEqual(0, MODULE.main())
        sign_in.assert_not_called()
        self.assertIn("All Vantage telemetry requirements", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "plugins/vantage/hooks/vantage_codex_hook.py"
LAUNCHER = (
    [sys.executable, "-I", str(SCRIPT.with_name("run_vantage_codex_hook.py"))]
    if os.name == "nt" else ["sh", str(SCRIPT.with_name("run_vantage_codex_hook.sh"))]
)
SPEC = importlib.util.spec_from_file_location("vantage_codex_hook", SCRIPT)
assert SPEC and SPEC.loader
HOOK = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HOOK
SPEC.loader.exec_module(HOOK)


class VantageCodexHookTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="vantage-codex-plugin-hook"
        )
        self.directory = Path(self.temporary_directory.name)
        self.state_directory = self.directory / "state"
        self.addCleanup(self.temporary_directory.cleanup)

    def install_cached_classifier(self, codex_home, token=False):
        vantage = Path(codex_home) / "vantage"
        vantage.mkdir(parents=True, exist_ok=True)
        (vantage / "classifier.md").write_text(
            "# Test classifier\n", encoding="utf-8"
        )
        endpoint, ingest_token = "http://127.0.0.1:1", "fixture-token"
        (vantage / "connection.json").write_text(json.dumps({
            "token": ingest_token, "endpoint": endpoint, "since_ns": 1,
        }))
        binding = hashlib.sha256((endpoint + "\0" + ingest_token).encode()).hexdigest()
        outbox = vantage / "outbox" / binding
        outbox.mkdir(parents=True)
        lock = (outbox / "worker.lock").open("a+b")
        self.addCleanup(lock.close)
        self.assertTrue(HOOK.try_lock(lock))
        if token:
            (vantage / "classifier_read_token").write_text(
                "classifier-token", encoding="utf-8"
            )

    def test_refreshes_classifier_with_etag_and_private_permissions(self):
        codex_home = self.directory / "codex"
        self.install_cached_classifier(codex_home, token=True)
        (codex_home / "vantage" / "classifier.etag").write_text(
            '"old"', encoding="utf-8"
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {"classifier_markdown": "# Updated classifier\n"}
        ).encode("utf-8")
        response.headers = {"ETag": '"new"'}
        opener = mock.Mock()
        opener.open.return_value = response
        env = {
            "CODEX_HOME": str(codex_home),
        }

        with mock.patch.object(HOOK.urllib.request, "build_opener", return_value=opener):
            self.assertTrue(HOOK.refresh_classifier(env, stderr=io.StringIO()))

        request = opener.open.call_args.args[0]
        self.assertEqual("https://api.staging.vantage.sh/plugins/classifier", request.full_url)
        self.assertEqual(
            HOOK.CLASSIFIER_TIMEOUT,
            opener.open.call_args.kwargs["timeout"],
        )
        self.assertEqual("Bearer classifier-token", request.get_header("Authorization"))
        self.assertEqual('"old"', request.get_header("If-none-match"))
        classifier = codex_home / "vantage" / "classifier.md"
        self.assertEqual("# Updated classifier\n", classifier.read_text(encoding="utf-8"))
        if os.name != "nt":
            self.assertEqual(0o600, classifier.stat().st_mode & 0o777)
        self.assertEqual(
            '"new"',
            (codex_home / "vantage" / "classifier.etag").read_text(encoding="utf-8"),
        )

    @unittest.skipIf(os.name == "nt", "the POSIX hook launcher is a shell script")
    def test_launcher_skips_the_hook_when_python_is_unavailable(self):
        launcher = SCRIPT.parent / "run_vantage_codex_hook.sh"
        environment = os.environ.copy()
        environment.update(
            {
                "PLUGIN_ROOT": str(ROOT / "plugins/vantage"),
                "VANTAGE_CODEX_PYTHON": str(self.directory / "missing-python"),
            }
        )

        result = subprocess.run(
            ["sh", str(launcher)],
            input=json.dumps({"hook_event_name": "Stop", "session_id": "session-1"}),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual("", result.stderr)

    def test_launcher_resolves_an_explicit_plugin_root(self):
        environment = os.environ.copy()
        environment.pop("PLUGIN_ROOT", None)
        codex_home = self.directory / "codex"
        self.install_cached_classifier(codex_home)
        environment.update(
            {
                "CODEX_HOME": str(codex_home),
                "VANTAGE_CODEX_PLUGIN_ROOT": str(ROOT / "plugins/vantage"),
                "VANTAGE_CODEX_PYTHON": sys.executable,
            }
        )

        result = subprocess.run(
            LAUNCHER,
            input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "session-1", "turn_id":"turn-1"}),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode)
        self.assertIn("set_turn_attribution with session_id=session-1, turn_id=turn-1", result.stdout)
        self.assertEqual("", result.stderr)

    def test_launcher_uses_the_newest_declared_plugin_version(self):
        codex_home = self.directory / "codex"
        cache_root = codex_home / "plugins" / "cache" / "vantage-codex-plugin" / "vantage"
        old_plugin = cache_root / "999-not-a-version"
        new_plugin = cache_root / "carton-at-the-back"
        old_hooks = old_plugin / "hooks"
        new_hooks = new_plugin / "hooks"
        old_hooks.mkdir(parents=True)
        new_hooks.mkdir(parents=True)
        (old_plugin / "plugin.json").write_text(
            json.dumps({"name": "vantage", "version": "0.3.1"}), encoding="utf-8"
        )
        (new_plugin / "plugin.json").write_text(
            json.dumps({"name": "vantage", "version": "0.3.2"}), encoding="utf-8"
        )
        (old_hooks / "vantage_codex_hook.py").write_text(
            "print('old hook')\n", encoding="utf-8"
        )
        (new_hooks / "vantage_codex_hook.py").write_text(
            "print('new hook')\n", encoding="utf-8"
        )

        environment = os.environ.copy()
        self.install_cached_classifier(codex_home)
        environment.update(
            {
                "CODEX_HOME": str(codex_home),
                "VANTAGE_CODEX_PYTHON": sys.executable,
            }
        )
        result = subprocess.run(
            LAUNCHER,
            input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "session-1", "turn_id":"turn-1"}),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual("new hook\n", result.stdout)
        self.assertEqual("", result.stderr)

    def test_launcher_does_nothing_when_no_valid_hook_is_installed(self):
        codex_home = self.directory / "codex"
        invalid_plugin = (
            codex_home
            / "plugins"
            / "cache"
            / "workspace-directory"
            / "vantage"
            / "newest-looking-folder"
        )
        (invalid_plugin / "hooks").mkdir(parents=True)
        (invalid_plugin / "plugin.json").write_text(
            json.dumps({"name": "vantage", "version": "not-a-version"}),
            encoding="utf-8",
        )
        (invalid_plugin / "hooks" / "vantage_codex_hook.py").write_text(
            "print('should not run')\n", encoding="utf-8"
        )

        environment = os.environ.copy()
        environment.update(
            {
                "CODEX_HOME": str(codex_home),
                "VANTAGE_CODEX_PYTHON": sys.executable,
            }
        )
        result = subprocess.run(
            LAUNCHER,
            input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "session-1", "turn_id":"turn-1"}),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual("", result.stderr)

        command = subprocess.run(
            [sys.executable, "-I", str(SCRIPT.parent / "run_vantage_codex_hook.py"), "tag"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(0, command.returncode)

    def test_launcher_ignores_pythonpath_modules(self):
        injected_modules = self.directory / "injected-modules"
        injected_modules.mkdir()
        (injected_modules / "re.py").write_text(
            "raise RuntimeError('loaded injected re.py')\n", encoding="utf-8"
        )

        environment = os.environ.copy()
        codex_home = self.directory / "codex"
        self.install_cached_classifier(codex_home)
        environment.update(
            {
                "CODEX_HOME": str(codex_home),
                "PYTHONPATH": str(injected_modules),
                "VANTAGE_CODEX_PLUGIN_ROOT": str(ROOT / "plugins/vantage"),
                "VANTAGE_CODEX_PYTHON": sys.executable,
            }
        )
        result = subprocess.run(
            LAUNCHER,
            input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "session-1", "turn_id":"turn-1"}),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode)
        self.assertIn("set_turn_attribution with session_id=session-1, turn_id=turn-1", result.stdout)
        self.assertEqual("", result.stderr)


if __name__ == "__main__":
    unittest.main()

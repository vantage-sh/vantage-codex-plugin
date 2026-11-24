from contextlib import closing, redirect_stdout
import io
import os
import datetime
import hashlib
import json
import time
import sys
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[4] / "plugins/vantage/hooks"),
)
import vantage_codex_hook as store

launch = store.launch
handle, recover, drain = store.handle, store.recover, store.drain


class HookTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        state = patch.object(store, "STATE", self.root / "state")
        state.start()
        self.addCleanup(state.stop)
        kick = patch.object(store, "launch")
        kick.start()
        self.addCleanup(kick.stop)
        self.db = store.connect()
        self.addCleanup(self.db.close)

    def payloads(self):
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT payload FROM outbox ORDER BY rowid")
        ]

    def submit(self, turn, session="s", branch="feature", head="abc", worktree="work", cwd=None):
        payload = {"session_id": session, "turn_id": turn, "hook_event_name": "UserPromptSubmit",
                   "cwd": str(cwd or self.root), "cli_version": "test"}
        classifier = self.root / "classifier.md"
        facts = {"worktree": worktree, "branch": branch, "head": head} if worktree else {}
        with patch.dict(os.environ, VANTAGE_CLASSIFIER_FILE=str(classifier)), patch.object(
            store, "git_observation", return_value=(facts, None)
        ), redirect_stdout(io.StringIO()) as output:
            handle(self.db, "codex", payload)
        return json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]

    def tag(self, turn, *args):
        with patch.object(store, "load_connection"), patch.object(
            sys, "argv", ["hook", "tag", "codex", "s", turn, *args]
        ):
            store.main()

    def test_reuse_survives_commit_and_retry_preserves_override(self):
        self.submit("first")
        self.tag("first", "--set", "pr=10", "--source", "explicit")
        self.tag("first", "--set", "purpose=development", "--source", "inferred")
        self.assertIn("Current attribution", self.submit("next", head="new"))
        state = json.loads(self.payloads()[-1]["state"])
        self.assertEqual(state["tags"], {"pr": "10", "purpose": "development"})
        self.assertEqual(state["tag_sources"], {"pr": "explicit", "purpose": "inferred"})
        self.tag("next", "--replace", "purpose=research")
        self.assertIn('"purpose": "research"', self.submit("next"))
        self.assertEqual(json.loads(self.payloads()[-1]["state"])["tags"], {"purpose": "research"})
        self.tag("next", "--replace")
        self.assertIn("Current attribution: {}", self.submit("third"))

    def test_context_changes_and_unclassified_turns_require_classification(self):
        classifier = self.root / "classifier.md"
        classifier.write_text("v1")
        for i, (options, reuse) in enumerate([
            ({}, False), ({}, False),  # first turn wasn't classified
            ({"branch": "other"}, False), ({"worktree": "other"}, False),
            ({"branch": ""}, False), ({"branch": "", "head": "new"}, False),
            ({"worktree": ""}, False), ({"worktree": ""}, True),
            ({"worktree": "", "cwd": self.root / "other"}, False),
            ({}, False), ({}, False), ({"session": "other"}, False),
        ]):
            with self.subTest(i=i, options=options):
                if i == 10:
                    classifier.write_text("v2")
                turn = str(i)
                self.assertEqual("Current attribution" in self.submit(turn, **options), reuse)
                if i and "session" not in options:
                    self.tag(turn, "--replace", "purpose=research")

    def test_grpc_ack_requires_success_trailer_and_full_batch(self):
        headers = (
            b"HTTP/2 200 \r\ncontent-type: application/grpc\r\n\r\ngrpc-status: 0\r\n"
        )
        empty = bytes(5)
        store.require_ack(headers, empty)
        store.require_ack(headers, bytes.fromhex("00000000020a00"))
        for head, frame in [
            (headers.replace(b"200", b"500"), empty),
            (headers.replace(b"grpc-status: 0", b"grpc-status: 14"), empty),
            (b"HTTP/2 200", empty),
            (headers, bytes.fromhex("00000000040a020801")),  # one rejected log
            (headers, b""),
        ]:
            with self.subTest(head=head, frame=frame), self.assertRaises(RuntimeError):
                store.require_ack(head, frame)

    def test_first_model_request_belongs_to_child_and_tag_survives_stop(self):
        p = {
            "session_id": "parent",
            "agent_id": "child",
            "turn_id": "turn",
            "hook_event_name": "PostModelRequest",
            "token_usage": {"input_tokens": 18},
            "status": "completed",
            "prompt": "never forward this",
        }
        handle(self.db, "codex", p)
        events = self.payloads()
        self.assertEqual(
            [row["event.name"] for row in events], [store.TURN, store.REQUEST]
        )
        self.assertEqual(events[1]["session.id"], "child")
        self.assertNotIn("never forward", json.dumps(events))
        key = ("codex", "child", "turn")
        with store.write(self.db):
            store.snapshot(self.db, key, tags={"purpose": "research"})
        handle(self.db, "codex", dict(p, hook_event_name="Stop"))
        self.assertEqual(
            json.loads(self.payloads()[-1]["state"])["tags"], {"purpose": "research"}
        )
        with store.write(self.db):
            store.snapshot(self.db, key, tags={"purpose": None})
        self.assertEqual(json.loads(self.payloads()[-1]["state"])["tags"], {})

    def test_git_identity_is_frozen_at_observation_for_live_late_and_recovered_events(
        self,
    ):
        subprocess.run(["git", "init", "-q", self.root], check=True)

        def configure(email, name):
            subprocess.run(["git", "-C", self.root, "config", "user.name", name], check=True)
            subprocess.run(
                ["git", "-C", self.root, "config", "user.email", email], check=True
            )

        p = {"session_id": "a", "turn_id": "b", "cwd": str(self.root)}
        key = ("codex", "a", "b")
        configure("git-a@example.invalid", "Dev A")
        handle(self.db, "codex", dict(p, hook_event_name="UserPromptSubmit"))
        handle(self.db, "codex", dict(p, hook_event_name="PostModelRequest"))
        earlier = self.db.execute("SELECT max(timestamp) FROM outbox").fetchone()[0]
        configure("git-b@example.invalid", "Dev B")
        handle(self.db, "codex", dict(p, hook_event_name="Stop"))
        with store.write(self.db):
            store.snapshot(self.db, key, tags={"ticket": "A-1"})
            for then in (1, earlier):
                store.correlation(
                    self.db, key, {}, "rollout", status="completed", timestamp=then
                )
        self.assertEqual(
            [e.get("git.user.email") for e in self.payloads()],
            [
                "git-a@example.invalid",
                "git-a@example.invalid",
                "git-b@example.invalid",
                "git-b@example.invalid",
                None,
                "git-a@example.invalid",
            ],
        )
        self.assertEqual(self.payloads()[-2]["git.identity.source"], "unknown")
        self.assertEqual([e.get("git.user.name") for e in self.payloads()],
                         ["Dev A", "Dev A", "Dev B", "Dev B", None, "Dev A"])
        configure("invalid input", "")
        handle(self.db, "codex", dict(p, hook_event_name="PostModelRequest"))
        self.assertNotIn("git.user.email", self.payloads()[-1])
        self.assertNotIn("git.user.name", self.payloads()[-1])

    def test_post_tool_observes_git_changes_without_spawning_git_for_stable_refs(self):
        git_environment = patch.dict(
            os.environ,
            {key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
            clear=True,
        )
        git_environment.start()
        self.addCleanup(git_environment.stop)
        repo = self.root / "repo"
        tree = self.root / "worktree"

        def git(cwd, *args):
            return subprocess.check_output(["git", "-C", cwd, *args], text=True).strip()

        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.name", "Test User")
        git(repo, "config", "user.email", "git@example.invalid")
        git(repo, "remote", "add", "origin", "git@example.invalid:test/project.git")
        git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        git(repo, "commit", "-qm", "initial", "--allow-empty")
        git(repo, "worktree", "add", "-q", "-b", "emily/feature", tree)
        git(repo, "pack-refs", "--all")
        initial = git(tree, "rev-parse", "HEAD")
        p = {"session_id": "s", "turn_id": "t", "cwd": str(tree), "cli_version": "test"}
        with redirect_stdout(io.StringIO()):
            handle(self.db, "codex", dict(p, hook_event_name="UserPromptSubmit"))
        self.db.close()
        self.db = store.connect()
        self.addCleanup(self.db.close)

        tool = dict(p, hook_event_name="PostToolUse", tool_name="Bash")
        with patch.object(store, "run_git", wraps=store.run_git) as run_git:
            handle(self.db, "codex", dict(p, hook_event_name="PreToolUse", tool_name="Bash"))
            handle(self.db, "codex", dict(tool, tool_name="update_plan"))
            handle(self.db, "codex", tool)
            run_git.assert_not_called()
            git(tree, "commit", "-qm", "advance", "--allow-empty")
            advanced = git(tree, "rev-parse", "HEAD")
            handle(self.db, "codex", tool)
            self.assertEqual([call.args[1] for call in run_git.call_args_list], ["rev-parse"])
            git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/emily/feature")
            handle(self.db, "codex", tool)
            git(tree, "checkout", "--detach", "-q", initial)
            handle(self.db, "codex", tool)

        states = json.loads(self.payloads()[-1]["state"])["git"]
        facts = [state["facts"] for state in states]
        self.assertEqual([fact["head"] for fact in facts], [initial, advanced, advanced, initial])
        self.assertEqual([fact["branch"] for fact in facts], ["emily/feature"] * 3 + [""])
        self.assertEqual({fact["repository"] for fact in facts}, {"example.invalid/test/project"})
        self.assertIs(facts[1]["branch_is_default"], False)
        self.assertIs(facts[2]["branch_is_default"], True)

    def test_git_observation_resolves_relative_common_dir(self):
        repo = self.root / "repo"
        subdirectory = repo / "nested"
        subdirectory.mkdir(parents=True)

        def git(*args):
            return subprocess.check_output(["git", "-C", repo, *args], text=True).strip()

        git("init", "-q", "-b", "main")
        git("remote", "add", "origin", "git@example.invalid:test/project.git")
        git("-c", "user.name=Test User", "-c", "user.email=git@example.invalid",
            "commit", "-qm", "initial", "--allow-empty")
        head = git("rev-parse", "HEAD")
        salt = b"fixture"
        root_facts, _ = store.git_observation(self.db, str(repo), salt)
        self.db.execute("DELETE FROM git_worktrees")
        nested_facts, _ = store.git_observation(self.db, str(subdirectory), salt)
        self.assertEqual(root_facts, nested_facts)
        self.assertEqual(head, root_facts["head"])
        self.assertEqual("main", root_facts["branch"])
        self.assertEqual("example.invalid/test/project", root_facts["repository"])

    def test_request_model_switch_survives_retry(self):
        p = {"session_id": "s", "turn_id": "t", "hook_event_name": "PostModelRequest",
             "cli_version": "0.171.0", "status": "completed"}
        for model in ("first", "second"):
            handle(self.db, "codex", dict(p, model=model))
        handle(self.db, "codex", dict(p, hook_event_name="Stop", cli_version="0.172.0"))
        events = self.payloads()
        requests = [e for e in events if e["event.name"] == store.REQUEST]
        self.assertEqual([e["model"] for e in requests], ["first", "second"])
        self.assertTrue(all(e["agent.version"] == "0.171.0" for e in requests))
        self.assertEqual(events[-1]["model"], "multiple")
        with patch.object(store, "export", side_effect=RuntimeError), self.assertRaises(RuntimeError):
            drain(self.db)
        self.assertEqual(self.payloads(), events)

    def test_long_git_history_keeps_first_latest_and_tag_revision(self):
        key = ("codex", "s", "t")
        for i in range(160):
            store.snapshot(self.db, key, facts={"head": str(i), "branch": "feature-" + "a"*450})
        store.snapshot(self.db, key, tags={"ticket":"PLA-123"})
        encoded = self.payloads()[-1]["state"]
        self.assertLessEqual(len(encoded.encode()), 65536)
        state = json.loads(encoded)
        self.assertEqual(state["git"][0]["facts"]["head"], "0")
        self.assertEqual(state["git"][-1]["facts"]["head"], "159")
        self.assertGreater(state["omitted_git_observations"], 0)
        self.assertEqual(state["tags"]["ticket"], "PLA-123")

    def test_recover_unregistered_post_install_session(self):
        when = datetime.datetime.now(datetime.timezone.utc)
        ms = int(when.timestamp() * 1000)
        session = str(uuid.UUID(int=(ms << 80) | (7 << 76) | (2 << 62)))
        folder = self.root / "sessions"
        folder.mkdir()
        record = {"timestamp": when.isoformat(), "type": "token_usage_record",
                  "payload": {"thread_id": session, "turn_id": "t", "usage": {"output_tokens": 8}}}
        (folder / f"rollout-{session}.jsonl").write_text(json.dumps(record) + "\n")
        with patch.object(store, "HOME", self.root), patch.object(store, "OPT_IN", (ms-1)*1_000_000):
            recover(self.db)
            recover(self.db)
        self.assertEqual([event["event.name"] for event in self.payloads()], [store.TURN, store.REQUEST])

    def test_recovery_isolates_unreadable_file_and_keeps_cursor(self):
        folder = self.root / "sessions"
        folder.mkdir()
        sessions = [str(uuid.uuid4()) for _ in range(2)]
        paths = []
        for session in sessions:
            self.db.execute("INSERT INTO sessions VALUES ('codex',?,?,0)", (session, str(self.root)))
            path = folder / f"rollout-{session}.jsonl"
            paths.append(path)
            record = {"timestamp": "2026-01-01T00:00:00Z", "type": "token_usage_record",
                      "payload": {"thread_id": session, "turn_id": "t", "usage": {"input_tokens": 8}}}
            path.write_text(json.dumps(record) + "\n")
        original_open = Path.open
        def selective_open(path, *args, **kwargs):
            if path == paths[0]:
                raise PermissionError()
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", selective_open):
            recover(self.db)
        self.assertEqual({e["session.id"] for e in self.payloads()}, {sessions[1]})
        self.assertEqual(self.db.execute("SELECT offset FROM sessions WHERE session=?", (sessions[0],)).fetchone()[0], 0)
        recover(self.db)
        self.assertEqual(len(self.payloads()), 4)

    def test_resume_refreshes_version_without_rewriting_historical_requests(self):
        payload = {"session_id": "session", "hook_event_name": "SessionStart"}
        with patch.object(store, "installed_version", side_effect=["1.0", "2.0"]) as lookup, patch.object(
            store.time, "time_ns", return_value=10
        ) as clock:
            handle(self.db, "codex", payload)
            clock.return_value = 20
            handle(self.db, "codex", dict(payload, hook_event_name="PostModelRequest", turn_id="a"))
            clock.return_value = 30
            handle(self.db, "codex", payload)
            clock.return_value = 40
            handle(self.db, "codex", dict(payload, hook_event_name="PostModelRequest", turn_id="b"))
            store.correlation(self.db, ("codex", "session", "a"), {}, "rollout",
                              status="completed", timestamp=25)
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual([e["agent.version"] for e in self.payloads() if e["event.name"] == store.REQUEST],
                         ["1.0", "2.0", "1.0"])

    def test_busy_sender_coalesces_kicks_and_retries_same_id(self):
        key = ("codex", "s", "t")
        store.enqueue(self.db, store.REQUEST, key, {"status":"completed"})
        initial = self.payloads()
        with (store.STATE / "worker.lock").open("a+b") as lock:
            self.assertTrue(store.try_lock(lock))
            with patch.object(store.subprocess, "Popen") as spawn:
                for _ in range(20):
                    launch(self.db, True)
                spawn.assert_not_called()
            self.assertEqual(self.db.execute("SELECT count(*) FROM recovery_queue").fetchone()[0], 1)
            with patch.object(store, "recover") as scan, patch.object(store, "export", side_effect=RuntimeError):
                store.run_sender(self.db, lock)
                scan.assert_called_once()
        self.assertEqual(initial, self.payloads())
        with patch.object(store, "export") as send:
            launch(self.db, foreground=True, force=True)
        self.assertEqual(send.call_args.args[1][0][1]["event_id"], initial[0]["event_id"])
        self.assertFalse(self.payloads())

    def test_detached_sender_reacquires_lock_and_retains_failed_batch(self):
        endpoint, token = "http://127.0.0.1:1", "fixture-token"
        binding = hashlib.sha256((endpoint + "\0" + token).encode()).hexdigest()
        vantage = self.root / "vantage"
        vantage.mkdir()
        (vantage / "connection.json").write_text(json.dumps({
            "endpoint": endpoint, "token": token, "since_ns": 1,
        }))
        processes = []
        popen = subprocess.Popen

        def spawn(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process

        with patch.object(store, "STATE", vantage / "outbox" / binding), closing(store.connect()) as db:
            store.enqueue(db, store.REQUEST, ("codex", "s", "t"), {"status": "completed"})
            with patch.dict(os.environ, CODEX_HOME=str(self.root),
                            VANTAGE_CODEX_CURL=str(self.root / "missing-curl")), patch.object(
                store.subprocess, "Popen", side_effect=spawn
            ):
                launch(db)
            self.assertEqual(len(processes), 1)
            with processes[0] as worker:
                self.assertEqual(worker.wait(timeout=10), 0)
            self.assertEqual(db.execute("SELECT failures FROM sender").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)

    def test_recovery_handles_partial_lines_parent_history_and_retries(self):
        session = str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO sessions VALUES ('codex',?,?,0)", (session, str(self.root))
        )
        folder = self.root / "sessions"
        folder.mkdir()
        path = folder / f"rollout-{session}.jsonl"
        row = {
            "type": "token_usage_record",
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "payload": {
                "thread_id": session,
                "turn_id": "a",
                "usage": {"input_tokens": 4},
            },
        }
        ancestor = dict(row, payload=dict(row["payload"], thread_id="other"))
        path.write_text(json.dumps(ancestor) + "\n" + json.dumps(row))
        recover(self.db)
        self.assertEqual(self.payloads(), [])
        with path.open("a") as f:
            f.write("\n")
        recover(self.db)
        events = self.payloads()
        self.assertEqual(len(events), 2)
        recover(self.db)
        self.assertEqual(self.payloads(), events)
        with (
            patch.object(store, "export", side_effect=RuntimeError("offline")),
            self.assertRaises(RuntimeError),
        ):
            drain(self.db)
        self.assertEqual(self.payloads(), events)
        with patch.object(store, "export"):
            drain(self.db)
        self.assertEqual(self.payloads(), [])


if __name__ == "__main__":
    unittest.main()

import json
import io
import os
import subprocess
import sys
import tempfile
import runpy
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from dev_snapshot.agent_history import AgentHistory, MAX_INPUT
from dev_snapshot.common import SafetyError, encode, settings_from, sha
from dev_snapshot.engine import Engine, Session
from dev_snapshot.git import Git
from dev_snapshot.snapshots import SnapshotService
from dev_snapshot.store import Store


class AgentHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ambertrail-agent-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "repo"
        self.root.mkdir()
        self.identity = {"schema": 1, "root": str(self.root), "id": sha(str(self.root).encode()),
                         "gitDir": str(self.root / ".git"), "commonDir": str(self.root / ".git")}
        self.history = AgentHistory(self.base / "agents" / self.identity["id"], self.identity)

    def enable(self):
        self.history.consent(True)
        self.grant = self.history.policy()["grant"]

    def event(self, turn="turn-1", text="로그인과 회원가입 기능 구현\n에이전트 보고: 관련 테스트 완료"):
        return {"hook_event_name": "Stop", "session_id": "session-1", "turn_id": turn,
                "cwd": str(self.root), "last_assistant_message": text}

    def collect(self, **kwargs):
        return self.history.collect(self.event(**kwargs), self.grant)

    def test_disabled_has_no_polling_io_and_no_directory(self):
        with patch("dev_snapshot.agent_history.os.scandir", side_effect=AssertionError("unexpected scan")):
            self.assertFalse(self.history.poll())
            self.assertEqual(self.history.records(), [])
        self.assertFalse(self.history.directory.exists())

    def test_final_report_minimizes_text_and_preserves_identifiers(self):
        self.enable()
        event = self.event()
        event.update(prompt="PRIVATE USER PROMPT", reasoning="PRIVATE REASONING", transcript_path="DO NOT OPEN")
        self.assertTrue(self.history.collect(event, self.grant))
        record = self.history.records()[0]
        self.assertEqual(record["title"], "로그인과 회원가입 기능 구현")
        self.assertEqual(record["association"], {"status": "unlinked", "evidence": []})
        self.assertEqual(record["phase"], "final_answer")
        self.assertIsNone(record["occurredAt"])
        self.assertEqual((record["sessionId"], record["turnId"]), ("session-1", "turn-1"))
        stored = (self.history.directory / "messages" / (record["id"] + ".json")).read_text()
        self.assertNotIn("PRIVATE", stored)
        self.assertNotIn("DO NOT OPEN", stored)

    def test_duplicate_retry_restart_and_identical_text_in_different_turns(self):
        self.enable()
        self.assertTrue(self.collect())
        self.assertFalse(self.collect())
        self.assertFalse(self.collect(text="different retry"))
        self.assertTrue(self.collect(turn="turn-2"))
        restarted = AgentHistory(self.history.directory, self.identity)
        self.assertEqual(len(restarted.records()), 2)
        self.assertEqual({r["turnId"] for r in restarted.records()}, {"turn-1", "turn-2"})

    def test_revoke_and_reenable_invalidates_old_command(self):
        self.enable()
        old = self.grant
        self.history.consent(False)
        self.assertFalse(self.history.collect(self.event(), old))
        self.history.consent(True)
        self.assertFalse(self.history.collect(self.event(), old))
        self.assertTrue(self.history.collect(self.event(), self.history.policy()["grant"]))

    def test_wrong_worktree_nested_repo_and_missing_cwd_are_rejected(self):
        self.enable()
        other = self.base / "other"
        other.mkdir()
        nested = self.root / "nested"
        nested.mkdir()
        (nested / ".git").mkdir()
        for cwd in (str(other), str(nested), "", "relative"):
            event = self.event()
            event["cwd"] = cwd
            self.assertFalse(self.history.collect(event, self.grant))
        self.assertEqual(self.history.records(), [])

    def test_empty_secret_and_oversize_reports_are_not_saved(self):
        self.enable()
        for text in ("", " \n", "```python\nx=1\n```", "password: private-value", "x" * 1000 + "\napi_key=private-value", "x" * (MAX_INPUT + 1)):
            self.assertFalse(self.collect(text=text))
        self.assertTrue(self.collect(text="# Title\n" + "x" * 5000))
        record = self.history.records()[0]
        self.assertEqual(record["title"], "Title")
        self.assertEqual(len(record["description"]), 700)

    def test_progress_delta_and_non_assistant_events_are_not_collected(self):
        self.enable()
        for name in ("item/agentMessage/delta", "item/completed", "SubagentStop", "UserPromptSubmit"):
            event = self.event()
            event["hook_event_name"] = name
            self.assertFalse(self.history.collect(event, self.grant))

    def test_out_of_order_reports_keep_receive_time_and_not_generated_time(self):
        self.enable()
        self.collect(turn="turn-2")
        self.collect(turn="turn-1")
        records = self.history.records()
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r["occurredAt"] is None for r in records))
        self.assertEqual([r["collectedAt"] for r in records], sorted([r["collectedAt"] for r in records], reverse=True))

    def test_record_quota_and_clear_are_independent_of_snapshots(self):
        self.enable()
        with patch("dev_snapshot.agent_history.MAX_RECORDS", 3):
            for i in range(6):
                self.collect(turn=f"turn-{i}")
            self.assertEqual(len(self.history.records()), 3)
        unrelated = self.base / "snapshot-data"
        unrelated.write_bytes(b"preserve")
        self.history.clear()
        self.assertEqual(self.history.records(), [])
        self.assertFalse(self.history.policy()["enabled"])
        self.assertEqual(unrelated.read_bytes(), b"preserve")

    def test_corrupt_metadata_falls_back_and_readonly_does_not_modify(self):
        self.enable()
        self.collect()
        path = next((self.history.directory / "messages").glob("*.json"))
        path.write_bytes(b"corrupt")
        restarted = AgentHistory(self.history.directory, self.identity, readonly=True)
        self.assertEqual(restarted.records(), [])
        self.assertTrue(restarted.problem)
        with self.assertRaises(SafetyError):
            restarted.clear()
        self.assertEqual(path.read_bytes(), b"corrupt")

    def test_expired_records_are_pruned_and_idle_poll_reuses_cache(self):
        self.enable()
        self.collect()
        self.history.records()
        self.history._prune_after = time.monotonic() + 3600
        with patch("dev_snapshot.agent_history.small_json", side_effect=AssertionError("unnecessary read")):
            self.assertFalse(self.history.poll())
        path = next((self.history.directory / "messages").glob("*.json"))
        old = time.time() - 31 * 86400
        os.utime(path, (old, old))
        self.history._next_poll = self.history._prune_after = 0
        self.history.poll()
        self.assertFalse(path.exists())

    def test_collection_has_no_network_or_child_process(self):
        self.enable()
        with patch("subprocess.Popen", side_effect=AssertionError("process started")), patch("socket.socket", side_effect=AssertionError("network opened")):
            self.assertTrue(self.collect())
        self.assertEqual(len(self.history.records()), 1)

    def test_hook_protocol_always_exits_zero_with_empty_json(self):
        self.enable()
        script = Path(__file__).resolve().parents[1] / "codex_hook.py"
        command = [sys.executable, "-I", "-S", str(script), "--storage", str(self.history.directory), "--grant", self.grant]
        for data in (encode(self.event()), b"bad JSON", b"x" * (MAX_INPUT + 1), b"{}"):
            result = subprocess.run(command, input=data, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"{}\n")
            self.assertEqual(result.stderr, b"")
        self.assertEqual(len(AgentHistory(self.history.directory, self.identity).records()), 1)
        invalid = subprocess.run(command[:4], input=b"{}", capture_output=True, timeout=5)
        self.assertEqual((invalid.returncode, invalid.stdout, invalid.stderr), (0, b"{}\n", b""))

    def test_setup_is_reviewable_only_and_preserves_existing_settings(self):
        self.enable()
        script = Path(__file__).resolve().parents[1] / "codex_hook.py"
        setup = self.history.setup(sys.executable, str(script))
        handler = setup["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(handler["type"], "command")
        self.assertNotIn("codex exec", handler["command"])
        self.assertFalse((self.root / ".codex").exists())
        for field in ("decision", "continue", "additionalContext", "prompt"):
            self.assertNotIn(field, handler)

    def real_snapshot(self):
        git = Git(self.root)
        git.run("init", "-b", "main")
        identity = git.identity()
        store = Store(self.base / "snapshots", identity)
        self.addCleanup(store.close)
        (self.root / "login.py").write_bytes(b"login implementation\n")
        core = SnapshotService(git, store, settings_from(None))
        snapshot = core.capture()
        history = AgentHistory(self.base / "real-agent-history" / identity["id"], identity)
        history.consent(True)
        history.collect(self.event(), history.policy()["grant"])
        return git, store, core, snapshot, history

    def test_manual_link_uses_code_evidence_and_does_not_change_manifest_or_git(self):
        git, store, core, snapshot, history = self.real_snapshot()
        before = store.record("snapshots", snapshot["id"]).read_bytes(), git.context(), git.index_bytes()
        message = history.records()[0]
        review = history.review_link(message["id"], [snapshot["id"]], store)
        self.assertEqual(review["evidence"][0]["changedPaths"], ["login.py"])
        with self.assertRaises(SafetyError):
            history.link(message["id"], [snapshot["id"]], "stale", store)
        history.link(message["id"], [snapshot["id"]], review["reviewHash"], store)
        session = Session(git, store, core.config, agent_directory=history.directory)
        item = session.timeline()[0]
        self.assertEqual(item["agentTitle"], message["title"])
        self.assertEqual(item["summary"], snapshot["summary"])
        self.assertEqual(before, (store.record("snapshots", snapshot["id"]).read_bytes(), git.context(), git.index_bytes()))
        store.snapshot(snapshot["id"], verify=True)
        history.clear()
        self.assertNotIn("agentTitle", session.timeline()[0])

    def test_corrupt_agent_record_does_not_block_capture_diff_or_rule_summary(self):
        git, store, core, snapshot, history = self.real_snapshot()
        next((history.directory / "messages").glob("*.json")).write_bytes(b"corrupt")
        session = Session(git, store, core.config, agent_directory=history.directory)
        (self.root / "login.py").write_bytes(b"new login implementation\n")
        saved = session.capture("manual")
        self.assertNotIn("agentTitle", session.timeline()[0])
        self.assertEqual(session.diff({"snapshotId": saved["id"], "path": "login.py"})["right"], "new login implementation\n")

    def test_rpc_requires_approval_for_consent_clear_and_link(self):
        git, store, core, snapshot, history = self.real_snapshot()
        engine = Engine()
        engine.sessions[store.identity["id"]] = Session(git, store, core.config, agent_directory=history.directory)
        self.addCleanup(engine.close)
        params = {"repositoryId": store.identity["id"]}
        for method in ("agentConsent", "agentClear", "agentLink"):
            with self.subTest(method=method), self.assertRaises(SafetyError):
                engine.call(method, params)
        self.assertEqual(len(engine.call("agentHistory", params)), 1)
        engine.call("agentConsent", {**params, "enabled": False, "approved": True})
        self.assertFalse(history.policy()["enabled"])
        store.snapshot(snapshot["id"], verify=True)

    def test_multiple_reports_and_snapshots_do_not_force_one_to_one_or_reuse_stale_review(self):
        git, store, core, first, history = self.real_snapshot()
        (self.root / "signup.py").write_bytes(b"signup implementation\n")
        second = core.capture()
        first_message = history.records()[0]
        history.collect(self.event(turn="turn-2"), history.policy()["grant"])
        second_message = next(r for r in history.records() if r["turnId"] == "turn-2")
        review = history.review_link(first_message["id"], [first["id"], second["id"]], store)
        history.link(first_message["id"], [first["id"], second["id"]], review["reviewHash"], store)
        with self.assertRaises(SafetyError):
            history.link(first_message["id"], [first["id"], second["id"]], review["reviewHash"], store)
        review2 = history.review_link(second_message["id"], [first["id"]], store)
        history.link(second_message["id"], [first["id"]], review2["reviewHash"], store)
        self.assertEqual(sorted(len(r["association"]["evidence"]) for r in history.records()), [1, 2])

    def test_deeply_nested_metadata_is_isolated_from_core(self):
        self.enable()
        self.collect()
        path = next((self.history.directory / "messages").glob("*.json"))
        bad = b'{"schema":1,"x":' + b'[' * 1500 + b'0' + b']' * 1500 + b'}'
        path.write_bytes(bad)
        self.assertEqual(AgentHistory(self.history.directory, self.identity).records(), [])
        (self.history.directory / "consent.json").write_bytes(bad)
        self.assertFalse(AgentHistory(self.history.directory, self.identity).enabled)

    def test_hook_storage_failure_still_returns_no_flow_control_or_context(self):
        self.enable()
        module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "codex_hook.py"))
        output = io.BytesIO()
        args = ["codex_hook.py", "--storage", str(self.history.directory), "--grant", self.grant]
        with patch.object(sys, "argv", args), patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(encode(self.event())))), patch.object(sys, "stdout", SimpleNamespace(buffer=output)), patch.object(AgentHistory, "collect", side_effect=OSError("disk full")):
            self.assertEqual(module["main"](), 0)
        self.assertEqual(json.loads(output.getvalue()), {})
        self.assertEqual(self.history.records(), [])
        status = json.loads((self.history.directory / "hook-status.json").read_bytes())
        self.assertEqual(status["state"], "collection-error")
        self.history.poll()
        self.assertIn("수집 오류", self.history.problem)

    def test_inaccessible_optional_storage_does_not_fail_initialization(self):
        with patch.object(Path, "exists", side_effect=PermissionError("denied")):
            history = AgentHistory(self.history.directory, self.identity)
        self.assertFalse(history.enabled)
        self.assertTrue(history.problem)
        self.assertEqual(history.records(), [])

    def test_in_place_report_edit_invalidates_link_review_even_when_cached(self):
        git, store, core, snapshot, history = self.real_snapshot()
        record = history.records()[0]
        review = history.review_link(record["id"], [snapshot["id"]], store)
        path = history.directory / "messages" / (record["id"] + ".json")
        edited = json.loads(path.read_bytes())
        edited["title"] = "Changed after review"
        path.write_bytes(encode(edited))
        with self.assertRaises(SafetyError):
            history.link(record["id"], [snapshot["id"]], review["reviewHash"], store)


if __name__ == "__main__":
    unittest.main()

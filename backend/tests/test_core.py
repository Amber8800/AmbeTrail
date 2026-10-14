from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import shlex
import threading
import time
from pathlib import Path
from unittest.mock import patch

from dev_snapshot.common import (ABSENT, SafetyError, relative_path,
                                 settings_from, write_json)
from dev_snapshot.commits import CommitService
from dev_snapshot.engine import Engine, Session, display_bytes
from dev_snapshot.git import Git
from dev_snapshot.restore import RestoreService
from dev_snapshot.snapshots import SnapshotService
from dev_snapshot.store import Store


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="dev-snapshot-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "repo"
        self.root.mkdir()
        self.git = Git(self.root)
        self.git.run("init", "-b", "main")
        for key, value in (("user.name", "Snapshot Test"), ("user.email", "snapshot@example.invalid"),
                           ("commit.gpgsign", "false"), ("core.autocrlf", "false"), ("core.hooksPath", str(self.base / "hooks"))):
            self.git.run("config", key, value)
        (self.base / "hooks").mkdir()
        self.write("a.txt", b"baseline\r\n")
        self.write("b.txt", b"second\n")
        self.git.add(["a.txt", "b.txt"])
        self.git.run("commit", "-m", "initial")
        self.identity = self.git.identity()
        self.store = Store(self.base / "storage" / self.identity["id"], self.identity)
        self.addCleanup(lambda: self.store.close())
        self.config = settings_from(None)
        self.core = SnapshotService(self.git, self.store, self.config)
        self.initial = self.core.capture()
        assert self.initial

    def write(self, name: str, content: bytes):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def test_capture_does_not_change_git_or_working_files(self):
        self.write("a.txt", b"work in progress\r\n")
        self.write("new.bin", b"\x00\xff\x01")
        self.git.add(["b.txt"])
        before = self.git.context()
        index = self.git.index_bytes()
        snapshot = self.core.capture()
        self.assertEqual(self.git.context(), before)
        self.assertEqual(self.git.index_bytes(), index)
        self.assertEqual(self.store.get(snapshot["entries"]["a.txt"]["hash"]), b"work in progress\r\n")
        self.assertEqual(self.store.get(snapshot["entries"]["new.bin"]["hash"]), b"\x00\xff\x01")

    def test_unchanged_content_is_not_duplicated(self):
        self.assertIsNone(self.core.capture())
        os.utime(self.root / "a.txt", None)
        self.assertIsNone(self.core.capture())
        self.assertEqual(len(self.store.snapshots()), 1)

    def test_creation_deletion_and_rename_are_restorable(self):
        (self.root / "a.txt").rename(self.root / "renamed.txt")
        (self.root / "b.txt").unlink()
        snapshot = self.core.capture()
        self.assertEqual(snapshot["entries"]["a.txt"], ABSENT)
        self.assertEqual(snapshot["entries"]["b.txt"], ABSENT)
        restore = RestoreService(self.core)
        plan = restore.prepare(self.initial["id"])
        index = self.git.index_bytes()
        restore.apply(plan["id"])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"baseline\r\n")
        self.assertEqual((self.root / "b.txt").read_bytes(), b"second\n")
        self.assertFalse((self.root / "renamed.txt").exists())
        self.assertEqual(index, self.git.index_bytes())

    def test_unknown_new_file_survives_restore(self):
        self.write("a.txt", b"changed")
        self.write("unknown.txt", b"not yet recorded")
        restore = RestoreService(self.core)
        restore.apply(restore.prepare(self.initial["id"])["id"])
        self.assertEqual((self.root / "unknown.txt").read_bytes(), b"not yet recorded")

    def test_tracked_sensitive_files_are_excluded(self):
        self.write(".env", b"SECRET=never-store-this")
        self.write("credentials.key", b"key")
        self.git.add([".env", "credentials.key"])
        snapshot = self.core.capture()
        self.assertEqual(snapshot["entries"][".env"]["state"], "excluded")
        self.assertEqual(snapshot["entries"]["credentials.key"]["state"], "excluded")
        from dev_snapshot.common import sha
        self.assertFalse(self.store._object(sha(b"SECRET=never-store-this")).exists())

    def test_explicit_sensitive_allow_and_extra_exclusion(self):
        self.write(".env.example", b"PLACEHOLDER=x")
        self.config["allowSensitiveFiles"] = [".env.example"]
        snapshot = self.core.capture()
        self.assertEqual(snapshot["entries"][".env.example"]["state"], "present")
        self.config["exclude"] = [".env.example"]
        snapshot = self.core.capture()
        self.assertEqual(snapshot["entries"][".env.example"]["reason"], "user-exclusion")

    def test_gitignore_and_newly_ignored_files_are_protected(self):
        self.write("loose.txt", b"private")
        self.core.capture()
        self.write(".gitignore", b"loose.txt\nignored.txt\n")
        self.write("ignored.txt", b"ignored")
        snapshot = self.core.capture()
        self.assertEqual(snapshot["entries"]["loose.txt"]["state"], "excluded")
        self.assertNotIn("ignored.txt", snapshot["entries"])
        restore = RestoreService(self.core)
        restore.apply(restore.prepare(self.initial["id"])["id"])
        self.assertEqual((self.root / "loose.txt").read_bytes(), b"private")

    def test_large_files_and_venv_are_excluded(self):
        self.config["maxFileMiB"] = 1
        self.write("big.bin", b"a" * (1024 * 1024 + 1))
        self.write("custom_env/pyvenv.cfg", b"version = 3.12")
        self.write("custom_env/package.py", b"dont ingest")
        snapshot = self.core.capture()
        self.assertEqual(snapshot["entries"]["big.bin"]["reason"], "file-size-limit")
        self.assertEqual(snapshot["entries"]["custom_env/package.py"]["state"], "excluded")

    def test_corrupt_object_blocks_restore_before_changes(self):
        entry = self.initial["entries"]["a.txt"]
        self.store._object(entry["hash"]).write_bytes(b"broken")
        self.write("a.txt", b"keep this")
        with self.assertRaises(SafetyError):
            RestoreService(self.core).prepare(self.initial["id"])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"keep this")

    def test_manifest_tampering_is_rejected(self):
        snapshot = dict(self.initial)
        snapshot["entries"] = dict(snapshot["entries"])
        snapshot["entries"]["../../outside"] = dict(ABSENT)
        write_json(self.store.record("snapshots", snapshot["id"]), snapshot)
        with self.assertRaises(SafetyError):
            self.store.snapshot(snapshot["id"])

    def test_unknown_schema_preserves_objects_and_blocks_gc(self):
        before = list((self.store.directory / "objects").glob("*/*"))
        write_json(self.store.record("snapshots", self.initial["id"]), {"schema": 99})
        with self.assertRaises(SafetyError):
            self.store.gc()
        self.assertEqual(before, list((self.store.directory / "objects").glob("*/*")))

    def test_approval_review_detects_external_file_changes(self):
        self.write("a.txt", b"before review")
        restore = RestoreService(self.core)
        plan = restore.prepare(self.initial["id"])
        self.write("a.txt", b"after review")
        with self.assertRaises(SafetyError):
            restore.apply(plan["id"])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"after review")

    def test_dirty_editor_blocks_restore(self):
        self.write("a.txt", b"disk edit")
        restore = RestoreService(self.core, lambda: {"a.txt"})
        with self.assertRaises(SafetyError):
            restore.prepare(self.initial["id"])

    def test_failure_after_write_rolls_back_and_keeps_backup(self):
        self.write("a.txt", b"important work")
        def fault(phase, name):
            if phase == "afterWrite":
                raise RuntimeError("injected crash")
        restore = RestoreService(self.core, fault=fault)
        plan = restore.prepare(self.initial["id"])
        with self.assertRaises(RuntimeError):
            restore.apply(plan["id"])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"important work")
        self.assertEqual(self.store.journals()[0]["phase"], "rolledBack")
        self.store.snapshot(plan["backupId"], verify=True)

    def test_external_write_during_failure_is_never_rolled_back(self):
        self.write("a.txt", b"important work")
        def fault(phase, name):
            if phase == "afterWrite":
                self.write(name, b"external writer")
                raise RuntimeError("injected failure")
        restore = RestoreService(self.core, fault=fault)
        plan = restore.prepare(self.initial["id"])
        with self.assertRaises(RuntimeError):
            restore.apply(plan["id"])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"external writer")
        self.assertEqual(self.store.journals()[0]["phase"], "recoveryRequired")
        backup = self.store.snapshot(plan["backupId"], verify=True)
        self.assertEqual(self.store.get(backup["entries"]["a.txt"]["hash"]), b"important work")

    def test_undo_uses_same_review_and_preserves_new_unknown_files(self):
        self.write("a.txt", b"work")
        restore = RestoreService(self.core)
        restore.apply(restore.prepare(self.initial["id"])["id"])
        self.write("newer.txt", b"keep")
        plan, original = restore.prepare_undo()
        restore.apply(plan["id"], kind="undo", undoes=original)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"work")
        self.assertEqual((self.root / "newer.txt").read_bytes(), b"keep")

    def test_branch_change_blocks_reviewed_restore(self):
        self.write("a.txt", b"work")
        restore = RestoreService(self.core)
        plan = restore.prepare(self.initial["id"])
        self.git.run("switch", "-c", "other")
        with self.assertRaises(SafetyError):
            restore.apply(plan["id"])
        with self.assertRaises(SafetyError):
            restore.prepare(self.initial["id"])

    def test_staging_survives_snapshot_restore(self):
        self.write("a.txt", b"staged bytes")
        self.git.add(["a.txt"])
        self.write("a.txt", b"unstaged bytes")
        index = self.git.index_bytes()
        restore = RestoreService(self.core)
        restore.apply(restore.prepare(self.initial["id"])["id"])
        self.assertEqual(index, self.git.index_bytes())
        self.assertEqual(self.git.file_content("a.txt", staged=True), b"staged bytes")

    def test_existing_staging_and_intent_to_add_block_commits(self):
        self.write("a.txt", b"staged")
        self.git.add(["a.txt"])
        index = self.git.index_bytes()
        with self.assertRaises(SafetyError):
            CommitService(self.core).prepare(["a.txt"])
        self.assertEqual(index, self.git.index_bytes())
        self.git.run("restore", "--staged", "a.txt")
        self.write("new.txt", b"new")
        self.git.run("add", "-N", "--", "new.txt")
        with self.assertRaises(SafetyError):
            CommitService(self.core).prepare(["new.txt"])

    def test_commit_only_selected_files_and_delete(self):
        self.write("a.txt", b"selected")
        self.write("b.txt", b"unselected")
        self.write("한글 공백 [x].txt", b"new")
        service = CommitService(self.core)
        journal = service.prepare(["a.txt", "한글 공백 [x].txt"])
        receipt = service.execute(journal, "chore: selected", [self.initial["id"]])
        self.assertTrue(receipt["expected"])
        self.assertEqual(self.git.file_content("a.txt", head=receipt["oid"]), b"selected")
        self.assertEqual(self.git.file_content("b.txt", head=receipt["oid"]), b"second\n")
        self.assertEqual((self.root / "b.txt").read_bytes(), b"unselected")
        (self.root / "a.txt").unlink()
        self.assertTrue(service.execute(service.prepare(["a.txt"]), "chore: delete", [])["expected"])

    def test_cancel_restores_exact_index_bytes(self):
        self.write("a.txt", b"selected")
        before = self.git.index_bytes()
        service = CommitService(self.core)
        journal = service.prepare(["a.txt"])
        service.cancel(journal)
        self.assertEqual(before, self.git.index_bytes())
        self.assertEqual((self.root / "a.txt").read_bytes(), b"selected")

    def test_cancel_does_not_destroy_external_staging(self):
        self.write("a.txt", b"selected")
        self.write("b.txt", b"external staging")
        service = CommitService(self.core)
        journal = service.prepare(["a.txt"])
        self.git.add(["b.txt"])
        index = self.git.index_bytes()
        service.cancel(journal)
        self.assertEqual(index, self.git.index_bytes())
        self.assertEqual(journal["phase"], "recoveryRequired")

    def test_hook_failure_preserves_work_and_recovers_index(self):
        self.write("a.txt", b"selected")
        hook = self.base / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o700)
        before = self.git.index_bytes()
        service = CommitService(self.core)
        journal = service.prepare(["a.txt"])
        with self.assertRaises(SafetyError):
            service.execute(journal, "chore: denied", [])
        self.assertEqual(before, self.git.index_bytes())
        self.assertEqual((self.root / "a.txt").read_bytes(), b"selected")

    def test_content_mutating_hook_is_reported_without_reset(self):
        self.write("a.txt", b"selected")
        self.write("b.txt", b"hook-added")
        hook = self.base / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\ngit add -- b.txt\n", encoding="utf-8")
        hook.chmod(0o700)
        service = CommitService(self.core)
        receipt = service.execute(service.prepare(["a.txt"]), "chore: hook", [])
        self.assertFalse(receipt["expected"])
        self.assertEqual(self.git.context()["head"], receipt["oid"])
        self.assertEqual(self.store.journals()[0]["phase"], "recoveryRequired")

    def test_git_filter_result_is_what_is_reviewed(self):
        self.write(".gitattributes", b"*.txt text eol=lf\n")
        self.git.add([".gitattributes"])
        self.git.run("commit", "-m", "attributes")
        self.write("a.txt", b"reviewed\r\n")
        service = CommitService(self.core)
        journal = service.prepare(["a.txt"])
        self.assertEqual(self.git.file_content("a.txt", staged=True), b"reviewed\n")
        self.assertTrue(service.execute(journal, "chore: filtered", [])["expected"])

    def test_epoch_changes_when_external_commit_advances_head(self):
        self.write("a.txt", b"external")
        self.git.add(["a.txt"])
        self.git.run("commit", "-m", "external")
        new = self.core.capture()
        self.assertNotEqual(new["epochId"], self.initial["epochId"])
        restore = RestoreService(self.core)
        restore.apply(restore.prepare(self.initial["id"])["id"])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"baseline\r\n")

    def test_link_and_nested_repository_are_not_followed(self):
        outside = self.base / "outside.txt"
        outside.write_bytes(b"private outside")
        try:
            (self.root / "link.txt").symlink_to(outside)
        except OSError:
            self.skipTest("Creating symlinks requires developer mode or permission on Windows")
        self.write("nested/.git", b"gitdir: ignored")
        self.write("nested/code.py", b"nested")
        snapshot = self.core.capture()
        self.assertEqual(snapshot["entries"]["link.txt"]["state"], "excluded")
        self.assertEqual(outside.read_bytes(), b"private outside")

    def test_writer_lock_and_reopen_keep_history(self):
        second = Store(self.store.directory, self.identity)
        self.assertFalse(second.writable)
        self.assertEqual(second.snapshot(self.initial["id"])["id"], self.initial["id"])
        second.close()
        self.store.close()
        self.store = Store(self.store.directory, self.identity)
        self.assertTrue(self.store.writable)
        self.store.snapshot(self.initial["id"], verify=True)

    def test_retention_preserves_initial_latest_pin_and_recovery_backup(self):
        self.write("a.txt", b"one")
        pinned = self.core.capture()
        self.store.update_metadata(pinned["id"], pinned=True)
        self.write("a.txt", b"two")
        disposable = self.core.capture()
        self.write("a.txt", b"three")
        latest = self.core.capture()
        backup = RestoreService(self.core).prepare(self.initial["id"])["backupId"]
        self.config["maxSnapshots"] = 2
        self.store.prune(self.config)
        ids = {s["id"] for s in self.store.snapshots()}
        self.assertTrue({self.initial["id"], pinned["id"], latest["id"], backup} <= ids)
        self.assertNotIn(disposable["id"], ids)

    def test_worktree_identity_is_independent(self):
        other = self.base / "worktree"
        self.git.run("worktree", "add", "-b", "feature", str(other))
        identity = Git(other).identity()
        self.assertEqual(identity["commonDir"], self.identity["commonDir"])
        self.assertNotEqual(identity["id"], self.identity["id"])

    def test_capture_does_not_publish_continuously_changing_files(self):
        original = self.core.read_entry
        def unstable(*args, **kwargs):
            result = original(*args, **kwargs)
            self.core.changed()
            return result
        with patch.object(self.core, "read_entry", unstable):
            with self.assertRaises(SafetyError):
                self.core.capture()
        self.assertEqual(len(self.store.snapshots()), 1)

    def test_protocol_engine_requires_explicit_approval(self):
        engine = Engine()
        result = engine.call("open", {"root": str(self.root), "storageRoot": str(self.base / "second-storage")})
        self.addCleanup(engine.close)
        with self.assertRaises(SafetyError):
            engine.call("commitPrepare", {"repositoryId": result["id"], "selected": ["a.txt"]})

    def test_new_repository_without_head_and_first_commit(self):
        root = self.base / "empty"
        root.mkdir()
        git = Git(root)
        git.run("init", "-b", "main")
        git.run("config", "user.name", "Test")
        git.run("config", "user.email", "test@example.invalid")
        (root / "hello.py").write_bytes(b"print('hello')\n")
        identity = git.identity()
        store = Store(self.base / "empty-store", identity)
        self.addCleanup(store.close)
        core = SnapshotService(git, store, settings_from(None))
        self.assertIsNone(core.capture()["context"]["head"])
        service = CommitService(core)
        journal = service.prepare(["hello.py"])
        self.assertFalse(journal["indexExisted"])
        service.cancel(journal)
        self.assertIsNone(git.index_bytes())
        self.assertTrue(service.execute(service.prepare(["hello.py"]), "feat: initial", [])["expected"])

    def test_process_crash_after_write_can_be_recovered_on_restart(self):
        self.write("a.txt", b"recover me")
        storage = self.store.directory
        self.store.close()
        backend = str(Path(__file__).resolve().parents[1])
        code = """
import sys, os
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from dev_snapshot.git import Git
from dev_snapshot.store import Store
from dev_snapshot.snapshots import SnapshotService
from dev_snapshot.restore import RestoreService
from dev_snapshot.common import settings_from
git = Git(Path(sys.argv[2])); store = Store(Path(sys.argv[3]), git.identity())
core = SnapshotService(git, store, settings_from(None))
def fault(phase, name):
    if phase == 'afterWrite': os._exit(42)
service = RestoreService(core, fault=fault)
service.apply(service.prepare(sys.argv[4])['id'])
"""
        result = subprocess.run([sys.executable, "-I", "-S", "-c", code, backend, str(self.root), str(storage), self.initial["id"]], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 42, result.stderr.decode())
        self.store = Store(storage, self.identity)
        self.core = SnapshotService(self.git, self.store, self.config)
        journal = self.store.journals()[0]
        self.assertEqual(journal["phase"], "applying")
        service = RestoreService(self.core)
        service.rollback(journal)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"recover me")
        self.assertEqual(journal["phase"], "rolledBack")

    def test_path_validation_and_text_decoding(self):
        for name in ("../x", "/x", "a/../x", "a\\b", "a/.git/config", "C:/x", "a\0b"):
            with self.assertRaises(SafetyError):
                relative_path(name)
        self.assertEqual(display_bytes(b"\xef\xbb\xbfhello\r\n"), "hello\r\n")
        self.assertEqual(display_bytes("안녕".encode("utf-16")), "안녕")
        self.assertIn("unsupported", display_bytes(b"\x00\xff"))

    def test_incremental_capture_reads_only_event_paths_and_detects_new_files(self):
        self.write("a.txt", b"changed")
        self.write("new.txt", b"created without event")
        original = self.core.read_entry
        with patch.object(self.core, "read_entry", wraps=original) as read:
            snapshot = self.core.capture("automatic", full=False, changed_paths={"a.txt"})
        self.assertEqual({call.args[0] for call in read.call_args_list}, {"a.txt", "new.txt"})
        self.assertEqual(self.store.get(snapshot["entries"]["new.txt"]["hash"]), b"created without event")

    def test_directory_delete_event_includes_tracked_descendants(self):
        self.write("folder/c.py", b"recorded")
        self.git.add(["folder/c.py"])
        self.git.run("commit", "-m", "folder")
        self.core.capture()
        shutil.rmtree(self.root / "folder")
        snapshot = self.core.capture("automatic", full=False, changed_paths={"folder"})
        self.assertEqual(snapshot["entries"]["folder/c.py"], ABSENT)

    def test_cached_object_is_recreated_after_ignore_retention_and_allow(self):
        self.write("loose.txt", b"recorded")
        saved = self.core.capture()
        self.write(".gitignore", b"loose.txt\n")
        self.core.capture()
        self.store.delete_snapshot(saved["id"])
        self.store.gc()
        self.write(".gitignore", b"")
        snapshot = self.core.capture("automatic", full=False)
        self.assertEqual(self.store.get(snapshot["entries"]["loose.txt"]["hash"]), b"recorded")

    def test_background_status_never_executes_clean_filters(self):
        marker = self.base / "filter-ran"
        script = self.base / "hooks" / "clean.sh"
        script.write_text(f"#!/bin/sh\nprintf x >> {shlex.quote(marker.as_posix())}\ncat\n", encoding="utf-8")
        self.git.run("config", "filter.watch.clean", f"sh {shlex.quote(script.as_posix())}")
        self.write(".gitattributes", b"*.txt filter=watch\n")
        self.git.add([".gitattributes", "a.txt"])
        self.git.run("commit", "-m", "filter config")
        marker.unlink(missing_ok=True)
        self.write("a.txt", b"new uncommitted content")
        self.core.capture()
        self.git.statuses()
        self.assertFalse(marker.exists(), "Background status must disable custom clean/process filters")
        journal = CommitService(self.core).prepare(["a.txt"])
        self.assertTrue(marker.exists(), "Explicitly approved staging must preserve normal Git filters")
        CommitService(self.core).cancel(journal)

    def test_unpublished_store_temporary_is_cleaned_after_restart(self):
        temporary = self.store.directory / "objects" / "ab" / ".dev-snapshot-interrupted.tmp"
        temporary.parent.mkdir(exist_ok=True)
        temporary.write_bytes(b"unpublished")
        self.store.close()
        self.store = Store(self.store.directory, self.identity)
        self.assertFalse(temporary.exists())
        self.store.gc()
        self.store.snapshot(self.initial["id"], verify=True)

    def test_sparse_and_partial_clones_are_rejected(self):
        self.git.run("config", "core.sparseCheckout", "true")
        with self.assertRaises(SafetyError):
            self.core.capture()
        self.git.run("config", "core.sparseCheckout", "false")
        self.git.run("config", "remote.origin.promisor", "true")
        with self.assertRaises(SafetyError):
            self.git.check_supported()

    def test_quota_failure_does_not_publish_partial_snapshot(self):
        self.config["maxStorageMiB"] = 0.000001
        self.write("a.txt", b"cannot fit")
        with self.assertRaises(SafetyError):
            self.core.capture()
        self.assertEqual(len(self.store.snapshots()), 1)
        self.store.snapshot(self.initial["id"], verify=True)

    def test_background_capture_remains_queryable_and_can_be_cancelled(self):
        self.write("a.txt", b"pending capture")
        session = Session(self.git, self.store, self.config)
        engine = Engine()
        engine.sessions[self.identity["id"]] = session
        self.addCleanup(engine.close)
        entered, release = threading.Event(), threading.Event()
        original = session.core.read_entry
        def slow(*args, **kwargs):
            entered.set()
            release.wait(10)
            return original(*args, **kwargs)
        with patch.object(session.core, "read_entry", slow):
            job = session.start_capture("manual")
            self.assertTrue(entered.wait(5))
            start = time.monotonic()
            state = engine.call("list", {})
            self.assertLess(time.monotonic() - start, 1)
            self.assertEqual(state[0]["job"]["id"], job["id"])
            engine.call("cancelCapture", {"repositoryId": self.identity["id"]})
            release.set()
            session.worker.join(10)
            self.assertEqual(session.job["state"], "cancelled")
            self.assertEqual(len(self.store.snapshots()), 1)

    def test_pause_preference_survives_restart(self):
        engine = Engine()
        session = Session(self.git, self.store, self.config)
        engine.sessions[self.identity["id"]] = session
        engine.call("pause", {"repositoryId": self.identity["id"]})
        self.assertTrue(Session(self.git, self.store, self.config).paused)

    def test_file_directory_conflict_blocks_restore_without_deleting_children(self):
        (self.root / "a.txt").unlink()
        self.write("a.txt/child.py", b"new work")
        self.core.capture()
        with self.assertRaises(SafetyError):
            RestoreService(self.core).prepare(self.initial["id"])
        self.assertEqual((self.root / "a.txt/child.py").read_bytes(), b"new work")

    @unittest.skipUnless(os.name == "nt", "Windows case-only rename guard")
    def test_case_only_rename_restore_does_not_delete_the_aliased_file(self):
        self.git.run("mv", "a.txt", "temporary-name.txt")
        self.git.run("mv", "temporary-name.txt", "A.txt")
        self.core.capture()
        with self.assertRaises(SafetyError):
            RestoreService(self.core).prepare(self.initial["id"])
        self.assertEqual((self.root / "A.txt").read_bytes(), b"baseline\r\n")

    def test_push_sends_only_reviewed_branch_without_tags(self):
        remote = self.base / "remote.git"
        self.git.run("init", "--bare", str(remote))
        self.git.run("remote", "add", "origin", str(remote))
        self.git.run("push", "-u", "origin", "main")
        self.write("a.txt", b"push me")
        service = CommitService(self.core)
        receipt = service.execute(service.prepare(["a.txt"]), "chore: push", [])
        self.git.run("tag", "do-not-push")
        self.git.run("branch", "do-not-push")
        review = service.push_review()
        service.push(review)
        remote_git = Git(remote)
        self.assertEqual(remote_git.text("rev-parse", "refs/heads/main"), receipt["oid"])
        self.assertEqual(remote_git.text("show-ref", "--verify", "refs/tags/do-not-push", allow_failure=True), "")
        self.assertEqual(remote_git.text("show-ref", "--verify", "refs/heads/do-not-push", allow_failure=True), "")

    def test_changed_push_destination_invalidates_approval(self):
        remote = self.base / "remote.git"
        self.git.run("init", "--bare", str(remote))
        self.git.run("remote", "add", "origin", str(remote))
        self.git.run("push", "-u", "origin", "main")
        service = CommitService(self.core)
        review = service.push_review()
        self.git.run("remote", "set-url", "--push", "origin", str(self.base / "different.git"))
        with self.assertRaises(SafetyError):
            service.push(review)

    @unittest.skipIf(os.name == "nt", "POSIX executable bits")
    def test_executable_bit_is_preserved(self):
        target = self.root / "a.txt"
        target.chmod(0o700)
        snapshot = self.core.capture()
        target.chmod(0o600)
        restore = RestoreService(self.core)
        restore.apply(restore.prepare(snapshot["id"])["id"])
        self.assertTrue(target.stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()

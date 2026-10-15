from __future__ import annotations

import os
import gzip
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

from dev_snapshot.common import (ABSENT, MAX_FILE, SafetyError, relative_path,
                                 settings_from, sha, write_json)
from dev_snapshot.commits import CommitService
from dev_snapshot.engine import Engine, Session, display_bytes
from dev_snapshot.git import Git
from dev_snapshot.restore import RestoreService
from dev_snapshot.snapshots import SnapshotService
from dev_snapshot.store import Store


class StoreReadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dev-snapshot-object-test-")
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name).resolve()
        self.store = Store(base / "storage", {"schema": 1, "root": str(base / "repo"), "id": sha(b"test")})
        self.addCleanup(self.store.close)

    def test_bounded_read_checks_empty_small_and_multibuffer_objects(self):
        for data in (b"", b"small", bytes(range(256)) * 1024):
            with self.subTest(size=len(data)):
                object_hash = self.store.put(data)
                self.assertEqual(self.store.get(object_hash, len(data)), data)
                self.assertEqual(self.store.get(object_hash), data)
                self.assertEqual(self.store.put(data), object_hash)
                with self.assertRaises(SafetyError):
                    self.store.get(object_hash, len(data) + 1)
                if data:
                    with self.assertRaises(SafetyError):
                        self.store.get(object_hash, len(data) - 1)

    def test_bounded_read_rejects_crc_truncation_hash_and_extra_members(self):
        data = b"stored bytes"
        object_hash = self.store.put(data)
        compressed = gzip.compress(data, mtime=0)
        bad_crc = bytearray(compressed)
        bad_crc[-8] ^= 1
        empty_member = bytearray(gzip.compress(b"", mtime=0))
        empty_member[-8] ^= 1
        for corrupted in (bytes(bad_crc), compressed[:-1], gzip.compress(b"changed data", mtime=0),
                          compressed + gzip.compress(b"extra", mtime=0), compressed + bytes(empty_member)):
            with self.subTest(corrupted=corrupted):
                self.store._object(object_hash).write_bytes(corrupted)
                with self.assertRaises(SafetyError):
                    self.store.get(object_hash, len(data))
                # Deduplication must still verify an existing object and fail closed.
                with self.assertRaises(SafetyError):
                    self.store.put(data)

    def test_invalid_expected_sizes_cannot_request_an_unbounded_read(self):
        object_hash = self.store.put(b"small")
        for size in (-2, -1, MAX_FILE + 1, True, 2.5):
            with self.subTest(size=size), self.assertRaises(SafetyError):
                self.store.get(object_hash, size)


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

    def test_clean_start_is_internal_and_first_change_compares_with_it(self):
        session = Session(self.git, self.store, self.config)
        before = self.git.context(), self.git.index_bytes()
        self.assertEqual(self.initial["initialChanges"], [])
        self.assertEqual(session.timeline(), [])
        self.assertIsNone(session.summary()["latestAt"])
        size = self.store.usage()
        restarted = SnapshotService(self.git, self.store, self.config)
        self.assertIsNone(restarted.capture("initial"))
        self.assertEqual(self.store.usage(), size)
        self.write("a.txt", b"first work")
        first = restarted.capture()
        self.assertEqual(first["previousSnapshotId"], self.initial["id"])
        self.assertEqual([s["id"] for s in session.timeline()], [first["id"]])
        changes = session.snapshot_changes(first["id"])
        self.assertEqual([(c["path"], c["kind"]) for c in changes], [("a.txt", "modified")])
        self.assertEqual(session.diff({"snapshotId": first["id"], "path": "a.txt"})["left"], "baseline\r\n")
        self.write("a.txt", b"later work")
        self.core.capture()
        restore = RestoreService(self.core)
        restore.apply(restore.prepare(first["id"])["id"])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"first work")
        self.assertEqual((self.git.context(), self.git.index_bytes()), before)

    def test_startup_with_existing_work_records_only_managed_changed_paths(self):
        self.write("a.txt", b"existing modification")
        self.write("b.txt", b"already staged")
        self.git.add(["b.txt"])
        self.write("new.txt", b"untracked work")
        self.write(".env", b"excluded secret")
        store = Store(self.base / "dirty-start", self.identity)
        self.addCleanup(store.close)
        before = self.git.context(), self.git.index_bytes()
        core = SnapshotService(self.git, store, self.config)
        startup = core.capture("initial")
        session = Session(self.git, store, self.config)
        self.assertEqual(startup["initialChanges"], ["a.txt", "b.txt", "new.txt"])
        self.assertEqual(session.timeline()[0]["changedFiles"], 3)
        self.assertEqual({c["path"] for c in session.snapshot_changes(startup["id"])}, {"a.txt", "b.txt", "new.txt"})
        self.assertEqual(store.get(startup["entries"]["new.txt"]["hash"]), b"untracked work")
        self.assertEqual((self.git.context(), self.git.index_bytes()), before)

    def test_excluded_only_startup_changes_do_not_create_a_visible_record(self):
        self.write(".env", b"excluded secret")
        store = Store(self.base / "excluded-start", self.identity)
        self.addCleanup(store.close)
        startup = SnapshotService(self.git, store, self.config).capture("initial")
        self.assertEqual(startup["initialChanges"], [])
        self.assertEqual(Session(self.git, store, self.config).timeline(), [])
        self.assertEqual(startup["entries"][".env"]["state"], "excluded")

    def test_branch_first_snapshot_compares_carried_work_with_its_saved_head(self):
        self.write("a.txt", b"carried work")
        previous = self.core.capture()
        self.git.run("switch", "-c", "feature/carried")
        first = self.core.capture()
        session = Session(self.git, self.store, self.config)
        self.assertEqual(first["previousSnapshotId"], previous["id"])
        changes = session.snapshot_changes(first["id"])
        self.assertEqual([(c["path"], c["kind"]) for c in changes], [("a.txt", "modified")])
        self.assertEqual(session.timeline()[0]["changedFiles"], len(changes))
        diff = session.diff({"snapshotId": first["id"], "path": "a.txt"})
        self.assertEqual(diff["left"], "baseline\r\n")
        self.assertEqual(diff["right"], "carried work")
        self.write("a.txt", b"next work")
        following = self.core.capture()
        self.assertEqual(session.diff({"snapshotId": following["id"], "path": "a.txt"})["left"], "carried work")

    def test_branch_first_snapshot_uses_destination_head_and_survives_gc_offline(self):
        self.git.run("switch", "-c", "feature/different-head")
        self.write("a.txt", b"feature baseline")
        self.git.add(["a.txt"])
        self.git.run("commit", "-m", "feature baseline")
        self.git.run("switch", "main")
        self.write("b.txt", b"carried local work")
        self.core.capture()
        self.git.run("switch", "feature/different-head")
        self.write("a.txt", b"carried local work")
        first = self.core.capture()
        self.assertEqual(first["summary"], "a.txt·b.txt 수정")
        # This HEAD version has never been captured as worktree content.
        baseline_hash = first["initialBefore"]["a.txt"]["hash"]
        self.store.gc()
        self.assertEqual(self.store.get(baseline_hash), b"feature baseline")
        self.write("a.txt", b"feature baseline")
        self.git.run("switch", "main")
        reader = Store(self.store.directory, self.identity, readonly=True)
        self.addCleanup(reader.close)
        session = Session(self.git, reader, self.config, git_enabled=False)
        with patch.object(self.git, "run", side_effect=AssertionError("Stored comparisons must not run Git")):
            diff = session.diff({"snapshotId": first["id"], "path": "a.txt"})
        self.assertEqual(diff["left"], "feature baseline")
        self.assertEqual(diff["right"], "carried local work")
        self.assertFalse(next(s for s in session.timeline() if s["id"] == first["id"])["comparisonPruned"])
        self.core.capture()  # A new main epoch releases the feature epoch's protection.
        self.store.delete_snapshot(first["id"])
        self.store.gc()
        self.assertFalse(self.store._object(baseline_hash).exists(), "Deleting the owning snapshot must release its baseline")

    def test_branch_first_snapshot_shows_carried_additions_deletions_and_renames(self):
        (self.root / "a.txt").rename(self.root / "renamed.txt")
        (self.root / "b.txt").unlink()
        self.write("new.txt", b"new local file")
        self.core.capture()
        self.git.run("switch", "-c", "feature/files")
        first = self.core.capture()
        session = Session(self.git, self.store, self.config)
        changes = session.snapshot_changes(first["id"])
        self.assertEqual({c["path"]: c["kind"] for c in changes}, {
            "a.txt": "deleted", "b.txt": "deleted", "new.txt": "added", "renamed.txt": "added"})
        self.assertEqual(session.timeline()[0]["changedFiles"], len(changes))
        removed = session.diff({"snapshotId": first["id"], "path": "a.txt"})
        self.assertEqual(removed["left"], "baseline\r\n")
        self.assertTrue(removed["rightAbsent"])
        added = session.diff({"snapshotId": first["id"], "path": "new.txt"})
        self.assertTrue(added["leftAbsent"])
        self.assertEqual(added["right"], "new local file")

    def test_legacy_branch_first_snapshot_uses_recorded_head_without_rewriting_history(self):
        self.write("a.txt", b"carried work")
        self.core.capture()
        self.git.run("switch", "-c", "feature/legacy")
        first = self.core.capture()
        first.pop("initialBefore")
        first["summaryVersion"] = 2
        path = self.store.record("snapshots", first["id"])
        write_json(path, first)
        original = path.read_bytes()
        self.git.add(["a.txt"])
        self.git.run("commit", "-m", "advance after capture")
        session = Session(self.git, self.store, self.config)
        diff = session.diff({"snapshotId": first["id"], "path": "a.txt"})
        self.assertEqual(diff["left"], "baseline\r\n")
        self.assertEqual(diff["right"], "carried work")
        self.assertEqual(path.read_bytes(), original)
        session.git_enabled = False
        with patch.object(self.git, "run", side_effect=AssertionError("Git is disabled")):
            self.assertTrue(any(s["id"] == first["id"] for s in session.timeline()))
        with self.assertRaisesRegex(SafetyError, "Enable Git"):
            session.snapshot_changes(first["id"])

    def test_branch_first_snapshot_does_not_count_index_only_changes_as_disk_changes(self):
        self.write("a.txt", b"staged work")
        self.git.add(["a.txt"])
        self.write("a.txt", b"baseline\r\n")
        self.git.run("switch", "-c", "feature/index-only")
        index = self.git.index_bytes()
        first = self.core.capture()
        self.assertEqual(first["initialChanges"], [])
        self.assertEqual(first["changedFiles"], 0)
        self.assertEqual(Session(self.git, self.store, self.config).snapshot_changes(first["id"]), [])
        self.assertEqual(self.git.index_bytes(), index)

    def test_legacy_clean_baseline_is_hidden_without_rewriting_or_losing_pins(self):
        legacy = dict(self.initial)
        legacy.pop("initialChanges")
        legacy.pop("initialBefore")
        legacy["changedFiles"] = 2
        path = self.store.record("snapshots", legacy["id"])
        write_json(path, legacy)
        original = path.read_bytes()
        session = Session(self.git, self.store, self.config)
        self.assertEqual(session.timeline(), [])
        reader = Store(self.store.directory, self.identity, readonly=True)
        self.addCleanup(reader.close)
        self.assertEqual(Session(self.git, reader, self.config).timeline(), [])
        self.store.update_metadata(legacy["id"], pinned=True)
        self.assertEqual(session.timeline()[0]["id"], legacy["id"])
        self.store.update_metadata(legacy["id"], pinned=False, title="보존한 기준")
        self.assertEqual(session.timeline()[0]["metadata"]["title"], "보존한 기준")
        self.assertEqual(path.read_bytes(), original)

    def test_legacy_existing_work_remains_visible_using_recorded_startup_status(self):
        self.write("a.txt", b"existing work")
        store = Store(self.base / "legacy-dirty-start", self.identity)
        self.addCleanup(store.close)
        startup = SnapshotService(self.git, store, self.config).capture()
        startup.pop("initialChanges")
        startup.pop("initialBefore")
        startup["changedFiles"] = 2
        write_json(store.record("snapshots", startup["id"]), startup)
        self.git.run("checkout", "--", "a.txt")  # Classification uses startup evidence, not today's clean tree.
        timeline = Session(self.git, store, self.config).timeline()
        self.assertEqual(timeline[0]["id"], startup["id"])
        self.assertEqual(timeline[0]["changedFiles"], 1)

    def test_clean_epoch_baselines_do_not_consume_normal_snapshot_count(self):
        for number in range(2):
            self.git.run("commit", "--allow-empty", "-m", f"external {number}")
            baseline = self.core.capture()
            self.assertEqual(baseline["initialChanges"], [])
        self.write("a.txt", b"first work")
        first = self.core.capture()
        self.write("a.txt", b"second work")
        second = self.core.capture()
        self.store.prune({**self.config, "maxSnapshots": 2})
        self.assertEqual({s["id"] for s in Session(self.git, self.store, self.config).timeline()}, {first["id"], second["id"]})
        self.store.snapshot(first["id"], verify=True)
        self.store.snapshot(baseline["id"], verify=True)

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

    def test_restore_rechecks_after_loading_replacement_bytes(self):
        self.write("a.txt", b"reviewed work")
        restore = RestoreService(self.core)
        plan = restore.prepare(self.initial["id"])
        original_write, original_get = restore.files.write, self.store.get
        changed = False
        def write_with_race(*args):
            def read_then_change(object_hash, size=None):
                nonlocal changed
                data = original_get(object_hash, size)
                if not changed:
                    changed = True
                    self.write("a.txt", b"external during object read")
                return data
            with patch.object(self.store, "get", side_effect=read_then_change):
                return original_write(*args)
        with patch.object(restore.files, "write", side_effect=write_with_race):
            with self.assertRaises(SafetyError):
                restore.apply(plan["id"])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"external during object read")

    def test_restore_preserves_actual_file_changed_after_last_comparison(self):
        self.write("a.txt", b"reviewed work")
        def fault(phase, name):
            if phase == "beforeDisplace":
                self.write(name, b"external in final race window")
        restore = RestoreService(self.core, fault=fault)
        plan = restore.prepare(self.initial["id"])
        with self.assertRaises(SafetyError):
            restore.apply(plan["id"])
        journal = self.store.journals()[0]
        self.assertEqual(journal["phase"], "recoveryRequired")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"external in final race window")
        preserved = restore.files.directory(journal["fileMoves"][0])
        self.assertEqual((preserved / "original").read_bytes(), b"external in final race window")
        self.assertFalse(any(".dev-snapshot-" in name for name in self.git.files()), "Recovery contents must remain Git-ignored")
        engine, session = Engine(), Session(self.git, self.store, self.config)
        engine.sessions[self.identity["id"]] = session
        self.addCleanup(engine.close)
        params = {"repositoryId": self.identity["id"], "operationId": journal["id"]}
        report = engine.call("recoveryReport", params)
        self.assertEqual(report["preservedFiles"], [{"path": "a.txt", "directory": restore.files.relative_directory(journal["fileMoves"][0])}])
        destination = self.base / "export"
        destination.mkdir()
        with self.assertRaisesRegex(SafetyError, "selected recovery backup"):
            engine.call("exportBackup", {**params, "snapshotId": self.initial["id"], "destination": str(destination), "approved": True})
        self.assertEqual(list(destination.iterdir()), [], "Invalid backup selection must not write an export")
        export = Path(engine.call("exportBackup", {**params, "snapshotId": plan["backupId"], "destination": str(destination), "approved": True}))
        self.assertIn(b"external in final race window", [p.read_bytes() for p in export.rglob("original")])
        self.assertIn(b"external in final race window", [p.read_bytes() for p in export.rglob("current")])
        self.assertTrue(list(export.rglob("manifest.json")))
        self.assertEqual((export / "a.txt").read_bytes(), b"reviewed work")
        self.store.snapshot(plan["backupId"], verify=True)

    def test_restore_never_overwrites_a_file_created_in_the_publication_gap(self):
        self.write("a.txt", b"reviewed work")
        def fault(phase, name):
            if phase == "afterDisplace":
                self.write(name, b"new external file")
        restore = RestoreService(self.core, fault=fault)
        plan = restore.prepare(self.initial["id"])
        with self.assertRaises(SafetyError):
            restore.apply(plan["id"])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"new external file")
        journal = self.store.journals()[0]
        self.assertEqual(journal["phase"], "recoveryRequired")
        self.assertEqual((restore.files.directory(journal["fileMoves"][0]) / "original").read_bytes(), b"reviewed work")

    def test_restore_creation_never_overwrites_a_racing_creator(self):
        (self.root / "a.txt").unlink()
        self.core.capture()
        def fault(phase, name):
            if phase == "afterDisplace":
                self.write(name, b"external creation")
        restore = RestoreService(self.core, fault=fault)
        with self.assertRaises(SafetyError):
            restore.apply(restore.prepare(self.initial["id"])["id"])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"external creation")

    def test_restore_deletion_preserves_a_racing_modification(self):
        self.write("remove.txt", b"reviewed deletion")
        self.core.capture()
        def fault(phase, name):
            if phase == "beforeDisplace" and name == "remove.txt":
                self.write(name, b"external before deletion")
        restore = RestoreService(self.core, fault=fault)
        with self.assertRaises(SafetyError):
            restore.apply(restore.prepare(self.initial["id"])["id"])
        self.assertEqual((self.root / "remove.txt").read_bytes(), b"external before deletion")

    def test_restore_preserves_writes_to_a_displaced_inode(self):
        self.write("a.txt", b"reviewed work")
        def fault(phase, name):
            if phase == "afterPublish":
                journal = self.store.journals()[0]
                directory = restore.files.directory(journal["fileMoves"][-1])
                (directory / "original").write_bytes(b"late write through an old handle")
        restore = RestoreService(self.core, fault=fault)
        with self.assertRaises(SafetyError):
            restore.apply(restore.prepare(self.initial["id"])["id"])
        journal = self.store.journals()[0]
        self.assertEqual(journal["phase"], "recoveryRequired")
        directory = restore.files.directory(journal["fileMoves"][0])
        self.assertEqual((directory / "original").read_bytes(), b"late write through an old handle")

    def test_restore_without_safe_publication_support_leaves_original_untouched(self):
        self.write("a.txt", b"keep this work")
        restore = RestoreService(self.core)
        plan = restore.prepare(self.initial["id"])
        with patch("dev_snapshot.restore_io.os.link", side_effect=OSError("unsupported")):
            with self.assertRaisesRegex(SafetyError, "hard-link support"):
                restore.apply(plan["id"])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"keep this work")
        self.assertEqual(self.store.journals()[0]["phase"], "rolledBack")
        self.assertFalse(list(self.root.glob(".dev-snapshot-*.restore")))

    def test_restore_preserves_a_concurrent_edit_during_rollback(self):
        self.write("a.txt", b"reviewed work")
        displacements = 0
        def fault(phase, name):
            nonlocal displacements
            if phase == "beforeDisplace":
                displacements += 1
                if displacements == 2:
                    self.write(name, b"external during rollback")
            if phase == "afterWrite":
                raise RuntimeError("start rollback")
        restore = RestoreService(self.core, fault=fault)
        with self.assertRaises(RuntimeError):
            restore.apply(restore.prepare(self.initial["id"])["id"])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"external during rollback")
        journal = self.store.journals()[0]
        self.assertEqual(journal["phase"], "recoveryRequired")
        originals = [(restore.files.directory(move) / "original").read_bytes() for move in journal["fileMoves"]]
        self.assertIn(b"reviewed work", originals)
        self.assertIn(b"external during rollback", originals)

    def test_restore_missing_path_recovery_respects_new_unsaved_edits(self):
        self.write("a.txt", b"reviewed work")
        dirty = set()
        def fault(phase, name):
            if phase == "afterDisplace":
                dirty.add(name)
        restore = RestoreService(self.core, dirty=lambda: dirty, fault=fault)
        with self.assertRaisesRegex(SafetyError, "unsaved edits"):
            restore.apply(restore.prepare(self.initial["id"])["id"])
        journal = self.store.journals()[0]
        self.assertEqual(journal["phase"], "recoveryRequired")
        self.assertFalse((self.root / "a.txt").exists(), "Recovery must respect unsaved-document guards")
        self.assertEqual((restore.files.directory(journal["fileMoves"][0]) / "original").read_bytes(), b"reviewed work")
        dirty.clear()
        restore.rollback(journal)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"reviewed work")
        self.assertEqual(journal["phase"], "rolledBack")

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
        initial = core.capture()
        self.assertIsNone(initial["context"]["head"])
        initial.pop("initialBefore")  # Older unborn-HEAD records need no Git baseline lookup.
        write_json(store.record("snapshots", initial["id"]), initial)
        offline = Session(git, store, settings_from(None), git_enabled=False)
        with patch.object(git, "run", side_effect=AssertionError("Git is disabled")):
            diff = offline.diff({"snapshotId": initial["id"], "path": "hello.py"})
        self.assertTrue(diff["leftAbsent"])
        self.assertEqual(diff["right"], "print('hello')\n")
        service = CommitService(core)
        journal = service.prepare(["hello.py"])
        self.assertFalse(journal["indexExisted"])
        service.cancel(journal)
        self.assertIsNone(git.index_bytes())
        self.assertTrue(service.execute(service.prepare(["hello.py"]), "feat: initial", [])["expected"])

    def test_process_crash_after_write_can_be_recovered_on_restart(self):
        self.crash_restore_at("afterWrite")

    def test_process_crash_after_displacement_recovers_the_missing_path(self):
        self.crash_restore_at("afterDisplace")

    def test_process_crash_after_publication_recovers_the_original_file(self):
        self.crash_restore_at("afterPublish")

    def crash_restore_at(self, phase):
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
    if phase == sys.argv[5]: os._exit(42)
service = RestoreService(core, fault=fault)
service.apply(service.prepare(sys.argv[4])['id'])
"""
        result = subprocess.run([sys.executable, "-I", "-S", "-c", code, backend, str(self.root), str(storage), self.initial["id"], phase], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 42, result.stderr.decode(errors="replace"))
        self.store = Store(storage, self.identity)
        self.core = SnapshotService(self.git, self.store, self.config)
        journal = self.store.journals()[0]
        self.assertEqual(journal["phase"], "applying")
        service = RestoreService(self.core)
        service.rollback(journal)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"recover me")
        self.assertEqual(journal["phase"], "rolledBack")
        self.assertFalse(list(self.root.glob(".dev-snapshot-*.restore")))

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

    def test_directory_events_match_ancestors_without_matching_sibling_prefixes(self):
        self.write("folder/nested/한글.txt", b"before")
        self.write("folder2/untouched.txt", b"sibling")
        self.core.capture()
        self.write("folder/nested/한글.txt", b"after")
        self.write("a.txt", b"also changed")
        with patch.object(self.core, "read_entry", wraps=self.core.read_entry) as read:
            snapshot = self.core.capture("automatic", full=False, changed_paths={"folder", "folder/nested", "a.txt"})
        self.assertEqual({call.args[0] for call in read.call_args_list}, {"folder/nested/한글.txt", "a.txt"})
        self.assertEqual(self.store.get(snapshot["entries"]["folder/nested/한글.txt"]["hash"]), b"after")
        self.assertEqual(snapshot["entries"]["folder2/untouched.txt"], self.core.cache["folder2/untouched.txt"][1])

    def test_bounded_worktree_read_rejects_growth_and_shrink_during_read(self):
        original = os.fstat
        for content in (b"longer than before", b"x"):
            self.write("a.txt", b"before")
            calls = 0
            def changing(fd):
                nonlocal calls
                metadata = original(fd)
                calls += 1
                if calls == 1:
                    self.write("a.txt", content)
                return metadata
            with self.subTest(content=content), patch("dev_snapshot.snapshots.os.fstat", side_effect=changing):
                with self.assertRaises(SafetyError):
                    self.core.read_entry("a.txt")
        self.assertEqual(len(self.store.snapshots()), 1)

    def test_periodic_full_hash_detects_missed_same_size_and_mtime_edit(self):
        target = self.root / "a.txt"
        before = target.stat()
        content = b"x" * before.st_size
        target.write_bytes(content)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        session = Session(self.git, self.store, self.config)
        session.core.cache = dict(self.core.cache)
        session.full_hashed -= self.config["fullHashSeconds"] + 1
        self.assertTrue(session.tick())
        session.worker.join(10)
        self.assertFalse(session.worker.is_alive())
        self.assertEqual(session.job["state"], "complete")
        self.assertEqual(self.store.get(session.core.latest()["entries"]["a.txt"]["hash"]), content)

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

    def boundary_fixture(self):
        from dev_snapshot.boundary import BoundaryCommitService
        snapshots = []
        for number in range(1, 6):
            self.write("a.txt", f"work {number}".encode())
            if number == 3:
                self.write("later.txt", b"later file")
            snapshots.append(self.core.capture())
        return BoundaryCommitService(self.core), snapshots

    def test_boundary_commits_2_then_5_preserving_latest_files(self):
        service, snapshots = self.boundary_fixture()
        original_head, index = self.git.context()["head"], self.git.index_bytes()
        before_disk = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        second = service.prepare_boundary(snapshots[1]["id"])
        self.assertEqual(self.git.index_bytes(), index)
        r2 = service.execute(second, "chore: first interval", [])
        self.assertTrue(r2["expected"])
        self.assertEqual(r2["parentOid"], original_head)
        self.assertEqual(self.git.file_content("a.txt", head=r2["oid"]), b"work 2")
        self.assertIsNone(self.git.file_content("later.txt", head=r2["oid"]))
        self.assertEqual(before_disk, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        # Recreate the service to prove that the boundary is persisted rather than held in memory.
        from dev_snapshot.boundary import BoundaryCommitService
        service = BoundaryCommitService(self.core)
        fifth = service.prepare_boundary(snapshots[4]["id"])
        self.assertEqual(fifth["boundaryId"], snapshots[1]["id"])
        self.assertEqual(set(fifth["selected"]), {"a.txt", "later.txt"})
        r5 = service.execute(fifth, "chore: second interval", [])
        self.assertTrue(r5["expected"])
        self.assertEqual(r5["parentOid"], r2["oid"])
        self.assertEqual(self.git.file_content("a.txt", head=r5["oid"]), b"work 5")
        self.assertEqual(self.git.file_content("later.txt", head=r5["oid"]), b"later file")
        self.assertTrue(self.git.clean_index())
        self.assertEqual(before_disk, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        with self.assertRaises(SafetyError):
            service.prepare_boundary(snapshots[1]["id"])

    def test_boundary_selected_code_then_docs_from_same_snapshot_survive_restart(self):
        self.write("a.txt", b"selected code")
        self.write("guide.md", b"selected docs")
        selected = self.core.capture()
        self.write("a.txt", b"newer local code")
        self.write("guide.md", b"newer local docs")
        self.core.capture()
        engine, session = Engine(), Session(self.git, self.store, self.config)
        engine.sessions[self.identity["id"]] = session
        self.addCleanup(engine.close)
        params = {"repositoryId": self.identity["id"], "snapshotId": selected["id"]}
        review = engine.call("commitBoundaryReview", {**params, "selected": ["a.txt"]})
        self.assertEqual([c["path"] for c in review["omitted"]], ["guide.md"])
        prepared = engine.call("commitPrepare", {**params, "selected": ["a.txt"], "reviewHash": review["reviewHash"], "approved": True})
        self.assertEqual(prepared["selected"], ["a.txt"])
        first = engine.call("commitExecute", {"repositoryId": self.identity["id"], "operationId": prepared["id"], "message": "feat: code only", "approved": True})
        self.assertTrue(first["expected"])
        self.assertTrue(first["partial"])
        self.assertNotIn("cleanup", first, "Incomplete snapshots must not offer history cleanup")
        self.assertEqual(self.git.file_content("a.txt", head=first["oid"]), b"selected code")
        self.assertIsNone(self.git.file_content("guide.md", head=first["oid"]))
        timeline = {s["id"]: s for s in session.timeline()}
        self.assertEqual(timeline[selected["id"]]["boundaryState"], "partial")
        self.assertEqual(timeline[selected["id"]]["commitChangedFiles"], 1)
        # Reopen application state, then commit the remaining documentation at the same boundary.
        session = Session(self.git, self.store, self.config)
        engine.sessions[self.identity["id"]] = session
        self.store.gc()
        remaining = engine.call("commitBoundaryReview", params)
        self.assertEqual([c["path"] for c in remaining["changes"]], ["guide.md"])
        prepared = engine.call("commitPrepare", {**params, "selected": ["guide.md"], "reviewHash": remaining["reviewHash"], "approved": True})
        second = engine.call("commitExecute", {"repositoryId": self.identity["id"], "operationId": prepared["id"], "message": "docs: guide", "approved": True})
        self.assertTrue(second["expected"])
        self.assertFalse(second.get("partial"))
        self.assertIn("cleanup", second)
        self.assertEqual(self.git.file_content("a.txt", head=second["oid"]), b"selected code")
        self.assertEqual(self.git.file_content("guide.md", head=second["oid"]), b"selected docs")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"newer local code")
        self.assertEqual((self.root / "guide.md").read_bytes(), b"newer local docs")
        self.assertTrue(self.git.clean_index())
        self.assertEqual(next(s for s in session.timeline() if s["id"] == selected["id"])["boundaryState"], "committed")

    def test_boundary_selected_deletion_leaves_other_files_for_later_commit(self):
        from dev_snapshot.boundary import BoundaryCommitService
        (self.root / "a.txt").rename(self.root / "renamed.txt")
        self.write("guide.md", b"docs later")
        snapshot = self.core.capture()
        service = BoundaryCommitService(self.core)
        first = service.execute(service.prepare_boundary(snapshot["id"], selected=["a.txt"]), "chore: remove old path", [])
        self.assertTrue(first["partial"])
        self.assertIsNone(self.git.file_content("a.txt", head=first["oid"]))
        self.assertIsNone(self.git.file_content("renamed.txt", head=first["oid"]))
        self.assertEqual({c["path"] for c in service.review(snapshot["id"])["changes"]}, {"guide.md", "renamed.txt"})
        final = service.execute(service.prepare_boundary(snapshot["id"]), "chore: remaining files", [])
        self.assertTrue(final["expected"])
        self.assertEqual(self.git.file_content("renamed.txt", head=final["oid"]), b"baseline\r\n")
        self.assertEqual(self.git.file_content("guide.md", head=final["oid"]), b"docs later")

    def test_boundary_selection_rejects_unavailable_empty_and_changed_reviews(self):
        service, snapshots = self.boundary_fixture()
        target = snapshots[1]["id"]
        before = self.git.context(), self.git.index_bytes()
        for selected in (["b.txt"], ["a.txt", "a.txt"], ["../outside"], "a.txt", []):
            with self.assertRaises(SafetyError):
                service.prepare_boundary(target, selected=selected)
        review = service.review(target, selected=["a.txt"])
        with self.assertRaisesRegex(SafetyError, "changed after review"):
            service.prepare_boundary(target, selected=["a.txt"], review_hash="stale")
        prepared = service.prepare_boundary(target, selected=["a.txt"], review_hash=review["reviewHash"])
        service.cancel(prepared)
        self.assertEqual((self.git.context(), self.git.index_bytes()), before)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"work 5")

    def test_boundary_partial_filter_baselines_survive_gc_and_later_snapshots(self):
        from dev_snapshot.boundary import BoundaryCommitService
        self.write(".gitattributes", b"*.txt text eol=lf\n")
        self.git.add([".gitattributes"])
        self.git.run("commit", "-m", "configure line endings")
        self.core.capture()
        self.write("a.txt", b"first\r\ncode\r\n")
        self.write("b.txt", b"second\r\n")  # A selected file with no change after Git normalization.
        self.write("guide.md", b"docs pending")
        first = self.core.capture()
        service = BoundaryCommitService(self.core)
        code = service.execute(service.prepare_boundary(first["id"], selected=["a.txt", "b.txt"]), "feat: first code", [])
        self.assertEqual(self.git.file_content("a.txt", head=code["oid"]), b"first\ncode\n")
        self.assertEqual([c["path"] for c in service.review(first["id"])["changes"]], ["guide.md"])
        first_hash = first["entries"]["a.txt"]["hash"]
        self.write("a.txt", b"second\r\ncode\r\n")
        self.write("b.txt", b"another feature\r\n")
        later = self.core.capture()
        service.execute(service.prepare_boundary(later["id"], selected=["b.txt"]), "feat: b only", [])
        self.store.delete_snapshot(first["id"])
        # Remove completed-operation backups to leave only the partial receipt owning the old raw bytes.
        for snapshot in self.store.snapshots():
            if snapshot["reason"] == "backup":
                self.store.update_metadata(snapshot["id"], protectedUntil="")
                self.store.delete_snapshot(snapshot["id"])
        self.store.gc()
        self.assertEqual(self.store.get(first_hash), b"first\r\ncode\r\n")
        service = BoundaryCommitService(self.core)
        self.assertEqual({c["path"] for c in service.review(later["id"])["changes"]}, {"a.txt", "guide.md"})
        service.execute(service.prepare_boundary(later["id"], selected=["a.txt"]), "feat: second code", [])
        self.store.gc()
        self.assertFalse(self.store._object(first_hash).exists(), "Superseded partial receipts must not leak old objects")
        self.assertEqual([c["path"] for c in service.review(later["id"])["changes"]], ["guide.md"])
        docs = service.execute(service.prepare_boundary(later["id"]), "docs: separate guide", [])
        self.assertTrue(docs["expected"])
        self.assertEqual(self.git.file_content("guide.md", head=docs["oid"]), b"docs pending")

    def test_boundary_cancel_leaves_real_git_state_untouched(self):
        service, snapshots = self.boundary_fixture()
        before = self.git.context(), self.git.index_bytes()
        journal = service.prepare_boundary(snapshots[1]["id"])
        service.cancel(journal)
        self.assertEqual(journal["phase"], "rolledBack")
        self.assertEqual((self.git.context(), self.git.index_bytes()), before)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"work 5")
        self.assertFalse((self.store.directory / "tmp" / journal["id"]).exists())

    def test_boundary_external_staging_or_head_changes_are_preserved(self):
        service, snapshots = self.boundary_fixture()
        journal = service.prepare_boundary(snapshots[1]["id"])
        self.git.add(["later.txt"])
        index = self.git.index_bytes()
        with self.assertRaises(SafetyError):
            service.execute(journal, "chore: blocked", [])
        service.cancel(journal)
        self.assertEqual(self.git.index_bytes(), index)
        self.assertEqual(journal["phase"], "recoveryRequired")

    def test_boundary_hook_failure_preserves_latest_work_and_index(self):
        service, snapshots = self.boundary_fixture()
        hook = self.base / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o700)
        before = self.git.context(), self.git.index_bytes()
        journal = service.prepare_boundary(snapshots[1]["id"])
        with self.assertRaises(SafetyError):
            service.execute(journal, "chore: hook failure", [])
        self.assertEqual((self.git.context(), self.git.index_bytes()), before)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"work 5")
        self.assertEqual(journal["phase"], "rolledBack")

    def test_boundary_hook_mutation_is_reported_without_reset(self):
        service, snapshots = self.boundary_fixture()
        hook = self.base / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nprintf 'hook change' > a.txt\ngit add -- a.txt\n", encoding="utf-8")
        hook.chmod(0o700)
        index = self.git.index_bytes()
        journal = service.prepare_boundary(snapshots[1]["id"])
        receipt = service.execute(journal, "chore: altered", [])
        self.assertFalse(receipt["expected"])
        self.assertEqual(self.git.context()["head"], receipt["oid"])
        self.assertEqual(self.git.file_content("a.txt", head=receipt["oid"]), b"hook change")
        self.assertEqual(self.git.index_bytes(), index)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"work 5")
        self.assertEqual(journal["phase"], "recoveryRequired")

    def test_boundary_external_commit_requires_fresh_snapshot(self):
        service, snapshots = self.boundary_fixture()
        self.git.add(["b.txt"])
        self.git.run("commit", "--allow-empty", "-m", "external")
        with self.assertRaises(SafetyError):
            service.prepare_boundary(snapshots[1]["id"])

    def test_boundary_deletion_filters_and_sensitive_exclusion(self):
        from dev_snapshot.boundary import BoundaryCommitService
        self.write(".gitattributes", b"*.txt text eol=lf\n")
        self.write("a.txt", b"first\r\nsecond\r\n")
        self.write(".env", b"SECRET=excluded")
        (self.root / "b.txt").unlink()
        selected = self.core.capture()
        self.write("a.txt", b"later version")
        service = BoundaryCommitService(self.core)
        journal = service.prepare_boundary(selected["id"])
        self.assertEqual(service.diff(journal, "a.txt")[1], b"first\nsecond\n")
        receipt = service.execute(journal, "chore: filtered interval", [])
        self.assertTrue(receipt["expected"])
        self.assertIsNone(self.git.file_content("b.txt", head=receipt["oid"]))
        self.assertIsNone(self.git.file_content(".env", head=receipt["oid"]))
        self.assertEqual((self.root / "a.txt").read_bytes(), b"later version")

    def test_boundary_engine_api_and_checkpoint_survive_history_cleanup(self):
        service, snapshots = self.boundary_fixture()
        session, engine = Session(self.git, self.store, self.config), Engine()
        engine.sessions[self.identity["id"]] = session
        self.addCleanup(engine.close)
        params = {"repositoryId": self.identity["id"]}
        journal = engine.call("commitPrepare", {**params, "snapshotId": snapshots[1]["id"], "approved": True})
        preview = engine.call("diff", {**params, "kind": "boundaryStaged", "operationId": journal["id"], "path": "a.txt"})
        self.assertEqual(preview["right"], "work 2")
        receipt = engine.call("commitExecute", {**params, "operationId": journal["id"], "message": "chore: interval", "approved": True})
        self.assertIn(snapshots[1]["id"], self.store.protected_ids())
        engine.call("historyDelete", {**params, "token": receipt["cleanup"]["token"], "approved": True})
        self.store.snapshot(snapshots[1]["id"], verify=True)
        review = engine.call("commitBoundaryReview", {**params, "snapshotId": snapshots[4]["id"]})
        self.assertEqual({c["path"] for c in review["changes"]}, {"a.txt", "later.txt"})
        timeline = session.timeline()
        self.assertEqual(next(s for s in timeline if s["id"] == snapshots[1]["id"])["boundaryState"], "committed")

    def test_boundary_new_repository_first_commit_keeps_later_disk_content(self):
        from dev_snapshot.boundary import BoundaryCommitService
        root = self.base / "new-repo"
        root.mkdir()
        git = Git(root)
        git.run("init", "-b", "main")
        git.run("config", "user.name", "Test")
        git.run("config", "user.email", "test@example.invalid")
        git.run("config", "commit.gpgsign", "false")
        store = Store(self.base / "new-storage", git.identity())
        self.addCleanup(store.close)
        core = SnapshotService(git, store, self.config)
        (root / "한글 [file].txt").write_bytes(b"snapshot")
        target = core.capture()
        (root / "한글 [file].txt").write_bytes(b"latest")
        service = BoundaryCommitService(core)
        receipt = service.execute(service.prepare_boundary(target["id"]), "chore: root", [])
        self.assertTrue(receipt["expected"])
        self.assertIsNone(receipt["parentOid"])
        self.assertEqual(git.file_content("한글 [file].txt", head=receipt["oid"]), b"snapshot")
        self.assertEqual((root / "한글 [file].txt").read_bytes(), b"latest")
        self.assertTrue(git.clean_index())

    def test_boundary_linked_worktree_does_not_change_other_branch(self):
        from dev_snapshot.boundary import BoundaryCommitService
        linked = self.base / "linked"
        original = self.git.context(), self.git.index_bytes()
        self.git.run("worktree", "add", "-b", "linked-work", str(linked))
        git = Git(linked)
        store = Store(self.base / "linked-storage", git.identity())
        self.addCleanup(store.close)
        core = SnapshotService(git, store, self.config)
        (linked / "a.txt").write_bytes(b"linked snapshot")
        target = core.capture()
        (linked / "a.txt").write_bytes(b"linked latest")
        service = BoundaryCommitService(core)
        receipt = service.execute(service.prepare_boundary(target["id"]), "chore: linked", [])
        self.assertTrue(receipt["expected"])
        self.assertEqual((self.git.context(), self.git.index_bytes()), original)
        self.assertEqual((linked / "a.txt").read_bytes(), b"linked latest")

    def test_boundary_exclusion_change_invalidates_prepared_commit(self):
        service, snapshots = self.boundary_fixture()
        journal = service.prepare_boundary(snapshots[1]["id"])
        head, index = self.git.context()["head"], self.git.index_bytes()
        self.config["exclude"] = ["a.txt"]
        with self.assertRaises(SafetyError):
            service.execute(journal, "chore: excluded", [])
        service.cancel(journal)
        self.assertEqual(self.git.context()["head"], head)
        self.assertEqual(self.git.index_bytes(), index)

    def test_boundary_crash_after_commit_preserves_work_backup_and_recovers_index(self):
        _, snapshots = self.boundary_fixture()
        before = self.git.index_bytes()
        self.store.close()
        script = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from dev_snapshot.git import Git
from dev_snapshot.store import Store
from dev_snapshot.snapshots import SnapshotService
from dev_snapshot.boundary import BoundaryCommitService
from dev_snapshot.common import settings_from
git = Git(Path(sys.argv[2]))
store = Store(Path(sys.argv[3]), git.identity())
service = BoundaryCommitService(SnapshotService(git, store, settings_from(None)))
journal = service.prepare_boundary(sys.argv[4])
index = git.index_path()
replace = os.replace
def crash(source, destination):
    if Path(destination) == index:
        os._exit(43)
    return replace(source, destination)
os.replace = crash
service.execute(journal, 'chore: crash boundary', [])
"""
        result = subprocess.run([sys.executable, "-I", "-S", "-c", script, str(Path(__file__).resolve().parents[1]),
                                 str(self.root), str(self.store.directory), snapshots[1]["id"]], timeout=60, capture_output=True)
        self.assertEqual(result.returncode, 43, result.stderr.decode(errors="replace"))
        reopened = Store(self.store.directory, self.identity)
        self.addCleanup(reopened.close)
        journal = next(j for j in reopened.journals() if j.get("mode") == "boundary")
        self.assertEqual(journal["phase"], "recoveryRequired")
        self.assertEqual(self.git.index_bytes(), before)
        self.assertEqual(reopened.get(journal["indexBackupHash"]), before)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"work 5")
        reopened.snapshot(journal["backupId"], verify=True)
        reopened.snapshot(journal["targetId"], verify=True)
        reopened.gc()
        reopened.get(journal["preparedIndexHash"])
        from dev_snapshot.boundary import BoundaryCommitService
        service = BoundaryCommitService(SnapshotService(self.git, reopened, self.config))
        service.cancel(journal)
        self.assertEqual(journal["phase"], "recoveryRequired", "Unknown/stale Git locks are never automatically removed")
        # This test owns the exited child and its temporary repository, so removal is safe here.
        Path(str(self.git.index_path()) + ".lock").unlink()
        service.cancel(journal)
        self.assertEqual(journal["phase"], "complete")
        self.assertTrue(self.git.clean_index())
        self.assertEqual(self.git.file_content("a.txt", head=self.git.context()["head"]), b"work 2")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"work 5")


    @unittest.skipUnless(os.name == "nt", "Windows extended path regression")
    def test_boundary_windows_long_global_storage_paths(self):
        from dev_snapshot.boundary import BoundaryCommitService
        self.git.run("config", "core.autocrlf", "true")
        self.git.run("config", "core.longpaths", "false")
        # Match the real default VS Code storage prefix instead of the short test prefix.
        storage_length = len(str(Path(r"C:\Users\playdata2\AppData\Roaming\Code\User\globalStorage\dev-snapshot.dev-snapshot\repositories") / self.identity["id"]))
        storage = self.base / ("storage-" + "x" * (storage_length - len(str(self.base)) - 1 - len("storage-")))
        store = Store(storage, self.identity)
        self.addCleanup(store.close)
        core = SnapshotService(self.git, store, self.config)
        paths = ["backend/dev_snapshot/snapshots.py", "nested/" + "/".join(["directory"] * 9) + "/module.py"]
        for name in paths:
            self.write(name, b"historical\n")
        target = core.capture()
        for name in paths:
            self.write(name, b"latest\n")
        service = BoundaryCommitService(core)
        head, index = self.git.context()["head"], self.git.index_bytes()
        journal = service.prepare_boundary(target["id"])
        isolated = service.isolated(journal)
        self.assertGreaterEqual(len(str(isolated.root / paths[0])), 246)
        self.assertGreater(len(str(isolated.root / paths[1])), 260)
        self.assertEqual(self.git.context()["head"], head)
        self.assertEqual(self.git.index_bytes(), index)
        receipt = service.execute(journal, "chore: long path snapshot", [])
        self.assertTrue(receipt["expected"])
        for name in paths:
            self.assertEqual(self.git.file_content(name, head=receipt["oid"]), b"historical\n")
            self.assertEqual((self.root / name).read_bytes(), b"latest\n")
        self.assertFalse((storage / "tmp" / journal["id"]).exists())

    def test_boundary_preparation_error_reports_stage_without_private_details(self):
        service, snapshots = self.boundary_fixture()
        before = self.git.context(), self.git.index_bytes()
        with patch("dev_snapshot.boundary.atomic_write", side_effect=PermissionError(13, "private-error-content", "private-file-name")):
            with self.assertRaises(SafetyError) as caught:
                service.prepare_boundary(snapshots[1]["id"])
        self.assertIn("스냅샷 파일 준비 실패", str(caught.exception))
        self.assertIn("PermissionError", str(caught.exception))
        self.assertNotIn("private-error-content", str(caught.exception))
        self.assertNotIn("private-file-name", str(caught.exception))
        journal = self.store.journals()[0]
        self.assertEqual(journal["failure"]["step"], "materialize")
        self.assertEqual(journal["failure"]["errno"], 13)
        self.assertEqual(journal["phase"], "rolledBack")
        self.assertEqual((self.git.context(), self.git.index_bytes()), before)

    def test_boundary_baseline_error_keeps_stored_timeline_readable(self):
        self.write("a.txt", b"pending work")
        selected = self.core.capture()
        session = Session(self.git, self.store, self.config)
        with patch.object(session.commits, "baseline", side_effect=SafetyError("Baseline unavailable")):
            timeline = session.timeline()
            self.assertEqual(timeline[0]["id"], selected["id"])
            self.assertEqual(timeline[0]["rangeError"], "Baseline unavailable")
            with self.assertRaises(SafetyError):
                session.commits.review(self.initial["id"])

    def test_timeline_labels_do_not_use_cumulative_commit_summary(self):
        self.write("backend/dev_snapshot/store.py", b"store work")
        first = self.core.capture()
        self.write("src/extension.ts", b"UI work")
        second = self.core.capture()
        session = Session(self.git, self.store, self.config)
        timeline = {s["id"]: s for s in session.timeline()}
        self.assertEqual(timeline[first["id"]]["summary"], "store.py 추가")
        self.assertEqual(timeline[second["id"]]["summary"], "extension.ts 추가")
        self.assertEqual(timeline[second["id"]]["changedFiles"], 1)
        self.assertEqual(timeline[second["id"]]["commitChangedFiles"], 2)
        self.assertEqual(len(session.commits.review(second["id"])["changes"]), 2)
        # A background capture must not switch row labels or file counts.
        with patch.object(session, "worker", threading.current_thread()):
            during = {s["id"]: s for s in session.timeline()}
            self.assertEqual(during[second["id"]]["summary"], "extension.ts 추가")
            self.assertEqual(during[second["id"]]["changedFiles"], 1)
        receipt = session.commits.execute(session.commits.prepare_boundary(first["id"]), "chore: store", [])
        self.assertTrue(receipt["expected"])
        after = {s["id"]: s for s in session.timeline()}
        self.assertEqual(after[second["id"]]["summary"], "extension.ts 추가")
        self.assertEqual(after[second["id"]]["commitChangedFiles"], 1)
        self.assertEqual(after[first["id"]]["boundaryState"], "committed")
        self.assertEqual(len(session.commits.review(second["id"])["changes"]), 1)

    def test_timeline_legacy_labels_preserve_custom_titles_and_manifest_bytes(self):
        self.write("src/one.ts", b"one")
        first = self.core.capture()
        self.write("src/two.ts", b"two")
        second = self.core.capture()
        for record in (first, second):
            record["summaryVersion"] = 2
            record["summary"] = "UI 추가"
            self.store.write_record("snapshots", record["id"], record)
        originals = {s["id"]: self.store.record("snapshots", s["id"]).read_bytes() for s in (first, second)}
        self.store.update_metadata(second["id"], title="사용자 작업 이름")
        session = Session(self.git, self.store, self.config)
        records = {s["id"]: s for s in session.timeline()}
        self.assertEqual(records[first["id"]]["summary"], "one.ts 추가")
        self.assertEqual(records[second["id"]]["summary"], "two.ts 추가")
        self.assertEqual(records[second["id"]]["metadata"]["title"], "사용자 작업 이름")
        for snapshot_id, contents in originals.items():
            self.assertEqual(self.store.record("snapshots", snapshot_id).read_bytes(), contents)

    def test_snapshot_labels_disambiguate_duplicate_file_names(self):
        from dev_snapshot.snapshots import snapshot_summary
        summary = snapshot_summary([{"path": "src/auth/index.ts", "kind": "modified"},
                                    {"path": "src/api/index.ts", "kind": "modified"}])
        self.assertIn("auth/index.ts", summary)
        self.assertIn("api/index.ts", summary)

    def test_work_summary_uses_changed_files_instead_of_generic_root(self):
        from dev_snapshot.snapshots import summarize, suggest_commit
        changes = [{"path": "backend/dev_snapshot/restore.py", "kind": "modified"},
                   {"path": "backend/dev_snapshot/commits.py", "kind": "modified"}]
        summary = summarize(changes)
        self.assertIn("복원", summary)
        self.assertIn("커밋", summary)
        self.assertNotIn("backend", summary)
        self.assertEqual(summarize([{"path": "src/extension.ts", "kind": "modified"}]), "UI 수정")
        self.assertEqual(summarize([{"path": "backend/dev_snapshot/rpc.py", "kind": "modified"}]), "코어 통신 수정")
        self.assertEqual(summarize([{"path": "backend/tests/test_core.py", "kind": "added"}]), "테스트 추가")
        self.assertEqual(suggest_commit(["tests/test_auth.py"]), "test: 테스트 수정")
        self.assertEqual(suggest_commit(["src/cart.ts"]), "chore: cart.ts 수정")

    def test_local_summary_uses_saved_function_diff_and_upgrades_old_headers_without_writes(self):
        self.write("auth.py", b"def validate_login(value):\n    return True\n")
        self.core.capture()
        self.write("auth.py", b"def validate_login(value):\n    return bool(value)\n")
        selected = self.core.capture()
        self.assertEqual(selected["summary"], "로그인 검증 함수 수정")
        self.assertEqual(selected["summaryVersion"], 4)
        selected["summaryVersion"] = 3
        selected["summary"] = "auth.py 수정"
        self.store.write_record("snapshots", selected["id"], selected)
        original = self.store.record("snapshots", selected["id"]).read_bytes()
        self.store.update_metadata(selected["id"], title="직접 지정한 작업")
        self.write("auth.py", b"def unrelated_current_file():\n    pass\n")
        session = Session(self.git, self.store, self.config, git_enabled=False)
        item = session.timeline()[0]
        self.assertEqual(item["summary"], "로그인 검증 함수 수정")
        self.assertEqual(item["metadata"]["title"], "직접 지정한 작업")
        with patch.object(self.store, "get", side_effect=AssertionError("Cached labels must not reread blobs")):
            self.assertEqual(session.timeline()[0]["summary"], "로그인 검증 함수 수정")
        self.assertEqual(self.store.record("snapshots", selected["id"]).read_bytes(), original)

    def test_work_title_survives_restart_without_mutating_snapshot(self):
        self.write("src/cart.ts", b"export const count = 1;")
        selected = self.core.capture()
        original = self.store.record("snapshots", selected["id"]).read_bytes()
        session, engine = Session(self.git, self.store, self.config), Engine()
        engine.sessions[self.identity["id"]] = session
        params = {"repositoryId": self.identity["id"], "snapshotId": selected["id"]}
        engine.call("renameSnapshot", {**params, "title": "장바구니 수량 표시"})
        self.assertEqual(engine.call("suggestCommit", {**params, "paths": ["src/cart.ts"]}), "chore: 장바구니 수량 표시")
        self.assertEqual(self.store.record("snapshots", selected["id"]).read_bytes(), original)
        self.store.close()
        reopened = Store(self.base / "storage" / self.identity["id"], self.identity)
        self.addCleanup(reopened.close)
        reloaded = Session(self.git, reopened, self.config)
        self.assertEqual(reloaded.timeline()[0]["metadata"]["title"], "장바구니 수량 표시")
        reader = Store(reopened.directory, self.identity, readonly=True)
        self.addCleanup(reader.close)
        self.assertEqual(Session(self.git, reader, self.config).timeline()[0]["metadata"]["title"], "장바구니 수량 표시")
        with self.assertRaises(SafetyError):
            reader.update_metadata(selected["id"], title="must not write")

    def test_work_title_validation_and_clear_to_automatic_summary(self):
        self.write("a.txt", b"pending work")
        selected = self.core.capture()
        session, engine = Session(self.git, self.store, self.config), Engine()
        engine.sessions[self.identity["id"]] = session
        params = {"repositoryId": self.identity["id"], "snapshotId": selected["id"]}
        for invalid in ("a\nb", "x" * 101, None):
            with self.assertRaises(SafetyError):
                engine.call("renameSnapshot", {**params, "title": invalid})
        engine.call("renameSnapshot", {**params, "title": "초기 작업"})
        engine.call("renameSnapshot", {**params, "title": ""})
        self.assertEqual(session.timeline()[0]["metadata"]["title"], "")
        self.assertNotIn("초기 작업", engine.call("suggestCommit", {**params, "paths": ["a.txt"]}))

    def test_legacy_timeline_summary_is_updated_without_rewriting_manifest(self):
        self.write("src/timeline.ts", b"export const timeline = true;")
        selected = self.core.capture()
        selected.pop("summaryVersion")
        selected["summary"] = "src · 추가 1"
        self.store.write_record("snapshots", selected["id"], selected)
        original = self.store.record("snapshots", selected["id"]).read_bytes()
        session = Session(self.git, self.store, self.config)
        self.assertEqual(session.timeline()[0]["summary"], "timeline.ts 추가")
        self.assertEqual(self.store.record("snapshots", selected["id"]).read_bytes(), original)

    def snapshot_delete_fixture(self):
        self.write("a.txt", b"older work")
        older = self.core.capture()
        self.write("a.txt", b"selected work")
        selected = self.core.capture()
        self.write("a.txt", b"latest work")
        latest = self.core.capture()
        engine = Engine()
        session = Session(self.git, self.store, self.config)
        engine.sessions[self.identity["id"]] = session
        self.addCleanup(engine.close)
        return engine, session, {"repositoryId": self.identity["id"]}, older, selected, latest

    def test_usage_identifies_saved_branches_custom_titles_internal_baselines_and_backups(self):
        engine, session, params, older, selected, latest = self.snapshot_delete_fixture()
        self.store.update_metadata(selected["id"], title="삭제 확인용 작업")
        self.git.run("switch", "-c", "feature/history")
        switched = self.core.capture()
        backup = self.core.backup(switched["entries"], switched["context"])
        original = {s["id"]: self.store.record("snapshots", s["id"]).read_bytes() for s in self.store.snapshots()}
        records = {s["id"]: s for s in engine.call("usage", params)["snapshots"]}
        self.assertEqual(records[selected["id"]]["title"], "삭제 확인용 작업")
        self.assertEqual(records[selected["id"]]["context"]["branch"], "refs/heads/main")
        self.assertEqual(records[switched["id"]]["context"]["branch"], "refs/heads/feature/history")
        self.assertEqual(records[self.initial["id"]]["title"], "이전 내부 비교 기준")
        self.assertEqual(records[backup["id"]]["title"], "복구 백업")
        self.assertTrue(records[backup["id"]]["protected"])
        for record_id, contents in original.items():
            self.assertEqual(self.store.record("snapshots", record_id).read_bytes(), contents)

    def test_single_delete_requires_approval_and_preserves_other_snapshots_git_and_files(self):
        engine, session, params, older, selected, latest = self.snapshot_delete_fixture()
        with self.assertRaises(SafetyError):
            engine.call("deleteSnapshot", {**params, "snapshotId": selected["id"]})
        self.store.snapshot(selected["id"], verify=True)
        context, index = self.git.context(), self.git.index_bytes()
        engine.call("deleteSnapshot", {**params, "snapshotId": selected["id"], "approved": True})
        with self.assertRaises(FileNotFoundError):
            self.store.snapshot(selected["id"])
        for snapshot in (self.initial, older, latest):
            self.store.snapshot(snapshot["id"], verify=True)
        self.assertFalse(self.store._object(selected["entries"]["a.txt"]["hash"]).exists())
        timeline = {s["id"]: s for s in session.timeline()}
        self.assertTrue(timeline[latest["id"]]["comparisonPruned"])
        self.assertTrue(session.snapshot_changes(latest["id"]))
        self.assertEqual(self.git.context(), context)
        self.assertEqual(self.git.index_bytes(), index)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"latest work")
        restore = RestoreService(self.core)
        restore.apply(restore.prepare(older["id"])["id"])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"older work")

    def test_single_delete_rechecks_protection_and_preserves_latest_initial_pins_and_backups(self):
        engine, session, params, older, selected, latest = self.snapshot_delete_fixture()
        review = engine.call("usage", params)
        self.assertFalse(next(s for s in review["snapshots"] if s["id"] == selected["id"])["protected"])
        self.store.update_metadata(selected["id"], pinned=True)
        backup = self.core.backup(latest["entries"], latest["context"])
        for snapshot in (selected, latest, self.initial, backup):
            with self.assertRaises(SafetyError):
                engine.call("deleteSnapshot", {**params, "snapshotId": snapshot["id"], "approved": True})
            self.store.snapshot(snapshot["id"], verify=True)

    def test_single_delete_preserves_latest_committed_boundary(self):
        engine, session, params, older, selected, latest = self.snapshot_delete_fixture()
        journal = engine.call("commitPrepare", {**params, "snapshotId": selected["id"], "approved": True})
        receipt = engine.call("commitExecute", {**params, "operationId": journal["id"], "message": "chore: boundary",
                                               "snapshotIds": [selected["id"]], "approved": True})
        self.assertTrue(receipt["expected"])
        with self.assertRaises(SafetyError):
            engine.call("deleteSnapshot", {**params, "snapshotId": selected["id"], "approved": True})
        self.store.snapshot(selected["id"], verify=True)

    def history_fixture(self):
        self.write("a.txt", b"older work")
        older = self.core.capture()
        self.write("a.txt", b"selected work")
        selected = self.core.capture()
        self.write("a.txt", b"later work")
        later = self.core.capture()
        engine = Engine()
        session = Session(self.git, self.store, self.config)
        engine.sessions[self.identity["id"]] = session
        self.addCleanup(engine.close)
        params = {"repositoryId": self.identity["id"]}
        journal = engine.call("commitPrepare", {**params, "selected": ["a.txt"], "approved": True})
        receipt = engine.call("commitExecute", {**params, "operationId": journal["id"], "message": "chore: work",
                                               "snapshotIds": [selected["id"]], "approved": True})
        return engine, session, params, older, selected, later, receipt

    def test_commit_cleanup_deletes_only_approved_older_history(self):
        engine, session, params, older, selected, later, receipt = self.history_fixture()
        review = receipt["cleanup"]
        self.assertEqual([s["id"] for s in review["candidates"]], [older["id"]])
        self.assertFalse(session.tick(), "Automatic recording waits for the cleanup decision")
        head, index = self.git.context()["head"], self.git.index_bytes()
        result = engine.call("historyDelete", {**params, "token": review["token"], "approved": True})
        self.assertEqual(result["deleted"], 1)
        with self.assertRaises(FileNotFoundError):
            self.store.snapshot(older["id"])
        for snapshot in (self.initial, selected, later):
            self.store.snapshot(snapshot["id"], verify=True)
        self.assertEqual(self.git.context()["head"], head)
        self.assertEqual(self.git.index_bytes(), index)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"later work")
        self.assertFalse(self.store._object(older["entries"]["a.txt"]["hash"]).exists())

    def test_commit_cleanup_cancel_and_missing_approval_preserve_history(self):
        engine, session, params, older, selected, later, receipt = self.history_fixture()
        token = receipt["cleanup"]["token"]
        with self.assertRaises(SafetyError):
            engine.call("historyDelete", {**params, "token": token})
        engine.call("historyCancel", {**params, "token": token})
        self.assertFalse(session.history_reviews)
        with self.assertRaises(SafetyError):
            engine.call("historyDelete", {**params, "token": token, "approved": True})
        self.store.snapshot(older["id"], verify=True)

    def test_commit_cleanup_preserves_other_branches_uncommitted_snapshots(self):
        self.git.run("switch", "-c", "feature/uncommitted")
        self.write("a.txt", b"feature work only in history")
        feature = self.core.capture()
        self.write("a.txt", b"baseline\r\n")
        self.git.run("switch", "main")
        self.core.capture()
        self.write("a.txt", b"older main work")
        older = self.core.capture()
        self.write("a.txt", b"main selected")
        selected = self.core.capture()
        engine, session = Engine(), Session(self.git, self.store, self.config)
        engine.sessions[self.identity["id"]] = session
        self.addCleanup(engine.close)
        params = {"repositoryId": self.identity["id"]}
        journal = engine.call("commitPrepare", {**params, "snapshotId": selected["id"], "approved": True})
        receipt = engine.call("commitExecute", {**params, "operationId": journal["id"], "message": "chore: main only", "snapshotIds": [selected["id"]], "approved": True})
        review = receipt["cleanup"]
        self.assertEqual(review["branch"], "refs/heads/main")
        self.assertTrue(all(s["branch"] == "refs/heads/main" for s in review["candidates"]))
        self.assertNotIn(feature["id"], {s["id"] for s in review["candidates"]})
        self.assertIn(older["id"], {s["id"] for s in review["candidates"]})
        engine.call("historyDelete", {**params, "token": review["token"], "approved": True})
        self.store.snapshot(feature["id"], verify=True)
        self.assertEqual(self.store.get(feature["entries"]["a.txt"]["hash"]), b"feature work only in history")
        with self.assertRaises(FileNotFoundError):
            self.store.snapshot(older["id"])

    def test_cleanup_review_cannot_be_changed_to_another_branch(self):
        engine, session, params, older, selected, later, receipt = self.history_fixture()
        review = dict(receipt["cleanup"])
        review.pop("token")
        review["branch"] = "refs/heads/other"
        with self.assertRaises(SafetyError):
            self.store.delete_history_before(review)
        self.store.snapshot(older["id"], verify=True)

    def test_history_cleanup_blocks_detached_head_boundaries(self):
        self.git.run("switch", "--detach", "HEAD")
        self.write("a.txt", b"detached work")
        selected = self.core.capture()
        with self.assertRaisesRegex(SafetyError, "attached branch"):
            self.store.history_before(selected["id"])
        self.store.snapshot(self.initial["id"], verify=True)

    def test_commit_cleanup_rechecks_new_pins_before_any_deletion(self):
        engine, session, params, older, selected, later, receipt = self.history_fixture()
        self.store.update_metadata(older["id"], pinned=True)
        with self.assertRaises(SafetyError):
            engine.call("historyDelete", {**params, "token": receipt["cleanup"]["token"], "approved": True})
        self.store.snapshot(older["id"], verify=True)

    def test_commit_cleanup_blocks_corrupt_retained_snapshot(self):
        engine, session, params, older, selected, later, receipt = self.history_fixture()
        self.store._object(selected["entries"]["a.txt"]["hash"]).write_bytes(b"corrupt")
        with self.assertRaises(SafetyError):
            engine.call("historyDelete", {**params, "token": receipt["cleanup"]["token"], "approved": True})
        self.store.snapshot(older["id"], verify=True)

    def test_history_cleanup_preserves_pins_and_recent_backups(self):
        self.write("a.txt", b"pinned work")
        pinned = self.core.capture()
        self.store.update_metadata(pinned["id"], pinned=True)
        backup = self.core.backup({"a.txt": self.core.read_entry("a.txt")}, self.git.context())
        self.write("a.txt", b"boundary")
        selected = self.core.capture()
        review = self.store.history_before(selected["id"])
        self.assertEqual(review["candidates"], [])
        self.assertEqual(review["protectedCount"], 3)
        self.assertEqual(self.store.delete_history_before(review), 0)
        for snapshot in (self.initial, pinned, backup, selected):
            self.store.snapshot(snapshot["id"], verify=True)

    def test_unexpected_commit_does_not_offer_history_cleanup(self):
        self.write("a.txt", b"selected")
        selected = self.core.capture()
        self.write("b.txt", b"hook-added")
        hook = self.base / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\ngit add -- b.txt\n", encoding="utf-8")
        hook.chmod(0o700)
        session, engine = Session(self.git, self.store, self.config), Engine()
        engine.sessions[self.identity["id"]] = session
        self.addCleanup(engine.close)
        params = {"repositoryId": self.identity["id"]}
        journal = engine.call("commitPrepare", {**params, "selected": ["a.txt"], "approved": True})
        receipt = engine.call("commitExecute", {**params, "operationId": journal["id"], "message": "chore: hook",
                                               "snapshotIds": [selected["id"]], "approved": True})
        self.assertFalse(receipt["expected"])
        self.assertNotIn("cleanup", receipt)
        self.assertFalse(session.history_reviews)
        self.store.snapshot(selected["id"], verify=True)

    def test_settings_during_capture_are_applied_after_cancellation(self):
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
            session.start_capture("manual")
            self.assertTrue(entered.wait(5))
            engine.call("settings", {"repositoryId": self.identity["id"], "settings": {"exclude": ["a.txt"]}})
            release.set()
            session.worker.join(10)
        session.tick()
        self.assertEqual(session.core.config["exclude"], ["a.txt"])
        self.assertIsNone(session.pending_settings)
        session.start_capture("manual")
        session.worker.join(10)
        self.assertEqual(session.core.latest()["entries"]["a.txt"]["state"], "excluded")

    def test_failed_incremental_capture_retries_all_pending_content(self):
        session = Session(self.git, self.store, self.config)
        self.addCleanup(lambda: session.worker.join(10) if session.worker else None)
        self.write("a.txt", b"change A")
        session.event("a.txt")
        session.last_event -= 10
        with patch.object(session, "capture", side_effect=SafetyError("Injected failure")):
            self.assertTrue(session.tick())
            session.worker.join(10)
        self.assertEqual(session.job["state"], "failed")
        self.write("b.txt", b"change B")
        session.event("b.txt")
        session.last_event -= 10
        session.retry_after = 0
        self.assertTrue(session.tick())
        session.worker.join(10)
        snapshot = session.core.latest()
        self.assertEqual(self.store.get(snapshot["entries"]["a.txt"]["hash"]), b"change A")
        self.assertEqual(self.store.get(snapshot["entries"]["b.txt"]["hash"]), b"change B")

    def test_restore_ui_cleanup_is_allowed_during_followup_capture(self):
        session = Session(self.git, self.store, self.config)
        engine = Engine()
        engine.sessions[self.identity["id"]] = session
        self.addCleanup(engine.close)
        params = {"repositoryId": self.identity["id"]}
        self.write("a.txt", b"before restore")
        plan = engine.call("restorePrepare", {**params, "snapshotId": self.initial["id"]})
        entered, release = threading.Event(), threading.Event()
        original = session.capture
        def slow(*args, **kwargs):
            entered.set()
            release.wait(10)
            return original(*args, **kwargs)
        with patch.object(session, "capture", slow):
            engine.call("restoreApply", {**params, "planId": plan["id"], "approved": True, "writersStopped": True})
            self.assertTrue(entered.wait(5))
            try:
                engine.call("restoreCancel", {**params, "planId": plan["id"]})
            finally:
                release.set()
                session.worker.join(10)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"baseline\r\n")

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

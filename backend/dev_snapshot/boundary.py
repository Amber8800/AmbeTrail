"""Ordered snapshot commits using an isolated worktree and index, with normal Git hooks."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .common import (ABSENT, SafetyError, atomic_write, digest, fsync_directory,
                     is_link, native_path, new_id, read_json, relative_path, safe_path, sha, utc_now, validate_entries, validate_id)
from .commits import CommitService
from .git import Git, same_context
from .restore import unfinished
from .snapshots import Policy, changes_between, summarize, suggest_commit


class BoundaryCommitService(CommitService):
    def __init__(self, core, dirty=None):
        super().__init__(core, dirty)
        self.baselines: dict[tuple, dict] = {}

    def anchor(self, context: dict) -> dict | None:
        for source in sorted((self.core.store.directory / "receipts").glob("*.json")):
            receipt = read_json(source)
            if receipt.get("mode") == "boundary" and receipt.get("expected") is True and receipt["oid"] == context["head"] and receipt["branch"] == context["branch"]:
                return receipt
        return None

    def baseline(self, context: dict, anchor: dict | None) -> dict:
        if anchor:
            if anchor.get("partial"):
                entries = dict(self.baseline(context, None))
                entries.update(validate_entries(anchor["committedEntries"]))
                return entries
            return self.core.store.snapshot(anchor["snapshotIds"][0])["entries"]
        key = (context["head"], digest(self.core.config))
        if key in self.baselines:
            return self.baselines[key]
        policy = Policy(self.core)
        records = [(name, mode, oid) for name, (mode, oid) in self.core.git.tree_entries(context["head"]).items()
                   if mode in ("100644", "100755") and not policy.exclusion(name)]
        entries = {}
        for offset in range(0, len(records), 128):
            group = records[offset:offset + 128]
            sizes = self.core.git.run("cat-file", "--batch-check", data=("\n".join(r[2] for r in group) + "\n").encode()).splitlines()
            for record, header in zip(group, sizes, strict=True):
                fields = header.split()
                if len(fields) != 3 or fields[1] != b"blob" or int(fields[2]) > self.core.config["maxFileMiB"] * 1024 * 1024:
                    raise SafetyError("A baseline blob exceeds the supported file limit.")
            # Bound each batch to 16 MiB, independent of the number of files.
            batch, total = [], 0
            for record, header in zip(group, sizes, strict=True):
                size = int(header.split()[2])
                if batch and total + size > 16 * 1024 * 1024:
                    self._read_batch(batch, entries)
                    batch, total = [], 0
                batch.append((record, size))
                total += size
            if batch:
                self._read_batch(batch, entries)
        self.baselines = {key: entries}
        return entries

    def _read_batch(self, batch: list, entries: dict) -> None:
        output = self.core.git.run("cat-file", "--batch", data=("\n".join(item[0][2] for item in batch) + "\n").encode())
        position = 0
        for (name, mode, oid), size in batch:
            end = output.index(b"\n", position)
            if output[position:end] != f"{oid} blob {size}".encode():
                raise SafetyError("Invalid Git baseline blob.")
            data = output[end + 1:end + 1 + size]
            if len(data) != size:
                raise SafetyError("Incomplete Git baseline blob.")
            entries[name] = {"state": "present", "hash": sha(data), "size": size, "executable": os.name != "nt" and mode == "100755"}
            position = end + size + 2

    def review(self, snapshot_id: str, *, verify: bool = True, selected: list[str] | None = None) -> dict:
        git, store = self.core.git, self.core.store
        context, target = git.context(), store.snapshot(snapshot_id, verify=verify)
        if context["busy"] or context["sparse"] or not context["branch"] or not git.clean_index():
            raise SafetyError("Commit requires an attached branch, an empty staging area, and no Git operation.")
        if target["reason"] == "backup" or target["context"]["branch"] != context["branch"]:
            raise SafetyError("Select a timeline snapshot from the current branch.")
        anchor = self.anchor(context)
        if anchor:
            if target["endedAt"] < anchor["boundaryAt"] or (target["endedAt"] == anchor["boundaryAt"] and not anchor.get("partial")):
                raise SafetyError("Select a snapshot after the last committed boundary.")
            allowed_heads = anchor["sourceHeads"] + [context["head"]]
        else:
            allowed_heads = [context["head"]]
        if target["context"]["head"] not in allowed_heads:
            raise SafetyError("An external commit changed the baseline. Capture a new snapshot before committing.")
        baseline = self.baseline(context, anchor)
        head_entries = git.tree_entries(context["head"])
        policy, before, after, skipped = Policy(self.core), {}, {}, []
        for name, desired in target["entries"].items():
            reason = policy.exclusion(name)
            if desired["state"] == "excluded" or reason:
                skipped.append({"path": name, "reason": reason or desired.get("reason", "excluded")})
                continue
            if name in head_entries and head_entries[name][0] not in ("100644", "100755"):
                raise SafetyError("A snapshot path conflicts with a Git link or unsupported tracked file.")
            after[name] = desired
            before[name] = baseline.get(name, ABSENT)
        changes = changes_between(before, after)
        omitted = []
        if selected is not None:
            if not isinstance(selected, list) or any(not isinstance(p, str) for p in selected):
                raise SafetyError("Commit selection must be a list of file paths.")
            for name in selected:
                relative_path(name)
            names = set(selected)
            if len(names) != len(selected) or names - {c["path"] for c in changes}:
                raise SafetyError("Commit selection contains duplicate or unavailable files. Review the file list again.")
            omitted = [c for c in changes if c["path"] not in names]
            changes = [c for c in changes if c["path"] in names]
        self.guard([c["path"] for c in changes])
        if not same_context(context, git.context()):
            raise SafetyError("Git state changed during boundary review.")
        title = store.metadata(snapshot_id).get("title", "") or summarize(changes)
        result = {"snapshotId": snapshot_id, "boundaryId": anchor["snapshotIds"][0] if anchor else None,
                "context": context, "changes": changes, "skipped": skipped, "summary": title,
                "omitted": omitted,
                "message": suggest_commit([c["path"] for c in changes], title),
                "sourceHeads": list(dict.fromkeys([*(anchor["sourceHeads"] if anchor else []), target["context"]["head"]]))}
        result["reviewHash"] = digest(result)
        return result

    def isolated(self, journal: dict) -> Git:
        directory = self.core.store.directory / "tmp" / validate_id(journal["id"])
        if is_link(directory.parent) or is_link(directory) or directory.resolve().parent != (self.core.store.directory / "tmp").resolve():
            raise SafetyError("Unsafe isolated commit directory.")
        worktree = directory / "worktree"
        if is_link(worktree) or is_link(directory / "index"):
            raise SafetyError("Unexpected link in isolated commit files.")
        return Git(worktree, self.core.git.executable, git_dir=Path(self.core.store.identity["gitDir"]),
                   index=directory / "index", hooks=journal["hooks"])

    def prepare_boundary(self, snapshot_id: str, *, selected: list[str] | None = None, review_hash: str | None = None) -> dict:
        git, store = self.core.git, self.core.store
        store.assert_writer()
        if any(unfinished(j) for j in store.journals()):
            raise SafetyError("Review incomplete operations first.")
        review = self.review(snapshot_id, selected=selected)
        if review_hash is not None and review_hash != review["reviewHash"]:
            raise SafetyError("Commit scope or Git state changed after review. Review the selection again.")
        target = store.snapshot(snapshot_id, verify=True)
        selected = [c["path"] for c in review["changes"]]
        if not selected:
            raise SafetyError("There are no net changes in this snapshot interval.")
        backup = self.core.backup({name: self.core.read_entry(name) for name in selected}, review["context"])
        index = git.index_bytes()
        journal = {"schema": 1, "id": new_id(), "kind": "commit", "mode": "boundary", "phase": "prepared",
                   "createdAt": utc_now(), "context": review["context"], "backupId": backup["id"],
                   "targetId": snapshot_id, "boundaryId": review["boundaryId"], "sourceHeads": review["sourceHeads"],
                   "boundaryAt": target["endedAt"], "selected": selected, "scope": selected, "changes": [], "applied": [], "conflicts": [],
                   "indexExisted": index is not None, "planHash": digest(review),
                   "policyHash": digest(self.core.config),
                   "hooks": git.text("rev-parse", "--path-format=absolute", "--git-path", "hooks"),
                   "skipped": review["skipped"], "summary": review["summary"], "message": review["message"],
                   "partial": bool(review["omitted"])}
        if journal["partial"]:
            anchor = self.anchor(review["context"])
            committed = {}
            if anchor:
                committed = anchor["committedEntries"] if anchor.get("partial") else store.snapshot(anchor["snapshotIds"][0])["entries"]
            journal["committedEntries"] = {p: e for p, e in committed.items() if e["state"] != "excluded"}
        if index is not None:
            journal["indexBackupHash"] = store.put(index)
        store.journal(journal)
        try:
            journal["step"] = "materialize"
            store.journal(journal)
            temporary = store.directory / "tmp" / journal["id"]
            if is_link(temporary.parent):
                raise SafetyError("Unexpected link in isolated commit storage.")
            temporary.mkdir(mode=0o700)
            isolated = self.isolated(journal)
            isolated.root.mkdir(mode=0o700)
            skipped = {entry["path"] for entry in review["skipped"]}
            for name, entry in target["entries"].items():
                if entry["state"] == "present" and name not in skipped:
                    atomic_write(safe_path(isolated.root, name), store.get(entry["hash"], entry["size"]), 0o700 if entry["executable"] else 0o600)
            journal["step"] = "readTree"
            store.journal(journal)
            isolated.run("read-tree", review["context"]["head"] or "--empty")
            paths = selected
            if paths:
                journal["step"] = "stage"
                store.journal(journal)
                isolated.add(paths, force=True)
            journal["step"] = "verify"
            store.journal(journal)
            journal["preparedTree"] = isolated.tree()
            journal["preparedIndexHash"] = store.put(isolated.index_bytes() or b"no-index")
            journal["selected"] = [name for name in isolated.run("diff", "--cached", "--name-only", "-z", "--no-renames").decode("utf-8").split("\0") if name]
            if not journal["selected"]:
                raise SafetyError("The snapshot has no changes after Git filters.")
            if set(journal["selected"]) - set(paths):
                raise SafetyError("Git filters changed files outside the approved snapshot scope.")
            if not same_context(review["context"], git.context()):
                raise SafetyError("Git state changed while preparing the snapshot commit.")
            if store.usage() > self.core.config["maxStorageMiB"] * 1024 * 1024:
                raise SafetyError("Isolated commit preparation exceeds the storage quota. Manage history first.")
            journal["phase"] = "applying"
            store.journal(journal)
            return journal
        except Exception as error:
            journal["phase"] = "rolledBack"
            journal["failure"] = {"step": journal.get("step"), "type": type(error).__name__,
                                  "errno": getattr(error, "errno", None), "winerror": getattr(error, "winerror", None)}
            store.journal(journal)
            self.cleanup(journal)
            step = {"materialize": "스냅샷 파일 준비", "readTree": "Git 기준 트리 읽기", "stage": "Git 필터 적용 및 임시 Staging", "verify": "준비 결과 검증"}.get(journal.get("step"), "커밋 준비")
            reason = str(error) if isinstance(error, SafetyError) else f"{type(error).__name__} (errno={getattr(error, 'errno', None)}, winerror={getattr(error, 'winerror', None)})"
            raise SafetyError(f"{step} 실패: {reason}. 현재 작업 파일과 원래 인덱스는 변경하지 않았습니다.") from error

    def diff(self, journal: dict, name: str) -> tuple[bytes | None, bytes | None]:
        if journal.get("mode") != "boundary" or name not in journal["selected"]:
            raise SafetyError("File is outside the prepared snapshot commit.")
        isolated = self.isolated(journal)
        return (self.core.git.file_content(name, head=journal["context"]["head"]),
                isolated.file_content(name, staged=True))

    def execute(self, journal: dict, message: str, references: list[str]) -> dict:
        if journal.get("mode") != "boundary":
            return super().execute(journal, message, references)
        git, store = self.core.git, self.core.store
        store.assert_writer()
        if journal["phase"] != "applying" or not message.strip() or "\0" in message or len(message) > 65536:
            raise SafetyError("Invalid prepared snapshot commit or message.")
        self.guard(journal["selected"])
        isolated = self.isolated(journal)
        if digest(self.core.config) != journal["policyHash"] or not same_context(journal["context"], git.context()) or isolated.tree() != journal["preparedTree"] or sha(isolated.index_bytes() or b"no-index") != journal["preparedIndexHash"]:
            raise SafetyError("Git state or isolated staging changed after review.")
        store.snapshot(journal["targetId"], verify=True)
        index_path = git.index_path()
        lock = Path(str(index_path) + ".lock")
        owned = False
        try:
            with open(lock, "xb") as stream:
                owned = True
                if not same_context(journal["context"], git.context(own_index_lock=True)):
                    raise SafetyError("Git state changed before acquiring the index lock.")
                journal["phase"] = "applying"
                journal["indexLockOwnerPid"] = os.getpid()
                store.journal(journal)
                isolated.run("commit", "--file=-", data=message.encode("utf-8"), timeout=120)
                after = git.context(own_index_lock=True)
                actual_tree = git.text("rev-parse", after["head"] + "^{tree}")
                parents = git.text("rev-list", "--parents", "-n", "1", after["head"]).split()[1:]
                expected_parents = [journal["context"]["head"]] if journal["context"]["head"] else []
                expected = (parents == expected_parents and actual_tree == journal["preparedTree"] and
                            after["branch"] == journal["context"]["branch"] and not after["busy"] and
                            after["indexHash"] == journal["context"]["indexHash"] and isolated.tree() == actual_tree)
                receipt = {"schema": 1, "id": new_id(), "mode": "boundary", "branch": journal["context"]["branch"],
                           "oid": after["head"], "parentOid": journal["context"]["head"], "reviewedTree": journal["preparedTree"],
                           "actualTree": actual_tree, "snapshotIds": [journal["targetId"]], "boundaryAt": journal["boundaryAt"],
                           "sourceHeads": journal["sourceHeads"], "selected": journal["selected"], "expected": expected, "createdAt": utc_now()}
                if journal.get("partial"):
                    target = store.snapshot(journal["targetId"])
                    receipt["partial"] = True
                    receipt["committedEntries"] = {**journal["committedEntries"], **{p: target["entries"][p] for p in journal["scope"]}}
                # Record the commit before syncing the real index, so a crash has a durable receipt.
                store.write_record("receipts", receipt["id"], receipt)
                journal["receiptId"] = receipt["id"]
                journal["phase"] = "recoveryRequired"
                store.journal(journal)
                if expected:
                    stream.write(store.get(journal["preparedIndexHash"]))
                    stream.flush()
                    os.fsync(stream.fileno())
            if expected:
                current = git.context(own_index_lock=True)
                if current["head"] != receipt["oid"] or current["branch"] != receipt["branch"] or current["indexHash"] != journal["context"]["indexHash"] or current["busy"]:
                    raise SafetyError("HEAD or the actual index changed before commit index publication.")
                os.replace(lock, index_path)
                owned = False
                fsync_directory(index_path.parent)
                journal["phase"] = "complete"
                store.journal(journal)
                metadata = store.metadata(journal["targetId"])
                store.update_metadata(journal["targetId"], commitIds=sorted(set(metadata["commitIds"]) | {receipt["oid"]}))
                self.cleanup(journal)
            self.core.changed()
            return receipt
        except Exception:
            current = git.context(own_index_lock=owned)
            journal["phase"] = "rolledBack" if same_context(journal["context"], current) else "recoveryRequired"
            store.journal(journal)
            if journal["phase"] == "rolledBack":
                self.cleanup(journal)
            raise
        finally:
            if owned:
                lock.unlink(missing_ok=True)

    def cancel(self, journal: dict) -> None:
        if journal.get("mode") != "boundary":
            return super().cancel(journal)
        self.core.store.assert_writer()
        if journal.get("receiptId"):
            receipt = read_json(self.core.store.record("receipts", journal["receiptId"]))
            current = self.core.git.context()
            if (receipt.get("expected") is True and current["head"] == receipt["oid"] and
                    current["branch"] == journal["context"]["branch"] and not current["busy"] and not current["sparse"]):
                if current["indexHash"] == journal["context"]["indexHash"]:
                    self._finish_index_recovery(journal, receipt, current)
                    return
                if self.core.git.tree() == receipt["actualTree"]:
                    journal["phase"] = "complete"
                    self.core.store.journal(journal)
                    self.cleanup(journal)
                    return
        # The actual index/worktree were never staged or replaced during preparation.
        journal["phase"] = "rolledBack" if same_context(journal["context"], self.core.git.context()) else "recoveryRequired"
        self.core.store.journal(journal)
        if journal["phase"] == "rolledBack":
            self.cleanup(journal)

    def _finish_index_recovery(self, journal: dict, receipt: dict, context: dict) -> None:
        git = self.core.git
        if journal["preparedTree"] != receipt["actualTree"]:
            raise SafetyError("Commit result differs from the reviewed recovery tree.")
        index_path = git.index_path()
        lock, owned = Path(str(index_path) + ".lock"), False
        try:
            with open(lock, "xb") as stream:
                owned = True
                if not same_context(context, git.context(own_index_lock=True)):
                    raise SafetyError("Git changed before index recovery.")
                stream.write(self.core.store.get(journal["preparedIndexHash"]))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(lock, index_path)
            owned = False
            fsync_directory(index_path.parent)
            journal["phase"] = "complete"
            self.core.store.journal(journal)
            metadata = self.core.store.metadata(journal["targetId"])
            self.core.store.update_metadata(journal["targetId"], commitIds=sorted(set(metadata["commitIds"]) | {receipt["oid"]}))
            self.cleanup(journal)
        finally:
            if owned:
                lock.unlink(missing_ok=True)

    def cleanup(self, journal: dict) -> None:
        directory = self.core.store.directory / "tmp" / validate_id(journal["id"])
        if directory.exists():
            if is_link(directory.parent) or is_link(directory) or directory.resolve().parent != (self.core.store.directory / "tmp").resolve():
                raise SafetyError("Unsafe isolated commit cleanup directory.")
            shutil.rmtree(native_path(directory))
        self.core.store._used_bytes = None

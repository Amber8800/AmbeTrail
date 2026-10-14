"""Two-step Git commits with reviewed staging and index-preserving failure recovery."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from .common import (SafetyError, digest, fsync_directory, new_id, relative_path,
                     utc_now)
from .git import same_context
from .restore import unfinished
from .snapshots import Policy, SnapshotService


class CommitService:
    def __init__(self, core: SnapshotService, dirty: Callable[[], set[str]] | None = None):
        self.core = core
        self.dirty = dirty or (lambda: set())

    def guard(self, names: list[str]) -> None:
        if set(names) & self.dirty():
            raise SafetyError("Resolve unsaved edits in selected files before committing.")

    def choices(self) -> dict:
        policy = Policy(self.core)
        statuses = self.core.git.statuses()
        choices = []
        for item in statuses:
            for name in [item["path"], *([item["original"]] if item.get("original") else [])]:
                if not policy.exclusion(name):
                    choices.append({"path": name, "code": item["code"]})
        return {"files": list({i["path"]: i for i in choices}.values()),
                "stagingBlocked": not self.core.git.clean_index()}

    def prepare(self, selected: list[str]) -> dict:
        store, git = self.core.store, self.core.git
        store.assert_writer()
        selected = sorted(set(relative_path(p) for p in selected))
        if not selected:
            raise SafetyError("Select at least one changed file.")
        if any(unfinished(j) for j in store.journals()):
            raise SafetyError("Review incomplete operations first.")
        context = git.context()
        if context["busy"] or context["sparse"] or not context["branch"] or not git.clean_index():
            raise SafetyError("Commit requires an attached branch, an empty staging area including intent-to-add, and no Git operation.")
        self.guard(selected)
        allowed = {i["path"] for i in self.choices()["files"]}
        if not set(selected) <= allowed:
            raise SafetyError("A selected file is no longer an eligible current change.")
        policy, entries = Policy(self.core), {}
        for name in selected:
            entries[name] = self.core.read_entry(name, policy)
            if entries[name]["state"] == "excluded":
                raise SafetyError("Excluded files cannot be committed through Amberog.")
        backup = self.core.backup(entries, context)
        original_index = git.index_bytes()
        journal = {"schema": 1, "id": new_id(), "kind": "commit", "phase": "prepared", "createdAt": utc_now(),
                   "backupId": backup["id"], "context": context, "changes": [], "applied": [], "conflicts": [],
                   "selected": selected, "indexExisted": original_index is not None,
                   "planHash": digest([context, selected, entries])}
        if original_index is not None:
            journal["indexBackupHash"] = store.put(original_index)
        store.journal(journal)
        unexpected = False
        try:
            if not same_context(context, git.context()) or not git.clean_index():
                raise SafetyError("Git state changed during commit preparation.")
            self.guard(selected)
            for name in selected:
                if not self.core.matches(name, entries[name]):
                    raise SafetyError("A selected file changed before staging.")
            git.add(selected)
            journal["preparedTree"] = git.tree()
            # Record the exact index bytes as well as the tree to detect index-only external edits.
            journal["preparedIndexHash"] = git.context()["indexHash"]
            journal["phase"] = "applying"
            store.journal(journal)
            after = git.context()
            if any(after[k] != context[k] for k in ("head", "branch")):
                raise SafetyError("HEAD changed while staging.")
            staged = [s for s in git.statuses() if s["code"] != "??" and s["code"][0] != " "]
            unexpected = any(s["path"] not in selected or s.get("original") and s["original"] not in selected for s in staged)
            if unexpected:
                raise SafetyError("Unexpected staged files detected. Existing index contents have been preserved.")
            if context["head"] and git.text("rev-parse", context["head"] + "^{tree}") == journal["preparedTree"]:
                raise SafetyError("Selected files have no changes after Git filters.")
            return journal
        except Exception:
            if journal.get("preparedTree") and not unexpected:
                self.cancel(journal)
            else:
                journal["phase"] = "recoveryRequired"
                store.journal(journal)
            raise

    def execute(self, journal: dict, message: str, references: list[str]) -> dict:
        git, store = self.core.git, self.core.store
        store.assert_writer()
        if journal["kind"] != "commit" or journal["phase"] != "applying" or not journal.get("preparedTree"):
            raise SafetyError("Invalid prepared commit.")
        if not message.strip() or "\0" in message or len(message) > 65536:
            raise SafetyError("Enter a valid, nonempty commit message.")
        for reference in references:
            store.snapshot(reference)
        self.guard(journal["selected"])
        context = git.context()
        if context["busy"] or any(context[k] != journal["context"][k] for k in ("head", "branch")) or context["indexHash"] != journal["preparedIndexHash"] or git.tree() != journal["preparedTree"]:
            raise SafetyError("HEAD or staging changed after review. Review recovery before retrying.")
        try:
            git.run("commit", "--file=-", data=message.encode("utf-8"), timeout=120)
            after = git.context()
            if not after["head"]:
                raise SafetyError("Git did not produce a commit.")
            actual_tree = git.text("rev-parse", after["head"] + "^{tree}")
            parents = git.text("rev-list", "--parents", "-n", "1", after["head"]).split()[1:]
            expected_parents = [journal["context"]["head"]] if journal["context"]["head"] else []
            expected = actual_tree == journal["preparedTree"] and parents == expected_parents and after["branch"] == journal["context"]["branch"]
            receipt = {"schema": 1, "id": new_id(), "oid": after["head"], "parentOid": journal["context"]["head"],
                       "reviewedTree": journal["preparedTree"], "actualTree": actual_tree,
                       "selected": journal["selected"], "snapshotIds": references, "expected": expected, "createdAt": utc_now()}
            store.write_record("receipts", receipt["id"], receipt)
            journal["receiptId"] = receipt["id"]
            journal["phase"] = "complete" if expected else "recoveryRequired"
            store.journal(journal)
            for reference in references:
                metadata = store.metadata(reference)
                store.update_metadata(reference, commitIds=sorted(set(metadata["commitIds"]) | {after["head"]}))
            self.core.changed()
            return receipt
        except Exception:
            if git.context()["head"] == journal["context"]["head"]:
                self.cancel(journal)
            else:
                journal["phase"] = "recoveryRequired"
                store.journal(journal)
            raise

    def cancel(self, journal: dict) -> None:
        git, store = self.core.git, self.core.store
        store.assert_writer()
        context = git.context()
        if context["busy"] or any(context[k] != journal["context"][k] for k in ("head", "branch")) or not journal.get("preparedTree") or context["indexHash"] != journal.get("preparedIndexHash") or git.tree() != journal["preparedTree"]:
            journal["phase"] = "recoveryRequired"
            store.journal(journal)
            return
        index = git.index_path()
        lock = Path(str(index) + ".lock")
        owned = False
        try:
            with open(lock, "xb") as stream:
                owned = True
                latest = git.context()
                if any(latest[k] != context[k] for k in ("head", "branch", "indexHash")):
                    raise SafetyError("Git changed before index recovery.")
                if journal["indexExisted"]:
                    stream.write(store.get(journal["indexBackupHash"]))
                stream.flush()
                os.fsync(stream.fileno())
            if journal["indexExisted"]:
                os.replace(lock, index)
            else:
                index.unlink(missing_ok=True)
                lock.unlink()
            owned = False
            fsync_directory(index.parent)
            journal["phase"] = "rolledBack"
            store.journal(journal)
        except Exception:
            journal["phase"] = "recoveryRequired"
            store.journal(journal)
            raise
        finally:
            if owned:
                lock.unlink(missing_ok=True)

    def push_review(self) -> dict:
        info = self.core.git.upstream()
        context = self.core.git.context()
        commits = self.core.git.text("log", "--format=%h %s", "@{upstream}..HEAD", allow_failure=True)
        return {"context": context, **info, "commits": commits.splitlines()}

    def push(self, review: dict) -> None:
        git = self.core.git
        if not same_context(review["context"], git.context()):
            raise SafetyError("Git state changed after push review.")
        if git.upstream() != {k: review[k] for k in ("remote", "branch", "localRef", "destinationHash")}:
            raise SafetyError("Push destination changed after review.")
        git.run("-c", "push.followTags=false", "-c", "push.recurseSubmodules=no", "push",
                "--no-follow-tags", "--recurse-submodules=no", "--", review["remote"],
                f"{review['context']['head']}:{review['branch']}", timeout=120)

"""Reviewed restore transactions. Never use git reset, clean, checkout, or stash."""
from __future__ import annotations

import os
import sys
import time
from typing import Callable

from .common import (ABSENT, SafetyError, digest,
                     new_id, safe_path, utc_now)
from .git import same_context
from .snapshots import Policy, SnapshotService
from .restore_io import RestoreFiles


def unfinished(journal: dict) -> bool:
    return journal["phase"] not in ("complete", "rolledBack", "resolved")


class RestoreService:
    def __init__(self, core: SnapshotService, dirty: Callable[[], set[str]] | None = None,
                 fault: Callable[[str, str], None] | None = None):
        self.core = core
        self.dirty = dirty or (lambda: set())
        self.fault = fault or (lambda phase, name: None)
        self.plans: dict[str, dict] = {}
        self.files = RestoreFiles(core, self.guard, self.fault)

    def guard(self, names: list[str]) -> None:
        if set(names) & self.dirty():
            raise SafetyError("Save or otherwise resolve unsaved edits in the affected files first.")

    def prepare(self, target_id: str, *, only: set[str] | None = None) -> dict:
        store, git = self.core.store, self.core.git
        store.assert_writer()
        if any(unfinished(j) for j in store.journals()):
            raise SafetyError("Review incomplete operations before restoring.")
        context, target = git.context(), store.snapshot(target_id, verify=True)
        if context["busy"] or context["sparse"] or target["context"]["branch"] != context["branch"]:
            raise SafetyError("Restore requires the same branch, no sparse checkout, and no Git operation.")
        if not context["branch"] and context["head"] != target["context"]["head"]:
            raise SafetyError("Cross-commit restore in detached HEAD is not supported.")
        # Every manifest carries previously known paths, including verified absences.
        latest = self.core.latest()
        managed = {p for p, e in (latest["entries"] if latest else {}).items() if e["state"] != "excluded"}
        policy, listed = Policy(self.core), set(git.files())
        names = (set(target["entries"]) | managed) if only is None else only
        if os.name == "nt" or sys.platform == "darwin":
            folded = {}
            for name in names:
                key = name.lower()
                if key in folded and folded[key] != name:
                    raise SafetyError("Case-colliding or case-only rename restores are not supported.")
                folded[key] = name
        changes, backup, skipped = [], {}, []
        for name in sorted(names):
            desired = target["entries"].get(name, ABSENT)
            reason = policy.exclusion(name)
            if desired["state"] == "present" and reason in ("not-regular-file", "unsafe-or-inaccessible-path"):
                raise SafetyError("A target file conflicts with a directory, link, or inaccessible path.")
            if desired["state"] == "excluded" or reason:
                skipped.append({"path": name, "reason": reason or "excluded-by-snapshot"})
                continue
            if name not in listed:
                try:
                    safe_path(git.root, name).lstat()
                    skipped.append({"path": name, "reason": "git-ignore-or-unmanaged"})
                    continue
                except FileNotFoundError:
                    pass
            before = self.core.read_entry(name, policy)
            if before["state"] == "excluded":
                raise SafetyError("An affected path cannot be safely backed up.")
            if before == desired:
                continue
            if desired["state"] == "present":
                store.get(desired["hash"], desired["size"])
            changes.append({"path": name, "before": before, "after": desired})
            backup[name] = before
        names_lower = set()
        for change in changes:
            safe_path(git.root, change["path"])
            lower = change["path"].lower()
            if (os.name == "nt" or sys.platform == "darwin") and lower in names_lower:
                raise SafetyError("Case-colliding restore paths are not supported.")
            names_lower.add(lower)
        self.guard([c["path"] for c in changes])
        saved = self.core.backup(backup, context)
        if not same_context(context, git.context()):
            raise SafetyError("Git state changed while preparing restore.")
        plan = {"schema": 1, "id": new_id(), "targetId": target_id, "backupId": saved["id"],
                "context": context, "changes": changes, "skipped": skipped}
        plan["planHash"] = digest(plan)
        self.plans[plan["id"]] = plan
        return plan

    def apply(self, plan_id: str, *, kind: str = "restore", undoes: str | None = None) -> dict:
        if plan_id not in self.plans:
            raise SafetyError("Restore review expired. Prepare a new plan.")
        plan = self.plans.pop(plan_id)
        if digest({k: v for k, v in plan.items() if k != "planHash"}) != plan["planHash"]:
            raise SafetyError("Restore plan failed integrity verification.")
        store, git = self.core.store, self.core.git
        store.assert_writer()
        self.guard([c["path"] for c in plan["changes"]])
        if not same_context(plan["context"], git.context()):
            raise SafetyError("Git state changed after review. Prepare a new restore plan.")
        store.snapshot(plan["backupId"], verify=True)
        store.snapshot(plan["targetId"], verify=True)
        policy = Policy(self.core)
        for change in plan["changes"]:
            if not self.core.matches(change["path"], change["before"], policy):
                raise SafetyError("Files changed after review. Prepare a new restore plan.")
        journal = {"schema": 1, "id": plan["id"], "kind": kind, "phase": "prepared",
                   "createdAt": utc_now(), "backupId": plan["backupId"], "targetId": plan["targetId"],
                   "planHash": plan["planHash"], "context": plan["context"], "changes": plan["changes"],
                   "applied": [], "conflicts": [], "undoes": undoes}
        store.journal(journal)
        try:
            journal["phase"] = "applying"
            store.journal(journal)
            checked_context = 0.0
            for change in plan["changes"]:
                self.guard([c["path"] for c in plan["changes"]])
                if time.monotonic() - checked_context > 0.25:
                    if not same_context(plan["context"], git.context()):
                        raise SafetyError("Git state changed during restore.")
                    checked_context = time.monotonic()
                if not self.core.matches(change["path"], change["before"], policy):
                    raise SafetyError("Concurrent file modification detected.")
                journal["inFlight"] = change["path"]
                store.journal(journal)
                self.fault("beforeWrite", change["path"])
                self.files.write(journal, change["path"], change["after"], change["before"], policy)
                self.fault("afterWrite", change["path"])
                if not self.core.matches(change["path"], change["after"], policy):
                    raise SafetyError("Restored file failed verification.")
                journal["applied"].append(change["path"])
                journal.pop("inFlight", None)
                store.journal(journal)
            if not same_context(plan["context"], git.context()):
                raise SafetyError("Git state changed during restore.")
            for change in plan["changes"]:
                if not self.core.matches(change["path"], change["after"], policy):
                    raise SafetyError("Restored file changed before final verification.")
            if self.files.cleanup(journal):
                raise SafetyError("Files changed during restore. Preserved files require recovery review.")
            journal["phase"] = "complete"
            store.journal(journal)
            self.core.changed()
            return journal
        except Exception:
            self.rollback(journal)
            raise

    def rollback(self, journal: dict) -> None:
        candidates = set(journal["applied"])
        if journal.get("inFlight"):
            candidates.add(journal["inFlight"])
        conflicts = set()
        policy = Policy(self.core)
        if not same_context(journal["context"], self.core.git.context()):
            conflicts.update(candidates)
            conflicts.update(move["path"] for move in journal.get("fileMoves", []))
        else:
            conflicts.update(self.files.recover_missing(journal))
            for change in reversed(journal["changes"]):
                if change["path"] not in candidates or change["path"] in conflicts:
                    continue
                try:
                    self.guard([change["path"]])
                    if self.core.matches(change["path"], change["before"], policy):
                        continue
                    if not self.core.matches(change["path"], change["after"], policy):
                        conflicts.add(change["path"])
                        continue
                    self.files.write(journal, change["path"], change["before"], change["after"], policy)
                except (OSError, SafetyError):
                    conflicts.add(change["path"])
            conflicts.update(self.files.recover_missing(journal))
        conflicts = self.files.cleanup(journal, preserve=conflicts)
        journal["conflicts"] = sorted(set(conflicts))
        journal["phase"] = "recoveryRequired" if conflicts else "rolledBack"
        journal.pop("inFlight", None)
        self.core.store.journal(journal)
        self.core.changed()

    def report(self, journal: dict) -> list[dict]:
        self.core.store.snapshot(journal["backupId"], verify=True)
        result = []
        policy = Policy(self.core)
        for change in journal["changes"]:
            status = "conflict"
            try:
                if self.core.matches(change["path"], change["before"], policy):
                    status = "before"
                elif self.core.matches(change["path"], change["after"], policy):
                    status = "after"
            except (OSError, SafetyError):
                pass
            result.append({"path": change["path"], "status": status})
        return result

    def prepare_undo(self) -> tuple[dict, str]:
        journals = self.core.store.journals()
        original = next((j for j in journals if j["kind"] == "restore" and j["phase"] == "complete"
                         and not any(u.get("undoes") == j["id"] and u["phase"] == "complete" for u in journals)), None)
        if original is None:
            raise SafetyError("No completed restore is available to undo.")
        plan = self.prepare(original["backupId"], only={c["path"] for c in original["changes"]})
        return plan, original["id"]

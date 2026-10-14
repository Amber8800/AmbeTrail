"""Policy, stable disk observations, and independent snapshot manifests."""
from __future__ import annotations

import fnmatch
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .common import (ABSENT, SafetyError, digest, metadata_is_link, new_id, relative_path,
                     safe_path, signature, utc_now)
from .git import Git, same_context
from .store import Store, protect_backup

SENSITIVE = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa", "id_ed25519", ".npmrc", ".pypirc")


def glob_match(name: str, pattern: str) -> bool:
    # Documented fnmatch semantics: slash-free globs match basenames; path globs match paths.
    candidate = name if "/" in pattern else name.rsplit("/", 1)[-1]
    if os.name == "nt":
        candidate, pattern = candidate.lower(), pattern.lower()
    return fnmatch.fnmatchcase(candidate, pattern)


class Policy:
    def __init__(self, core: SnapshotService):
        self.root, self.storage, self.config = core.git.root, core.store.directory, core.config
        self.submodules = core.git.submodules()
        self._nested: dict[Path, bool] = {}
        self.observations: dict[str, tuple[Path, os.stat_result]] = {}

    def exclusion(self, name: str) -> str | None:
        relative_path(name)
        parts = name.split("/")
        if any(p.startswith(".dev-snapshot-") for p in parts):
            return "internal-temporary"
        if any(glob_match(name, p) for p in self.config["exclude"]):
            return "user-exclusion"
        if any(glob_match(name, p) for p in SENSITIVE) and not any(glob_match(name, p) for p in self.config["allowSensitiveFiles"]):
            return "sensitive-name"
        if any(name == s or name.startswith(s + "/") for s in self.submodules):
            return "submodule"
        parent = self.root
        for part in parts[:-1]:
            parent /= part
            if parent not in self._nested:
                self._nested[parent] = (parent / ".git").exists() or (parent / "pyvenv.cfg").exists()
            if self._nested[parent]:
                return "nested-repository-or-python-environment"
        try:
            target = safe_path(self.root, name)
            if target.is_relative_to(self.storage):
                return "extension-storage"
            metadata = target.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata_is_link(metadata):
                return "not-regular-file"
            if metadata.st_size > self.config["maxFileMiB"] * 1024 * 1024:
                return "file-size-limit"
            self.observations[name] = (target, metadata)
        except FileNotFoundError:
            return None
        except (OSError, SafetyError):
            return "unsafe-or-inaccessible-path"
        return None


class SnapshotService:
    def __init__(self, git: Git, store: Store, config: dict):
        self.git, self.store, self.config = git, store, config
        self.generation = 0
        self.cache: dict[str, tuple[tuple, dict]] = {}
        self.cancelled = threading.Event()
        self.progress = lambda completed, total: None

    def changed(self) -> None:
        self.generation += 1

    def read_entry(self, name: str, policy: Policy | None = None, *, full: bool = True,
                   persist: bool = True) -> dict:
        policy = policy or Policy(self)
        reason = policy.exclusion(name)
        if reason:
            return {"state": "excluded", "reason": reason}
        observed = policy.observations.get(name)
        target = observed[0] if observed else safe_path(self.git.root, name)
        try:
            before = observed[1] if observed else target.lstat()
            sig = signature(before)
            if not full and name in self.cache and self.cache[name][0] == sig and self.store._object(self.cache[name][1]["hash"]).exists():
                return self.cache[name][1]
            fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or signature(opened) != sig:
                    raise SafetyError("File changed while being read.")
                data = stream.read(int(self.config["maxFileMiB"] * 1024 * 1024) + 1)
                if len(data) > self.config["maxFileMiB"] * 1024 * 1024 or signature(os.fstat(stream.fileno())) != sig:
                    raise SafetyError("File changed while being read.")
            if signature(target.lstat()) != sig:
                raise SafetyError("File changed while being read.")
            from .common import sha
            entry = {"state": "present", "hash": self.store.put(data) if persist else sha(data),
                     "size": len(data), "executable": os.name != "nt" and bool(before.st_mode & 0o111)}
            if persist:
                self.cache[name] = (sig, entry)
            return entry
        except FileNotFoundError:
            self.cache.pop(name, None)
            return dict(ABSENT)

    def latest(self) -> dict | None:
        header = next((s for s in self.store.snapshots() if s["reason"] != "backup"), None)
        return self.store.snapshot(header["id"]) if header else None

    def capture(self, reason: str = "manual", *, full: bool = True,
                changed_paths: set[str] | None = None) -> dict | None:
        self.store.assert_writer()
        self.store.max_bytes = int(self.config["maxStorageMiB"] * 1024 * 1024)
        previous = self.latest()
        for attempt in range(3):
            if self.cancelled.is_set():
                raise SafetyError("Capture cancelled before publication.")
            started, generation = utc_now(), self.generation
            context = self.git.context()
            if context["busy"] or context["sparse"]:
                raise SafetyError("Recording deferred: Git is busy or sparse checkout is enabled.")
            policy, listed = Policy(self), self.git.files()
            listed_set = set(listed)
            names = sorted(listed_set | set(previous["entries"] if previous else {}))
            entries, observed = {}, {}
            same_epoch = previous and all(context[k] == previous["context"][k] for k in ("head", "branch"))
            incremental = changed_paths is not None and same_epoch and not full
            affected = set()
            if incremental:
                affected = {p for p in names if p in changed_paths or any(p.startswith(changed + "/") for changed in changed_paths)}
            try:
                to_read = []
                for name in names:
                    relative_path(name)
                    if name not in listed_set:
                        try:
                            safe_path(self.git.root, name).lstat()
                            entries[name] = {"state": "excluded", "reason": "git-ignore-or-no-longer-managed"}
                        except FileNotFoundError:
                            entries[name] = dict(ABSENT)
                        except (SafetyError, OSError):
                            entries[name] = {"state": "excluded", "reason": "unsafe-or-inaccessible-path"}
                        continue
                    if incremental and name not in affected and name in previous["entries"]:
                        entries[name] = previous["entries"][name]
                        continue
                    to_read.append(name)
                def read_one(name):
                    if self.cancelled.is_set():
                        raise SafetyError("Capture cancelled before publication.")
                    return name, self.read_entry(name, policy, full=full)
                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="snapshot-read") as workers:
                    for completed, (name, entry) in enumerate(workers.map(read_one, to_read), 1):
                        entries[name] = entry
                        if entry["state"] == "present":
                            observed[name] = self.cache[name][0]
                        if completed % 50 == 0 or completed == len(to_read):
                            self.progress(completed, len(to_read))
                if generation != self.generation or not same_context(context, self.git.context()) or listed != self.git.files():
                    continue
                if any(signature((self.git.root / p).lstat()) != sig for p, sig in observed.items()):
                    continue
                if generation != self.generation:
                    continue
                if self.cancelled.is_set():
                    raise SafetyError("Capture cancelled before publication.")
                new_epoch = not previous or any(context[k] != previous["context"][k] for k in ("head", "branch"))
                tree_hash = digest(entries)
                if not new_epoch and previous["treeHash"] == tree_hash:
                    return None
                record_id, epoch_id = new_id(), new_id() if new_epoch else previous["epochId"]
                if new_epoch:
                    changes = [s["path"] for s in self.git.statuses()]
                    if not same_context(context, self.git.context()) or generation != self.generation:
                        continue
                    self.store.write_record("epochs", epoch_id, {
                        "schema": 1, "id": epoch_id, "branch": context["branch"], "head": context["head"],
                        "initialSnapshot": record_id, "initialChanges": changes})
                snapshot = {"schema": 1, "id": record_id, "repositoryId": self.store.identity["id"],
                            "epochId": epoch_id, "previousSnapshotId": previous["id"] if previous else None,
                            "startedAt": started, "endedAt": utc_now(), "reason": "initial" if new_epoch else reason,
                            "context": context, "policyHash": digest(self.config), "treeHash": tree_hash, "entries": entries}
                changes = changes_between(previous["entries"] if previous else {}, entries)
                snapshot["summary"] = summarize(changes)
                snapshot["changedFiles"] = len(changes)
                self.store.save_snapshot(snapshot)
                return snapshot
            except (FileNotFoundError, SafetyError):
                if attempt == 2 or self.cancelled.is_set():
                    raise
        raise SafetyError("Recording delayed: files or Git state kept changing during capture.")

    def backup(self, entries: dict, context: dict) -> dict:
        previous = self.latest()
        snapshot = {"schema": 1, "id": new_id(), "repositoryId": self.store.identity["id"],
                    "epochId": previous["epochId"] if previous else new_id(),
                    "previousSnapshotId": previous["id"] if previous else None,
                    "startedAt": utc_now(), "endedAt": utc_now(), "reason": "backup", "context": context,
                    "policyHash": digest(self.config), "treeHash": digest(entries), "entries": entries}
        self.store.save_snapshot(snapshot)
        protect_backup(self.store, snapshot["id"])
        return snapshot

    def matches(self, name: str, expected: dict, policy: Policy | None = None) -> bool:
        if expected["state"] == "excluded":
            return False
        return self.read_entry(name, policy, persist=False) == expected


def changes_between(before: dict, after: dict) -> list[dict]:
    changes = []
    for name in sorted(set(before) | set(after)):
        a, b = before.get(name, ABSENT), after.get(name, ABSENT)
        if a["state"] == "excluded" or b["state"] == "excluded" or a == b:
            continue
        kind = "added" if a["state"] == "absent" else "deleted" if b["state"] == "absent" else "modified"
        changes.append({"path": name, "kind": kind, "before": a, "after": b})
    return changes


def summarize(changes: list[dict]) -> str:
    if not changes:
        return "작업 시작 상태 · 파일 내용 변경 없음"
    roots = sorted({c["path"].split("/")[0] if "/" in c["path"] else "루트" for c in changes})
    counts = [(label, sum(c["kind"] == kind for c in changes)) for kind, label in (("added", "추가"), ("modified", "수정"), ("deleted", "삭제"))]
    return "·".join(roots[:2]) + (" 외" if len(roots) > 2 else "") + " · " + ", ".join(f"{label} {n}" for label, n in counts if n)


def suggest_commit(paths: list[str]) -> str:
    docs = all(p.endswith((".md", ".rst", ".txt")) or p.startswith("docs/") for p in paths)
    tests = all("test" in p.split("/") or "tests" in p.split("/") or ".test." in p or ".spec." in p for p in paths)
    return f"{'docs' if docs else 'test' if tests else 'chore'}: update project files"

"""Policy, stable disk observations, and independent snapshot manifests."""
from __future__ import annotations

import fnmatch
import os
import re
import stat
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .common import (ABSENT, SafetyError, digest, metadata_is_link, new_id, relative_path,
                     safe_path, sha, signature, utc_now)
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

    def head_entries(self, head: str | None, paths: list[str], *, persist: bool = True) -> dict:
        """Read only the managed startup changes from the exact recorded HEAD."""
        tree = self.git.tree_entries(head) if paths else {}
        entries = {}
        for name in paths:
            if persist and self.cancelled.is_set():
                raise SafetyError("Capture cancelled before publication.")
            relative_path(name)
            if name not in tree:
                entries[name] = dict(ABSENT)
                continue
            mode, oid = tree[name]
            if mode not in ("100644", "100755"):
                raise SafetyError("An initial comparison baseline contains an unsupported Git file type.")
            size = int(self.git.text("cat-file", "-s", oid))
            if size > self.config["maxFileMiB"] * 1024 * 1024:
                raise SafetyError("An initial comparison baseline exceeds the configured file limit.")
            data = self.git.run("cat-file", "blob", oid)
            if len(data) != size:
                raise SafetyError("An initial comparison baseline could not be read completely.")
            entries[name] = {"state": "present", "hash": self.store.put(data) if persist else sha(data),
                             "size": size, "executable": os.name != "nt" and mode == "100755"}
        return entries

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
                initial_changes = None
                if new_epoch:
                    statuses = self.git.statuses()
                    changes = sorted({name for s in statuses for name in (s["path"], s.get("original", s["path"]))})
                    initial_changes = [name for name in changes if name in entries and entries[name]["state"] != "excluded"]
                    initial_before = self.head_entries(context["head"], initial_changes)
                    initial_diff = changes_between(initial_before, {name: entries[name] for name in initial_changes})
                    initial_changes = [c["path"] for c in initial_diff]
                    initial_before = {name: initial_before[name] for name in initial_changes}
                    if self.cancelled.is_set():
                        raise SafetyError("Capture cancelled before publication.")
                    if not same_context(context, self.git.context()) or generation != self.generation:
                        continue
                    self.store.write_record("epochs", epoch_id, {
                        "schema": 1, "id": epoch_id, "branch": context["branch"], "head": context["head"],
                        "initialSnapshot": record_id, "initialChanges": initial_changes})
                snapshot = {"schema": 1, "id": record_id, "repositoryId": self.store.identity["id"],
                            "epochId": epoch_id, "previousSnapshotId": previous["id"] if previous else None,
                            "startedAt": started, "endedAt": utc_now(), "reason": "initial" if new_epoch else reason,
                            "context": context, "policyHash": digest(self.config), "treeHash": tree_hash, "entries": entries}
                changes = changes_between(previous["entries"] if previous else {}, entries)
                if initial_changes is not None:
                    snapshot["initialChanges"] = initial_changes
                    snapshot["initialBefore"] = initial_before
                    changes = initial_diff
                snapshot["summary"] = snapshot_summary(changes)
                snapshot["summaryVersion"] = 3
                snapshot["changedFiles"] = len(initial_changes) if initial_changes is not None else len(changes)
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


def snapshot_summary(changes: list[dict]) -> str:
    """Label one capture using its actual changed paths, independent of Git commit boundaries."""
    if not changes:
        return "파일 변경 없음"
    names = [c["path"].rsplit("/", 1)[-1] for c in changes]
    repeated = {name for name, count in Counter(names).items() if count > 1}
    labels = []
    for change, name in zip(changes, names, strict=True):
        if name in repeated and "/" in change["path"]:
            name = "/".join(change["path"].split("/")[-2:])
        name = " ".join(name.split())
        labels.append(name if len(name) <= 28 else name[:27] + "…")
    kinds = {c["kind"] for c in changes}
    action = {"added": "추가", "modified": "수정", "deleted": "삭제"}.get(next(iter(kinds)), "변경") if len(kinds) == 1 else "변경"
    return "·".join(labels[:2]) + (f" 외 {len(labels) - 2}개" if len(labels) > 2 else "") + " " + action


def summarize(changes: list[dict]) -> str:
    if not changes:
        return "작업 시작 상태"
    areas: dict[str, int] = {}
    for change in changes:
        area = work_area(change["path"])
        areas[area] = areas.get(area, 0) + 1
    ordered = sorted(areas, key=lambda area: (-areas[area], area))
    kinds = {c["kind"] for c in changes}
    action = {"added": "추가", "modified": "수정", "deleted": "삭제"}.get(next(iter(kinds)), "변경") if len(kinds) == 1 else "변경"
    return "·".join(ordered[:2]) + (" 외" if len(ordered) > 2 else "") + " " + action


def work_area(name: str) -> str:
    """Describe named files/areas only; do not infer feature completion from contents."""
    lower = name.lower()
    tokens = set(re.split(r"[/_.-]+", lower))
    if tokens & {"test", "tests", "testing", "spec"}:
        return "테스트"
    if lower.endswith((".md", ".rst")) or "docs" in tokens:
        return "문서"
    if lower.startswith(".github/"):
        return "CI"
    for keywords, label in (
        ({"restore", "recovery"}, "복원"),
        ({"commit", "commits"}, "커밋"),
        ({"extension", "timeline", "documents", "ui", "components"}, "UI"),
        ({"bridge", "rpc", "protocol"}, "코어 통신"),
        ({"auth", "login", "authentication"}, "인증"),
        ({"api", "router", "routes"}, "API"),
        ({"git"}, "Git"),
        ({"css", "scss", "styles"}, "스타일"),
        ({"config", "settings", "package", "tsconfig", "pyproject", "esbuild"}, "설정"),
        ({"snapshot", "snapshots", "store"}, "스냅샷 저장"),
    ):
        if tokens & keywords:
            return label
    return name.rsplit("/", 1)[-1]


def suggest_commit(paths: list[str], title: str = "") -> str:
    docs = all(p.endswith((".md", ".rst", ".txt")) or p.startswith("docs/") for p in paths)
    tests = all(work_area(p) == "테스트" for p in paths)
    description = title or summarize([{"path": p, "kind": "modified"} for p in paths])
    return f"{'docs' if docs else 'test' if tests else 'chore'}: {description}"

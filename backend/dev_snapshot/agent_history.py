"""Opt-in, bounded Codex reports. Never a source of snapshot or Git evidence."""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import stat
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .common import (SafetyError, atomic_write, digest, encode, is_link, metadata_is_link,
                     new_id, sha, utc_now, validate_hash, validate_id)

MAX_INPUT = 256 * 1024
MAX_RECORD = 16 * 1024
MAX_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 500
RETENTION_DAYS = 30
SECRET = re.compile(
    r"-----BEGIN [\w ]*PRIVATE KEY-----|\b(?:sk-[\w-]{16,}|gh[pousr]_[\w]{20,}|"
    r"github_pat_[\w]{20,}|AKIA[A-Z0-9]{16})\b|"
    r"\b(?:api[_ -]?key|password|passwd|secret|access[_ -]?token|authorization)\s*[:=]\s*\S+",
    re.IGNORECASE,
)


def small_json(path: Path) -> dict:
    metadata = path.lstat()
    if metadata_is_link(metadata) or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_RECORD:
        raise SafetyError("Invalid agent metadata.")
    with path.open("rb") as stream:
        data = stream.read(MAX_RECORD + 1)
    value = json.loads(data)
    if len(data) > MAX_RECORD or not isinstance(value, dict) or value.get("schema") != 1:
        raise SafetyError("Invalid agent metadata.")
    return value


def identifier(value) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or any(ord(c) < 33 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise SafetyError("Missing or invalid Codex identifier.")
    return value


def excerpt(text: str) -> tuple[str, str] | None:
    # Examine the entire bounded message before discarding code and truncating it.
    # This is deliberately conservative, not a guarantee that all secrets are detected.
    if SECRET.search(text):
        return None
    text = re.sub(r"```[\s\S]*?(?:```|$)", "", text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:#{1,6}\s*|[-*+]\s+|\d+[.)]\s+|>\s*)", "", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = " ".join("".join(c for c in line if ord(c) >= 32).replace("`", "").split())
        if line:
            lines.append(line)
    if not lines:
        return None
    title, description = lines[0], "\n".join(lines[1:])
    return (title if len(title) <= 100 else title[:99] + "…",
            description if len(description) <= 700 else description[:699] + "…")


class AgentHistory:
    def __init__(self, directory: Path, identity: dict, *, readonly: bool = False):
        self.directory, self.identity = directory, identity
        self.readonly = readonly
        self.enabled = False
        self.present = False
        self.problem = ""
        try:
            self.present = directory.exists()
        except OSError:
            self.problem = "에이전트 저장소 접근 불가 · 파일 기록은 계속됩니다."
        self._signature = None
        self._cache: list[dict] = []
        self._next_poll = 0.0
        self._directory_stamp = None
        self._scan_after = 0.0
        self._prune_after = 0.0
        self._status_stamp = None
        if self.present:
            try:
                self.enabled = self.policy().get("enabled") is True
            except (OSError, ValueError, RecursionError, SafetyError):
                self.problem = "에이전트 메타데이터 확인 필요 · 파일 기록은 계속됩니다."

    def _check(self) -> None:
        if any(is_link(p) for p in (self.directory, self.directory.parent, self.directory.parent.parent)):
            raise SafetyError("Links are not allowed in agent history storage.")

    @contextlib.contextmanager
    def lock(self):
        if self.readonly:
            raise SafetyError("Agent history is read-only.")
        self._check()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.directory / "writer.lock"
        if is_link(target):
            raise SafetyError("Invalid agent history lock.")
        with target.open("a+b") as stream:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b" ")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise SafetyError("Agent history is busy. Try again.") from error
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def policy(self) -> dict:
        self._check()
        value = small_json(self.directory / "consent.json")
        if (value.get("repositoryId") != self.identity["id"] or value.get("root") != self.identity["root"]
                or value.get("gitDir") != self.identity.get("gitDir")
                or type(value.get("enabled")) is not bool):
            raise SafetyError("Agent consent does not match this worktree.")
        validate_id(value.get("grant"))
        return value

    def consent(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise SafetyError("Invalid collection preference.")
        with self.lock():
            value = {"schema": 1, "repositoryId": self.identity["id"], "root": self.identity["root"],
                     "gitDir": self.identity.get("gitDir"), "enabled": enabled, "grant": new_id(), "updatedAt": utc_now()}
            atomic_write(self.directory / "consent.json", encode(value))
            self.present, self.enabled, self.problem = True, enabled, ""

    def setup(self, python: str, script: str) -> dict:
        policy = self.policy()
        if not policy["enabled"]:
            raise SafetyError("Approve collection for this repository first.")
        args = [python, "-I", "-S", script, "--storage", str(self.directory), "--grant", policy["grant"]]
        if (not isinstance(python, str) or not isinstance(script, str) or not Path(script).is_absolute()
                or not Path(script).is_file() or any("\n" in arg or "\r" in arg for arg in args)
                or python not in ("python", "python3", "py") and not Path(python).is_absolute()
                or os.name == "nt" and any(re.search(r"[&|<>^%!\"]", arg) for arg in args)):
            raise SafetyError("The hook command requires safe executable and script paths.")
        return {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": shlex.join(args),
                 "commandWindows": subprocess.list2cmdline(args), "timeout": 2}]}]}}

    def _files(self) -> list[tuple[Path, os.stat_result]]:
        self._check()
        directory = self.directory / "messages"
        if is_link(directory):
            raise SafetyError("Invalid agent message directory.")
        result = []
        try:
            with os.scandir(directory) as sources:
                for source in sources:
                    if len(result) >= MAX_RECORDS + 1 or not re.fullmatch(r"[0-9a-f]{64}\.json", source.name):
                        raise SafetyError("Agent history contains unexpected files.")
                    metadata = source.stat(follow_symlinks=False)
                    if metadata_is_link(metadata) or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_RECORD:
                        raise SafetyError("Invalid agent message file.")
                    result.append((Path(source.path), metadata))
        except FileNotFoundError:
            pass
        return result

    def _prune(self, incoming: int = 0) -> None:
        files = sorted(self._files(), key=lambda item: item[1].st_mtime_ns)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).timestamp()
        total = sum(metadata.st_size for _, metadata in files)
        count = len(files)
        for path, metadata in files:
            if metadata.st_mtime < cutoff or count + bool(incoming) > MAX_RECORDS or total + incoming > MAX_BYTES:
                path.unlink()
                total -= metadata.st_size
                count -= 1
        if total + incoming > MAX_BYTES:
            raise SafetyError("Agent history quota reached.")

    def collect(self, event: dict, grant: str) -> bool:
        # The hook never examines prompts, transcripts, reasoning, or tool logs.
        if not isinstance(event, dict) or event.get("hook_event_name") != "Stop":
            return False
        text = event.get("last_assistant_message")
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_INPUT:
            return False
        cleaned = excerpt(text)
        if not cleaned:
            return False
        session, turn = identifier(event.get("session_id")), identifier(event.get("turn_id"))
        given_cwd = event.get("cwd")
        if not isinstance(given_cwd, str) or not Path(given_cwd).is_absolute():
            return False
        cwd = Path(given_cwd).resolve(strict=True)
        root = Path(self.identity["root"])
        if not cwd.is_dir() or not cwd.is_relative_to(root):
            return False
        # A nested repo/worktree must have its own approval, even inside an approved root.
        for parent in (cwd, *cwd.parents):
            if parent == root:
                break
            if (parent / ".git").exists():
                return False
        record_id = digest([self.identity["id"], grant, session, turn, "Stop"])
        with self.lock():
            policy = self.policy()
            if not policy["enabled"] or policy["grant"] != grant:
                return False
            destination = self.directory / "messages" / (record_id + ".json")
            if destination.exists():
                return False  # First completed report wins; never rewrite a retry into a new fact.
            record = {"schema": 1, "id": record_id, "repositoryId": self.identity["id"],
                      "worktreeId": digest([self.identity["root"], self.identity.get("gitDir")]),
                      "agent": "codex", "source": "codex-stop", "phase": "final_answer",
                      "kind": "final_report", "sessionId": session, "turnId": turn,
                      "occurredAt": None, "collectedAt": utc_now(), "title": cleaned[0],
                      "description": cleaned[1], "contentHash": sha(text.encode("utf-8")),
                      "association": {"status": "unlinked", "evidence": []}}
            content = encode(record)
            self._prune(len(content))
            atomic_write(destination, content)
            return True

    def _validate(self, value: dict) -> dict:
        validate_hash(value.get("id"))
        validate_hash(value.get("contentHash"))
        if (value.get("repositoryId") != self.identity["id"] or value.get("agent") != "codex"
                or value.get("source") != "codex-stop" or value.get("phase") != "final_answer"
                or value.get("kind") != "final_report" or value.get("occurredAt") is not None):
            raise SafetyError("Unsupported agent report.")
        if value.get("worktreeId") != digest([self.identity["root"], self.identity.get("gitDir")]):
            raise SafetyError("Agent report belongs to another worktree.")
        identifier(value.get("sessionId"))
        identifier(value.get("turnId"))
        if datetime.fromisoformat(value["collectedAt"]).tzinfo is None:
            raise SafetyError("Agent collection time must include a timezone.")
        for key, limit in (("title", 100), ("description", 700)):
            text = value.get(key)
            if not isinstance(text, str) or len(text) > limit or SECRET.search(text) or any(ord(c) < 32 and c != "\n" for c in text):
                raise SafetyError("Invalid agent report text.")
        association = value.get("association")
        if not isinstance(association, dict) or association.get("status") not in ("confirmed", "uncertain", "unlinked"):
            raise SafetyError("Invalid agent association.")
        evidence = association.get("evidence")
        if not isinstance(evidence, list) or len(evidence) > 20:
            raise SafetyError("Invalid agent evidence.")
        if association["status"] == "confirmed" and not evidence:
            raise SafetyError("Missing reviewed code evidence.")
        if association["status"] == "confirmed" and association.get("method") != "user-reviewed-code":
            raise SafetyError("Unsupported association evidence.")
        for item in evidence:
            validate_id(item["snapshotId"])
            validate_hash(item["treeHash"])
        return value

    def records(self) -> list[dict]:
        if not self.present:
            return []
        try:
            self._check()
            directory = self.directory / "messages"
            if is_link(directory):
                raise SafetyError("Invalid agent message directory.")
            try:
                metadata = directory.stat()
                stamp = (metadata.st_mtime_ns, metadata.st_ctime_ns)
            except FileNotFoundError:
                stamp = None
            if stamp == self._directory_stamp and time.monotonic() < self._scan_after:
                return self._cache
            files = self._files()
            if sum(s.st_size for _, s in files) > MAX_BYTES:
                raise SafetyError("Agent history quota reached.")
            sig = tuple(sorted((p.name, s.st_size, s.st_mtime_ns, s.st_ctime_ns) for p, s in files))
            if sig != self._signature:
                result = []
                cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
                for path, _ in files:
                    try:
                        value = self._validate(small_json(path))
                        if value["id"] != path.stem:
                            raise SafetyError("Agent report identity mismatch.")
                        if datetime.fromisoformat(value["collectedAt"]) >= cutoff:
                            result.append(value)
                    except (OSError, ValueError, KeyError, TypeError, RecursionError, SafetyError):
                        self.problem = "일부 에이전트 기록을 읽지 못했습니다. 파일 기록은 유지됩니다."
                self._cache = sorted(result, key=lambda r: (r["collectedAt"], r["id"]), reverse=True)
                self._signature = sig
            self._directory_stamp = stamp
            self._scan_after = time.monotonic() + 60
            return self._cache
        except (OSError, ValueError, RecursionError, SafetyError):
            self.problem = "에이전트 기록을 읽지 못했습니다. 파일 기록은 유지됩니다."
            return []

    def poll(self) -> bool:
        if not self.present or time.monotonic() < self._next_poll:
            return False
        self._next_poll = time.monotonic() + 2
        if not self.readonly and time.monotonic() >= self._prune_after:
            self._prune_after = time.monotonic() + 3600
            try:
                with self.lock():
                    self.policy()
                    self._prune()
            except (OSError, ValueError, RecursionError, SafetyError):
                self.problem = "에이전트 보존 정책 확인 필요 · 파일 기록은 계속됩니다."
        before, previous_problem = self._signature, self.problem
        self.records()
        try:
            status_path = self.directory / "hook-status.json"
            metadata = status_path.lstat()
            stamp = (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
            if stamp != self._status_stamp:
                status = small_json(status_path)
                hook_problem = "Codex 수집 오류 · 연동 설정을 확인하세요. 파일 기록은 계속됩니다."
                if status.get("state") == "collection-error":
                    self.problem = hook_problem
                elif status.get("state") == "ok" and self.problem == hook_problem:
                    self.problem = ""
                self._status_stamp = stamp
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RecursionError, SafetyError):
            self.problem = "Codex 수집 상태 확인 불가 · 파일 기록은 계속됩니다."
        return before != self._signature or previous_problem != self.problem

    def clear(self) -> None:
        with self.lock():
            # Revoke the generated command before removing only owned report files.
            atomic_write(self.directory / "consent.json", encode({"schema": 1, "repositoryId": self.identity["id"],
                         "root": self.identity["root"], "gitDir": self.identity.get("gitDir"),
                         "enabled": False, "grant": new_id(), "updatedAt": utc_now()}))
            for path, _ in self._files():
                path.unlink()
            (self.directory / "hook-status.json").unlink(missing_ok=True)
            self.enabled, self._cache, self._signature, self.problem = False, [], None, ""
            self._directory_stamp, self._scan_after = None, 0.0
            self._status_stamp = None

    def review_link(self, message_id: str, snapshot_ids: list[str], store) -> dict:
        from .snapshots import changes_between
        if store.identity["id"] != self.identity["id"]:
            raise SafetyError("Agent reports cannot be linked across worktrees.")
        validate_hash(message_id)
        self._check()
        if is_link(self.directory / "messages"):
            raise SafetyError("Invalid agent message directory.")
        record = self._validate(small_json(self.directory / "messages" / (message_id + ".json")))
        if (record["id"] != message_id or datetime.fromisoformat(record["collectedAt"]) < datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
                or not isinstance(snapshot_ids, list) or not 1 <= len(snapshot_ids) <= 20 or len(set(snapshot_ids)) != len(snapshot_ids)):
            raise SafetyError("Select a stored report and 1–20 snapshots.")
        evidence = []
        for snapshot_id in snapshot_ids:
            snapshot = store.snapshot(snapshot_id)
            if snapshot["reason"] == "backup":
                raise SafetyError("Backup snapshots cannot describe agent work.")
            if snapshot["reason"] == "initial":
                before = snapshot.get("initialBefore", {})
                after = {p: snapshot["entries"][p] for p in snapshot.get("initialChanges", [])}
            else:
                before = store.snapshot(snapshot["previousSnapshotId"])["entries"]
                after = snapshot["entries"]
            changes = changes_between(before, after)
            if not changes:
                raise SafetyError("The selected snapshot has no verifiable code changes.")
            evidence.append({"snapshotId": snapshot_id, "treeHash": snapshot["treeHash"],
                             "context": snapshot["context"], "changedPaths": [c["path"] for c in changes][:100],
                             "changedPathsHash": digest([c["path"] for c in changes]), "changedFiles": len(changes)})
        review = {"messageId": message_id, "title": record["title"], "evidence": evidence,
                  "recordHash": digest(record)}
        return {**review, "reviewHash": digest(review)}

    def link(self, message_id: str, snapshot_ids: list[str], review_hash: str, store) -> None:
        with self.lock():
            review = self.review_link(message_id, snapshot_ids, store)
            if review["reviewHash"] != review_hash:
                raise SafetyError("The reviewed report or snapshots changed. Review again.")
            path = self.directory / "messages" / (validate_hash(message_id) + ".json")
            record = self._validate(small_json(path))
            if digest(record) != review["recordHash"]:
                raise SafetyError("The report changed after review. Review again.")
            record["association"] = {"status": "confirmed", "method": "user-reviewed-code",
                                     "evidence": review["evidence"], "reviewedAt": utc_now()}
            content = encode(record)
            if len(content) > MAX_RECORD:
                raise SafetyError("Link evidence exceeds the metadata limit. Select fewer snapshots.")
            if sum(s.st_size for _, s in self._files()) - path.stat().st_size + len(content) > MAX_BYTES:
                raise SafetyError("Agent history quota reached. Delete older reports first.")
            metadata = path.stat()
            atomic_write(path, content)
            os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            self._signature = None
            self._directory_stamp, self._scan_after = None, 0.0

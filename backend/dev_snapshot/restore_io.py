"""Preserve the actual displaced file; publish replacements without overwriting new arrivals."""
from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath

from .common import (MAX_FILE, SafetyError, atomic_write, encode, fsync_directory, metadata_is_link,
                     native_path, new_id, relative_path, safe_path, sha, signature,
                     validate_entry, validate_id)


def validate_move(move: dict) -> None:
    if not isinstance(move, dict) or set(move) != {"id", "path", "before", "after"}:
        raise SafetyError("Invalid restore preservation record.")
    validate_id(move["id"])
    relative_path(move["path"])
    for entry in (move["before"], move["after"]):
        if validate_entry(entry)["state"] == "excluded":
            raise SafetyError("Excluded files cannot be preserved by a restore.")


class RestoreFiles:
    def __init__(self, core, guard, fault):
        self.core, self.guard, self.fault = core, guard, fault

    def relative_directory(self, move: dict) -> str:
        validate_move(move)
        return (PurePosixPath(move["path"]).parent / (".dev-snapshot-" + move["id"] + ".restore")).as_posix()

    def directory(self, move: dict) -> Path:
        return native_path(safe_path(self.core.git.root, self.relative_directory(move)))

    @staticmethod
    def read(source: Path) -> tuple[dict, bytes]:
        metadata = source.lstat()
        if metadata_is_link(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise SafetyError("A preserved restore file is no longer a regular file.")
        fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            if signature(os.fstat(stream.fileno())) != signature(metadata):
                raise SafetyError("A preserved file changed while being read.")
            data = stream.read(MAX_FILE + 1)
            if len(data) > MAX_FILE or signature(os.fstat(stream.fileno())) != signature(metadata):
                raise SafetyError("A preserved file changed or exceeds the supported size.")
        if signature(source.lstat()) != signature(metadata):
            raise SafetyError("A preserved file changed while being read.")
        return {"state": "present", "hash": sha(data), "size": len(data),
                "executable": os.name != "nt" and bool(metadata.st_mode & 0o111)}, data

    def write(self, journal: dict, name: str, desired: dict, expected: dict, policy) -> None:
        move = {"id": new_id(), "path": name, "before": expected, "after": desired}
        validate_move(move)
        self.guard([name])
        # Read and stage before the last comparison. No object read or temporary-file write
        # is allowed to widen the old compare-and-replace window.
        data = self.core.store.get(desired["hash"], desired["size"]) if desired["state"] == "present" else b""
        target = native_path(safe_path(self.core.git.root, name))
        target.parent.mkdir(parents=True, exist_ok=True)
        journal.setdefault("fileMoves", []).append(move)
        self.core.store.journal(journal)  # Persist paths before any displacement, including crash recovery.
        directory = self.directory(move)
        directory.mkdir(mode=0o700)
        atomic_write(directory / ".gitignore", b"*\n")
        staged, original = directory / "replacement", directory / "original"
        atomic_write(staged, data, 0o700 if desired.get("executable") else 0o600)
        # Fail before touching the work file on filesystems without hard-link support.
        try:
            os.link(staged, directory / "probe", follow_symlinks=False)
            (directory / "probe").unlink()
        except OSError as error:
            raise SafetyError("Safe restore publication requires hard-link support on this filesystem.") from error
        self.guard([name])
        if not self.core.matches(name, expected, policy):
            raise SafetyError("File changed before replacement; existing contents were preserved.")
        self.fault("beforeDisplace", name)
        if expected["state"] == "present":
            os.rename(target, original)
            fsync_directory(directory)
            fsync_directory(target.parent)
            if self.read(original)[0] != expected:
                raise SafetyError("Concurrent modification detected in the displaced file. Preserved files require recovery review.")
        self.fault("afterDisplace", name)
        self.guard([name])
        safe_path(self.core.git.root, name)
        if desired["state"] == "present":
            try:
                # link() is an atomic create-if-absent. A racing creator is never overwritten.
                os.link(staged, target, follow_symlinks=False)
            except FileExistsError as error:
                raise SafetyError("A new file appeared during restore. It was not overwritten; review preserved files.") from error
        self.fault("afterPublish", name)
        fsync_directory(target.parent)

    def recover_missing(self, journal: dict) -> set[str]:
        conflicts = set()
        # A failed rollback can displace a newer version of the same path.
        for move in reversed(journal.get("fileMoves", [])):
            try:
                directory = self.directory(move)
                original = directory / "original"
                if not original.exists():
                    continue
                entry, _ = self.read(original)
                if entry != move["before"]:
                    conflicts.add(move["path"])
                target = native_path(safe_path(self.core.git.root, move["path"]))
                self.guard([move["path"]])
                try:
                    os.link(original, target, follow_symlinks=False)
                    fsync_directory(target.parent)
                except FileExistsError:
                    pass  # Keep both the current path and the displaced version.
            except (OSError, SafetyError):
                conflicts.add(move["path"])
        return conflicts

    def cleanup(self, journal: dict, preserve: set[str] | None = None) -> set[str]:
        conflicts, retained = set(preserve or ()), []
        for move in journal.get("fileMoves", []):
            try:
                directory = self.directory(move)
                if not directory.exists():
                    continue
                if move["path"] in conflicts:
                    retained.append(move)
                    continue
                for filename, expected in (("original", move["before"]), ("replacement", move["after"]), ("probe", move["after"])):
                    source = directory / filename
                    if not source.exists():
                        continue
                    entry, data = self.read(source)
                    matches = entry == expected if filename == "original" or expected["state"] == "present" else data == b""
                    if not matches:
                        raise SafetyError("A preserved file changed during restore; recovery review is required.")
                for filename in ("original", "replacement", "probe", ".gitignore"):
                    (directory / filename).unlink(missing_ok=True)
                directory.rmdir()
                fsync_directory(directory.parent)
            except (OSError, SafetyError):
                conflicts.add(move["path"])
                retained.append(move)
        if retained:
            journal["fileMoves"] = retained
        else:
            journal.pop("fileMoves", None)
        return conflicts

    def preserved(self, journal: dict) -> list[dict]:
        return [{"path": move["path"], "directory": self.relative_directory(move)}
                for move in journal.get("fileMoves", []) if self.directory(move).exists()]

    def export(self, journal: dict, destination: Path) -> None:
        for move in journal.get("fileMoves", []):
            directory = self.directory(move)
            exported = destination / (".dev-snapshot-preserved-" + journal["id"]) / move["id"]
            atomic_write(exported / "manifest.json", encode(move))
            sources = {filename: directory / filename for filename in ("original", "replacement")}
            sources["current"] = native_path(safe_path(self.core.git.root, move["path"]))
            for filename, source in sources.items():
                if source.exists():
                    _, data = self.read(source)
                    atomic_write(exported / filename, data)

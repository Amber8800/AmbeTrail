"""Content-addressed storage, OS-owned writer locks, and conservative retention."""
from __future__ import annotations

import gzip
import io
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .common import (MAX_FILE, SafetyError, atomic_write, digest, encode, fsync_directory,
                     is_link, native_path, read_json, relative_path, sha, utc_now, validate_entries,
                     validate_hash, validate_id, validate_title, write_json)


class Store:
    COLLECTIONS = {"snapshots", "epochs", "metadata", "operations", "receipts"}

    def __init__(self, directory: Path, identity: dict, *, readonly: bool = False):
        self.directory = directory
        self.identity = identity
        self.writable = False
        self._lock_stream = None
        self.max_bytes: int | None = None
        self._used_bytes: int | None = None
        self._headers: dict[str, tuple[tuple, dict]] = {}
        self._quota_lock = threading.Lock()
        self._put_locks: dict[str, threading.Lock] = {}
        self._headers_lock = threading.RLock()
        root = Path(identity["root"])
        if directory.resolve().is_relative_to(root.resolve()):
            raise SafetyError("Snapshot storage must be outside the working repository.")
        if not readonly:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._acquire_lock()
        try:
            stored = read_json(directory / "identity.json")
            if stored != identity:
                raise SafetyError("Storage identity mismatch. Existing records have been preserved.")
        except FileNotFoundError:
            if self.writable:
                write_json(directory / "identity.json", identity)
            elif readonly:
                raise SafetyError("No stored history exists for this repository.")
        except Exception:
            self.close()
            raise
        if self.writable:
            try:
                for name in (*self.COLLECTIONS, "objects", "tmp"):
                    (directory / name).mkdir(exist_ok=True, mode=0o700)
                self._clean_unpublished_temporaries()
            except Exception:
                self.close()
                raise

    def _clean_unpublished_temporaries(self) -> None:
        # Manifests/journals never reference these names. Keep all recovery records intact.
        for directory, dirs, files in os.walk(native_path(self.directory), followlinks=False):
            if any(is_link(Path(directory) / name) for name in dirs):
                raise SafetyError("Unexpected link in storage. Writes are disabled.")
            for name in files:
                if re.fullmatch(r"\.dev-snapshot-[\w-]+\.tmp", name):
                    source = Path(directory) / name
                    if is_link(source):
                        raise SafetyError("Unexpected temporary link in storage.")
                    source.unlink()

    def _acquire_lock(self) -> None:
        # Lock lifetime is owned by the OS, so crashes release it without racy PID-file deletion.
        stream = open(self.directory / "writer.lock", "a+b")
        if stream.seek(0, io.SEEK_END) == 0:
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
        except (OSError, BlockingIOError):
            stream.close()
            return
        self._lock_stream = stream
        self.writable = True

    def close(self) -> None:
        if self._lock_stream:
            self._lock_stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lock_stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_UN)
            self._lock_stream.close()
            self._lock_stream = None
        self.writable = False

    def assert_writer(self) -> None:
        if not self.writable:
            raise SafetyError("Another window owns recording; this window is read-only.")

    def record(self, collection: str, record_id: str) -> Path:
        if collection not in self.COLLECTIONS:
            raise SafetyError("Invalid record collection.")
        return self.directory / collection / (validate_id(record_id) + ".json")

    def _object(self, object_hash: str) -> Path:
        validate_hash(object_hash)
        prefix = self.directory / "objects" / object_hash[:2]
        if is_link(prefix):
            raise SafetyError("Unexpected link in snapshot storage.")
        return prefix / object_hash

    def put(self, data: bytes) -> str:
        self.assert_writer()
        if len(data) > MAX_FILE:
            raise SafetyError("Object exceeds the storage size limit.")
        object_hash = sha(data)
        lock = self._put_locks.setdefault(object_hash, threading.Lock())
        with lock:
            destination = self._object(object_hash)
            if destination.exists():
                self.get(object_hash, len(data))
                return object_hash
            compressed = gzip.compress(data, compresslevel=6, mtime=0)
            with self._quota_lock:
                if self._used_bytes is None:
                    self._used_bytes = self.usage()
                if self.max_bytes is not None and self._used_bytes + len(compressed) > self.max_bytes:
                    raise SafetyError("Storage quota reached. Manage history before continuing.")
                self._used_bytes += len(compressed)
            try:
                atomic_write(destination, compressed)
                self.get(object_hash, len(data))
            except Exception:
                with self._quota_lock:
                    self._used_bytes = None  # Recount after an uncertain/partial write.
                raise
        return object_hash

    def get(self, object_hash: str, expected_size: int | None = None) -> bytes:
        source = self._object(object_hash)
        import stat
        from .common import metadata_is_link
        metadata = source.lstat()
        if metadata_is_link(metadata) or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE + 1024 * 1024:
            raise SafetyError("Invalid stored object.")
        try:
            with gzip.open(source, "rb") as stream:
                data = stream.read(MAX_FILE + 1)
        except (OSError, EOFError) as error:
            raise SafetyError("Stored object is unreadable.") from error
        if len(data) > MAX_FILE or sha(data) != object_hash or expected_size is not None and len(data) != expected_size:
            raise SafetyError("Stored object failed integrity verification.")
        return data

    def save_snapshot(self, snapshot: dict) -> None:
        self.assert_writer()
        self._validate_snapshot(snapshot)
        if len(encode(snapshot)) > 64 * 1024 * 1024:
            raise SafetyError("Snapshot manifest exceeds the supported size. Narrow the recording scope.")
        destination = self.record("snapshots", snapshot["id"])
        if destination.exists():
            raise SafetyError("A snapshot is immutable and cannot be overwritten.")
        self.write_record("snapshots", snapshot["id"], snapshot)

    def write_record(self, collection: str, record_id: str, value: dict) -> None:
        self.assert_writer()
        destination = self.record(collection, record_id)
        try:
            old_size = destination.stat().st_size
        except FileNotFoundError:
            old_size = 0
        content = encode(value)
        atomic_write(destination, content)
        if self._used_bytes is not None:
            self._used_bytes += len(content) - old_size

    def _validate_snapshot(self, snapshot: dict) -> None:
        if snapshot.get("schema") != 1 or snapshot.get("repositoryId") != self.identity["id"]:
            raise SafetyError("Unsupported snapshot schema or repository.")
        validate_id(snapshot.get("id"))
        validate_id(snapshot.get("epochId"))
        if snapshot.get("previousSnapshotId"):
            validate_id(snapshot["previousSnapshotId"])
        if not isinstance(snapshot.get("context"), dict) or not isinstance(snapshot.get("endedAt"), str):
            raise SafetyError("Invalid snapshot context.")
        validate_entries(snapshot.get("entries"))
        if "initialChanges" in snapshot:
            if not isinstance(snapshot["initialChanges"], list):
                raise SafetyError("Invalid initial change list.")
            for name in snapshot["initialChanges"]:
                relative_path(name)
        if digest(snapshot["entries"]) != snapshot.get("treeHash"):
            raise SafetyError("Snapshot manifest failed integrity verification.")

    def snapshot(self, snapshot_id: str, *, verify: bool = False) -> dict:
        snapshot = read_json(self.record("snapshots", snapshot_id))
        self._validate_snapshot(snapshot)
        if snapshot["id"] != snapshot_id:
            raise SafetyError("Snapshot identity mismatch.")
        if verify:
            for entry in snapshot["entries"].values():
                if entry["state"] == "present":
                    self.get(entry["hash"], entry["size"])
        return snapshot

    def snapshots(self) -> list[dict]:
        with self._headers_lock:
            return self._snapshot_headers()

    def initial_changed_paths(self, snapshot: dict) -> list[str] | None:
        """Use recorded startup status, never today's Git status, to classify old baselines."""
        if snapshot["reason"] != "initial":
            return None
        if "initialChanges" in snapshot:
            return snapshot["initialChanges"]
        try:
            epoch = read_json(self.record("epochs", snapshot["epochId"]))
        except FileNotFoundError:
            return None  # Missing evidence must not hide existing history.
        paths = epoch.get("initialChanges")
        if epoch.get("initialSnapshot") != snapshot["id"] or not isinstance(paths, list):
            return None
        entries = self.snapshot(snapshot["id"])["entries"]
        return [name for name in paths if name in entries and entries[name]["state"] != "excluded"]

    def is_internal_baseline(self, snapshot: dict) -> bool:
        if snapshot["reason"] != "initial" or self.initial_changed_paths(snapshot) != []:
            return False
        metadata = self.metadata(snapshot["id"])
        # Preserve records the user deliberately named, pinned, or linked to a commit.
        return not (metadata["pinned"] or metadata.get("title") or metadata["commitIds"])

    def _snapshot_headers(self) -> list[dict]:
        directory = self.directory / "snapshots"
        if not directory.exists():
            return []
        # Cache headers only. Holding 1,000 complete 10,000-file manifests would exhaust memory.
        result = []
        present = set()
        with os.scandir(directory) as files:
            for source in files:
                if not source.name.endswith(".json"):
                    continue
                record_id = source.name[:-5]
                validate_id(record_id)
                if source.is_symlink():
                    raise SafetyError("Unexpected snapshot link.")
                metadata = source.stat(follow_symlinks=False)
                sig = (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
                cached = self._headers.get(record_id)
                if cached and cached[0] == sig:
                    header = cached[1]
                else:
                    snapshot = self.snapshot(record_id)
                    header = {k: v for k, v in snapshot.items() if k != "entries"}
                    header["excludedCount"] = sum(e["state"] == "excluded" for e in snapshot["entries"].values())
                    self._headers[record_id] = (sig, header)
                present.add(record_id)
                result.append(header)
        self._headers = {key: value for key, value in self._headers.items() if key in present}
        return sorted(result, key=lambda s: (s["endedAt"], s["id"]), reverse=True)

    def metadata(self, snapshot_id: str) -> dict:
        try:
            value = read_json(self.record("metadata", snapshot_id))
        except FileNotFoundError:
            return {"schema": 1, "pinned": False, "validation": "unverified", "commitIds": []}
        if type(value.get("pinned")) is not bool or not isinstance(value.get("commitIds"), list):
            raise SafetyError("Invalid snapshot status metadata.")
        validate_title(value.get("title", ""))
        return value

    def update_metadata(self, snapshot_id: str, **patch) -> None:
        self.assert_writer()
        value = self.metadata(snapshot_id)
        value.update(patch)
        validate_title(value.get("title", ""))
        self.write_record("metadata", snapshot_id, value)

    def journal(self, value: dict) -> None:
        self.assert_writer()
        value["updatedAt"] = utc_now()
        self.write_record("operations", value["id"], value)

    def journals(self) -> list[dict]:
        result = []
        for source in (self.directory / "operations").glob("*.json"):
            item = read_json(source)
            validate_id(item.get("id"))
            validate_id(item.get("backupId"))
            if source.stem != item["id"] or item.get("kind") not in ("restore", "undo", "commit") or item.get("phase") not in ("prepared", "applying", "complete", "rolledBack", "recoveryRequired", "resolved"):
                raise SafetyError("Invalid recovery journal.")
            if not isinstance(item.get("changes"), list) or not isinstance(item.get("applied"), list):
                raise SafetyError("Invalid recovery journal file list.")
            for change in item["changes"]:
                validate_entries({change["path"]: change["before"]})
                validate_entries({change["path"]: change["after"]})
            if item.get("targetId"):
                validate_id(item["targetId"])
            result.append(item)
        return sorted(result, key=lambda j: j["createdAt"], reverse=True)

    def usage(self) -> int:
        def walk(directory: Path) -> int:
            directory = native_path(directory)
            subtotal = 0
            with os.scandir(directory) as entries:
                for entry in entries:
                    metadata = entry.stat(follow_symlinks=False)
                    if entry.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
                        raise SafetyError("Unexpected link in snapshot storage.")
                    subtotal += walk(Path(entry.path)) if entry.is_dir(follow_symlinks=False) else metadata.st_size
            return subtotal
        total = walk(self.directory)
        self._used_bytes = total
        return total

    def cached_usage(self) -> int:
        return self.usage() if self._used_bytes is None else self._used_bytes

    def protected_ids(self) -> set[str]:
        snapshots = self.snapshots()
        protected = set()
        latest = next((s for s in snapshots if s["reason"] != "backup"), None)
        if latest:
            protected.add(latest["id"])
            epoch = read_json(self.record("epochs", latest["epochId"]))
            protected.add(validate_id(epoch["initialSnapshot"]))
        for snapshot in snapshots:
            metadata = self.metadata(snapshot["id"])
            if metadata["pinned"] or metadata.get("protectedUntil", "") > utc_now():
                protected.add(snapshot["id"])
        for journal in self.journals():
            if journal["phase"] not in ("complete", "rolledBack", "resolved"):
                protected.add(journal["backupId"])
                if journal.get("targetId"):
                    protected.add(journal["targetId"])
        # Keep the last successful boundary of each branch until a newer one is committed.
        boundaries = {}
        for source in (self.directory / "receipts").glob("*.json"):
            receipt = read_json(source)
            if receipt.get("mode") == "boundary" and receipt.get("expected") is True:
                branch = receipt["branch"]
                if branch not in boundaries or receipt["createdAt"] > boundaries[branch]["createdAt"]:
                    boundaries[branch] = receipt
        for receipt in boundaries.values():
            protected.add(validate_id(receipt["snapshotIds"][0]))
        return protected

    def delete_snapshot(self, snapshot_id: str) -> None:
        with self._headers_lock:
            self._delete_snapshot(snapshot_id)

    def history_before(self, snapshot_id: str) -> dict:
        """Review older history; independent manifests keep the cutoff restorable."""
        target = self.snapshot(snapshot_id)
        if target["reason"] == "backup":
            raise SafetyError("Select a timeline snapshot as the cleanup boundary.")
        protected = self.protected_ids()
        older = [s for s in self.snapshots() if s["endedAt"] < target["endedAt"]]
        return {"snapshotId": snapshot_id, "cutoffAt": target["endedAt"],
                "candidates": [{"id": s["id"], "endedAt": s["endedAt"], "reason": s["reason"]}
                               for s in older if s["id"] not in protected],
                "protectedCount": sum(s["id"] in protected for s in older)}

    def delete_history_before(self, review: dict) -> int:
        self.assert_writer()
        with self._headers_lock:
            if self.history_before(review["snapshotId"]) != review:
                raise SafetyError("History or protection changed after review. Review cleanup again.")
            self.snapshot(review["snapshotId"], verify=True)
            # Validate all surviving manifests before deleting any approved history.
            for header in self.snapshots():
                self.snapshot(header["id"])
            for candidate in review["candidates"]:
                self._delete_snapshot(candidate["id"])
            self.gc()
            return len(review["candidates"])

    def _delete_snapshot(self, snapshot_id: str) -> None:
        self.assert_writer()
        self.snapshot(snapshot_id)
        if snapshot_id in self.protected_ids():
            raise SafetyError("This snapshot is protected by a pin, active epoch, or recovery operation.")
        self.record("snapshots", snapshot_id).unlink()
        self.record("metadata", snapshot_id).unlink(missing_ok=True)
        fsync_directory(self.directory / "snapshots")
        self._used_bytes = None
        self._headers.pop(snapshot_id, None)

    def gc(self) -> None:
        self.assert_writer()
        live = set()
        snapshots = self.snapshots()  # Fail closed if any manifest is unreadable.
        ids = {s["id"] for s in snapshots}
        for header in snapshots:
            snapshot = self.snapshot(header["id"])
            live.update(e["hash"] for e in snapshot["entries"].values() if e["state"] == "present")
        for journal in self.journals():
            if journal["phase"] not in ("complete", "rolledBack", "resolved"):
                if journal["backupId"] not in ids:
                    raise SafetyError("Recovery backup is missing. Garbage collection stopped.")
                for change in journal["changes"]:
                    live.update(e["hash"] for e in (change["before"], change["after"]) if e["state"] == "present")
                if journal.get("indexBackupHash"):
                    live.add(validate_hash(journal["indexBackupHash"]))
                if journal.get("mode") == "boundary" and journal.get("preparedIndexHash"):
                    live.add(validate_hash(journal["preparedIndexHash"]))
        for source in (self.directory / "objects").glob("*/*"):
            if is_link(source) or is_link(source.parent):
                raise SafetyError("Unexpected link in object storage.")
            validate_hash(source.name)
            if source.parent.name != source.name[:2]:
                raise SafetyError("Unexpected object storage path.")
            if source.name not in live:
                source.unlink()
        self._used_bytes = None

    def prune(self, config: dict) -> None:
        self.assert_writer()
        snapshots = self.snapshots()
        protected = self.protected_ids()
        for header in snapshots:
            self.snapshot(header["id"])  # Validate every manifest before deleting any history.
        count = sum(s["reason"] != "backup" and not self.is_internal_baseline(s) for s in snapshots)
        expiry = (datetime.now(timezone.utc) - timedelta(days=config["retentionDays"])).isoformat()
        for snapshot in reversed(snapshots):
            if snapshot["id"] in protected:
                continue
            if snapshot["endedAt"] < expiry or snapshot["reason"] == "backup" or count > config["maxSnapshots"]:
                normal_record = snapshot["reason"] != "backup" and not self.is_internal_baseline(snapshot)
                self.delete_snapshot(snapshot["id"])
                count -= normal_record
        self.gc()
        limit = int(config["maxStorageMiB"] * 1024 * 1024)
        if self.usage() > limit:
            for snapshot in reversed(self.snapshots()):
                if snapshot["id"] in protected:
                    continue
                self.delete_snapshot(snapshot["id"])
                self.gc()
                if self.usage() <= limit:
                    break
        if self.usage() > limit:
            raise SafetyError("Protected history exceeds the storage quota. Manage history to resume.")


def protect_backup(store: Store, snapshot_id: str) -> None:
    until = datetime.now(timezone.utc) + timedelta(days=7)
    store.update_metadata(snapshot_id, protectedUntil=until.isoformat())

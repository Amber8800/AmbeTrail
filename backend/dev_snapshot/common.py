"""Wire models, filesystem safety, and durable storage primitives."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = 1
MAX_FILE = 100 * 1024 * 1024
DEFAULTS = {
    "idleSeconds": 5, "maxIntervalSeconds": 60, "reconcileSeconds": 120,
    "fullHashSeconds": 600, "maxFileMiB": 10, "retentionDays": 30,
    "maxSnapshots": 1000, "maxStorageMiB": 2048,
    "exclude": [], "allowSensitiveFiles": [],
}


class SafetyError(Exception):
    """An operation cannot safely proceed. Messages must not contain source contents."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    return sha(encode(value))


def new_id() -> str:
    return str(uuid.uuid4())


def validate_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value):
        raise SafetyError("Invalid record ID.")
    return value


def validate_hash(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise SafetyError("Invalid content hash.")
    return value


def relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise SafetyError("Unsafe relative path.")
    parts = value.split("/")
    if any(not p or p in (".", "..") or p.lower() == ".git" or ":" in p for p in parts):
        raise SafetyError("Unsafe relative path.")
    if PurePosixPath(value).is_absolute() or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise SafetyError("Unsupported path encoding.")
    return value


def is_link(path: Path) -> bool:
    try:
        return metadata_is_link(path.lstat())
    except FileNotFoundError:
        return False


def metadata_is_link(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def safe_path(root: Path, name: str) -> Path:
    relative_path(name)
    root_stat = root.lstat()
    if metadata_is_link(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise SafetyError("Repository root is no longer a regular directory.")
    current = root
    parts = name.split("/")
    for index, part in enumerate(parts):
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if metadata_is_link(metadata):
            raise SafetyError("Links and junctions are excluded.")
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise SafetyError("File/directory path conflict.")
    return current


def fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return  # Node/Python cannot portably flush a Windows directory handle.
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(destination: Path, content: bytes, mode: int = 0o600) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".dev-snapshot-", suffix=".tmp", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            if os.name != "nt":
                os.fchmod(stream.fileno(), mode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(destination: Path, value: Any) -> None:
    atomic_write(destination, encode(value))


def read_json(source: Path) -> dict:
    if is_link(source) or source.stat().st_size > 64 * 1024 * 1024:
        raise SafetyError("Invalid metadata file.")
    try:
        value = json.loads(source.read_bytes())
    except (ValueError, UnicodeError) as error:
        raise SafetyError("Metadata is unreadable. Existing records have been preserved.") from error
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise SafetyError("Unsupported metadata schema. Writes are disabled.")
    return value


def signature(metadata: os.stat_result) -> tuple:
    # Windows 3.12 reports incompatible ctime values through stat and fstat.
    # Use file identity + mtime + size + mode there; periodic full hashes cover missed events.
    return (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns if os.name != "nt" else 0,
            metadata.st_ino, metadata.st_dev, metadata.st_mode)


def validate_entry(entry: Any) -> dict:
    if not isinstance(entry, dict) or entry.get("state") not in ("present", "absent", "excluded"):
        raise SafetyError("Invalid file entry.")
    state = entry["state"]
    if state == "present":
        validate_hash(entry.get("hash"))
        if type(entry.get("size")) is not int or not 0 <= entry["size"] <= MAX_FILE or type(entry.get("executable")) is not bool:
            raise SafetyError("Invalid file object metadata.")
        if set(entry) != {"state", "hash", "size", "executable"}:
            raise SafetyError("Unknown file entry fields.")
    elif state == "absent" and set(entry) != {"state"}:
        raise SafetyError("Invalid absent entry.")
    elif state == "excluded" and not isinstance(entry.get("reason"), str):
        raise SafetyError("Invalid exclusion metadata.")
    return entry


def validate_entries(entries: Any) -> dict:
    if not isinstance(entries, dict):
        raise SafetyError("Invalid snapshot file map.")
    for name, entry in entries.items():
        relative_path(name)
        validate_entry(entry)
    return entries


def settings_from(value: dict | None) -> dict:
    value = value or {}
    result = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        candidate = value.get(key, default)
        if isinstance(default, list):
            if not isinstance(candidate, list) or any(not isinstance(x, str) or len(x) > 1024 for x in candidate) or len(candidate) > 1000:
                raise SafetyError("Invalid exclusion settings.")
        elif type(candidate) not in (int, float) or not 1 <= candidate <= 1_000_000:
            raise SafetyError("Invalid numeric settings.")
        result[key] = candidate
    if result["maxFileMiB"] > 100 or result["maxSnapshots"] < 2:
        raise SafetyError("Invalid snapshot limits.")
    return result


ABSENT = {"state": "absent"}

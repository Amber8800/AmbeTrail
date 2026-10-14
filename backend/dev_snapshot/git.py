"""Bounded Git subprocess adapter. Background reads never refresh the index."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .common import SafetyError, digest, relative_path, sha


class Git:
    def __init__(self, root: Path, executable: str = "git", *, git_dir: Path | None = None, index: Path | None = None, hooks: str | None = None):
        self.root = root
        self.executable = executable
        self.git_dir, self.isolated_index, self.hooks = git_dir, index, hooks

    def run(self, *args: str, data: bytes | None = None, allow_failure: bool = False,
            timeout: int = 30) -> bytes:
        environment = dict(os.environ)
        # Parent process state must not redirect this repository to another index/worktree.
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE"):
            environment.pop(key, None)
        environment.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1",
                           GIT_TERMINAL_PROMPT="0")
        options = []
        if self.git_dir is not None and self.isolated_index is not None:
            environment.update(GIT_DIR=str(self.git_dir), GIT_WORK_TREE=str(self.root), GIT_INDEX_FILE=str(self.isolated_index))
            if os.name == "nt":
                options.extend(["-c", "core.longpaths=true"])
        if self.hooks is not None:
            options.extend(["-c", "core.hooksPath=" + self.hooks])
        try:
            result = subprocess.run(
                [self.executable, "--literal-pathspecs", "-c", "core.fsmonitor=false", *options, *args],
                cwd=self.root, env=environment, input=data,
                **({"stdin": subprocess.DEVNULL} if data is None else {}), stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, shell=False, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as error:
            raise SafetyError("Git timed out; review repository state before retrying.") from error
        except OSError as error:
            raise SafetyError("Git could not be started. Check the Git executable.") from error
        if result.returncode and not allow_failure:
            # Arbitrary hooks can print secrets. Never forward their output to the protocol/log.
            raise SafetyError(f"Git command failed (exit {result.returncode}). Check Git configuration, hooks, signing, and permissions.")
        if len(result.stdout) > 64 * 1024 * 1024:
            raise SafetyError("Git output limit exceeded.")
        return result.stdout

    def text(self, *args: str, allow_failure: bool = False) -> str:
        return self.run(*args, allow_failure=allow_failure).decode("utf-8", errors="strict").strip()

    def identity(self) -> dict:
        root = Path(self.text("rev-parse", "--show-toplevel")).resolve(strict=True)
        git_dir = Path(self.text("rev-parse", "--absolute-git-dir")).resolve(strict=True)
        common = Path(self.text("rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
        self.root = root
        return {"schema": 1, "id": digest([os.path.normcase(str(p)) for p in (root, git_dir, common)]),
                "root": str(root), "gitDir": str(git_dir), "commonDir": str(common)}

    def check_supported(self) -> None:
        match = re.search(r"git version (\d+)\.(\d+)", self.text("--version"))
        if not match or tuple(map(int, match.groups())) < (2, 43):
            raise SafetyError("Git 2.43 or newer is required.")
        partial = self.text("config", "--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor)$", allow_failure=True)
        if partial:
            raise SafetyError("Partial clones are excluded to prevent implicit network fetches.")

    def index_path(self) -> Path:
        return Path(self.text("rev-parse", "--path-format=absolute", "--git-path", "index"))

    def index_bytes(self) -> bytes | None:
        try:
            return self.index_path().read_bytes()
        except FileNotFoundError:
            return None

    def context(self, *, own_index_lock: bool = False) -> dict:
        git_dir = Path(self.text("rev-parse", "--absolute-git-dir"))
        index = self.index_bytes()
        busy_files = (
            "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-apply",
            "rebase-merge", "sequencer")
        if not own_index_lock:
            busy_files += ("index.lock",)
        busy = any((git_dir / name).exists() for name in busy_files)
        sparse = self.text("config", "--bool", "core.sparseCheckout", allow_failure=True) == "true"
        return {"head": self.text("rev-parse", "--verify", "HEAD", allow_failure=True) or None,
                "branch": self.text("symbolic-ref", "-q", "HEAD", allow_failure=True) or None,
                "indexHash": sha(index if index is not None else b"no-index"),
                "busy": busy, "sparse": sparse}

    def files(self) -> list[str]:
        data = self.run("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--deduplicate")
        return sorted(set(data.decode("utf-8", errors="strict").split("\0")) - {""})

    def submodules(self) -> list[str]:
        return [item.split("\t", 1)[1] for item in self.run("ls-files", "--stage", "-z").decode("utf-8").split("\0") if item.startswith("160000 ")]

    def statuses(self) -> list[dict]:
        # Git status may run clean/process filters while hashing changed worktree files.
        # Background inspection must not start arbitrary project tools (e.g. LFS network clients).
        keys = self.text("config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$", allow_failure=True).splitlines()
        drivers = {key.rsplit(".", 1)[0] for key in keys}
        overrides = []
        for driver in sorted(drivers):
            for suffix, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")):
                overrides.extend(["-c", f"{driver}.{suffix}={value}"])
        fields = self.run(*overrides, "status", "--porcelain=v1", "-z", "--untracked-files=all").decode("utf-8").split("\0")
        result = []
        index = 0
        while index < len(fields):
            field = fields[index]
            index += 1
            if not field:
                continue
            code, name = field[:2], relative_path(field[3:])
            item = {"path": name, "code": code}
            if "R" in code or "C" in code:
                if index == len(fields):
                    raise SafetyError("Invalid Git rename record.")
                item["original"] = relative_path(fields[index])
                index += 1
            result.append(item)
        return result

    def clean_index(self) -> bool:
        return not any(s["code"] != "??" and (s["code"][0] != " " or s["code"][1] == "A") for s in self.statuses())

    def add(self, paths: list[str], *, force: bool = False) -> None:
        for name in paths:
            relative_path(name)
        self.run("add", "-A", *(["--force"] if force else []), "--pathspec-from-file=-", "--pathspec-file-nul",
                 data=("\0".join(paths) + "\0").encode("utf-8"))

    def tree(self) -> str:
        return self.text("write-tree")

    def tree_entries(self, head: str | None) -> dict[str, tuple[str, str]]:
        if not head:
            return {}
        entries = {}
        for record in self.run("ls-tree", "-r", "-z", "--full-tree", head).decode("utf-8").split("\0"):
            if record:
                info, name = record.split("\t", 1)
                mode, _, oid = info.split(" ")
                entries[name] = (mode, oid)
        return entries

    def file_content(self, name: str, *, head: str | None = None, staged: bool = False) -> bytes | None:
        relative_path(name)
        if staged:
            line = self.run("ls-files", "--stage", "-z", "--", name).decode("utf-8")
            match = re.match(r"100(?:644|755) ([0-9a-f]+) 0\t", line)
        else:
            if head is None:
                return None
            line = self.run("ls-tree", "-z", head, "--", name).decode("utf-8")
            match = re.match(r"100(?:644|755) blob ([0-9a-f]+)\t", line)
        return self.run("cat-file", "blob", match[1]) if match else None

    def upstream(self) -> dict:
        context = self.context()
        if not context["head"] or not context["branch"] or context["busy"]:
            raise SafetyError("Push requires an attached branch and no Git operation.")
        name = context["branch"].removeprefix("refs/heads/")
        remote = self.text("config", "--get", f"branch.{name}.remote", allow_failure=True)
        branch = self.text("config", "--get", f"branch.{name}.merge", allow_failure=True)
        if not remote or remote.startswith("-") or remote == "." or not branch.startswith("refs/heads/"):
            raise SafetyError("Configure a remote upstream branch using Git before pushing.")
        urls = self.text("remote", "get-url", "--push", "--all", remote).splitlines()
        if len(urls) != 1:
            raise SafetyError("Push requires exactly one configured push destination.")
        return {"remote": remote, "branch": branch, "localRef": context["branch"], "destinationHash": digest(urls)}


def same_context(a: dict, b: dict) -> bool:
    return all(a[k] == b[k] for k in ("head", "branch", "indexHash")) and not b["busy"] and not b["sparse"]

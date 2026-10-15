"""Protocol-facing application service and the Python-owned recording scheduler."""
from __future__ import annotations

import os
import time
import threading
from pathlib import Path

from .common import (SafetyError, atomic_write, new_id, read_json,
                     relative_path, safe_path, settings_from, utc_now, validate_title)
from .boundary import BoundaryCommitService
from .git import Git
from .restore import RestoreService, unfinished
from .snapshots import Policy, SnapshotService, changes_between, snapshot_summary, suggest_commit, summarize
from .store import Store
from .agent_history import AgentHistory
from .local_summary import VERSION as SUMMARY_VERSION


def display_bytes(data: bytes | None) -> str:
    if data is None:
        return ""
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16", errors="strict")
        text = data.decode("utf-8-sig", errors="strict")
        if "\0" not in text:
            return text
    except UnicodeError:
        pass
    from .common import sha
    return f"[Binary or unsupported text encoding]\nBytes: {len(data)}\nSHA-256: {sha(data)}\nOriginal bytes remain preserved.\n"


class Session:
    def __init__(self, git: Git, store: Store, config: dict, *, git_enabled: bool = True,
                 agent_directory: Path | None = None):
        self.git, self.store = git, store
        self.git_enabled = git_enabled
        self.agent_history = AgentHistory(agent_directory, store.identity, readonly=not store.writable) if agent_directory is not None else None
        self.core = SnapshotService(git, store, config)
        self.dirty: set[str] = set()
        self.restore = RestoreService(self.core, lambda: self.dirty)
        self.commits = BoundaryCommitService(self.core, lambda: self.dirty)
        try:
            saved_pause = read_json(store.directory / "preferences.json").get("paused", False)
        except FileNotFoundError:
            saved_pause = False
        self.paused = bool(saved_pause) or not store.writable or any(unfinished(j) for j in store.journals())
        self.message = "읽기 전용" if not store.writable else "복구 검토 필요" if any(unfinished(j) for j in store.journals()) else "일시 중지" if self.paused else "기록 중"
        now = time.monotonic()
        self.last_event = now
        self.first_event: float | None = None
        self.reconciled = now
        self.full_hashed = now
        self.push_reviews: dict[str, dict] = {}
        self.pending_paths: set[str] = set()
        self.last_pruned = now
        self.force_full = False
        self.retry_after = 0.0
        self.pending_settings: dict | None = None
        self.history_reviews: dict[str, dict] = {}
        self.work_summaries: dict[tuple[str, bool], str] = {}
        self.interval_summaries: dict[tuple[str, str | None], tuple[str, int]] = {}
        self.worker: threading.Thread | None = None
        self.job: dict | None = None
        self.notify = lambda: None

    def event(self, name: str | None = None) -> None:
        self.core.changed()
        if name and len(self.pending_paths) < 10000:
            self.pending_paths.add(relative_path(name))
        elif name:
            self.force_full = True
        now = time.monotonic()
        self.last_event = now
        if self.first_event is None:
            self.first_event = now

    def capture(self, reason: str, *, full: bool = True, changed_paths: set[str] | None = None) -> dict | None:
        if any(unfinished(j) for j in self.store.journals()):
            raise SafetyError("Review recovery before recording more snapshots.")
        # Full GC is kept off the hot path unless needed; retention runs after published snapshots.
        if self.store.cached_usage() >= self.core.config["maxStorageMiB"] * 1024 * 1024:
            self.store.prune(self.core.config)
        snapshot = self.core.capture(reason, full=full, changed_paths=changed_paths)
        self.message = "기록 중" if not self.paused else "일시 중지"
        if snapshot and (time.monotonic() - self.last_pruned >= 1800 or len(self.store.snapshots()) > self.core.config["maxSnapshots"] or self.store.cached_usage() > self.core.config["maxStorageMiB"] * 1024 * 1024 * 0.9):
            self.store.prune(self.core.config)
            self.last_pruned = time.monotonic()
        return snapshot

    def start_capture(self, reason: str, *, full: bool = True, changed_paths: set[str] | None = None) -> dict:
        if self.worker and self.worker.is_alive():
            return dict(self.job)
        if any(unfinished(j) for j in self.store.journals()):
            raise SafetyError("Review recovery before recording more snapshots.")
        self.store.assert_writer()
        self.core.cancelled.clear()
        self.job = {"id": new_id(), "state": "running", "completed": 0, "total": 0,
                    "snapshotId": None, "message": ""}
        def progress(completed, total):
            self.job.update(completed=completed, total=total)
            self.message = f"기록 중 · {completed}/{total}"
            self.notify()
        self.core.progress = progress
        def work():
            try:
                snapshot = self.capture(reason, full=full, changed_paths=changed_paths)
                self.job.update(state="complete", snapshotId=snapshot["id"] if snapshot else None)
            except Exception as error:
                cancelled = self.core.cancelled.is_set()
                message = str(error) if isinstance(error, SafetyError) else "Capture failed. Existing snapshots are preserved."
                self.job.update(state="cancelled" if cancelled else "failed", message=message)
                self.message = "일시 중지" if self.paused else "기록 취소" if cancelled else "기록 지연 · 상태 검토 필요"
                # A failed attempt must not lose paths already removed from pending_paths.
                self.force_full = True
                self.retry_after = time.monotonic() + self.core.config["maxIntervalSeconds"]
            finally:
                self.notify()
        self.worker = threading.Thread(target=work, name="dev-snapshot-capture", daemon=True)
        self.worker.start()
        return dict(self.job)

    def tick(self) -> bool:
        busy = self.worker is not None and self.worker.is_alive()
        if self.pending_settings is not None and not busy:
            self.core.config = self.pending_settings
            self.pending_settings = None
            self.core.cache.clear()
            self.interval_summaries.clear()
            self.force_full = True
            self.event()
        if self.paused or not self.store.writable or self.restore.plans or self.history_reviews or busy or any(unfinished(j) for j in self.store.journals()) or time.monotonic() < self.retry_after:
            return False
        now, config = time.monotonic(), self.core.config
        full = self.force_full or now - self.full_hashed >= config["fullHashSeconds"]
        due = self.first_event is not None and (now - self.last_event >= config["idleSeconds"] or now - self.first_event >= config["maxIntervalSeconds"])
        reconcile = now - self.reconciled >= config["reconcileSeconds"]
        if not (full or due or reconcile):
            return False
        # Advance attempts even on errors; never spin repeatedly on a full disk or unstable capture.
        self.reconciled = now
        if full:
            self.full_hashed = now
        self.first_event = None
        try:
            paths = set(self.pending_paths) if self.pending_paths and due and not full and not reconcile else None
            self.pending_paths.clear()
            self.force_full = False
            result = self.start_capture("automatic", full=full, changed_paths=paths)
            return result is not None
        except (SafetyError, OSError):
            self.force_full = True
            self.retry_after = time.monotonic() + config["maxIntervalSeconds"]
            self.message = "기록 지연 · 상태 검토 필요"
            return True

    def summary(self) -> dict:
        snapshots = self.store.snapshots()
        latest = next((s for s in snapshots if s["reason"] != "backup"), None)
        latest_visible = next((s for s in snapshots if s["reason"] != "backup" and not self.store.is_internal_baseline(s)), None)
        return {"id": self.store.identity["id"], "root": str(self.git.root), "writable": self.store.writable,
                "paused": self.paused, "status": self.message, "dirtyCount": len(self.dirty),
                "latestAt": latest_visible["endedAt"] if latest_visible else None,
                "branch": latest["context"]["branch"] if latest else None,
                "excludedCount": latest.get("excludedCount", 0) if latest else 0,
                "job": dict(self.job) if self.job else None,
                "agentHistoryEnabled": bool(self.agent_history and self.agent_history.enabled),
                "agentHistoryPresent": bool(self.agent_history and self.agent_history.present),
                "agentHistoryProblem": self.agent_history.problem if self.agent_history else "",
                "recoveries": [j for j in self.store.journals() if unfinished(j)]}

    def timeline(self, limit: int = 50) -> list[dict]:
        snapshots = [s for s in self.store.snapshots() if s["reason"] != "backup"]
        indexed = {s["id"]: s for s in snapshots}
        self.work_summaries = {k: v for k, v in self.work_summaries.items() if k[0] in indexed}
        self.interval_summaries = {k: v for k, v in self.interval_summaries.items() if k[0] in indexed}
        context, anchor, baseline, range_error = None, None, None, None
        if self.git_enabled and self.store.writable and not (self.worker and self.worker.is_alive()) and not any(unfinished(j) for j in self.store.journals()):
            try:
                context = self.git.context()
                if not context["busy"] and not context["sparse"] and context["branch"]:
                    anchor = self.commits.anchor(context)
                    baseline = self.commits.baseline(context, anchor)
            except (SafetyError, OSError) as error:
                range_error = str(error) if isinstance(error, SafetyError) else "Git baseline could not be read. Stored history remains available."
                context, anchor, baseline = None, None, None
        result = []
        reports = self.agent_history.records() if self.agent_history else []
        report_by_tree = {}
        for report in reports:
            if report["association"]["status"] == "confirmed":
                for evidence in report["association"]["evidence"]:
                    report_by_tree.setdefault((evidence["snapshotId"], evidence["treeHash"]), report)
        visible = [s for s in snapshots if not self.store.is_internal_baseline(s)]
        for s in visible[:max(1, min(limit, 1000))]:
            previous = indexed.get(s["previousSnapshotId"], {})
            comparison_pruned = bool(s["reason"] != "initial" and s["previousSnapshotId"] and not previous)
            summary = s.get("summary", "저장된 파일 상태")
            changed_files = s.get("changedFiles", 0)
            initial_paths = self.store.initial_changed_paths(s)
            if initial_paths is not None:
                changed_files = len(initial_paths)
            commit_summary, commit_changed_files = None, None
            boundary_state = "unavailable"
            if s.get("summaryVersion") != SUMMARY_VERSION and not comparison_pruned and (self.git_enabled or s["reason"] != "initial"):
                key = (s["id"], comparison_pruned)
                if key not in self.work_summaries:
                    self.work_summaries[key] = snapshot_summary(self.snapshot_changes(s["id"]), self.store)
                summary = self.work_summaries[key]
            if baseline is not None and s["context"]["branch"] == context["branch"]:
                if anchor and (s["endedAt"] < anchor["boundaryAt"] or s["endedAt"] == anchor["boundaryAt"] and not anchor.get("partial")):
                    boundary_state = "committed"
                elif s["context"]["head"] in ([context["head"]] + (anchor["sourceHeads"] if anchor else [])):
                    boundary_state = "partial" if anchor and anchor.get("partial") and s["id"] == anchor["snapshotIds"][0] else "ready"
                    key = (s["id"], context["head"])
                    if key not in self.interval_summaries:
                        changes = changes_between(baseline, self.store.snapshot(s["id"])["entries"])
                        self.interval_summaries[key] = (summarize(changes), len(changes))
                    commit_summary, commit_changed_files = self.interval_summaries[key]
            result.append({"id": s["id"], "reason": s["reason"], "endedAt": s["endedAt"],
                           "summary": summary, "changedFiles": changed_files, "boundaryState": boundary_state, "rangeError": range_error,
                           "commitSummary": commit_summary, "commitChangedFiles": commit_changed_files,
                           "comparisonPruned": comparison_pruned,
                           "metadata": self.store.metadata(s["id"]), "context": s["context"]})
            confirmed = report_by_tree.get((s["id"], s["treeHash"]))
            if confirmed:
                result[-1]["agentTitle"] = confirmed["title"]
                result[-1]["agentDescription"] = confirmed["description"]
                result[-1]["agentSource"] = "Codex 최종 답변 · 사용자 확인 연결 · 에이전트 보고"
        return result

    def snapshot_changes(self, snapshot_id: str) -> list[dict]:
        snapshot = self.store.snapshot(snapshot_id)
        before = {}
        initial_paths = self.store.initial_changed_paths(snapshot)
        if snapshot["reason"] == "initial":
            if "initialBefore" in snapshot:
                before = snapshot["initialBefore"]
            else:
                if not self.git_enabled and snapshot["context"]["head"]:
                    raise SafetyError("Enable Git to compare this older initial snapshot with its recorded HEAD.")
                paths = initial_paths if initial_paths is not None else [p for p, e in snapshot["entries"].items() if e["state"] != "excluded"]
                before = self.core.head_entries(snapshot["context"]["head"], paths, persist=False)
        elif snapshot["previousSnapshotId"]:
            try:
                before = self.store.snapshot(snapshot["previousSnapshotId"])["entries"]
            except FileNotFoundError:
                pass
        after = snapshot["entries"] if initial_paths is None else {p: snapshot["entries"][p] for p in initial_paths}
        return changes_between(before, after)

    def entry_content(self, entry: dict) -> bytes | None:
        return self.store.get(entry["hash"], entry["size"]) if entry["state"] == "present" else None

    def diff(self, params: dict) -> dict:
        name = relative_path(params["path"])
        kind = params.get("kind", "snapshot")
        if kind == "snapshot":
            change = next((c for c in self.snapshot_changes(params["snapshotId"]) if c["path"] == name), None)
            if not change:
                raise SafetyError("This file is not a change in the selected snapshot.")
            snapshot = self.store.snapshot(params["snapshotId"])
            before = (self.git.file_content(name, head=snapshot["context"]["head"])
                      if snapshot["reason"] == "initial" and "initialBefore" not in snapshot
                      else self.entry_content(change["before"]))
            after = self.entry_content(change["after"])
        elif kind == "restore":
            plan = self.restore.plans.get(params["planId"])
            if not plan:
                raise SafetyError("Restore review expired.")
            change = next((c for c in plan["changes"] if c["path"] == name), None)
            if not change:
                raise SafetyError("File is outside the restore plan.")
            before, after = self.entry_content(change["before"]), self.entry_content(change["after"])
        elif kind == "boundaryStaged":
            journal = Engine._journal(self, params["operationId"])
            before, after = self.commits.diff(journal, name)
        elif kind == "boundary":
            review = self.commits.review(params["snapshotId"])
            change = next((c for c in review["changes"] if c["path"] == name), None)
            if not change:
                raise SafetyError("File is outside the current snapshot commit scope.")
            before = self.git.file_content(name, head=review["context"]["head"])
            after = self.entry_content(change["after"])
        elif kind in ("current", "staged"):
            if Policy(self.core).exclusion(name):
                raise SafetyError("Excluded files cannot be previewed.")
            before = self.git.file_content(name, head=self.git.context()["head"])
            if kind == "staged":
                after = self.git.file_content(name, staged=True)
            else:
                entry = self.core.read_entry(name)
                after = self.entry_content(entry)
        else:
            raise SafetyError("Unknown diff kind.")
        return {"left": display_bytes(before), "right": display_bytes(after), "path": name,
                "leftAbsent": before is None, "rightAbsent": after is None}


class Engine:
    def __init__(self, notify=lambda method, params: None):
        self.sessions: dict[str, Session] = {}
        self.notify = notify

    def changed(self, repo_id: str, name: str | None = None) -> None:
        session = self.sessions.get(repo_id)
        if session:
            session.event(name)

    def dirty(self, repo_id: str, names: list[str]) -> None:
        session = self.sessions.get(repo_id)
        if session:
            session.dirty = {relative_path(name) for name in names}

    def session(self, params: dict) -> Session:
        try:
            return self.sessions[params["repositoryId"]]
        except (KeyError, TypeError) as error:
            raise SafetyError("Repository is not open.") from error

    def tick(self) -> None:
        for repo_id, session in list(self.sessions.items()):
            changed = session.tick()
            if session.agent_history and session.agent_history.poll():
                changed = True
            if changed:
                self.notify("changed", {"repositoryId": repo_id})

    def close(self) -> None:
        for session in self.sessions.values():
            session.core.cancelled.set()
        for session in self.sessions.values():
            if session.worker:
                session.worker.join()
            session.store.close()
        self.sessions.clear()

    def call(self, method: str, params: dict) -> object:
        if method == "hello":
            return {"protocol": 1, "version": "0.2.1", "python": os.sys.version.split()[0]}
        if method == "open":
            config = settings_from(params.get("settings"))
            git = Git(Path(params["root"]).resolve(strict=True), params.get("gitPath", "git"))
            git.check_supported()
            identity = git.identity()
            if identity["id"] in self.sessions:
                return self.sessions[identity["id"]].summary()
            store = Store(Path(params["storageRoot"]).resolve() / "repositories" / identity["id"], identity)
            try:
                session = Session(git, store, config,
                                  agent_directory=Path(params["storageRoot"]).resolve() / "agent-history" / identity["id"])
                session.notify = lambda: self.notify("changed", {"repositoryId": identity["id"]})
                self.sessions[identity["id"]] = session
                if not session.paused:
                    session.start_capture("initial")
                return session.summary()
            except Exception:
                store.close()
                self.sessions.pop(identity["id"], None)
                raise
        if method == "offline":
            # A disabled built-in Git extension allows history reading but no Git execution.
            storage = Path(params["storageRoot"]) / "repositories"
            roots = [Path(p).resolve() for p in params.get("roots", [])]
            for directory in storage.glob("*"):
                identity = read_json(directory / "identity.json")
                root = Path(identity["root"])
                if not any(root == r or r.is_relative_to(root) for r in roots):
                    continue
                store = Store(directory, identity, readonly=True)
                session = Session(Git(root), store, settings_from(None), git_enabled=False,
                                  agent_directory=Path(params["storageRoot"]) / "agent-history" / identity["id"])
                session.message = "Git 비활성 · 과거 기록 조회 전용"
                self.sessions[identity["id"]] = session
            return [s.summary() for s in self.sessions.values()]
        if method == "list":
            return [s.summary() for s in self.sessions.values()]
        session = self.session(params)
        store = session.store
        busy = session.worker is not None and session.worker.is_alive()
        if busy and method not in ("close", "timeline", "snapshotChanges", "diff", "capture", "pause", "job", "cancelCapture", "settings", "restoreCancel", "historyCancel", "agentHistory", "agentConsent", "agentSetup", "agentClear"):
            raise SafetyError("Snapshot capture is running. Wait for completion or pause recording to cancel it.")
        if busy and method == "diff" and params.get("kind", "snapshot") != "snapshot":
            raise SafetyError("Wait for the active capture before reviewing live files.")
        if not store.writable and method not in ("close", "timeline", "snapshotChanges", "diff", "usage", "pause", "agentHistory"):
            raise SafetyError("This session is read-only. Only stored history is available.")
        if not store.writable and method == "diff" and params.get("kind", "snapshot") != "snapshot":
            raise SafetyError("Read-only sessions can only compare stored snapshots.")
        if method.startswith("agent"):
            history = session.agent_history
            if history is None:
                raise SafetyError("Agent history storage is unavailable.")
            if method == "agentHistory":
                return history.records()[:max(1, min(int(params.get("limit", 50)), 500))]
            if method in ("agentConsent", "agentClear", "agentLink") and params.get("approved") is not True:
                raise SafetyError("Explicit approval is required for agent history changes.")
            if method == "agentConsent":
                history.consent(params["enabled"])
            elif method == "agentSetup":
                return history.setup(params["python"], params["script"])
            elif method == "agentClear":
                history.clear()
            elif method == "agentLinkReview":
                return history.review_link(params["messageId"], params["snapshotIds"], store)
            elif method == "agentLink":
                history.link(params["messageId"], params["snapshotIds"], params["reviewHash"], store)
            else:
                raise SafetyError("Unknown agent history method.")
            self.notify("changed", {"repositoryId": store.identity["id"]})
            return None
        if method == "close":
            session.core.cancelled.set()
            if session.worker:
                session.worker.join()
            store.close()
            del self.sessions[params["repositoryId"]]
            return None
        if method == "settings":
            config = settings_from(params["settings"])
            if busy:
                session.pending_settings = config
                session.core.cancelled.set()
                return None
            session.core.config = config
            session.core.cache.clear()
            session.interval_summaries.clear()
            session.force_full = True
            session.event()
            return None
        if method == "timeline":
            return session.timeline(int(params.get("limit", 50)))
        if method == "snapshotChanges":
            return session.snapshot_changes(params["snapshotId"])
        if method == "diff":
            return session.diff(params)
        if method == "capture":
            result = session.start_capture("manual")
        elif method == "job":
            if not session.job or session.job["id"] != params["jobId"]:
                raise SafetyError("Capture job is no longer available.")
            return dict(session.job)
        elif method == "cancelCapture":
            session.core.cancelled.set()
            return None
        elif method == "pause":
            session.paused = True
            session.core.cancelled.set()
            session.message = "일시 중지"
            if store.writable:
                from .common import write_json
                write_json(store.directory / "preferences.json", {"schema": 1, "paused": True})
            result = None
        elif method == "resume":
            store.assert_writer()
            if any(unfinished(j) for j in store.journals()):
                raise SafetyError("Review recovery before resuming.")
            session.paused = False
            session.message = "기록 중"
            from .common import write_json
            write_json(store.directory / "preferences.json", {"schema": 1, "paused": False})
            session.event()
            result = None
        elif method == "restorePrepare":
            result = session.restore.prepare(params["snapshotId"])
        elif method == "restoreCancel":
            session.restore.plans.pop(params["planId"], None)
            result = None
        elif method == "undoPrepare":
            plan, original = session.restore.prepare_undo()
            result = {"plan": plan, "undoes": original}
        elif method == "restoreApply":
            self._approved(params, writers=True)
            result = session.restore.apply(params["planId"], kind="undo" if params.get("undoes") else "restore", undoes=params.get("undoes"))
            session.start_capture("restored", full=False, changed_paths={c["path"] for c in result["changes"]})
        elif method == "commitChoices":
            return session.commits.choices()
        elif method == "commitBoundaryReview":
            return session.commits.review(params["snapshotId"], selected=params.get("selected"))
        elif method == "commitPrepare":
            self._approved(params)
            result = session.commits.prepare_boundary(params["snapshotId"], selected=params.get("selected"), review_hash=params.get("reviewHash")) if params.get("snapshotId") else session.commits.prepare(params["selected"])
        elif method == "commitExecute":
            self._approved(params)
            result = session.commits.execute(self._journal(session, params["operationId"]), params["message"], params.get("snapshotIds", []))
            if result["expected"] and result["snapshotIds"] and not result.get("partial"):
                try:
                    review = store.history_before(result["snapshotIds"][0])
                    token = new_id()
                    session.history_reviews[token] = {"review": review, "receiptId": result["id"]}
                    result = {**result, "cleanup": {"token": token, **review}}
                except (OSError, SafetyError):
                    result = {**result, "cleanupError": True}
        elif method == "historyCancel":
            session.history_reviews.pop(params["token"], None)
            result = None
        elif method == "historyDelete":
            self._approved(params)
            prepared = session.history_reviews.pop(params["token"], None)
            if not prepared:
                raise SafetyError("Cleanup review expired. Existing history has been preserved.")
            receipt = read_json(store.record("receipts", prepared["receiptId"]))
            if receipt.get("expected") is not True or prepared["review"]["snapshotId"] not in receipt["snapshotIds"]:
                raise SafetyError("Cleanup requires a verified successful commit linked to the selected snapshot.")
            result = {"deleted": store.delete_history_before(prepared["review"])}
        elif method == "commitCancel":
            result = session.commits.cancel(self._journal(session, params["operationId"]))
        elif method == "suggestCommit":
            title = store.metadata(params["snapshotId"]).get("title", "") if params.get("snapshotId") else ""
            return suggest_commit(params["paths"], title)
        elif method == "renameSnapshot":
            store.snapshot(params["snapshotId"])
            result = store.update_metadata(params["snapshotId"], title=validate_title(params["title"]))
        elif method == "pushReview":
            review = session.commits.push_review()
            token = new_id()
            session.push_reviews[token] = review
            return {"token": token, **review}
        elif method == "pushExecute":
            self._approved(params)
            review = session.push_reviews.pop(params["token"], None)
            if not review:
                raise SafetyError("Push review expired.")
            result = session.commits.push(review)
        elif method == "pin":
            store.snapshot(params["snapshotId"])
            result = store.update_metadata(params["snapshotId"], pinned=not store.metadata(params["snapshotId"])["pinned"])
        elif method == "usage":
            protected = store.protected_ids()
            snapshots = []
            for s in store.snapshots():
                internal = store.is_internal_baseline(s)
                fallback_title = "이전 내부 비교 기준" if internal else s.get("summary", "복구 백업" if s["reason"] == "backup" else "저장된 파일 상태")
                snapshots.append({"id": s["id"], "endedAt": s["endedAt"], "reason": s["reason"],
                                  "internalBaseline": internal, "protected": s["id"] in protected,
                                  "title": store.metadata(s["id"]).get("title") or fallback_title, "context": s["context"]})
            return {"bytes": store.usage(), "limit": session.core.config["maxStorageMiB"] * 1024 * 1024,
                    "snapshots": snapshots}
        elif method == "prune":
            result = store.prune(session.core.config)
        elif method == "deleteSnapshot":
            self._approved(params)
            store.delete_snapshot(params["snapshotId"])
            result = store.gc()
        elif method == "recoveryReport":
            journal = self._journal(session, params["operationId"])
            return {"journal": journal, "files": session.restore.report(journal) if journal["kind"] != "commit" else [],
                    "currentContext": session.git.context(), "preservedFiles": session.restore.files.preserved(journal)}
        elif method == "recover":
            self._approved(params, writers=True)
            journal = self._journal(session, params["operationId"])
            if journal["kind"] == "commit":
                session.commits.cancel(journal)
            else:
                session.restore.rollback(journal)
            result = journal
        elif method == "resolveRecovery":
            self._approved(params)
            journal = self._journal(session, params["operationId"])
            journal["phase"] = "resolved"
            journal["resolvedAt"] = utc_now()
            result = store.journal(journal)
        elif method == "exportBackup":
            self._approved(params)
            snapshot = store.snapshot(params["snapshotId"], verify=True)
            journal = self._journal(session, params["operationId"]) if params.get("operationId") else None
            if journal and journal["backupId"] != snapshot["id"]:
                raise SafetyError("Preserved files must belong to the selected recovery backup.")
            destination = Path(params["destination"]).resolve(strict=True)
            if destination.is_relative_to(session.git.root) or destination.is_relative_to(store.directory):
                raise SafetyError("Choose an export directory outside the working repository and snapshot storage.")
            export = destination / ("dev-snapshot-recovery-" + new_id())
            export.mkdir(mode=0o700)
            for name, entry in snapshot["entries"].items():
                if entry["state"] == "present":
                    atomic_write(safe_path(export, name), store.get(entry["hash"], entry["size"]), 0o700 if entry["executable"] else 0o600)
            if journal:
                session.restore.files.export(journal, export)
            result = str(export)
        else:
            raise SafetyError("Unknown engine method.")
        self.notify("changed", {"repositoryId": store.identity["id"]})
        return result

    @staticmethod
    def _approved(params: dict, *, writers: bool = False) -> None:
        if params.get("approved") is not True or writers and params.get("writersStopped") is not True:
            raise SafetyError("Explicit review approval is required.")

    @staticmethod
    def _journal(session: Session, operation_id: str) -> dict:
        journal = next((j for j in session.store.journals() if j["id"] == operation_id), None)
        if not journal:
            raise SafetyError("Operation journal not found.")
        return journal

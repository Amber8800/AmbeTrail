// Wire types only. Filesystem/Git policy and every mutation live in Python.
export type Entry = { state: 'present'; hash: string; size: number; executable: boolean } | { state: 'absent' } | { state: 'excluded'; reason: string };
export interface Change { path: string; kind?: string; before: Entry; after: Entry }
export interface GitContext { head: string | null; branch: string | null; indexHash: string; busy: boolean; sparse: boolean }
export interface Journal { id: string; kind: 'restore' | 'undo' | 'commit'; mode?: 'boundary'; phase: string; createdAt: string; backupId: string; targetId?: string; changes: Change[]; conflicts: string[]; selected?: string[]; preparedTree?: string; context?: GitContext; indexLockOwnerPid?: number }
export interface Repository { id: string; root: string; writable: boolean; paused: boolean; status: string; dirtyCount: number; latestAt: string | null; branch: string | null; excludedCount: number; recoveries: Journal[]; agentHistoryEnabled?: boolean; agentHistoryPresent?: boolean; agentHistoryProblem?: string }
export interface SnapshotItem { id: string; reason: string; endedAt: string; summary: string; changedFiles: number; commitSummary?: string | null; commitChangedFiles?: number | null; comparisonPruned: boolean; boundaryState?: 'ready' | 'partial' | 'committed' | 'unavailable'; rangeError?: string | null; metadata: { pinned: boolean; validation: string; commitIds: string[]; title?: string }; context: GitContext; agentTitle?: string; agentDescription?: string; agentSource?: string }
export interface AgentEvidence { snapshotId: string; treeHash: string; context: GitContext; changedPaths: string[]; changedFiles: number }
export interface AgentReport { id: string; title: string; description: string; source: 'codex-stop'; phase: 'final_answer'; collectedAt: string; occurredAt: null; sessionId: string; turnId: string; association: { status: 'confirmed' | 'uncertain' | 'unlinked'; evidence: AgentEvidence[] } }
export interface AgentLinkReview { messageId: string; title: string; evidence: AgentEvidence[]; reviewHash: string }
export interface RestorePlan { id: string; targetId: string; backupId: string; context: GitContext; changes: Change[]; skipped: { path: string; reason: string }[]; planHash: string }
export interface Diff { path: string; left: string; right: string; leftAbsent: boolean; rightAbsent: boolean }
export interface HistoryReview { token: string; snapshotId: string; cutoffAt: string; branch: string; candidates: { id: string; endedAt: string; reason: string; branch: string }[]; protectedCount: number }
export interface BoundaryReview { snapshotId: string; boundaryId: string | null; context: GitContext; changes: Change[]; omitted: Change[]; reviewHash: string; skipped: { path: string; reason: string }[]; summary: string; message: string }
export interface Receipt { oid: string; expected: boolean; actualTree: string; reviewedTree: string; partial?: boolean; cleanup?: HistoryReview; cleanupError?: boolean }
export interface PushReview { token: string; remote: string; branch: string; context: GitContext; commits: string[] }
export interface CaptureJob { id: string; state: 'running' | 'complete' | 'failed' | 'cancelled'; completed: number; total: number; snapshotId: string | null; message: string }

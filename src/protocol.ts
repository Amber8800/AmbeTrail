// Wire types only. Filesystem/Git policy and every mutation live in Python.
export type Entry = { state: 'present'; hash: string; size: number; executable: boolean } | { state: 'absent' } | { state: 'excluded'; reason: string };
export interface Change { path: string; kind?: string; before: Entry; after: Entry }
export interface GitContext { head: string | null; branch: string | null; indexHash: string; busy: boolean; sparse: boolean }
export interface Journal { id: string; kind: 'restore' | 'undo' | 'commit'; phase: string; createdAt: string; backupId: string; targetId?: string; changes: Change[]; conflicts: string[]; selected?: string[]; preparedTree?: string }
export interface Repository { id: string; root: string; writable: boolean; paused: boolean; status: string; dirtyCount: number; latestAt: string | null; branch: string | null; excludedCount: number; recoveries: Journal[] }
export interface SnapshotItem { id: string; reason: string; endedAt: string; summary: string; changedFiles: number; comparisonPruned: boolean; metadata: { pinned: boolean; validation: string; commitIds: string[] }; context: GitContext }
export interface RestorePlan { id: string; targetId: string; backupId: string; context: GitContext; changes: Change[]; skipped: { path: string; reason: string }[]; planHash: string }
export interface Diff { path: string; left: string; right: string; leftAbsent: boolean; rightAbsent: boolean }
export interface HistoryReview { token: string; snapshotId: string; cutoffAt: string; candidates: { id: string; endedAt: string; reason: string }[]; protectedCount: number }
export interface Receipt { oid: string; expected: boolean; actualTree: string; reviewedTree: string; cleanup?: HistoryReview; cleanupError?: boolean }
export interface PushReview { token: string; remote: string; branch: string; context: GitContext; commits: string[] }
export interface CaptureJob { id: string; state: 'running' | 'complete' | 'failed' | 'cancelled'; completed: number; total: number; snapshotId: string | null; message: string }

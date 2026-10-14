import { Repository, SnapshotItem } from './protocol';

export function compactTime(value: string, now = new Date()): string {
  const date = new Date(value);
  const time = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
  return date.toDateString() === now.toDateString() ? time : `${date.getMonth() + 1}/${date.getDate()} ${time}`;
}

export function workTitle(snapshot: SnapshotItem): string {
  return snapshot.metadata.title || (snapshot.reason === 'initial' ? '작업 시작 상태' : snapshot.summary);
}

export function repositoryStatus(repo: Repository): string {
  return [repo.status, repo.dirtyCount ? `미저장 ${repo.dirtyCount}` : '', repo.excludedCount ? `제외 ${repo.excludedCount}` : ''].filter(Boolean).join(' · ');
}

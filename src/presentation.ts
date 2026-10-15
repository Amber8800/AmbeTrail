import { GitContext, Repository, SnapshotItem } from './protocol';

const branchColors = ['charts.blue', 'charts.green', 'charts.purple', 'charts.orange', 'charts.yellow', 'charts.red'];

export function branchColor(branch: string | null): string {
  if (!branch) return 'descriptionForeground';
  let hash = 0;
  for (const character of branch) hash = (Math.imul(hash, 31) + character.codePointAt(0)!) >>> 0;
  return branchColors[hash % branchColors.length]!;
}

export function branchLabel(context: GitContext): string {
  return context.branch?.replace(/^refs\/heads\//, '') || '브랜치 없음';
}

export function snapshotContext(context: GitContext): string {
  return `브랜치: ${branchLabel(context)}\nHEAD: ${context.head?.slice(0, 8) || '커밋 없음'}`;
}

export function compactTime(value: string, now = new Date()): string {
  const date = new Date(value);
  const time = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
  return date.toDateString() === now.toDateString() ? time : `${date.getMonth() + 1}/${date.getDate()} ${time}`;
}

export function workTitle(snapshot: SnapshotItem): string {
  return snapshot.metadata.title || snapshot.summary;
}

export function repositoryStatus(repo: Repository): string {
  return [repo.status, repo.dirtyCount ? `미저장 ${repo.dirtyCount}` : '', repo.excludedCount ? `제외 ${repo.excludedCount}` : ''].filter(Boolean).join(' · ');
}

import * as vscode from 'vscode';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { PythonBridge } from './bridge';
import { ReviewDocuments } from './documents';
import { compactTime, repositoryStatus, workTitle } from './presentation';
import { BoundaryReview, CaptureJob, Change, Diff, GitContext, Journal, PushReview, Receipt, Repository, RestorePlan, SnapshotItem } from './protocol';

interface GitRepository { rootUri: vscode.Uri; state: { onDidChange: vscode.Event<void> } }
interface GitApi { git: { path: string }; repositories: GitRepository[]; onDidOpenRepository: vscode.Event<GitRepository>; onDidCloseRepository: vscode.Event<GitRepository> }
interface GitExtension { enabled: boolean; getAPI(version: 1): GitApi; onDidChangeEnablement: vscode.Event<boolean> }

type Kind = 'repository' | 'snapshot' | 'file' | 'message' | 'recovery' | 'more';
class Item extends vscode.TreeItem {
  constructor(readonly kind: Kind, label: string, readonly repository?: Repository, readonly snapshot?: SnapshotItem, readonly change?: Change, readonly journal?: Journal) {
    super(label, ['repository', 'snapshot'].includes(kind) ? vscode.TreeItemCollapsibleState.Collapsed : vscode.TreeItemCollapsibleState.None);
    this.contextValue = kind;
  }
}

class Timeline implements vscode.TreeDataProvider<Item> {
  private emitter = new vscode.EventEmitter<Item | undefined>();
  readonly onDidChangeTreeData = this.emitter.event;
  readonly limits = new Map<string, number>();
  constructor(readonly app: Application) {}
  refresh(): void { this.emitter.fire(undefined); }
  getTreeItem(item: Item): vscode.TreeItem { return item; }
  async getChildren(item?: Item): Promise<Item[]> {
    if (!item) {
      if (!this.app.repositories.length) return [new Item('message', this.app.status)];
      return this.app.repositories.map(repo => {
        const node = new Item('repository', path.basename(repo.root), repo);
        node.description = repositoryStatus(repo);
        node.tooltip = `기록 범위: ${repo.root}\n${repo.branch ?? '브랜치 미확인'}\n마지막 기록: ${repo.latestAt ? new Date(repo.latestAt).toLocaleString() : '없음'}\n미저장 편집 ${repo.dirtyCount}개 미포함 / 제외 파일 ${repo.excludedCount}개\n로컬 기록은 원격 백업이 아닙니다.`;
        node.iconPath = new vscode.ThemeIcon(repo.recoveries.length ? 'warning' : repo.paused ? 'debug-pause' : 'repo');
        return node;
      });
    }
    const repo = item.repository;
    if (!repo) return [];
    if (item.kind === 'repository') {
      const limit = this.limits.get(repo.id) ?? 50;
      const snapshots = await this.app.call<SnapshotItem[]>('timeline', repo, { limit });
      const result = repo.recoveries.map(j => {
        const node = new Item('recovery', `복구 검토: ${j.kind} · ${j.phase}`, repo, undefined, undefined, j);
        node.iconPath = new vscode.ThemeIcon('warning');
        node.command = { command: 'devSnapshot.recover', title: '복구 검토', arguments: [node] };
        return node;
      });
      for (const snapshot of snapshots) {
        const node = new Item('snapshot', workTitle(snapshot), repo, snapshot);
        node.id = `${repo.id}/${snapshot.id}`;
        node.description = compactTime(snapshot.endedAt);
        if (snapshot.boundaryState === 'committed') node.description += ' · 커밋됨';
        node.tooltip = `${workTitle(snapshot)}\n${new Date(snapshot.endedAt).toLocaleString()}\n이번 기록: 파일 ${snapshot.changedFiles}개 변경 · 미검증${snapshot.metadata.pinned ? ' · 고정' : ''}${snapshot.metadata.commitIds.length ? ' · 커밋 연결' : ''}\n자동 요약: ${snapshot.summary}\n${snapshot.reason === 'initial' ? '작업 시작 상태 / Git 기준 변경' : snapshot.reason}\n커밋 버튼: 직전 커밋 경계부터 이 시점까지 / 복원 버튼: 이 스냅샷 복원\n우클릭 → 작업 이름 편집\n복원 시 객체 무결성을 검사합니다. 정상 동작을 검증한 상태는 아닙니다.\n${snapshot.comparisonPruned ? '이전 비교 기록이 삭제되어 포함 파일 기준으로 표시합니다.' : ''}`;
        if (snapshot.commitSummary && snapshot.commitChangedFiles != null) node.tooltip += `\n커밋 대상 구간: ${snapshot.commitSummary} · 파일 ${snapshot.commitChangedFiles}개`;
        if (snapshot.rangeError) node.tooltip += `\n커밋 구간 계산 불가: ${snapshot.rangeError}\n이번 기록의 요약은 그대로 표시합니다.`;
        node.iconPath = new vscode.ThemeIcon(snapshot.metadata.pinned ? 'pinned' : snapshot.boundaryState === 'committed' ? 'check' : 'circle-small-filled');
        result.push(node);
      }
      if (snapshots.length === limit && limit < 1000) {
        const more = new Item('more', '더 보기', repo);
        more.command = { command: 'devSnapshot.more', title: '더 보기', arguments: [more] }; result.push(more);
      }
      if (!result.length) result.push(new Item('message', '아직 기록이 없습니다.', repo));
      return result;
    }
    if (item.kind === 'snapshot' && item.snapshot) {
      const changes = await this.app.call<Change[]>('snapshotChanges', repo, { snapshotId: item.snapshot.id });
      const filenames = new Map<string, number>();
      for (const change of changes) { const name = path.posix.basename(change.path); filenames.set(name, (filenames.get(name) ?? 0) + 1); }
      return changes.map(change => {
        const kind = change.kind === 'added' ? '추가' : change.kind === 'deleted' ? '삭제' : '수정';
        const node = new Item('file', path.posix.basename(change.path), repo, item.snapshot, change);
        node.id = `${repo.id}/${item.snapshot!.id}/${change.path}`;
        node.description = (filenames.get(path.posix.basename(change.path)) ?? 0) > 1 ? `${kind} · ${path.posix.dirname(change.path)}` : kind;
        node.tooltip = `${change.path}\n${kind}${change.after.state === 'present' ? ` · ${change.after.size.toLocaleString()} B` : ''}`;
        node.iconPath = new vscode.ThemeIcon(change.kind === 'added' ? 'diff-added' : change.kind === 'deleted' ? 'diff-removed' : 'diff-modified');
        node.command = { command: 'devSnapshot.openDiff', title: 'Diff', arguments: [node] };
        return node;
      });
    }
    return [];
  }
}

class Application {
  repositories: Repository[] = [];
  status = 'Python 코어 시작 중…';
  readonly timeline = new Timeline(this);
  readonly documents = new ReviewDocuments();
  private timer?: NodeJS.Timeout;
  private listeners = new Map<string, vscode.Disposable[]>();
  private gitApi?: GitApi;
  private stopped = false;
  private errorShown = new Set<string>();
  constructor(readonly context: vscode.ExtensionContext, readonly bridge: PythonBridge) {}
  async start(): Promise<void> {
    this.context.subscriptions.push(vscode.window.registerTreeDataProvider('devSnapshot.timeline', this.timeline), vscode.workspace.registerTextDocumentContentProvider('dev-snapshot', this.documents));
    this.commands();
    this.bridge.on('changed', () => this.scheduleRefresh());
    this.bridge.on('failure', (error: Error) => { this.status = error.message; this.timeline.refresh(); void vscode.window.showErrorMessage(error.message); });
    await this.bridge.start();
    const extension = vscode.extensions.getExtension<GitExtension>('vscode.git');
    const git = extension ? await Promise.resolve(extension.activate()).catch(() => undefined) : undefined;
    if (git?.enabled) {
      const api = git.getAPI(1); this.gitApi = api;
      for (const repo of api.repositories) await this.add(repo);
      this.context.subscriptions.push(api.onDidOpenRepository(repo => { void this.add(repo).catch(e => this.error(e)); }), api.onDidCloseRepository(repo => { void this.remove(repo).catch(e => this.error(e)); }));
    } else {
      await this.bridge.call('offline', { storageRoot: this.context.globalStorageUri.fsPath, roots: (vscode.workspace.workspaceFolders ?? []).map(f => f.uri.fsPath) });
      this.status = '내장 Git 확장을 활성화하면 기록을 시작할 수 있습니다.';
    }
    if (git) this.context.subscriptions.push(git.onDidChangeEnablement(() => { void vscode.window.showInformationMessage('Git 활성화 상태가 변경되었습니다. Amberog을 다시 연결하려면 창을 다시 로드하세요.', '창 다시 로드').then(choice => { if (choice) void vscode.commands.executeCommand('workbench.action.reloadWindow'); }); }));
    this.context.subscriptions.push(
      vscode.workspace.onDidSaveTextDocument(doc => this.event(doc.uri)),
      vscode.workspace.onDidChangeTextDocument(() => this.dirty()),
      vscode.workspace.onDidCloseTextDocument(() => this.dirty()),
      vscode.workspace.onDidChangeConfiguration(event => {
        if (!event.affectsConfiguration('devSnapshot')) return;
        if (event.affectsConfiguration('devSnapshot.pythonPath')) { void vscode.window.showInformationMessage('Python 실행 파일 변경은 창을 다시 로드하면 적용됩니다.'); return; }
        for (const repo of this.repositories) void this.call('settings', repo, { settings: this.settings(repo.root) }).catch(e => this.error(e));
      }),
    );
    await this.refresh(); this.dirty();
    this.status = this.repositories.length ? '기록 중' : '열린 Git 저장소가 없습니다.';
    this.timeline.refresh();
  }
  private settings(root: string): object {
    const config = vscode.workspace.getConfiguration('devSnapshot', vscode.Uri.file(root));
    const keys = ['idleSeconds', 'maxIntervalSeconds', 'reconcileSeconds', 'fullHashSeconds', 'maxFileMiB', 'retentionDays', 'maxSnapshots', 'maxStorageMiB', 'exclude'];
    const value: Record<string, unknown> = Object.fromEntries(keys.map(key => [key, config.get(key)]));
    // Never accept a workspace/folder value that weakens sensitive filename exclusions.
    const sensitive = vscode.workspace.getConfiguration('devSnapshot').inspect<string[]>('allowSensitiveFiles');
    value.allowSensitiveFiles = sensitive?.globalValue ?? sensitive?.defaultValue ?? [];
    return value;
  }
  private async add(apiRepo: GitRepository): Promise<void> {
    const uri = apiRepo.rootUri;
    if (uri.scheme !== 'file') return;
    const watcher = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(uri, '**/*'));
    let repository: Repository | undefined;
    let earlyEvent = false;
    const changed = (changedUri?: vscode.Uri): void => {
      const relative = changedUri ? this.relative(uri.fsPath, changedUri.fsPath) : undefined;
      if (repository) this.bridge.notify('fileEvent', { repositoryId: repository.id, path: relative?.split('/').some(p => p.toLowerCase() === '.git') ? undefined : relative }); else earlyEvent = true;
    };
    const disposables = [watcher, watcher.onDidCreate(changed), watcher.onDidChange(changed), watcher.onDidDelete(changed), apiRepo.state.onDidChange(() => changed())];
    try {
      repository = await this.bridge.call<Repository>('open', { root: uri.fsPath, gitPath: this.gitApi?.git.path, storageRoot: this.context.globalStorageUri.fsPath, settings: this.settings(uri.fsPath) });
      this.listeners.get(repository.id)?.forEach(d => d.dispose());
      this.listeners.set(repository.id, disposables);
      if (earlyEvent) changed();
      await this.refresh(); this.dirty();
    } catch (e) { disposables.forEach(d => d.dispose()); this.status = '저장소 기록을 시작하지 못했습니다.'; throw e; }
  }
  private async remove(repo: GitRepository): Promise<void> {
    const found = this.repositories.find(r => path.resolve(r.root) === path.resolve(repo.rootUri.fsPath));
    if (found) {
      this.listeners.get(found.id)?.forEach(d => d.dispose()); this.listeners.delete(found.id);
      await this.call('close', found); await this.refresh();
    }
  }
  private event(uri: vscode.Uri): void {
    if (uri.scheme !== 'file') return;
    for (const repo of this.repositories) {
      const relative = this.relative(repo.root, uri.fsPath);
      if (relative !== undefined) this.bridge.notify('fileEvent', { repositoryId: repo.id, path: relative });
    }
    this.dirty();
  }
  private relative(root: string, target: string): string | undefined {
    const name = path.relative(root, target);
    return name && !path.isAbsolute(name) && name !== '..' && !name.startsWith(`..${path.sep}`) ? name.split(path.sep).join('/') : undefined;
  }
  private dirty(): void {
    for (const repo of this.repositories) {
      const paths = vscode.workspace.textDocuments.filter(d => d.isDirty && d.uri.scheme === 'file').map(d => this.relative(repo.root, d.uri.fsPath)).filter((p): p is string => p !== undefined);
      this.bridge.notify('dirty', { repositoryId: repo.id, paths });
      repo.dirtyCount = paths.length;
    }
    this.timeline.refresh();
  }
  call<T = unknown>(method: string, repo: Repository, params: object = {}): Promise<T> { return this.bridge.call<T>(method, { repositoryId: repo.id, ...params }); }
  private scheduleRefresh(): void { if (!this.timer && !this.stopped) this.timer = setTimeout(() => { this.timer = undefined; void this.refresh().catch(e => this.error(e)); }, 200); }
  private async refresh(): Promise<void> { if (this.stopped) return; this.repositories = await this.bridge.call<Repository[]>('list'); this.timeline.refresh(); }
  private error(error: unknown): void {
    const message = error instanceof Error ? error.message : 'Amberog 작업에 실패했습니다.';
    if (!this.errorShown.has(message)) { this.errorShown.add(message); void vscode.window.showErrorMessage(message); }
  }
  private async repository(item?: Item): Promise<Repository | undefined> {
    if (item?.repository) return this.repositories.find(r => r.id === item.repository!.id);
    if (!this.repositories.length) {
      void vscode.window.showInformationMessage('Git 저장소 탐색이 끝난 뒤 다시 실행하세요. 아직 인식된 저장소가 없습니다.');
      return undefined;
    }
    if (this.repositories.length === 1) return this.repositories[0];
    const picked = await vscode.window.showQuickPick(this.repositories.map(repo => ({ label: path.basename(repo.root), description: repo.root, repo })), { title: '저장소 선택' });
    return picked?.repo;
  }
  private async snapshot(repo: Repository, item?: Item): Promise<SnapshotItem | undefined> {
    if (item?.snapshot) return item.snapshot;
    const snapshots = await this.call<SnapshotItem[]>('timeline', repo, { limit: 1000 });
    const picked = await vscode.window.showQuickPick(snapshots.map(s => ({ label: workTitle(s), description: compactTime(s.endedAt), detail: `파일 ${s.changedFiles}개 · ${new Date(s.endedAt).toLocaleString()}`, snapshot: s })), { title: '작업 선택' });
    return picked?.snapshot;
  }
  private async reviewFiles(repo: Repository, paths: string[], params: object, title: string): Promise<boolean> {
    for (;;) {
      const items = [{ label: '검토 완료', description: '다음 승인 단계로 이동', file: undefined as string | undefined }, ...paths.map(file => ({ label: file, description: 'Diff 열기', file }))];
      const choice = await vscode.window.showQuickPick(items, { title, placeHolder: '파일을 선택해 차이를 검토하고, 검토 완료를 선택하세요.' });
      if (!choice) return false;
      if (!choice.file) return true;
      const diff = await this.call<Diff>('diff', repo, { ...params, path: choice.file });
      await this.documents.diff(diff, title);
    }
  }
  private async restore(repo: Repository, undo: boolean, item?: Item): Promise<void> {
    const target = undo ? undefined : await this.snapshot(repo, item); if (!undo && !target) return;
    this.dirty();
    const prepared = await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: 'Amberog: 복원 영향 분석 및 보호 백업 중' }, async () => undo ? await this.call<{ plan: RestorePlan; undoes: string }>('undoPrepare', repo) : { plan: await this.call<RestorePlan>('restorePrepare', repo, { snapshotId: target!.id }), undoes: undefined });
    const { plan } = prepared;
    try {
      await this.documents.text('복원 검토', [
        `저장소: ${repo.root}`, `작업: ${undo ? '복원 취소' : '스냅샷 복원'}`, `현재 HEAD: ${plan.context.head ?? '커밋 없음'}`,
        `보호 백업: ${plan.backupId}`, `영향 파일: ${plan.changes.length}`, `보호되어 제외된 파일: ${plan.skipped.length}`,
        '', '변경 목록:', ...plan.changes.map(c => `${c.before.state === 'absent' ? '생성' : c.after.state === 'absent' ? '삭제' : '수정'}  ${c.path}`),
        '', '제외 목록:', ...plan.skipped.map(s => `${s.path}: ${s.reason}`), '',
        '브랜치와 인덱스는 유지됩니다. 미저장 편집은 복원 대상이 아닙니다.',
        '외부 에이전트와 파일 수정 프로세스를 중지해야 합니다. 다중 파일 복원은 원자적이지 않습니다.',
      ].join('\n'));
      if (!plan.changes.length) { void vscode.window.showInformationMessage('현재 관리 파일이 복원 대상과 일치합니다.'); return; }
      if (!await this.reviewFiles(repo, plan.changes.map(c => c.path), { kind: 'restore', planId: plan.id }, '복원 영향 검토')) return;
      const approval = await vscode.window.showWarningMessage(`파일 ${plan.changes.length}개를 변경합니다. 삭제 ${plan.changes.filter(c => c.after.state === 'absent').length}개. 백업을 확인했고 외부 파일 수정 프로세스를 중지했습니까?`, { modal: true }, '수정 중지 확인 · 복원');
      if (!approval) return;
      this.dirty();
      await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: 'Amberog: 복원 및 검증 중' }, () => this.call('restoreApply', repo, { planId: plan.id, undoes: prepared.undoes, approved: true, writersStopped: true }));
      void vscode.window.showInformationMessage('복원이 완료되었습니다. 코드의 정상 동작은 별도로 검증하세요.');
    } finally { await this.call('restoreCancel', repo, { planId: plan.id }); await this.refresh(); }
  }
  private async commit(repo: Repository, item?: Item): Promise<void> {
    const reference = await this.snapshot(repo, item);
    if (!reference) return;
    const review = await this.call<BoundaryReview>('commitBoundaryReview', repo, { snapshotId: reference.id });
    if (!review.changes.length) { void vscode.window.showInformationMessage('이 구간에는 커밋할 최종 변경이 없습니다.'); return; }
    await this.documents.text('스냅샷 구간 커밋 검토', [
      `작업: ${review.summary}`, `시작 경계: ${review.boundaryId ?? '현재 브랜치의 기준 HEAD'}`,
      `완료 경계: ${reference.id} · ${new Date(reference.endedAt).toLocaleString()}`, `부모 커밋: ${review.context.head ?? '첫 커밋'}`,
      `구간 변경: ${review.changes.length}개 파일`, '이 구간의 변경 전체를 한 커밋으로 만듭니다. 최신 작업 파일은 유지합니다.', '',
      ...review.changes.map(c => `${c.kind}  ${c.path}`), '', '관리 범위에서 제외되어 기존 Git 상태를 유지하는 파일:',
      ...review.skipped.map(s => `${s.path}: ${s.reason}`),
    ].join('\n'));
    const message = await vscode.window.showInputBox({ title: '이 구간의 커밋 메시지', value: review.message, validateInput: text => !text.trim() ? '메시지가 필요합니다.' : undefined });
    if (!message) return;
    if (!await vscode.window.showWarningMessage('선택한 스냅샷을 별도 작업 폴더와 임시 인덱스에서 준비하고 실제 Git 변환 결과를 검토합니다. Git 필터를 사용합니다. 다른 Git 쓰기 작업을 중지하세요.', { modal: true }, '구간 커밋 준비 승인')) return;
    this.dirty();
    const journal = await this.call<Journal>('commitPrepare', repo, { snapshotId: reference.id, approved: true });
    let executed = false;
    try {
      if (!await this.reviewFiles(repo, journal.selected ?? [], { kind: 'boundaryStaged', operationId: journal.id }, '구간 전체의 실제 Staging 검토')) return;
      if (!await vscode.window.showWarningMessage(`이 시점까지의 변경 전체(${journal.selected?.length ?? 0}개 파일)를 현재 브랜치에 커밋합니다.\n${message}\n최신 작업 파일을 유지합니다. Git 훅·서명은 별도 작업 폴더에서 실행하며, 프로젝트 의존성이 필요하면 실패할 수 있습니다.`, { modal: true }, '구간 전체 커밋 승인')) return;
      this.dirty(); executed = true;
      const receipt = await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: 'Amberog: 커밋 실행 및 검증 중' }, () => this.call<Receipt>('commitExecute', repo, { operationId: journal.id, message, snapshotIds: [reference.id], approved: true }));
      if (receipt.expected) void vscode.window.showInformationMessage(`커밋 완료: ${receipt.oid.slice(0, 10)}. Push는 별도 승인입니다.`);
      else void vscode.window.showWarningMessage('커밋이 생성되었지만 검토한 상태와 다릅니다. 자동으로 이력을 변경하지 않았습니다. 복구 검토를 여세요.');
      if (receipt.cleanupError) void vscode.window.showWarningMessage('커밋은 완료되었지만 이전 기록의 삭제 목록을 준비하지 못했습니다. 기록 관리에서 확인하세요.');
      if (receipt.cleanup) {
        const review = receipt.cleanup;
        try {
          if (review.candidates.length) {
            await this.documents.text('커밋 후 이전 스냅샷 정리', [
              `완료된 커밋: ${receipt.oid}`, `선택한 스냅샷: ${review.snapshotId}`, `기준 시각: ${new Date(review.cutoffAt).toLocaleString()}`,
              `삭제 대상: ${review.candidates.length}개 / 보호되어 유지: ${review.protectedCount}개`,
              '선택한 스냅샷과 이후 기록은 유지합니다. 고정 기록·활성 작업 시작 상태·복구 백업은 보호합니다.',
              '기록 관리에서 제외한 파일과 Git 변환 전 원본 바이트는 Git 커밋의 보존 범위와 다를 수 있습니다.',
              '삭제한 기록은 되돌릴 수 없습니다. 취소하면 모든 기록을 유지합니다.', '',
              ...review.candidates.map(s => `${new Date(s.endedAt).toLocaleString()} · ${s.reason} · ${s.id}`),
            ].join('\n'));
            if (await vscode.window.showWarningMessage(`커밋이 완료되었습니다. 선택한 스냅샷보다 오래된 기록 ${review.candidates.length}개를 영구 삭제할까요? 보호 기록 ${review.protectedCount}개는 유지합니다.`, { modal: true }, '이전 기록 삭제 승인')) {
              const result = await this.call<{ deleted: number }>('historyDelete', repo, { token: review.token, approved: true });
              void vscode.window.showInformationMessage(`이전 스냅샷 ${result.deleted}개를 삭제했습니다.`);
            }
          }
        } finally { await this.call('historyCancel', repo, { token: review.token }); }
      }
    } finally { if (!executed) await this.call('commitCancel', repo, { operationId: journal.id }); await this.refresh(); }
  }
  private async push(repo: Repository): Promise<void> {
    const review = await this.call<PushReview>('pushReview', repo);
    await this.documents.text('Push 검토', [`원격: ${review.remote}`, `대상 브랜치: ${review.branch}`, `전송할 OID: ${review.context.head}`, '', '로컬 upstream 기준 커밋 목록:', ...review.commits, '', '원격 상태를 자동 fetch하지 않습니다. 강제 Push와 태그 일괄 Push를 하지 않습니다.'].join('\n'));
    if (!await vscode.window.showWarningMessage(`${review.remote}의 ${review.branch}로 검토한 커밋을 전송합니까?`, { modal: true }, 'Push 승인')) return;
    await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: 'Amberog: Push 중' }, () => this.call('pushExecute', repo, { token: review.token, approved: true }));
    void vscode.window.showInformationMessage('Push가 완료되었습니다.');
  }
  private async recover(repo: Repository, item?: Item): Promise<void> {
    let journal = item?.journal;
    if (!journal) journal = (await vscode.window.showQuickPick(repo.recoveries.map(j => ({ label: `${j.kind}: ${j.phase}`, description: j.createdAt, journal: j })), { title: '미완료 작업 선택' }))?.journal;
    if (!journal) return;
    const report = await this.call<{ journal: Journal; files: { path: string; status: string }[]; currentContext: GitContext }>('recoveryReport', repo, { operationId: journal.id });
    await this.documents.text('복구 상태', [`작업: ${journal.id}`, `상태: ${journal.phase}`, `보호 백업: ${journal.backupId}`, `작업 전 HEAD: ${report.journal.context?.head ?? '없음'}`, `현재 HEAD: ${report.currentContext.head ?? '없음'}`, `Git 작업 또는 잠금 감지: ${report.currentContext.busy ? '있음' : '없음'}`, '', ...report.files.map(f => `${f.status}  ${f.path}`), '', 'before: 작업 이전 / after: 확장 적용 결과 / conflict: 별도 수정 또는 불확실', '충돌 파일은 자동으로 덮어쓰지 않습니다. 커밋 이력은 자동 reset하지 않습니다.', ...(journal.mode === 'boundary' ? ['구간 커밋은 확인된 커밋 결과에 맞춰 인덱스를 복구하며 최신 작업 파일을 유지합니다.', '강제 종료 후 index.lock이 남으면 다른 Git 프로세스가 없는지 확인하고 수동 정리해야 합니다.', `마지막 잠금 소유 프로세스(참고): ${report.journal.indexLockOwnerPid ?? '미확인'}`] : [])].join('\n'));
    const action = await vscode.window.showQuickPick([journal.mode === 'boundary' ? '생성된 커밋 확인 및 안전한 인덱스 복구' : '안전하게 이전 상태로 복구 시도', '백업 파일을 외부 폴더로 내보내기', '직접 해결했음을 확인하고 기록 재개 허용'], { title: '복구 작업 선택' });
    if (!action) return;
    if (action.startsWith('안전하게') || action.startsWith('생성된')) {
      if (!await vscode.window.showWarningMessage('현재 파일과 Git 상태를 재검사하여 안전한 부분만 복구합니다. 외부 수정을 중지했습니까?', { modal: true }, '수정 중지 확인 · 복구')) return;
      this.dirty(); await this.call('recover', repo, { operationId: journal.id, approved: true, writersStopped: true });
    } else if (action.startsWith('백업')) {
      const destination = await vscode.window.showOpenDialog({ canSelectFolders: true, canSelectFiles: false, canSelectMany: false, title: '저장소 밖의 백업 내보내기 폴더 선택' });
      if (!destination?.[0]) return;
      const result = await this.call<string>('exportBackup', repo, { snapshotId: journal.backupId, destination: destination[0].fsPath, approved: true });
      void vscode.window.showInformationMessage(`보호 백업을 내보냈습니다: ${result}`);
    } else {
      if (!await vscode.window.showWarningMessage('작업 파일과 Git 상태를 직접 검토·해결했음을 확인합니까? 이 동작은 파일을 복구하지 않습니다.', { modal: true }, '해결 확인')) return;
      await this.call('resolveRecovery', repo, { operationId: journal.id, approved: true });
    }
    await this.refresh();
  }
  private async manage(repo: Repository): Promise<void> {
    const usage = await this.call<{ bytes: number; limit: number; snapshots: { id: string; endedAt: string; reason: string; protected: boolean }[] }>('usage', repo);
    const action = await vscode.window.showQuickPick(['보존 정책으로 정리', '스냅샷 삭제', repo.paused ? '자동 기록 재개' : '자동 기록 일시 중지'], { title: `기록 관리 · ${(usage.bytes / 1048576).toFixed(1)} / ${(usage.limit / 1048576).toFixed(0)} MiB` });
    if (!action) return;
    if (action === '보존 정책으로 정리') await this.call('prune', repo);
    else if (action === '스냅샷 삭제') {
      const picked = await vscode.window.showQuickPick(usage.snapshots.filter(s => !s.protected).map(s => ({ label: `${new Date(s.endedAt).toLocaleString()} · ${s.reason}`, id: s.id })), { title: '삭제할 비보호 기록 선택 · 제외 정책 변경은 과거 기록을 지우지 않습니다' });
      if (picked && await vscode.window.showWarningMessage('이 스냅샷을 영구 삭제합니까? 다른 기록에 참조되지 않는 파일 내용도 정리됩니다.', { modal: true }, '기록 삭제 승인')) await this.call('deleteSnapshot', repo, { snapshotId: picked.id, approved: true });
    } else await this.call(repo.paused ? 'resume' : 'pause', repo);
    await this.refresh();
  }
  private async capture(repo: Repository): Promise<void> {
    await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: 'Amberog: 저장된 파일 기록 중', cancellable: true }, async (progress, token) => {
      let job = await this.call<CaptureJob>('capture', repo);
      const subscription = token.onCancellationRequested(() => { void this.call('cancelCapture', repo).catch(e => this.error(e)); });
      try {
        while (job.state === 'running') {
          progress.report({ message: job.total ? `${job.completed}/${job.total}` : '파일 목록 확인 중…' });
          await new Promise(resolve => setTimeout(resolve, 300));
          job = await this.call<CaptureJob>('job', repo, { jobId: job.id });
        }
        if (job.state === 'failed') throw new Error(job.message);
        if (job.state === 'cancelled') void vscode.window.showInformationMessage('캡처를 취소했습니다. 이미 완료된 기록과 보호 백업은 유지됩니다.');
        else if (!job.snapshotId) void vscode.window.showInformationMessage('직전 기록과 내용이 같아 새 스냅샷을 만들지 않았습니다.');
      } finally { subscription.dispose(); }
    });
  }
  private commands(): void {
    const register = (name: string, action: (repo: Repository, item?: Item) => Promise<unknown>): void => {
      this.context.subscriptions.push(vscode.commands.registerCommand(`devSnapshot.${name}`, async (item?: Item) => {
        try { const repo = await this.repository(item); if (repo) await action(repo, item); } catch (e) { this.error(e); } finally { this.scheduleRefresh(); }
      }));
    };
    register('captureNow', repo => this.capture(repo));
    register('pauseRecording', repo => this.call('pause', repo));
    register('resumeRecording', repo => this.call('resume', repo));
    register('restoreSnapshot', (repo, item) => this.restore(repo, false, item));
    register('undoRestore', repo => this.restore(repo, true));
    register('prepareCommit', (repo, item) => this.commit(repo, item));
    register('renameSnapshot', async (repo, item) => {
      const snapshot = await this.snapshot(repo, item); if (!snapshot) return;
      const title = await vscode.window.showInputBox({
        title: '작업 이름 편집', value: snapshot.metadata.title || snapshot.summary,
        prompt: '예: 복원 안정성 개선. 커밋 메시지 초안에도 사용합니다. 비우면 자동 요약으로 돌아갑니다.',
        validateInput: text => text.length > 100 || /[\x00-\x1f\x7f]/u.test(text) ? '작업 이름은 한 줄, 최대 100자입니다.' : undefined,
      });
      if (title !== undefined) await this.call('renameSnapshot', repo, { snapshotId: snapshot.id, title });
    });
    register('push', repo => this.push(repo));
    register('pinSnapshot', async (repo, item) => { const snapshot = await this.snapshot(repo, item); if (snapshot) await this.call('pin', repo, { snapshotId: snapshot.id }); });
    register('manageHistory', repo => this.manage(repo));
    register('recover', (repo, item) => this.recover(repo, item));
    register('more', async repo => { this.timeline.limits.set(repo.id, (this.timeline.limits.get(repo.id) ?? 50) + 50); this.timeline.refresh(); });
    register('openDiff', async (repo, item) => {
      const snapshot = await this.snapshot(repo, item); if (!snapshot) return;
      let change = item?.change;
      if (!change) change = (await vscode.window.showQuickPick((await this.call<Change[]>('snapshotChanges', repo, { snapshotId: snapshot.id })).map(c => ({ label: c.path, change: c })), { title: '비교 파일 선택' }))?.change;
      if (change) await this.documents.diff(await this.call<Diff>('diff', repo, { snapshotId: snapshot.id, path: change.path }), '스냅샷 비교');
    });
    this.context.subscriptions.push(vscode.commands.registerCommand('devSnapshot.refresh', async () => {
      try { await this.refresh(); return this.repositories; } catch (e) { this.error(e); return []; }
    }));
  }
  async stop(): Promise<void> {
    this.stopped = true; if (this.timer) clearTimeout(this.timer);
    for (const list of this.listeners.values()) list.forEach(d => d.dispose()); this.listeners.clear();
    await this.bridge.stop();
  }
}

let application: Application | undefined;
export async function activate(context: vscode.ExtensionContext): Promise<{ ready: boolean }> {
  if (!vscode.workspace.isTrusted || vscode.env.remoteName || vscode.env.uiKind !== vscode.UIKind.Desktop || vscode.workspace.workspaceFolders?.some(f => f.uri.scheme !== 'file')) return { ready: false };
  const configured = vscode.workspace.getConfiguration('devSnapshot').inspect<string>('pythonPath')?.globalValue;
  let python = configured || (process.platform === 'win32' ? 'python' : 'python3');
  if (!configured && context.extensionMode === vscode.ExtensionMode.Development) {
    const developmentPython = path.join(context.extensionPath, 'log_venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
    if (fs.existsSync(developmentPython)) python = developmentPython;
  }
  const bridge = new PythonBridge(python, path.join(context.extensionPath, 'backend/run.py'), context.extensionPath);
  application = new Application(context, bridge);
  try { await application.start(); return { ready: true }; }
  catch (e) { void vscode.window.showErrorMessage(e instanceof Error ? e.message : 'Amberog 시작 실패'); return { ready: false }; }
}
export async function deactivate(): Promise<void> { await application?.stop(); application = undefined; }

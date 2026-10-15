import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import * as path from 'node:path';
import { EventEmitter } from 'node:events';
import { runInNewContext } from 'node:vm';
import type * as vscode from 'vscode';
import { branchColor, branchLabel, snapshotContext } from '../src/presentation';
import { AgentReport, Change, GitContext, Repository, SnapshotItem } from '../src/protocol';

const context: GitContext = { branch: 'refs/heads/main', head: 'abcdef0123456789', indexHash: '', busy: false, sparse: false };
function snapshot(id: string, overrides: Partial<SnapshotItem> = {}): SnapshotItem {
  return { id, reason: 'manual', endedAt: '2026-10-15T02:00:00Z', summary: 'a.txt 수정', changedFiles: 1,
    comparisonPruned: false, metadata: { pinned: false, validation: 'unverified', commitIds: [], title: '작업 이름' }, context, ...overrides };
}

// Exercise the real activation, provider, and command handlers with isolated VS Code/RPC adapters.
async function harness(snapshots = [snapshot('selected')], workspaceValues = new Map<string, unknown>()) {
  const repo: Repository = { id: 'repo', root: 'C:/fixture', writable: true, paused: true, status: '', dirtyCount: 0,
    latestAt: null, branch: context.branch, excludedCount: 0, recoveries: [] };
  const state = {
    snapshots, protected: new Set<string>(), changes: [] as Change[], warningChoice: undefined as string | undefined,
    warnings: [] as string[], messages: [] as string[], errors: [] as string[], calls: [] as { method: string; params: Record<string, unknown> }[],
    picks: [] as unknown[], pickItems: [] as { label: string; detail?: string }[][], deleteError: undefined as Error | undefined,
    commitChanges: undefined as Change[] | undefined, documents: [] as string[], input: undefined as string | undefined, workspaceValues,
    agentReports: [] as AgentReport[], showAgentReports: false,
  };
  let provider: vscode.TreeDataProvider<vscode.TreeItem> | undefined;
  let webviewProvider: { source: typeof provider; resolveWebviewView(view: unknown): void; dispose(): void };
  let receiveMessage: (message: unknown) => void;
  const viewMessages: Record<string, any>[] = [];
  const webview = {
    html: '', options: {}, cspSource: 'vscode-webview://fixture',
    asWebviewUri: (uri: unknown) => String(uri),
    onDidReceiveMessage: (listener: typeof receiveMessage) => { receiveMessage = listener; return disposable; },
    postMessage: async (message: Record<string, any>) => { viewMessages.push(message); return true; },
  };
  const commands = new Map<string, (item?: vscode.TreeItem) => Promise<unknown>>();
  const disposable = { dispose() {} };
  const event = () => disposable;
  class Bridge extends EventEmitter {
    async start() {}
    async stop() {}
    notify() {}
    async call(method: string, params: Record<string, unknown> = {}) {
      state.calls.push({ method, params });
      if (method === 'list') return [repo];
      if (method === 'timeline') return state.snapshots;
      if (method === 'snapshotChanges') return state.changes;
      if (method === 'agentHistory') return state.agentReports;
      if (method === 'agentConsent') { repo.agentHistoryEnabled = params.enabled as boolean; repo.agentHistoryPresent = true; }
      if (method === 'agentSetup') return { hooks: { Stop: [{ hooks: [{ type: 'command', command: 'python codex_hook.py' }] }] } };
      if (method === 'agentLinkReview') return { messageId: params.messageId, title: '에이전트 보고', evidence: [{ snapshotId: 'selected', treeHash: 'tree', context, changedFiles: 1, changedPaths: ['app.py'] }], reviewHash: 'agent-review' };
      if (method === 'commitBoundaryReview') {
        const available = state.commitChanges ?? state.changes;
        const changes = params.selected === undefined ? available : available.filter(c => (params.selected as string[]).includes(c.path));
        return { snapshotId: params.snapshotId, boundaryId: null, context, changes, omitted: available.filter(c => !changes.includes(c)), skipped: [], summary: '선택 작업', message: 'feat: selected', reviewHash: 'review/' + changes.map(c => c.path).join(',') };
      }
      if (method === 'commitPrepare') return { id: 'operation', kind: 'commit', phase: 'applying', selected: params.selected, changes: [] };
      if (method === 'commitExecute') return { oid: '0123456789abcdef', expected: true };
      if (method === 'usage') return { bytes: 0, limit: 1024, snapshots: state.snapshots.map(s => ({
        ...s, title: s.metadata.title || s.summary, internalBaseline: false, protected: state.protected.has(s.id),
      })) };
      if (method === 'deleteSnapshot') {
        if (state.deleteError) throw state.deleteError;
        state.snapshots = state.snapshots.filter(s => s.id !== params.snapshotId);
      }
    }
  }
  const mockVscode = {
    TreeItem: class { constructor(public label: string, public collapsibleState: number) {} },
    TreeItemCollapsibleState: { None: 0, Collapsed: 1 },
    ThemeIcon: class { constructor(public id: string, public color?: { id: string }) {} },
    ThemeColor: class { constructor(public id: string) {} },
    EventEmitter: class { event = event; fire() {} },
    UIKind: { Desktop: 1 }, ExtensionMode: { Development: 2 }, env: { uiKind: 1 },
    ProgressLocation: { Notification: 1 },
    Uri: { joinPath: (_base: unknown, ...parts: string[]) => parts.join('/') },
    extensions: { getExtension: () => undefined },
    commands: {
      registerCommand: (id: string, action: (item?: vscode.TreeItem) => Promise<unknown>) => { commands.set(id, action); return disposable; },
      executeCommand: async (id: string, item?: vscode.TreeItem) => commands.get(id)?.(item),
    },
    workspace: {
      isTrusted: true, workspaceFolders: [], textDocuments: [],
      getConfiguration: () => ({ inspect: () => undefined, get: (key: string, fallback: unknown) => key === 'showAgentReports' ? state.showAgentReports : fallback }), registerTextDocumentContentProvider: event,
      onDidSaveTextDocument: event, onDidChangeTextDocument: event, onDidCloseTextDocument: event, onDidChangeConfiguration: event,
    },
    window: {
      registerWebviewViewProvider: (_id: string, value: typeof webviewProvider) => { webviewProvider = value; provider = value.source; return disposable; },
      showInformationMessage: async (message: string) => { state.messages.push(message); },
      showErrorMessage: async (message: string) => { state.errors.push(message); },
      showWarningMessage: async (message: string) => { state.warnings.push(message); return state.warningChoice; },
      showInputBox: async () => state.input,
      withProgress: async (_options: unknown, task: () => Promise<unknown>) => task(),
      showQuickPick: async (items: { label: string; detail?: string }[]) => {
        state.pickItems.push(items); const picked = state.picks.shift(); return typeof picked === 'function' ? picked() : picked;
      },
    },
  };
  const filename = path.resolve(__dirname, '../src/extension.js');
  const localRequire = createRequire(filename);
  const viewModule = { exports: {} };
  runInNewContext(readFileSync(path.join(path.dirname(filename), 'timelineView.js'), 'utf8'), {
    exports: viewModule.exports, module: viewModule,
    require: (id: string) => id === 'vscode' ? mockVscode : localRequire(id), Error, setTimeout, clearTimeout,
  });
  const module = { exports: {} as { activate: (ctx: unknown) => Promise<{ ready: boolean }>; deactivate: () => Promise<void> } };
  runInNewContext(readFileSync(filename, 'utf8'), {
    exports: module.exports, module, __dirname: path.dirname(filename), __filename: filename, Error,
    require: (id: string) => id === 'vscode' ? mockVscode : id === './bridge' ? { PythonBridge: Bridge }
      : id === './documents' ? { ReviewDocuments: class { async text(_title: string, value: string) { state.documents.push(value); } } } : id === './timelineView' ? viewModule.exports : localRequire(id),
    process, setTimeout, clearTimeout,
  }, { filename });
  assert.equal((await module.exports.activate({ subscriptions: [], globalStorageUri: { fsPath: 'C:/storage' }, extensionPath: '.', extensionMode: 1,
    workspaceState: { get: (key: string, fallback: unknown) => workspaceValues.get(key) ?? fallback, update: async (key: string, value: unknown) => { workspaceValues.set(key, value); } },
  })).ready, true);
  return {
    state, repo, stop: async () => { webviewProvider.dispose(); await module.exports.deactivate(); },
    children: async (item?: vscode.TreeItem) => (await provider!.getChildren(item)) || [],
    command: async (id: string, item?: vscode.TreeItem) => { assert.ok(commands.has(id)); await commands.get(id)!(item); },
    webview, viewMessages,
    openView: () => webviewProvider.resolveWebviewView({ webview, visible: true, onDidChangeVisibility: event, onDidDispose: event }),
    message: async (message: unknown) => { receiveMessage(message); await new Promise(resolve => setImmediate(resolve)); },
  };
}

test('branch labels preserve slash/unicode names, distinguish missing branches, and use stable theme colors', () => {
  const branch = 'refs/heads/feature/한글-' + 'long'.repeat(50);
  assert.equal(branchLabel({ ...context, branch }), branch.slice('refs/heads/'.length));
  assert.equal(branchLabel({ ...context, branch: null }), '브랜치 없음');
  assert.equal(snapshotContext({ ...context, branch: null }), '브랜치: 브랜치 없음\nHEAD: abcdef01');
  assert.equal(snapshotContext({ ...context, branch: null, head: null }), '브랜치: 브랜치 없음\nHEAD: 커밋 없음');
  const color = branchColor(branch);
  for (const other of ['refs/heads/main', 'refs/heads/another', null]) branchColor(other);
  assert.equal(branchColor(branch), color);
  assert.match(color, /^charts\.(blue|green|purple|orange|yellow|red)$/);
  assert.notEqual(branchColor('refs/heads/main'), branchColor('refs/heads/feature'));
  assert.equal(branchColor(null), 'descriptionForeground');
});

test('timeline uses saved branch colors and metadata for ordinary, pinned, committed and empty records', async t => {
  const saved = snapshot('saved', { context: { ...context, branch: 'refs/heads/feature/저장-시점' } });
  const h = await harness([saved, snapshot('pinned', { metadata: { ...saved.metadata, pinned: true } }),
    snapshot('committed', { boundaryState: 'committed' }), snapshot('detached', { context: { ...context, branch: null } })]);
  t.after(h.stop);
  const [repo] = await h.children();
  const nodes = await h.children(repo);
  const first = nodes[0]!;
  assert.match(String(first.tooltip), /브랜치: feature\/저장-시점\nHEAD: abcdef01/);
  assert.match(first.accessibilityInformation!.label, /feature\/저장-시점/);
  assert.equal((first.iconPath as vscode.ThemeIcon).color!.id, branchColor(saved.context.branch));
  assert.equal((nodes[1]!.iconPath as vscode.ThemeIcon).id, 'pinned');
  assert.equal((nodes[2]!.iconPath as vscode.ThemeIcon).id, 'check');
  assert.equal((nodes[3]!.iconPath as vscode.ThemeIcon).color!.id, 'descriptionForeground');
  const [branch] = await h.children(first);
  assert.equal(branch!.label, '브랜치: feature/저장-시점');
  assert.equal(branch!.description, 'abcdef01');
  assert.equal(branch!.command, undefined);
  h.state.changes = [{ path: 'branch', kind: 'added', before: { state: 'absent' }, after: { state: 'present', hash: '', size: 4, executable: false } }];
  const expanded = await h.children(first);
  assert.equal(expanded.length, 2);
  assert.notEqual(expanded[0]!.id, expanded[1]!.id);
  assert.equal(expanded[1]!.command!.command, 'devSnapshot.openDiff');
  h.repo.branch = 'refs/heads/different-current-branch';
  await h.command('devSnapshot.more', repo);
  const reloaded = await h.children(repo);
  assert.equal((reloaded[0]!.iconPath as vscode.ThemeIcon).color!.id, (first.iconPath as vscode.ThemeIcon).color!.id);
  assert.match(String(reloaded[0]!.tooltip), /feature\/저장-시점/);
});

test('delete cancellation preserves history, approval sends only the selected ID and refreshes', async t => {
  const h = await harness([snapshot('selected'), snapshot('other')]); t.after(h.stop);
  const [repo] = await h.children(); const [selected] = await h.children(repo);
  await h.command('devSnapshot.deleteSnapshot', selected);
  assert.equal(h.state.calls.filter(c => c.method === 'deleteSnapshot').length, 0);
  assert.match(h.state.warnings[0]!, /작업: 작업 이름/);
  assert.match(h.state.warnings[0]!, /브랜치: main\nHEAD: abcdef01/);
  assert.match(h.state.warnings[0]!, /저장 시각:/);
  h.state.warningChoice = '기록 삭제 승인';
  await h.command('devSnapshot.deleteSnapshot', selected);
  const calls = h.state.calls.filter(c => c.method === 'deleteSnapshot');
  assert.equal(calls.length, 1);
  assert.equal(calls[0]!.params.snapshotId, 'selected'); assert.equal(calls[0]!.params.approved, true);
  assert.deepEqual(h.state.snapshots.map(s => s.id), ['other']);
  assert.equal(h.state.calls.at(-1)!.method, 'list');
});

test('protected, stale and read-only selections do not open deletion confirmation', async t => {
  const h = await harness(); t.after(h.stop);
  const [repo] = await h.children(); const [selected] = await h.children(repo);
  h.state.protected.add('selected');
  await h.command('devSnapshot.deleteSnapshot', selected);
  assert.match(h.state.messages.at(-1)!, /보호된 기록/);
  h.state.snapshots = [];
  await h.command('devSnapshot.deleteSnapshot', selected);
  assert.match(h.state.messages.at(-1)!, /이미 삭제/);
  h.repo.writable = false;
  await h.command('devSnapshot.deleteSnapshot', selected);
  assert.match(h.state.messages.at(-1)!, /읽기 전용/);
  assert.equal(h.state.warnings.length, 0);
  assert.equal(h.state.calls.filter(c => c.method === 'deleteSnapshot').length, 0);
});

test('backend refusal after approval is reported and preserves the record', async t => {
  const h = await harness(); t.after(h.stop);
  const [repo] = await h.children(); const [selected] = await h.children(repo);
  h.state.warningChoice = '기록 삭제 승인'; h.state.deleteError = new Error('Protection changed during confirmation');
  await h.command('devSnapshot.deleteSnapshot', selected);
  assert.deepEqual(h.state.errors, ['Protection changed during confirmation']);
  assert.equal(h.state.snapshots.length, 1);
});

test('history management shares deletion confirmation and reloads protection after selection', async t => {
  const h = await harness(); t.after(h.stop);
  h.state.picks = ['스냅샷 삭제', { id: 'selected' }];
  h.state.warningChoice = '기록 삭제 승인';
  await h.command('devSnapshot.manageHistory');
  assert.match(h.state.pickItems[1]![0]!.detail!, /브랜치: main/);
  assert.equal(h.state.calls.filter(c => c.method === 'usage').length, 2);
  assert.equal(h.state.calls.filter(c => c.method === 'deleteSnapshot').length, 1);
  assert.match(h.state.warnings[0]!, /작업: 작업 이름/);
});

test('history management stops if the selected record becomes protected while the picker is open', async t => {
  const h = await harness(); t.after(h.stop);
  h.state.picks = ['스냅샷 삭제', () => { h.state.protected.add('selected'); return { id: 'selected' }; }];
  await h.command('devSnapshot.manageHistory');
  assert.match(h.state.messages.at(-1)!, /보호된 기록/);
  assert.equal(h.state.warnings.length, 0);
  assert.equal(h.state.calls.filter(c => c.method === 'deleteSnapshot').length, 0);
});

test('webview serializes host data safely, lazily loads files and only runs allowed actions on known IDs', async t => {
  const h = await harness([snapshot('selected', { metadata: { pinned: false, validation: 'unverified', commitIds: [], title: '<script>unsafe</script>' } })]); t.after(h.stop);
  h.openView();
  assert.match(h.webview.html, /default-src 'none'/);
  assert.doesNotMatch(h.webview.html, /<script>unsafe/);
  await h.message({ type: 'ready' });
  const state = h.viewMessages.find(m => m.type === 'state')!;
  const record = state.repositories[0].children[0];
  assert.equal(record.label, '<script>unsafe</script>');
  assert.equal(record.branch, 'main');
  assert.equal(h.state.calls.filter(c => c.method === 'snapshotChanges').length, 0);
  h.state.changes = [{ path: 'a.txt', kind: 'modified', before: { state: 'absent' }, after: { state: 'absent' } }];
  await h.message({ type: 'expand', id: record.id });
  const files = h.viewMessages.find(m => m.type === 'children')!.children;
  assert.equal(files.find((f: { kind: string }) => f.kind === 'file').path, 'a.txt');
  for (const message of [null, { type: 'action', id: 'forged', action: 'deleteSnapshot' },
    { type: 'action', id: record.id, action: 'push' }, { type: 'action', id: files[1].id, action: 'deleteSnapshot' }]) await h.message(message);
  assert.equal(h.state.warnings.length, 0);
  await h.message({ type: 'action', id: record.id, action: 'deleteSnapshot' });
  assert.equal(h.state.warnings.length, 1);
  assert.equal(h.state.calls.filter(c => c.method === 'deleteSnapshot').length, 0, 'Modal cancellation must still preserve history');
});

function changed(path: string): Change {
  return { path, kind: 'added', before: { state: 'absent' }, after: { state: 'present', hash: '', size: 1, executable: false } };
}

function agentReport(): AgentReport {
  return { id: 'report', title: '<script>alert(1)</script>', description: '로그인과 회원가입 구현 · 에이전트 보고',
    source: 'codex-stop', phase: 'final_answer', occurredAt: null, collectedAt: '2026-10-15T03:00:00Z',
    sessionId: 'session', turnId: 'turn', association: { status: 'unlinked', evidence: [] } };
}

test('agent reports remain unlinked text beside snapshots and cannot invoke Git actions', async t => {
  const h = await harness(); t.after(h.stop);
  h.state.showAgentReports = true;
  h.repo.agentHistoryPresent = true; h.state.agentReports = [agentReport()]; h.openView();
  await h.message({ type: 'ready' });
  const records = h.viewMessages.find(m => m.type === 'state')!.repositories[0].children;
  const report = records.find((r: any) => r.kind === 'agent');
  assert.equal(report.label, '<script>alert(1)</script>');
  assert.equal(report.association, 'unlinked');
  assert.equal(records.filter((r: any) => r.kind === 'snapshot').length, 1);
  await h.message({ type: 'action', id: report.id, action: 'prepareCommit' });
  await h.message({ type: 'action', id: report.id, action: 'restoreSnapshot' });
  assert.equal(h.state.warnings.length, 0);
  await h.message({ type: 'action', id: report.id, action: 'agentReportDetails' });
  assert.match(h.state.documents[0]!, /원본 발생 시각은 Hook이 제공하지 않습니다/);
});

test('agent consent cancellation does not enable collection or produce setup', async t => {
  const h = await harness(); t.after(h.stop);
  h.state.picks = ['이 저장소의 최종 답변 수집 허용'];
  await h.command('devSnapshot.agentHistorySettings');
  assert.equal(h.state.calls.filter(c => c.method === 'agentConsent' || c.method === 'agentSetup').length, 0);
  h.state.picks = ['이 저장소의 최종 답변 수집 허용']; h.state.warningChoice = '이 저장소 수집 허용';
  await h.command('devSnapshot.agentHistorySettings');
  assert.equal(h.state.calls.find(c => c.method === 'agentConsent')!.params.approved, true);
  assert.match(h.state.documents[0]!, /이 예시는 설정을 설치하지 않습니다/);
  assert.match(h.state.documents[0]!, /중간 메시지는 지원하지 않습니다/);
});

test('agent link needs code review and explicit confirmation and preserves commit selection', async t => {
  const h = await harness(); t.after(h.stop);
  h.state.showAgentReports = true;
  h.repo.agentHistoryPresent = true; h.state.agentReports = [agentReport()];
  const [repo] = await h.children();
  const report = (await h.children(repo)).find(r => (r as any).kind === 'agent')!;
  h.state.picks = [[{ snapshot: h.state.snapshots[0] }]];
  await h.command('devSnapshot.linkAgentReport', report);
  assert.equal(h.state.calls.filter(c => c.method === 'agentLink').length, 0);
  assert.match(h.state.documents[0]!, /app.py/);
  h.state.picks = [[{ snapshot: h.state.snapshots[0] }]]; h.state.warningChoice = '검토한 기록에 연결';
  await h.command('devSnapshot.linkAgentReport', report);
  const linked = h.state.calls.find(c => c.method === 'agentLink')!;
  assert.equal(linked.params.reviewHash, 'agent-review');
  assert.equal(linked.params.approved, true);
  assert.equal(h.state.calls.filter(c => c.method === 'commitPrepare').length, 0);
});

test('local snapshot summaries take precedence over agent reports and user titles override both', async t => {
  const h = await harness([snapshot('selected', { agentTitle: '에이전트 제목', metadata: { pinned: false, validation: 'unverified', commitIds: [] } })]); t.after(h.stop);
  const [repo] = await h.children();
  assert.equal((await h.children(repo))[0]!.label, 'a.txt 수정');
  h.state.snapshots[0]!.metadata.title = '사용자 제목';
  assert.equal((await h.children(repo))[0]!.label, '사용자 제목');
  assert.equal(h.state.snapshots[0]!.summary, 'a.txt 수정');
});

test('local naming shows initial summaries and requires no agent report collection or linking', async t => {
  const h = await harness([snapshot('initial', { reason: 'initial', summary: '로그인 검증 함수 수정',
    metadata: { pinned: false, validation: 'unverified', commitIds: [] } })]); t.after(h.stop);
  h.repo.agentHistoryPresent = true; h.state.agentReports = [agentReport()];
  const [repo] = await h.children(); const children = await h.children(repo);
  assert.equal(children[0]!.label, '로그인 검증 함수 수정');
  assert.equal(children.length, 1);
  assert.equal(h.state.calls.some(c => c.method.startsWith('agent')), false);
  h.openView(); await h.message({ type: 'ready' });
  assert.equal(h.viewMessages.find(m => m.type === 'state')!.repositories[0].showAgentReports, false);
  assert.equal(h.state.warnings.length, 0);
});

test('commit file toggles include earlier pending docs and persist across refresh and restart', async t => {
  const records = [snapshot('selected', { boundaryState: 'ready' })];
  const saved = new Map<string, unknown>();
  const h = await harness(records, saved); t.after(h.stop);
  h.state.changes = [changed('app.py')]; h.state.commitChanges = [changed('app.py'), changed('docs/guide.md')];
  const [repo] = await h.children(); const [record] = await h.children(repo);
  let files = await h.children(record);
  const docs = files.find(f => f.label === 'guide.md')! as vscode.TreeItem & { commitIncluded: boolean; commitOnly: boolean };
  assert.equal(docs.commitIncluded, true); assert.equal(docs.commitOnly, true);
  await h.command('devSnapshot.toggleCommitFile', docs);
  files = await h.children(record);
  assert.equal((files.find(f => f.label === 'guide.md') as typeof docs).commitIncluded, false);
  assert.equal(h.state.calls.filter(c => c.method === 'commitPrepare').length, 0);
  const reopened = await harness(records, saved); t.after(reopened.stop);
  reopened.state.changes = h.state.changes; reopened.state.commitChanges = h.state.commitChanges;
  const [reopenedRepo] = await reopened.children(); const [reopenedRecord] = await reopened.children(reopenedRepo);
  const reopenedDocs = (await reopened.children(reopenedRecord)).find(f => f.label === 'guide.md') as typeof docs;
  assert.equal(reopenedDocs.commitIncluded, false);
  await reopened.command('devSnapshot.toggleCommitFile', reopenedDocs);
  assert.equal(((await reopened.children(reopenedRecord)).find(f => f.label === 'guide.md') as typeof docs).commitIncluded, true);
});

test('commit confirmation sends exactly included files and identifies omitted docs', async t => {
  const h = await harness([snapshot('selected', { boundaryState: 'ready' })]); t.after(h.stop);
  h.state.changes = [changed('app.py'), changed('guide.md')];
  const [repo] = await h.children(); const [record] = await h.children(repo);
  const docs = (await h.children(record)).find(f => f.label === 'guide.md')!;
  await h.command('devSnapshot.toggleCommitFile', docs);
  h.state.input = 'feat: app only'; h.state.warningChoice = '승인'; h.state.picks = [{ label: '검토 완료' }];
  await h.command('devSnapshot.prepareCommit', record);
  assert.deepEqual(h.state.errors, []);
  const prepared = h.state.calls.find(c => c.method === 'commitPrepare')!;
  assert.deepEqual(Array.from(prepared.params.selected as string[]), ['app.py']);
  assert.equal(prepared.params.reviewHash, 'review/app.py');
  assert.equal(prepared.params.approved, true);
  assert.match(h.state.documents[0]!, /이번 커밋에서 제외: 1개 파일\nguide.md/);
  assert.match(h.state.warnings.at(-1)!, /선택한 파일 1개/);
  assert.equal(h.state.calls.filter(c => c.method === 'commitExecute').length, 1);
});

test('empty selection and cancelled approval never prepare or execute a commit', async t => {
  const h = await harness([snapshot('selected', { boundaryState: 'ready' })]); t.after(h.stop);
  h.state.changes = [changed('guide.md')];
  const [repo] = await h.children(); const [record] = await h.children(repo);
  let docs = (await h.children(record)).find(f => f.label === 'guide.md')!;
  await h.command('devSnapshot.toggleCommitFile', docs);
  await h.command('devSnapshot.prepareCommit', record);
  assert.match(h.state.messages.at(-1)!, /포함할 파일이 없습니다/);
  docs = (await h.children(record)).find(f => f.label === 'guide.md')!;
  await h.command('devSnapshot.toggleCommitFile', docs);
  h.state.input = 'docs: guide';
  await h.command('devSnapshot.prepareCommit', record);
  assert.equal(h.state.calls.filter(c => ['commitPrepare', 'commitExecute'].includes(c.method)).length, 0);
});

test('webview commit toggle uses owned file path and refuses read-only changes', async t => {
  const h = await harness([snapshot('selected', { boundaryState: 'partial' })]); t.after(h.stop);
  h.state.changes = [changed('guide.md')]; h.openView();
  await h.message({ type: 'ready' });
  const record = h.viewMessages.find(m => m.type === 'state')!.repositories[0].children[0];
  assert.equal(record.partial, true);
  await h.message({ type: 'expand', id: record.id });
  const file = h.viewMessages.find(m => m.type === 'children')!.children.find((c: { kind: string }) => c.kind === 'file');
  assert.equal(file.commitIncluded, true);
  await h.message({ type: 'action', id: file.id, action: 'toggleCommitFile', path: '../../outside' });
  const saved = h.state.workspaceValues.get('commitExclusions') as Record<string, string[]>;
  assert.deepEqual(Array.from(saved['repo/selected']!), ['guide.md']);
  h.repo.writable = false;
  const [repo] = await h.children(); const [readonlyRecord] = await h.children(repo);
  const readonlyDocs = (await h.children(readonlyRecord)).find(f => f.label === 'guide.md');
  await h.command('devSnapshot.toggleCommitFile', readonlyDocs);
  assert.deepEqual(Array.from((h.state.workspaceValues.get('commitExclusions') as typeof saved)['repo/selected']!), ['guide.md']);
});

import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import * as path from 'node:path';
import { EventEmitter } from 'node:events';
import { runInNewContext } from 'node:vm';
import type * as vscode from 'vscode';
import { branchColor, branchLabel, snapshotContext } from '../src/presentation';
import { Change, GitContext, Repository, SnapshotItem } from '../src/protocol';

const context: GitContext = { branch: 'refs/heads/main', head: 'abcdef0123456789', indexHash: '', busy: false, sparse: false };
function snapshot(id: string, overrides: Partial<SnapshotItem> = {}): SnapshotItem {
  return { id, reason: 'manual', endedAt: '2026-10-15T02:00:00Z', summary: 'a.txt 수정', changedFiles: 1,
    comparisonPruned: false, metadata: { pinned: false, validation: 'unverified', commitIds: [], title: '작업 이름' }, context, ...overrides };
}

// Exercise the real activation, provider, and command handlers with isolated VS Code/RPC adapters.
async function harness(snapshots = [snapshot('selected')]) {
  const repo: Repository = { id: 'repo', root: 'C:/fixture', writable: true, paused: true, status: '', dirtyCount: 0,
    latestAt: null, branch: context.branch, excludedCount: 0, recoveries: [] };
  const state = {
    snapshots, protected: new Set<string>(), changes: [] as Change[], warningChoice: undefined as string | undefined,
    warnings: [] as string[], messages: [] as string[], errors: [] as string[], calls: [] as { method: string; params: Record<string, unknown> }[],
    picks: [] as unknown[], pickItems: [] as { label: string; detail?: string }[][], deleteError: undefined as Error | undefined,
  };
  let provider: vscode.TreeDataProvider<vscode.TreeItem> | undefined;
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
    extensions: { getExtension: () => undefined },
    commands: { registerCommand: (id: string, action: (item?: vscode.TreeItem) => Promise<unknown>) => { commands.set(id, action); return disposable; } },
    workspace: {
      isTrusted: true, workspaceFolders: [], textDocuments: [],
      getConfiguration: () => ({ inspect: () => undefined }), registerTextDocumentContentProvider: event,
      onDidSaveTextDocument: event, onDidChangeTextDocument: event, onDidCloseTextDocument: event, onDidChangeConfiguration: event,
    },
    window: {
      registerTreeDataProvider: (_id: string, value: typeof provider) => { provider = value; return disposable; },
      showInformationMessage: async (message: string) => { state.messages.push(message); },
      showErrorMessage: async (message: string) => { state.errors.push(message); },
      showWarningMessage: async (message: string) => { state.warnings.push(message); return state.warningChoice; },
      showQuickPick: async (items: { label: string; detail?: string }[]) => {
        state.pickItems.push(items); const picked = state.picks.shift(); return typeof picked === 'function' ? picked() : picked;
      },
    },
  };
  const filename = path.resolve(__dirname, '../src/extension.js');
  const localRequire = createRequire(filename);
  const module = { exports: {} as { activate: (ctx: unknown) => Promise<{ ready: boolean }>; deactivate: () => Promise<void> } };
  runInNewContext(readFileSync(filename, 'utf8'), {
    exports: module.exports, module, __dirname: path.dirname(filename), __filename: filename, Error,
    require: (id: string) => id === 'vscode' ? mockVscode : id === './bridge' ? { PythonBridge: Bridge }
      : id === './documents' ? { ReviewDocuments: class {} } : localRequire(id),
    process, setTimeout, clearTimeout,
  }, { filename });
  assert.equal((await module.exports.activate({ subscriptions: [], globalStorageUri: { fsPath: 'C:/storage' }, extensionPath: '.', extensionMode: 1 })).ready, true);
  return {
    state, repo, stop: module.exports.deactivate,
    children: async (item?: vscode.TreeItem) => (await provider!.getChildren(item)) || [],
    command: async (id: string, item?: vscode.TreeItem) => { assert.ok(commands.has(id)); await commands.get(id)!(item); },
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

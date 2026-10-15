import * as vscode from 'vscode';
import { randomBytes } from 'node:crypto';
import { branchColor, branchLabel, compactTime } from './presentation';
import { AgentReport, Change, Journal, Repository, SnapshotItem } from './protocol';

export interface TimelineNode extends vscode.TreeItem {
  readonly kind: 'repository' | 'snapshot' | 'file' | 'message' | 'recovery' | 'more' | 'agent';
  readonly repository?: Repository;
  readonly snapshot?: SnapshotItem;
  readonly change?: Change;
  readonly journal?: Journal;
  readonly commitIncluded?: boolean;
  readonly commitOnly?: boolean;
  readonly agentReport?: AgentReport;
}
interface TimelineSource {
  readonly showAgentReports?: boolean;
  readonly onDidChangeTreeData: vscode.Event<TimelineNode | undefined>;
  getChildren(item?: TimelineNode): Promise<TimelineNode[]>;
}

const actions: Record<TimelineNode['kind'], readonly string[]> = {
  repository: ['captureNow', 'refresh', 'manageHistory', 'pauseRecording', 'resumeRecording', 'agentHistorySettings'],
  snapshot: ['openDiff', 'prepareCommit', 'restoreSnapshot', 'renameSnapshot', 'pinSnapshot', 'deleteSnapshot'],
  file: ['openDiff', 'toggleCommitFile'], recovery: ['recover'], more: ['more'], message: [],
  agent: ['agentReportDetails', 'linkAgentReport', 'openAgentSnapshots'],
};

/** The webview only sends opaque item IDs. All actions use host-owned objects and existing reviewed commands. */
export class TimelineView implements vscode.WebviewViewProvider, vscode.Disposable {
  private view?: vscode.WebviewView;
  private nodes = new Map<string, TimelineNode>();
  private generation = 0;
  private timer?: NodeJS.Timeout;
  private readonly subscription: vscode.Disposable;
  private viewSubscriptions: vscode.Disposable[] = [];

  constructor(readonly source: TimelineSource, private readonly extensionUri: vscode.Uri) {
    this.subscription = source.onDidChangeTreeData(() => {
      if (!this.timer) this.timer = setTimeout(() => { this.timer = undefined; void this.update(); }, 100);
    });
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.viewSubscriptions.forEach(d => d.dispose());
    this.view = view;
    const media = vscode.Uri.joinPath(this.extensionUri, 'media');
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    const css = view.webview.asWebviewUri(vscode.Uri.joinPath(media, 'timeline.css'));
    const script = view.webview.asWebviewUri(vscode.Uri.joinPath(media, 'timeline.js'));
    const nonce = randomBytes(24).toString('hex');
    view.webview.html = `<!doctype html><html lang="ko"><head><meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${view.webview.cspSource}; script-src 'nonce-${nonce}';">
      <link rel="stylesheet" href="${css}"><title>AmberTrail 작업 기록</title></head>
      <body><main id="app" aria-label="작업 기록"><p class="empty">기록을 불러오는 중…</p></main>
      <div id="notice" role="status" aria-live="polite"></div><script nonce="${nonce}" src="${script}"></script></body></html>`;
    this.viewSubscriptions = [
      view.webview.onDidReceiveMessage(message => { void this.receive(message); }),
      view.onDidChangeVisibility(() => { if (view.visible) void this.update(); }),
      view.onDidDispose(() => { if (this.view === view) { this.view = undefined; this.generation++; this.nodes.clear(); } }),
    ];
  }

  private serialize(node: TimelineNode, nodes: Map<string, TimelineNode>): object {
    const id = node.id || `${node.kind}/${node.repository?.id || ''}/${node.journal?.id || ''}`;
    nodes.set(id, node);
    const snapshot = node.snapshot;
    return {
      id, kind: node.kind, label: typeof node.label === 'string' ? node.label : node.label?.label || '',
      description: typeof node.description === 'string' ? node.description : '',
      tooltip: typeof node.tooltip === 'string' ? node.tooltip : node.tooltip?.value || '',
      ...(node.kind === 'repository' && node.repository ? {
        writable: node.repository.writable, paused: node.repository.paused,
        branch: node.repository.branch?.replace(/^refs\/heads\//, '') || '브랜치 없음',
        status: node.repository.status, dirtyCount: node.repository.dirtyCount, excludedCount: node.repository.excludedCount,
        agentHistoryEnabled: node.repository.agentHistoryEnabled, agentHistoryProblem: node.repository.agentHistoryProblem,
        showAgentReports: this.source.showAgentReports === true,
      } : {}),
      ...(node.kind === 'snapshot' && snapshot ? {
        endedAt: snapshot.endedAt, time: compactTime(snapshot.endedAt), branch: branchLabel(snapshot.context),
        color: branchColor(snapshot.context.branch).replace('.', '-'), head: snapshot.context.head?.slice(0, 8) || '커밋 없음',
        changedFiles: snapshot.changedFiles, pinned: snapshot.metadata.pinned, committed: snapshot.boundaryState === 'committed', partial: snapshot.boundaryState === 'partial',
        summary: snapshot.summary, rangeError: snapshot.rangeError, comparisonPruned: snapshot.comparisonPruned,
        commitSummary: snapshot.commitSummary, commitChangedFiles: snapshot.commitChangedFiles,
        agentDescription: snapshot.agentDescription, agentSource: snapshot.agentSource,
      } : {}),
      ...(node.kind === 'agent' && node.agentReport ? {
        collectedAt: node.agentReport.collectedAt, reportDescription: node.agentReport.description,
        association: node.agentReport.association.status, linkedCount: node.agentReport.association.evidence.length,
      } : {}),
      ...(node.kind === 'file' && node.change ? { path: node.change.path, change: node.change.kind || 'modified', commitIncluded: node.commitIncluded, commitOnly: node.commitOnly } : {}),
    };
  }

  private async update(): Promise<void> {
    const view = this.view;
    if (!view?.visible) return;
    const generation = ++this.generation;
    const nodes = new Map<string, TimelineNode>();
    try {
      const roots = await this.source.getChildren();
      const repositories = [];
      for (const root of roots) {
        const children = root.kind === 'repository' ? await this.source.getChildren(root) : [];
        repositories.push({ ...this.serialize(root, nodes), children: children.map(child => this.serialize(child, nodes)) });
      }
      if (this.view !== view || generation !== this.generation) return;
      this.nodes = nodes;
      await view.webview.postMessage({ type: 'state', repositories });
    } catch (error) { if (this.view === view && generation === this.generation) await this.report(error); }
  }

  private async receive(message: unknown): Promise<void> {
    if (!message || typeof message !== 'object') return;
    const value = message as { type?: unknown; id?: unknown; action?: unknown };
    if (value.type === 'ready') { await this.update(); return; }
    if (typeof value.id !== 'string') return;
    const node = this.nodes.get(value.id);
    if (!node) { if (value.type === 'action' || value.type === 'expand') await this.update(); return; }
    try {
      if (value.type === 'expand' && node.kind === 'snapshot') {
        const generation = this.generation;
        const children = await this.source.getChildren(node);
        if (generation !== this.generation || this.nodes.get(value.id) !== node) return;
        await this.view?.webview.postMessage({ type: 'children', id: value.id, children: children.map(child => this.serialize(child, this.nodes)) });
      } else if (value.type === 'action' && typeof value.action === 'string' && actions[node.kind].includes(value.action)) {
        await vscode.commands.executeCommand(`devSnapshot.${value.action}`, node);
        await this.update();
      }
    } catch (error) { await this.report(error, value.type === 'expand' ? value.id : undefined); }
  }

  private async report(error: unknown, id?: string): Promise<void> {
    await this.view?.webview.postMessage({ type: 'error', id, message: error instanceof Error ? error.message : '기록을 불러오지 못했습니다. 새로고침해 주세요.' });
  }

  dispose(): void {
    this.subscription.dispose(); this.viewSubscriptions.forEach(d => d.dispose());
    if (this.timer) clearTimeout(this.timer);
    this.view = undefined; this.generation++; this.nodes.clear();
  }
}

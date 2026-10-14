import * as vscode from 'vscode';
import { randomUUID } from 'node:crypto';
import { Diff } from './protocol';

/** Ephemeral, opaque, read-only documents; content never becomes an executable command URI. */
export class ReviewDocuments implements vscode.TextDocumentContentProvider {
  private records = new Map<string, string>();
  private uri(content: string, label: string): vscode.Uri {
    const uri = vscode.Uri.from({ scheme: 'dev-snapshot', path: `/${randomUUID()}/${label.replace(/[^\p{L}\p{N}._-]/gu, '_')}` });
    this.records.set(uri.toString(), content);
    if (this.records.size > 200) {
      const open = new Set(vscode.workspace.textDocuments.map(d => d.uri.toString()));
      for (const key of this.records.keys()) { if (this.records.size <= 100) break; if (!open.has(key)) this.records.delete(key); }
    }
    return uri;
  }
  provideTextDocumentContent(uri: vscode.Uri): string {
    const value = this.records.get(uri.toString());
    if (value === undefined) throw new Error('검토 문서가 만료되었습니다. 다시 여세요.');
    return value;
  }
  async text(title: string, body: string): Promise<void> {
    const doc = await vscode.workspace.openTextDocument(this.uri(body, `${title}.txt`));
    await vscode.window.showTextDocument(doc, { preview: true });
  }
  async diff(value: Diff, title: string): Promise<void> {
    const label = value.path.split('/').pop() ?? 'file';
    const left = this.uri(value.left, `before-${label}`), right = this.uri(value.right, `after-${label}`);
    await vscode.commands.executeCommand('vscode.diff', left, right, `${title}: ${value.path}${value.leftAbsent ? ' (새 파일)' : value.rightAbsent ? ' (삭제)' : ''}`, { preview: true });
  }
}

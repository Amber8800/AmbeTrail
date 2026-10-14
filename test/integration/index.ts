import * as vscode from 'vscode';
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import * as path from 'node:path';

export async function run(): Promise<void> {
  let timer: NodeJS.Timeout | undefined;
  try {
    await Promise.race([scenario(), new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('Extension integration scenario timed out')), 90_000); })]);
  } finally { if (timer) clearTimeout(timer); }
}

async function scenario(): Promise<void> {
  const extension = vscode.extensions.getExtension<{ ready: boolean }>('dev-snapshot.dev-snapshot');
  assert.ok(extension, 'Extension must be registered');
  const api = await extension.activate(); assert.equal(api.ready, true, 'Python bridge and UI must activate');
  const commands = await vscode.commands.getCommands(true);
  for (const name of ['captureNow', 'openDiff', 'restoreSnapshot', 'undoRestore', 'prepareCommit', 'push', 'recover']) assert.ok(commands.includes(`devSnapshot.${name}`));
  const repo = process.env.DEV_SNAPSHOT_TEST_REPO!;
  for (let attempt = 0; attempt < 100; attempt++) {
    const status = await vscode.commands.executeCommand<unknown[]>('devSnapshot.refresh');
    if (status?.length) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok((await vscode.commands.executeCommand<unknown[]>('devSnapshot.refresh'))?.length, 'Git discovery must complete');
  await vscode.commands.executeCommand('devSnapshot.pauseRecording');
  for (let attempt = 0; attempt < 50; attempt++) {
    const status = await vscode.commands.executeCommand<{ job?: { state: string } }[]>('devSnapshot.refresh');
    if (!status?.some(r => r.job?.state === 'running')) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const current = path.join(repo, 'example.py');
  await fs.writeFile(current, 'print("external change")\n');
  await vscode.commands.executeCommand('devSnapshot.captureNow');
  const status = await vscode.commands.executeCommand<{ latestAt: string | null; job?: { state: string } }[]>('devSnapshot.refresh');
  assert.ok(status?.some(r => r.latestAt && r.job?.state === 'complete'), 'Manual capture must publish a real completed snapshot');
  assert.equal(await fs.readFile(current, 'utf8'), 'print("external change")\n');
  const doc = await vscode.workspace.openTextDocument(current);
  await vscode.window.showTextDocument(doc);
  const edit = new vscode.WorkspaceEdit(); edit.insert(doc.uri, new vscode.Position(0, 0), '# unsaved\n');
  await vscode.workspace.applyEdit(edit); assert.equal(doc.isDirty, true);
  await vscode.commands.executeCommand('devSnapshot.captureNow');
  assert.equal(await fs.readFile(current, 'utf8'), 'print("external change")\n', 'Capture must never save editor buffers');
  await vscode.commands.executeCommand('undo');
  assert.equal(doc.isDirty, false);
  await vscode.commands.executeCommand('workbench.action.closeActiveEditor');
  await vscode.commands.executeCommand('devSnapshot.resumeRecording');
  console.log('Amberog extension activation, commands, disk capture and unsaved-editor isolation passed.');
}

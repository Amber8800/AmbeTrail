import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import { existsSync } from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import { spawnSync } from 'node:child_process';
import { PythonBridge } from '../src/bridge';

const root = path.resolve(__dirname, '../..');
const local = path.join(root, 'log_venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const python = process.env.DEV_SNAPSHOT_PYTHON || (existsSync(local) ? local : process.platform === 'win32' ? 'python' : 'python3');
const entry = path.join(root, 'backend/run.py');

test('stdio bridge: handshake, Git capture, errors, and restart persistence', async () => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'dev-snapshot-bridge-'));
  const repo = path.join(temporary, 'repo'); await fs.mkdir(repo);
  const git = (...args: string[]): void => {
    const result = spawnSync('git', args, { cwd: repo, shell: false, windowsHide: true, env: { ...process.env, GIT_CONFIG_GLOBAL: process.platform === 'win32' ? 'NUL' : '/dev/null', GIT_CONFIG_NOSYSTEM: '1' } });
    assert.equal(result.status, 0, result.stderr?.toString());
  };
  git('init', '-b', 'main');
  await fs.writeFile(path.join(repo, 'hello.py'), 'print("안녕")\r\n');
  let bridge = new PythonBridge(python, entry, root);
  const waitCapture = async (repositoryId: string): Promise<void> => {
    const job = await bridge.call<{ id: string; state: string }>('capture', { repositoryId });
    for (;;) {
      const status = await bridge.call<{ state: string; message: string }>('job', { repositoryId, jobId: job.id });
      if (status.state !== 'running') { assert.equal(status.state, 'complete', status.message); break; }
      await new Promise(resolve => setTimeout(resolve, 50));
    }
  };
  try {
    await bridge.start();
    const opened = await bridge.call<{ id: string }>('open', { root: repo, storageRoot: path.join(temporary, 'storage') });
    const params = { repositoryId: opened.id };
    await waitCapture(opened.id);
    const initial = await bridge.call<{ id: string }[]>('timeline', params);
    assert.equal(initial.length, 1);
    const diff = await bridge.call<{ right: string }>('diff', { ...params, snapshotId: initial[0]!.id, path: 'hello.py' });
    assert.match(diff.right, /안녕/);
    await assert.rejects(bridge.call('commitPrepare', { ...params, selected: ['hello.py'] }), /approval/);
    await assert.rejects(bridge.call('unknownMethod', params), /Unknown/);
    await fs.writeFile(path.join(repo, 'hello.py'), 'print("second")\n');
    bridge.notify('fileEvent', params);
    await waitCapture(opened.id);
    const next = await bridge.call<{ id: string }[]>('timeline', params); assert.equal(next.length, 2);
    await bridge.stop();
    bridge = new PythonBridge(python, entry, root); await bridge.start();
    await bridge.call('open', { root: repo, storageRoot: path.join(temporary, 'storage') });
    await waitCapture(opened.id);
    assert.equal((await bridge.call<unknown[]>('timeline', params)).length, 2);
  } finally {
    await bridge.stop();
    // Only this test's newly-created temporary directory is removed.
    assert.ok(path.dirname(temporary) === path.resolve(os.tmpdir()) && path.basename(temporary).startsWith('dev-snapshot-bridge-'));
    await fs.rm(temporary, { recursive: true, force: true });
  }
});


test('ordered snapshot boundaries through stdio preserve latest files across restart', async () => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'dev-snapshot-interval-'));
  const repo = path.join(temporary, 'repo'); await fs.mkdir(repo);
  const git = (...args: string[]): string => {
    const result = spawnSync('git', args, { cwd: repo, shell: false, windowsHide: true, encoding: 'utf8' });
    assert.equal(result.status, 0); return result.stdout.trim();
  };
  git('init', '-b', 'main'); git('config', 'user.name', 'Boundary Test');
  git('config', 'user.email', 'boundary@example.invalid'); git('config', 'commit.gpgsign', 'false');
  let bridge = new PythonBridge(python, entry, root);
  try {
    await bridge.start();
    const opened = await bridge.call<{ id: string }>('open', { root: repo, storageRoot: path.join(temporary, 'storage') });
    const params = { repositoryId: opened.id };
    await bridge.call('pause', params);
    let state = await bridge.call<{ job?: { state: string } }[]>('list');
    while (state[0]?.job?.state === 'running') {
      await new Promise(resolve => setTimeout(resolve, 50));
      state = await bridge.call('list');
    }
    const snapshots: string[] = [];
    for (let number = 1; number <= 5; number++) {
      await fs.writeFile(path.join(repo, 'a.txt'), 'work ' + number);
      if (number === 3) await fs.writeFile(path.join(repo, 'later.txt'), 'later');
      const job = await bridge.call<{ id: string }>('capture', params);
      for (;;) {
        const status = await bridge.call<{ state: string; message: string }>('job', { ...params, jobId: job.id });
        if (status.state !== 'running') { assert.equal(status.state, 'complete', status.message); break; }
        await new Promise(resolve => setTimeout(resolve, 50));
      }
      snapshots.push((await bridge.call<{ id: string }[]>('timeline', params))[0]!.id);
    }
    const prepare = await bridge.call<{ id: string; selected: string[] }>('commitPrepare', { ...params, snapshotId: snapshots[1], approved: true });
    assert.deepEqual(prepare.selected, ['a.txt']);
    const staged = await bridge.call<{ right: string }>('diff', { ...params, kind: 'boundaryStaged', operationId: prepare.id, path: 'a.txt' });
    assert.equal(staged.right, 'work 2');
    const second = await bridge.call<{ oid: string; expected: boolean; cleanup: { token: string } }>('commitExecute', { ...params, operationId: prepare.id, message: 'chore: first interval', approved: true });
    assert.equal(second.expected, true);
    assert.equal(git('show', second.oid + ':a.txt'), 'work 2');
    assert.equal(await fs.readFile(path.join(repo, 'a.txt'), 'utf8'), 'work 5');
    await bridge.call('historyCancel', { ...params, token: second.cleanup.token });
    await bridge.stop();
    bridge = new PythonBridge(python, entry, root); await bridge.start();
    await bridge.call('open', { root: repo, storageRoot: path.join(temporary, 'storage') });
    const next = await bridge.call<{ id: string; selected: string[] }>('commitPrepare', { ...params, snapshotId: snapshots[4], approved: true });
    assert.deepEqual(new Set(next.selected), new Set(['a.txt', 'later.txt']));
    const fifth = await bridge.call<{ oid: string; expected: boolean; cleanup: { token: string } }>('commitExecute', { ...params, operationId: next.id, message: 'chore: second interval', approved: true });
    assert.equal(fifth.expected, true);
    assert.equal(git('rev-parse', fifth.oid + '^'), second.oid);
    assert.equal(git('show', fifth.oid + ':a.txt'), 'work 5');
    assert.equal(git('show', fifth.oid + ':later.txt'), 'later');
    assert.equal(await fs.readFile(path.join(repo, 'a.txt'), 'utf8'), 'work 5');
    assert.equal(git('diff', '--cached', '--name-only'), '');
    await bridge.call('historyCancel', { ...params, token: fifth.cleanup.token });
  } finally {
    await bridge.stop();
    assert.ok(path.dirname(temporary) === path.resolve(os.tmpdir()) && path.basename(temporary).startsWith('dev-snapshot-interval-'));
    await fs.rm(temporary, { recursive: true, force: true });
  }
});
test('invalid Python executable rejects startup without replay', async () => {
  const bridge = new PythonBridge(path.join(root, 'nonexistent-python-executable'), entry, root);
  await assert.rejects(bridge.start(), /Python/);
  await assert.rejects(bridge.call('capture', {}), /Python/);
  await bridge.stop();
});

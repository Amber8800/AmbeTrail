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

test('invalid Python executable rejects startup without replay', async () => {
  const bridge = new PythonBridge(path.join(root, 'nonexistent-python-executable'), entry, root);
  await assert.rejects(bridge.start(), /Python/);
  await assert.rejects(bridge.call('capture', {}), /Python/);
  await bridge.stop();
});

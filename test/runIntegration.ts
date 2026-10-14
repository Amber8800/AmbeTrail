import { runTests } from '@vscode/test-electron';
import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import * as os from 'node:os';
import { spawnSync } from 'node:child_process';

async function main(): Promise<void> {
  const extensionDevelopmentPath = path.resolve(__dirname, '../..');
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'dev-snapshot-vscode-'));
  const repo = path.join(temporary, 'repo'), userData = path.join(temporary, 'user');
  await fs.mkdir(repo); await fs.mkdir(path.join(userData, 'User'), { recursive: true });
  const init = spawnSync('git', ['init', '-b', 'main'], { cwd: repo, shell: false, windowsHide: true });
  if (init.status !== 0) throw new Error('Git fixture initialization failed.');
  await fs.writeFile(path.join(repo, 'example.py'), 'print("integration")\n');
  const settings: Record<string, unknown> = { 'security.workspace.trust.enabled': false, 'devSnapshot.idleSeconds': 1, 'devSnapshot.maxIntervalSeconds': 5, 'git.autofetch': false, 'extensions.autoUpdate': false, 'extensions.autoCheckUpdates': false, 'update.mode': 'none', 'telemetry.telemetryLevel': 'off' };
  if (process.env.DEV_SNAPSHOT_PYTHON) settings['devSnapshot.pythonPath'] = process.env.DEV_SNAPSHOT_PYTHON;
  await fs.writeFile(path.join(userData, 'User/settings.json'), JSON.stringify(settings));
  try {
    await runTests({
      extensionDevelopmentPath, extensionTestsPath: path.join(extensionDevelopmentPath, 'out/test/integration/index'),
      ...(process.env.VSCODE_EXECUTABLE_PATH ? { vscodeExecutablePath: process.env.VSCODE_EXECUTABLE_PATH } : { version: process.env.VSCODE_VERSION ?? 'stable' }),
      launchArgs: [repo, '--user-data-dir', userData, '--extensions-dir', path.join(temporary, 'extensions'), '--skip-welcome', '--skip-release-notes', '--disable-updates', '--disable-telemetry', '--disable-workspace-trust', '--no-sandbox'],
      extensionTestsEnv: { DEV_SNAPSHOT_TEST_REPO: repo },
    });
  } finally {
    if (path.dirname(temporary) !== path.resolve(os.tmpdir()) || !path.basename(temporary).startsWith('dev-snapshot-vscode-')) throw new Error('Unsafe test cleanup target.');
    await fs.rm(temporary, { recursive: true, force: true, maxRetries: 10, retryDelay: 500 });
  }
}
void main().catch(error => { console.error(error instanceof Error ? error.message : 'Integration test failed'); process.exitCode = 1; });

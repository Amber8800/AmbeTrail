import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
const windows = process.platform === 'win32';
const local = path.resolve('log_venv', windows ? 'Scripts/python.exe' : 'bin/python');
const python = process.env.DEV_SNAPSHOT_PYTHON || (existsSync(local) ? local : windows ? 'python' : 'python3');
const result = spawnSync(python, ['-I', '-S', '-u', ...process.argv.slice(2)], { stdio: 'inherit', shell: false, windowsHide: true });
if (result.error) console.error('Python 3.12+ is required. Set DEV_SNAPSHOT_PYTHON to its executable.');
process.exit(result.status ?? 1);

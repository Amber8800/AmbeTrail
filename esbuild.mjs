import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root = fileURLToPath(new URL('.', import.meta.url));
await build({ absWorkingDir: root, entryPoints: [path.join(root, 'src/extension.ts')], tsconfig: path.join(root, 'tsconfig.json'), bundle: true, platform: 'node', format: 'cjs', target: 'node20', external: ['vscode'], outfile: path.join(root, 'dist/extension.js'), sourcemap: true });

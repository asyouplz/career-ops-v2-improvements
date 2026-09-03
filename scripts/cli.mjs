#!/usr/bin/env node
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const [major, minor] = process.versions.node.split('.').map(Number);
if (major < 22 || (major === 22 && minor < 13)) {
  throw new Error('Node.js 22.13 or newer is required');
}
const [command, ...args] = process.argv.slice(2);
const runtimePath = join(root, 'config', 'runtime.json');
const runtime = existsSync(runtimePath) ? JSON.parse(readFileSync(runtimePath, 'utf8')) : {};
const python = process.env.CAREER_OPS_PYTHON || runtime.python_bin
  || (process.platform === 'win32' ? 'python' : 'python3');
let executable;
let parameters;
let cwd = root;
if (command === 'setup') {
  executable = python;
  parameters = [join(root, 'scripts', 'setup.py'), '--node', process.execPath, ...args];
} else {
  if (!existsSync(runtimePath)) throw new Error('Run npm run setup first');
  const engine = resolve(root, runtime.production_project_root || 'engine');
  const engineCommands = { scan: 'scan.mjs', verify: 'verify-pipeline.mjs', tracker: 'tracker.mjs' };
  if (Object.hasOwn(engineCommands, command)) {
    executable = process.execPath;
    parameters = [join(engine, engineCommands[command]), ...args];
    cwd = engine;
  } else if (command === 'run' || command === 'preview') {
    executable = python;
    parameters = [join(root, 'src', 'career_ops_daily_v2.py'), '--mode', 'dry-run',
      ...(command === 'preview' ? ['--skip-network'] : ['--include-collector']), ...args];
  } else {
    throw new Error('Command must be setup, run, preview, scan, verify, or tracker');
  }
}
const engine = resolve(root, runtime.production_project_root || 'engine');
const result = spawnSync(executable, parameters, {
  cwd,
  stdio: 'inherit',
  env: {
    ...process.env,
    PATH: [dirname(process.execPath), process.env.PATH || ''].join(process.platform === 'win32' ? ';' : ':'),
    CAREER_OPS_PROJECT_ROOT: engine,
    CAREER_OPS_EXPECTED_PROJECT_ROOT: engine,
    CAREER_OPS_PYTHON: python,
  },
});
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;

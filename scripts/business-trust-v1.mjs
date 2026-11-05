// Portable entrypoint; Python keeps credentials in the trusted coordinator.
import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const candidates = [
  process.env.BTV_PYTHON,
  process.env.WREN_PYTHON,
  resolve(root, '.venv', 'Scripts', 'python.exe'),
  resolve(root, '.venv', 'bin', 'python'),
].filter(Boolean);
const python = candidates.find((candidate) => existsSync(candidate)) || 'python';
const args = process.argv.slice(2).filter((argument, index) => !(index === 0 && argument === '--'));
const [command, ...rest] = args;
let invocation;

if (command === 'test') {
  invocation = ['-X', 'utf8', '-m', 'unittest', 'discover',
    '-s', 'benchmarks/business-trust-v1/tests', '-v', ...rest];
} else if (command === 'run-workers') {
  invocation = ['-X', 'utf8', 'benchmarks/business-trust-v1/runner.py', ...rest];
} else if (command === 'export-app-jobs') {
  invocation = ['-X', 'utf8', 'benchmarks/business-trust-v1/app_dispatch.py', 'export', ...rest];
} else if (command === 'import-app-trace') {
  invocation = ['-X', 'utf8', 'benchmarks/business-trust-v1/app_dispatch.py', 'import', ...rest];
} else if (command === 'transport-smoke') {
  invocation = ['-X', 'utf8', 'benchmarks/business-trust-v1/transport_smoke.py', ...rest];
} else if (command === 'review') {
  invocation = ['-X', 'utf8', 'benchmarks/business-trust-v1/review.py', ...rest];
} else {
  invocation = ['-X', 'utf8', 'benchmarks/business-trust-v1/experiment.py', ...args];
}

const child = spawnSync(python, invocation, { cwd: root, stdio: 'inherit', windowsHide: true });
if (child.error) {
  console.error('business-trust-v1 Python process could not start');
  process.exit(2);
}
process.exit(child.status ?? 2);

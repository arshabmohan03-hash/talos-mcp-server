#!/usr/bin/env node
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

if (['1', 'true', 'yes'].includes(String(process.env.TALOS_SKIP_PYTHON_INSTALL || '').toLowerCase())) {
  console.log('Skipping Python dependency install because TALOS_SKIP_PYTHON_INSTALL is set.');
  process.exit(0);
}

const requirePython = ['1', 'true', 'yes'].includes(String(process.env.TALOS_REQUIRE_PYTHON || '').toLowerCase());
const serverRoot = path.resolve(__dirname, '..');
const appRoot = process.env.TALOS_APP_ROOT
  ? path.resolve(process.env.TALOS_APP_ROOT)
  : fs.existsSync(path.join(serverRoot, 'app-runtime', 'requirements.txt'))
    ? path.join(serverRoot, 'app-runtime')
    : path.resolve(serverRoot, '..');
const requirements = process.env.TALOS_REQUIREMENTS_FILE
  ? path.resolve(process.env.TALOS_REQUIREMENTS_FILE)
  : path.join(appRoot, 'requirements.txt');

if (!fs.existsSync(requirements)) {
  console.warn(`No Python requirements file found at ${requirements}; skipping.`);
  process.exit(0);
}

const configuredPython = process.env.TALOS_PYTHON || process.env.PYTHON;
const fallbacks = process.platform === 'win32' ? ['python', 'py'] : ['python3', 'python'];
const candidates = Array.from(new Set([configuredPython, ...fallbacks].filter(Boolean)));

let last;
let sawPython = false;
for (const python of candidates) {
  console.log(`Installing Talos Python dependencies with ${python} from ${requirements}`);
  last = spawnSync(python, ['-m', 'pip', 'install', '-r', requirements], {
    stdio: 'inherit',
    shell: process.platform === 'win32',
  });
  if (last.error && last.error.code === 'ENOENT') {
    console.warn(`${python} was not found.`);
    continue;
  }
  sawPython = true;
  if (last.status === 0) {
    process.exit(0);
  }
}

if (!sawPython) {
  const message = 'No Python executable found. Continuing because Talos has a Node fallback for NitroCloud. Set TALOS_REQUIRE_PYTHON=1 to fail instead.';
  if (requirePython) {
    console.error(message);
    process.exit(1);
  }
  console.warn(message);
  process.exit(0);
}

process.exit(last?.status || 1);

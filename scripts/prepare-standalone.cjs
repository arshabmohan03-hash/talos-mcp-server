#!/usr/bin/env node
const fs = require('node:fs');
const path = require('node:path');

const serverRoot = path.resolve(__dirname, '..');
const sourceRoot = process.env.TALOS_SOURCE_ROOT
  ? path.resolve(process.env.TALOS_SOURCE_ROOT)
  : path.resolve(serverRoot, '..');
const runtimeRoot = process.env.TALOS_STANDALONE_ROOT
  ? path.resolve(process.env.TALOS_STANDALONE_ROOT)
  : path.join(serverRoot, 'app-runtime');

const requiredSource = path.join(sourceRoot, 'app', 'ai', 'tools.py');
if (!fs.existsSync(requiredSource)) {
  console.error(`Talos source app not found at ${sourceRoot}`);
  console.error('Set TALOS_SOURCE_ROOT to the AI_BruteForce_Detector-3 project root.');
  process.exit(1);
}

function cleanRuntime() {
  fs.rmSync(runtimeRoot, { recursive: true, force: true });
  fs.mkdirSync(runtimeRoot, { recursive: true });
}

function copyIfExists(relativePath) {
  const src = path.join(sourceRoot, relativePath);
  if (!fs.existsSync(src)) {
    return;
  }

  const dest = path.join(runtimeRoot, relativePath);
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.cpSync(src, dest, {
    recursive: true,
    filter: (entry) => {
      const normalized = entry.replaceAll(path.sep, '/');
      return !normalized.endsWith('/__pycache__')
        && !normalized.includes('/__pycache__/')
        && !normalized.endsWith('.pyc');
    },
  });
}

cleanRuntime();

[
  'app',
  'resources',
  'reports',
  'requirements.txt',
  'attack_model.pkl',
  'auth.log',
  '.env.example',
].forEach(copyIfExists);

const readme = [
  '# Talos App Runtime',
  '',
  'This folder is a generated deploy snapshot used when only `talos-mcp-server` can be uploaded.',
  'Regenerate it from the full project root with:',
  '',
  '```powershell',
  'npm run prepare:standalone',
  '```',
  '',
  'Do not place secrets here. Configure `.env` values as deployment environment variables.',
  '',
].join('\n');

fs.writeFileSync(path.join(runtimeRoot, 'README.md'), readme, 'utf8');

console.log(`Prepared standalone Talos runtime at ${runtimeRoot}`);

import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import type { ExecutionContext } from '@nitrostack/core';
import { callTalosNodeFallback, canHandleTalosNodeFallback } from './talos.node-fallback.js';

export type JsonObject = Record<string, unknown>;

const NODE_ONLY_TOOLS = new Set([
  'send_email',
  'find_research_papers',
  'analyze_link_safety',
  'generate_security_report',
  'send_report_email',
  'self_test_all_tools',
  'get_last_security_report',
]);

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

function resolveServerRoot(): string {
  const cwdRoot = process.cwd();
  if (existsSync(path.join(cwdRoot, 'bridge', 'talos_bridge.py'))) {
    return cwdRoot;
  }

  return path.resolve(__dirname, '..', '..', '..');
}

function hasTalosApp(root: string): boolean {
  return existsSync(path.join(root, 'app', 'ai', 'tools.py'))
    || existsSync(path.join(root, 'resources'));
}

function resolveAppRoot(serverRoot: string): string {
  const candidates = [
    ...(process.env.TALOS_APP_ROOT ? [path.resolve(process.env.TALOS_APP_ROOT)] : []),
    path.join(serverRoot, 'app-runtime'),
    path.resolve(serverRoot, '..'),
  ];

  for (const candidate of candidates) {
    if (hasTalosApp(candidate)) {
      return candidate;
    }
  }

  return candidates[0];
}

function resolvePythonCandidates(): string[] {
  const configured = process.env.TALOS_PYTHON || process.env.PYTHON;
  const fallbacks = process.platform === 'win32' ? ['python', 'py'] : ['python3', 'python'];
  return Array.from(new Set([configured, ...fallbacks].filter(Boolean) as string[]));
}

function isMissingExecutable(error: unknown): boolean {
  return typeof error === 'object'
    && error !== null
    && 'code' in error
    && (error as NodeJS.ErrnoException).code === 'ENOENT';
}

function envFlag(name: string): boolean {
  return ['1', 'true', 'yes'].includes(String(process.env[name] || '').toLowerCase());
}

async function runBridge(
  pythonBin: string,
  bridgeScript: string,
  appRoot: string,
  timeoutMs: number,
  tool: string,
  args: JsonObject,
): Promise<unknown> {
  return await new Promise((resolve, reject) => {
    const child = spawn(pythonBin, [bridgeScript], {
      cwd: appRoot,
      env: {
        ...process.env,
        TALOS_APP_ROOT: appRoot,
        PYTHONUTF8: '1',
        PYTHONDONTWRITEBYTECODE: '1',
      },
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });

    let stdout = '';
    let stderr = '';
    let settled = false;

    const timer = setTimeout(() => {
      if (settled) {
        return;
      }
      settled = true;
      child.kill();
      reject(new Error(`Talos tool '${tool}' timed out after ${timeoutMs}ms`));
    }, timeoutMs);

    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');

    child.stdout.on('data', (chunk) => {
      stdout += chunk;
    });

    child.stderr.on('data', (chunk) => {
      stderr += chunk;
    });

    child.on('error', (error) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      reject(error);
    });

    child.on('close', (code) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);

      if (code !== 0) {
        reject(new Error(`Talos bridge exited with code ${code}: ${stderr.trim() || stdout.trim()}`));
        return;
      }

      try {
        resolve(JSON.parse(stdout));
      } catch (error) {
        reject(new Error(`Talos bridge returned invalid JSON: ${(error as Error).message}; stdout=${stdout}; stderr=${stderr}`));
      }
    });

    child.stdin.end(JSON.stringify({ tool, args }));
  });
}

export async function callTalosTool(
  tool: string,
  args: JsonObject,
  ctx?: ExecutionContext,
): Promise<unknown> {
  const serverRoot = resolveServerRoot();
  const appRoot = resolveAppRoot(serverRoot);
  const bridgeScript = process.env.TALOS_BRIDGE_SCRIPT
    ? path.resolve(process.env.TALOS_BRIDGE_SCRIPT)
    : path.join(serverRoot, 'bridge', 'talos_bridge.py');
  const timeoutMs = Number.parseInt(process.env.TALOS_TOOL_TIMEOUT_MS || '60000', 10);

  if ((NODE_ONLY_TOOLS.has(tool) || envFlag('TALOS_FORCE_NODE_TOOLS')) && canHandleTalosNodeFallback(tool)) {
    ctx?.logger.info('Calling Talos Node fallback tool', { tool, appRoot });
    return callTalosNodeFallback(tool, args, appRoot);
  }

  if (!existsSync(bridgeScript)) {
    if (canHandleTalosNodeFallback(tool)) {
      ctx?.logger.warn('Talos bridge script not found, using Node fallback', { tool, appRoot });
      return callTalosNodeFallback(tool, args, appRoot);
    }
    throw new Error(`Talos bridge script not found at ${bridgeScript}`);
  }

  ctx?.logger.info('Calling Talos Python tool', { tool, appRoot });

  let lastError: unknown;
  for (const pythonBin of resolvePythonCandidates()) {
    try {
      return await runBridge(pythonBin, bridgeScript, appRoot, timeoutMs, tool, args);
    } catch (error) {
      lastError = error;
      if (!isMissingExecutable(error)) {
        if (envFlag('TALOS_NODE_FALLBACK_ON_ERROR') && canHandleTalosNodeFallback(tool)) {
          ctx?.logger.warn('Talos Python bridge failed, using Node fallback', {
            tool,
            error: error instanceof Error ? error.message : String(error),
          });
          return callTalosNodeFallback(tool, args, appRoot);
        }
        throw error;
      }
      ctx?.logger.warn('Python executable not found, trying next candidate', { pythonBin });
    }
  }

  if (canHandleTalosNodeFallback(tool)) {
    ctx?.logger.warn('No Python executable found, using Talos Node fallback', { tool, appRoot });
    return callTalosNodeFallback(tool, args, appRoot);
  }

  throw lastError;
}

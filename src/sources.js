'use strict';
/**
 * Which local processes are plausible log sources, and where each writes its logs.
 *
 * Asking the OS which files a process has open is far more reliable than guessing
 * conventional paths or probing HTTP endpoints - and it costs the watched app nothing.
 */

const { execFile } = require('child_process');
const { promisify } = require('util');
const run = promisify(execFile);

// Listening, but never an app emitting logs.
const IGNORED_PORTS = new Set([5432, 3306, 6379, 27017, 11211]);

async function listListeningPorts() {
  let stdout = '';
  try {
    ({ stdout } = await run('lsof', ['-nP', '-iTCP', '-sTCP:LISTEN'], { timeout: 8000 }));
  } catch {
    return [];
  }
  const seen = new Set();
  const ports = [];
  for (const line of stdout.split('\n').slice(1)) {
    const m = line.match(/^(\S+)\s+(\d+)\s+\S+\s+\S+\s+\S+\s+\S+\s+(.+)$/);
    if (!m) continue;
    const pm = m[3].match(/:(\d+)(?:\s|\(|$)/);
    if (!pm) continue;
    const port = Number(pm[1]);
    const pid = Number(m[2]);
    const key = `${pid}:${port}`;
    if (seen.has(key) || IGNORED_PORTS.has(port) || pid === process.pid) continue;
    seen.add(key);
    ports.push({ pid, port, process: m[1] });
  }
  return ports.sort((a, b) => a.port - b.port);
}

/** The log file a process already has open, if any. */
function logFileForPid(pid) {
  if (!pid) return null;
  const { execFileSync } = require('child_process');
  let out = '';
  try {
    out = execFileSync('lsof', ['-p', String(pid)], { encoding: 'utf8', timeout: 6000 });
  } catch {
    return null;
  }
  const files = [];
  for (const line of out.split('\n')) {
    const parts = line.split(/\s+/);
    const p = parts[parts.length - 1];
    if (!p || !p.startsWith('/') || !p.endsWith('.log')) continue;
    // Skip the OS's own logs - we want this project's output.
    if (/^\/(private\/)?var\/|^\/System\/|^\/Library\//.test(p)) continue;
    if (!files.includes(p)) files.push(p);
  }
  // A file under .logs/ or logs/ is almost certainly the app's own.
  return files.find((f) => f.includes('/.logs/') || f.includes('/logs/')) || files[0] || null;
}

module.exports = { listListeningPorts, logFileForPid };

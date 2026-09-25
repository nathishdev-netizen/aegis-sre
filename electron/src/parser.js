'use strict';
/**
 * Log line parsing - a port of app/core/parser.py.
 *
 * Every rule here exists because a real log format broke the naive version: Loguru
 * scaffolding, JSON-wrapped SSE frames, ANSI colour, stack traces spanning many
 * physical lines, and config values that merely contain the word "timeout".
 */

const RE_ANSI = /\x1b\[[0-9;]*[a-zA-Z]/g;
const stripAnsi = (s) => (s || '').replace(RE_ANSI, '');

// Leading timestamp in the shapes real loggers emit:
//   10:00:01 | 2026-08-28 16:00:18.807 | 2026-08-28T16:00:18.807Z
const RE_TS = /^\s*(?:(\d{4}-\d{2}-\d{2})[ T])?(\d{1,2}:\d{2}:\d{2})([.,]\d{1,6})?(Z|[+-]\d{2}:?\d{2})?\s*/;
// Loguru's "| LEVEL | module:func:line - " scaffolding.
const RE_SCAFFOLD = /^\s*\|?\s*(?:INFO|WARN|WARNING|ERROR|DEBUG|TRACE|CRITICAL|FATAL|SUCCESS)\s*\|\s*[\w.]+:[\w.<>]+:\d+\s*[-—]\s*/i;
const RE_LEVEL = /\b(INFO|WARN|WARNING|ERROR|DEBUG|TRACE|CRITICAL|FATAL|SUCCESS)\b/i;
const LEVEL_MAP = { WARNING: 'WARN', CRITICAL: 'ERROR', FATAL: 'ERROR', SUCCESS: 'INFO' };

const JSON_TEXT_KEYS = ['line', 'message', 'msg', 'log', 'text', 'event'];
const JSON_TS_KEYS = ['ts', 'time', 'timestamp', 'datetime', '@timestamp'];

function asJson(line) {
  const t = (line || '').trim();
  if (!(t.startsWith('{') && t.endsWith('}'))) return null;
  try {
    const v = JSON.parse(t);
    return v && typeof v === 'object' && !Array.isArray(v) ? v : null;
  } catch {
    return null;
  }
}

/** Unwrap a JSON payload but KEEP indentation - it is what marks a stack frame. */
function unwrapPreservingIndent(raw) {
  const text = stripAnsi(raw || '');
  const payload = asJson(text);
  if (!payload) return text.replace(/\s+$/, '');
  for (const k of JSON_TEXT_KEYS) {
    if (typeof payload[k] === 'string') return payload[k].replace(/\s+$/, '');
  }
  return text.replace(/\s+$/, '');
}

// Lines that continue a preceding entry rather than starting a new one.
const CONTINUATION = [
  /^\s*Traceback \(most recent call last\):/,
  /^\s+File "[^"]+", line \d+/,
  /^\s*(?:at|Caused by:|\.\.\.)\s+\S/,
  /^\s*\w+(?:\.\w+)*(?:Error|Exception)\b/,
  /^\s{2,}\S/,
];
const NEW_ENTRY = /^\s*(?:\d{4}-\d{2}-\d{2}[ T])?\d{1,2}:\d{2}:\d{2}|^\s*(?:INFO|WARN|WARNING|ERROR|DEBUG|TRACE|CRITICAL|FATAL)\b/i;

function isContinuation(line) {
  const t = stripAnsi(line || '').replace(/\s+$/, '');
  if (!t.trim()) return false;
  if (NEW_ENTRY.test(t)) return false;
  return CONTINUATION.some((re) => re.test(t));
}

function parseLine(raw) {
  let line = stripAnsi(raw || '').trim();

  const payload = asJson(line);
  if (payload) {
    for (const k of JSON_TEXT_KEYS) {
      if (typeof payload[k] !== 'string') continue;
      let body = payload[k].trim();
      const lvl = payload.level || payload.levelname || payload.severity;
      if (typeof lvl === 'string' && !RE_LEVEL.test(body)) body = `${lvl.toUpperCase()} ${body}`;
      // A structured log carries its own timestamp field; keep it rather than
      // falling back to "now" because the message text has no prefix.
      const stamp = JSON_TS_KEYS.map((tk) => payload[tk]).find((v) => typeof v === 'string' && v.trim());
      line = stamp ? `${stamp.trim()} ${body}` : body;
      break;
    }
  }

  const m = line.match(RE_TS);
  let ts;
  let rest;
  if (m) {
    ts = m[2].length === 7 ? `0${m[2]}` : m[2];
    rest = line.slice(m[0].length);
  } else {
    ts = new Date().toTimeString().slice(0, 8);
    rest = line;
  }
  rest = rest.replace(RE_SCAFFOLD, '');

  const lm = line.match(RE_LEVEL);
  let level;
  if (lm) level = lm[1].toUpperCase();
  else if (/error|failed|timeout|exception|refused|denied|unreachable/i.test(line)) level = 'ERROR';
  else if (/warn/i.test(line)) level = 'WARN';
  else level = 'INFO';
  level = LEVEL_MAP[level] || level;

  return { timestamp: ts, level, message: rest.trim() || line.trim(), raw_line: line };
}

/**
 * Failure causes. Each pattern requires a FAILING context, never a bare keyword:
 * "timeout=45.0s" in a startup banner was once reported as a connection timeout and
 * marked a healthy boot as failed.
 */
const CAUSES = [
  [/\b(timed\s+out|timeout\s+(?:after|calling|waiting|while|exceeded)|(?:read|connect|connection|request|operation)\s+timeout)\b/i,
    'Connection timed out',
    ['Verify the endpoint is reachable', 'Check service health', 'Inspect network latency']],
  [/\bconnection\s+(?:refused|reset|aborted|closed)\b/i,
    'Target service refused the connection',
    ['Confirm the service is running', 'Verify the port and host', 'Check firewall rules']],
  [/\b(?:dns\s+(?:lookup|resolution)\s+failed|name or service not known|could not resolve)\b/i,
    'DNS lookup failed',
    ['Verify the hostname', 'Check DNS resolution', 'Confirm service discovery config']],
  [/\b(unauthorized|forbidden|auth\w* (?:failed|error|denied|rejected)|invalid (?:token|credentials)|401|403)\b/i,
    'Authentication or authorization issue',
    ['Check credentials', 'Verify API keys and tokens', 'Confirm permission scopes']],
  [/\b(rate limit(?:ed|s)? (?:exceeded|hit|reached)|too many requests|429)\b/i,
    'Rate limit exceeded',
    ['Reduce request volume', 'Add backoff and retries', 'Check provider quotas']],
  [/\bpermission denied\b|\baccess denied\b/i,
    'Permission denied',
    ['Check file or service permissions', 'Review access policies']],
  [/\b(?:failed to (?:parse|decode)|(?:parse|decode|json|yaml|syntax) error|malformed|invalid (?:json|yaml|payload|format))\b/i,
    'Input parsing issue',
    ['Validate the payload format', 'Inspect the malformed record', 'Add schema validation']],
  [/\b(5\d{2}\s+(?:internal server error|bad gateway|service unavailable|gateway timeout)|internal server error|bad gateway|service unavailable)\b/i,
    'Upstream service returned a server error',
    ["Check the upstream service's own logs", 'Confirm the endpoint and payload it expects',
      'Verify the service is healthy and not mid-deploy']],
  [/\b(?:lookup|request|call|query|fetch)\s+failed\b/i,
    'A dependency call failed',
    ['Check the target service is reachable', 'Inspect the error detail on the failing line']],
  [/\bfail(?:ing|ed)?\s+closed\b|\bfallback\s+(?:failed|exhausted)\b/i,
    'Request was refused because a safety check could not complete',
    ['Fix the underlying check rather than the refusal',
      'Confirm whether the refusal is genuine or a side effect']],
  [/\b(?:name|attribute)\s+'[^']+'\s+is not defined|NameError|AttributeError\b/i,
    'Code error - an undefined name or attribute was referenced',
    ['Fix the referenced name in the code path shown']],
];

function inferCause(message) {
  for (const [re, cause, fixes] of CAUSES) {
    if (re.test(message || '')) return { cause, fixes };
  }
  return null;
}

const TRANSITIONS = [
  [/request received|incoming request|received request/i, { status: 'running', label: 'Request received' }],
  [/\b(timed\s+out|timeout\s+(?:after|calling|waiting|while|exceeded)|(?:read|connect|connection|request|operation)\s+timeout)\b/i,
    { status: 'failed', label: 'Timeout' }],
  [/\b(connection\s+(?:refused|reset|aborted)|econnrefused|could not resolve|host unreachable)\b/i,
    { status: 'failed', label: 'Connection failure' }],
  [/\bretry|retrying|attempt=\d|backoff\b/i, { status: 'retrying', label: 'Retry attempted' }],
  [/\bskip(?:ped|ping)?\b/i, { status: 'skipped', label: 'Step skipped' }],
  [/\b(error|exception|traceback|failed|failure)\b/i, { status: 'failed', label: 'Failure' }],
  [/response sent|completed|finished|done\b/i, { status: 'completed', label: 'Completed' }],
  [/\bwarn|warning\b/i, { status: 'warning', label: 'Warning' }],
];

function inferTransition(message) {
  for (const [re, t] of TRANSITIONS) {
    if (re.test(message || '')) return t;
  }
  return null;
}

module.exports = {
  stripAnsi, asJson, unwrapPreservingIndent, isContinuation,
  parseLine, inferCause, inferTransition,
};

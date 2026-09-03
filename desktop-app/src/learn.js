'use strict';
/**
 * Learn a source's shape from its own output - a port of app/core/learn.py.
 *
 * The agent must not assume a project's pipeline. A chatbot, an ETL job and a CLI
 * share nothing except that each repeats its own vocabulary, so stages are discovered
 * from the lines rather than declared in config. When evidence is thin these return
 * "unknown" and an empty list; the caller says so instead of drawing imagined stages.
 */

const { stripAnsi, asJson } = require('./parser');

const MIN_LINES_FOR_FORMAT = 8;
const MIN_LINES_FOR_COMPONENTS = 25;
const MAX_COMPONENTS = 12;

// Words that look like components but never are: debug scaffolding, log levels, and
// language builtins that appear inside bracketed type annotations.
const NOT_COMPONENTS = new Set([
  'hdr', 'header', 'headers', 'body', 'payload', 'req', 'res', 'request', 'response',
  'dump', 'raw', 'in', 'out',
  'info', 'warn', 'warning', 'error', 'debug', 'trace', 'critical', 'fatal', 'success',
  'dict', 'list', 'str', 'int', 'float', 'bool', 'any', 'none', 'null', 'true', 'false',
  'object', 'array', 'string', 'number', 'type', 'value', 'field', 'key', 'item',
  'optional', 'union', 'tuple', 'set', 'map', 'self', 'cls', 'args', 'kwargs',
]);

const RE_LOGURU = /\|\s*(?:INFO|WARN|WARNING|ERROR|DEBUG|TRACE|CRITICAL|SUCCESS)\s*\|\s*[\w.]+:/i;
const RE_UVICORN = /^(?:INFO|WARNING|ERROR|DEBUG|CRITICAL):\s{2,}/;
const RE_BRACKETED = /\[[a-zA-Z][\w.\-/]{1,30}\]/;
const RE_BRACKET_TAG = /\[([a-zA-Z][\w.\-]{1,30})\]/g;
const RE_LOGURU_MODULE = /\|\s*([\w.]+):[\w.<>]+:\d+\s*[-—]/;
const RE_ISO = /^\s*\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/;
const RE_DATETIME = /^\s*\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}:\d{2}/;
const RE_TIME = /^\s*\d{1,2}:\d{2}:\d{2}/;

const JSON_KEYS = ['component', 'service', 'module', 'logger', 'name', 'source', 'stage'];
// Rank by specificity, not count: a framework banner is boilerplate, while the
// application's own lines describe the pipeline.
const SPECIFICITY = { json: 4, loguru: 3, bracketed: 2, uvicorn: 1, plain: 0 };

function detectFormat(lines) {
  const clean = lines.filter((l) => l && l.trim()).map(stripAnsi);
  if (!clean.length) return { format: 'unknown', ts: 'unknown' };

  const fmt = {};
  const bump = (k) => { fmt[k] = (fmt[k] || 0) + 1; };
  for (const l of clean) {
    if (asJson(l)) bump('json');
    else if (RE_LOGURU.test(l)) bump('loguru');
    else if (RE_UVICORN.test(l)) bump('uvicorn');
    else if (RE_BRACKETED.test(l)) bump('bracketed');
    else bump('plain');
  }
  const structured = Object.entries(fmt).filter(([k, v]) => k !== 'plain' && v > 0);
  const format = structured.length
    ? structured.sort((a, b) => (SPECIFICITY[b[0]] - SPECIFICITY[a[0]]) || (b[1] - a[1]))[0][0]
    : 'plain';

  const ts = {};
  for (const l of clean) {
    const k = RE_ISO.test(l) ? 'iso' : RE_DATETIME.test(l) ? 'datetime' : RE_TIME.test(l) ? 'time' : 'none';
    ts[k] = (ts[k] || 0) + 1;
  }
  return { format, ts: Object.entries(ts).sort((a, b) => b[1] - a[1])[0][0] };
}

const normalise = (n) => {
  let v = (n || '').trim().replace(/^[.\-_/]+|[.\-_/]+$/g, '');
  if (v.includes('.')) v = v.split('.').pop();
  return v.toLowerCase();
};
const viable = (n) => n.length >= 2 && n.length <= 30 && !NOT_COMPONENTS.has(n)
  && !/^\d+$/.test(n) && /^[a-z][\w-]*$/.test(n);

function extractComponents(lines) {
  const counts = {};
  const firstSeen = {};
  let method = 'none';
  const record = (raw, i) => {
    const n = normalise(raw);
    if (!viable(n)) return;
    counts[n] = (counts[n] || 0) + 1;
    if (!(n in firstSeen)) firstSeen[n] = i;
  };

  lines.forEach((raw, i) => {
    if (!raw || !raw.trim()) return;
    const line = stripAnsi(raw);

    const payload = asJson(line);
    if (payload) {
      for (const k of JSON_KEYS) {
        if (typeof payload[k] === 'string' && payload[k].trim()) {
          record(payload[k], i); method = 'json-field'; break;
        }
      }
      return;
    }
    RE_BRACKET_TAG.lastIndex = 0;
    const tags = [...line.matchAll(RE_BRACKET_TAG)].map((m) => m[1]);
    if (tags.length) {
      tags.slice(0, 2).forEach((t) => record(t, i));
      if (method === 'none' || method === 'bracket-tag') method = 'bracket-tag';
      return;
    }
    const mod = line.match(RE_LOGURU_MODULE);
    if (mod) { record(mod[1], i); if (method === 'none') method = 'logger-module'; }
  });

  // An explicit convention counts at one occurrence; an inferred one must repeat.
  const threshold = (method === 'bracket-tag' || method === 'json-field') ? 1 : 2;
  const names = Object.keys(counts)
    .filter((n) => counts[n] >= threshold)
    .sort((a, b) => firstSeen[a] - firstSeen[b])
    .slice(0, MAX_COMPONENTS);
  if (!names.length) return { components: [], counts: {}, method: 'none' };
  const out = {};
  names.forEach((n) => { out[n] = counts[n]; });
  return { components: names, counts: out, method };
}

function learn(lines) {
  const seen = lines.filter((l) => l && l.trim()).length;
  const p = {
    lines_seen: seen, line_format: 'unknown', timestamp_style: 'unknown',
    components: [], component_counts: {}, method: 'none', confident: false,
  };
  if (seen < MIN_LINES_FOR_FORMAT) return p;

  const f = detectFormat(lines);
  p.line_format = f.format;
  p.timestamp_style = f.ts;
  const c = extractComponents(lines);
  p.components = c.components;
  p.component_counts = c.counts;
  p.method = c.method;
  p.confident = c.components.length > 0 && seen >= MIN_LINES_FOR_COMPONENTS;
  return p;
}

module.exports = { learn, detectFormat, extractComponents, MIN_LINES_FOR_FORMAT, MIN_LINES_FOR_COMPONENTS };

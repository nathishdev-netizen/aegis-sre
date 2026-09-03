'use strict';
/**
 * Model-backed interpretation - a port of app/core/llm.py.
 *
 * Design rule: when no model is configured every function returns null, and the caller
 * describes what was pattern-matched instead, labelled as such. Nothing here invents a
 * cause, and no output is presented as reasoning unless a model actually reasoned.
 */

const fs = require('fs');
const path = require('path');
const os = require('os');

const LOG_WINDOW = 60;
const TIMELINE_WINDOW = 30;

let lastError = null;

/** Read OPENAI_API_KEY from the environment or a .env beside the app. */
function readConfig() {
  const env = { ...process.env };
  for (const dir of [process.cwd(), path.join(__dirname, '..'), path.join(__dirname, '..', '..'), os.homedir()]) {
    const file = path.join(dir, '.env');
    try {
      if (!fs.existsSync(file)) continue;
      for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
        const t = line.trim();
        if (!t || t.startsWith('#') || !t.includes('=')) continue;
        const i = t.indexOf('=');
        const k = t.slice(0, i).trim();
        const v = t.slice(i + 1).trim().replace(/^["']|["']$/g, '');
        // A real environment variable always wins over a file.
        if (k && !(k in process.env) && v) env[k] = v;
      }
      break;
    } catch { /* unreadable .env is not fatal */ }
  }
  return {
    apiKey: env.OPENAI_API_KEY || null,
    model: env.LOG_AGENT_MODEL || 'gpt-4o-mini',
    enabled: (env.LOG_AGENT_LLM || 'true').toLowerCase() !== 'false',
  };
}

function backendStatus() {
  const cfg = readConfig();
  if (!cfg.enabled) {
    return { mode: 'patterns', available: false, detail: 'LLM disabled (LOG_AGENT_LLM=false).' };
  }
  if (!cfg.apiKey) {
    return { mode: 'patterns', available: false, detail: 'No OPENAI_API_KEY set - showing pattern matches only.' };
  }
  if (lastError) return { mode: 'patterns', available: false, detail: lastError };
  return { mode: 'llm', available: true, detail: `Interpreting with ${cfg.model}.` };
}

async function chat(messages, maxTokens = 700) {
  const cfg = readConfig();
  if (!cfg.enabled || !cfg.apiKey) return null;
  try {
    const res = await fetch('https://api.openai.com/v1/chat/completions', {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: `Bearer ${cfg.apiKey}` },
      body: JSON.stringify({
        model: cfg.model, messages, max_tokens: maxTokens, temperature: 0,
        response_format: { type: 'json_object' },
      }),
    });
    if (!res.ok) {
      const text = await res.text();
      // Record WHY, so the UI can say "out of credits" rather than silently behaving
      // as though no key were configured.
      if (/insufficient_quota|no credits remaining/.test(text)) {
        lastError = 'OpenAI account has no credits remaining - add credits to re-enable AI answers';
      } else if (res.status === 429) lastError = 'OpenAI rate limit hit - try again shortly';
      else if (res.status === 401) lastError = 'OPENAI_API_KEY is not valid';
      else lastError = `OpenAI error ${res.status}`;
      return null;
    }
    lastError = null;
    const data = await res.json();
    return data.choices?.[0]?.message?.content || null;
  } catch (err) {
    lastError = `Could not reach OpenAI: ${err.message}`;
    return null;
  }
}

function runContext(snapshot) {
  const timeline = (snapshot.timeline || []).slice(-TIMELINE_WINDOW);
  const logs = (snapshot.log_lines || []).slice(-LOG_WINDOW);
  const lines = [
    `Source: ${snapshot.source?.label || 'unknown'}`,
    `Events observed: ${snapshot.metrics?.total_events || 0}`,
    `Components: ${(snapshot.profile?.components || []).join(', ') || 'none discovered'}`,
    '', 'TIMELINE (oldest to newest):',
  ];
  timeline.forEach((t) => lines.push(`  [${t.time}] ${t.status}: ${t.message}`));
  lines.push('', 'RAW LOG LINES (oldest to newest):');
  logs.forEach((l) => {
    lines.push(`  ${l.level} ${l.message}`);
    // Stack traces fold into their parent entry; include them so the model can name
    // the failing frame instead of guessing from the summary line.
    (l.detail || []).slice(0, 20).forEach((d) => lines.push(`      ${d}`));
  });
  return lines.join('\n');
}

const ASK_SYSTEM = `You answer a developer's question about ONE specific run, using only its logs.

FIRST check whether the thing asked about appears in the logs at all. If it does not,
the verdict is "unknown" and you say it does not appear in this run - do NOT answer
about a different component that happens to have failed, and do NOT accept the
question's premise that it was involved.

Read the question precisely: "did X fail?" asks about failure, not about whether the
word X appears. A run that mentions X while succeeding means the answer is "no".

Prefer the EARLIEST line that explains an outcome over the last line that states it.
"refused as not whitelisted" may follow "lookup FAILED ... 500 Internal Server Error",
in which case the honest answer is that the lookup errored and the service failed
closed, NOT that the user was genuinely off the list.

Quote only lines that appear verbatim below. If you cannot quote a relevant line, the
verdict is "unknown".

Reply as JSON only:
{"verdict":"yes"|"no"|"unknown","answer":"1-2 sentences, direct","confidence":0-100,
 "evidence":["exact log lines you relied on"]}`;

/** Whether a quoted evidence line really occurs in the run's logs. */
function appearsIn(quoted, corpus) {
  const words = (quoted.toLowerCase().match(/[a-z0-9_:/.-]+/g) || []).filter((w) => w.length > 3);
  if (!words.length) return false;
  const hits = words.filter((w) => corpus.includes(w)).length;
  return hits >= Math.max(2, Math.floor(words.length / 2));
}

async function askModel(snapshot, question) {
  if (!question || !question.trim()) {
    return { verdict: 'unknown', answer: 'Ask a question about the current run.', confidence: 0, evidence: [] };
  }
  if (!(snapshot.log_lines || []).length) {
    return { verdict: 'unknown', answer: 'No run to ask about yet - attach a source first.', confidence: 0, evidence: [] };
  }

  const raw = await chat([
    { role: 'system', content: ASK_SYSTEM },
    { role: 'user', content: `${runContext(snapshot)}\n\nQUESTION: ${question}` },
  ], 500);

  if (!raw) {
    const status = backendStatus();
    return {
      verdict: 'unknown', confidence: 0, evidence: [], source: 'unavailable',
      answer: status.available ? 'That question could not be answered just now.' : status.detail,
    };
  }

  let data;
  try { data = JSON.parse(raw); } catch { return { verdict: 'unknown', answer: 'The model returned an unreadable answer.', confidence: 0, evidence: [] }; }

  let verdict = ['yes', 'no', 'unknown'].includes(data.verdict) ? data.verdict : 'unknown';
  let evidence = Array.isArray(data.evidence) ? data.evidence.filter(Boolean).map(String) : [];

  // An answer citing nothing, or citing lines that are not in the run, is not grounded.
  const corpus = (snapshot.log_lines || [])
    .map((l) => `${l.message} ${(l.detail || []).join(' ')}`).join(' ').toLowerCase();
  if (corpus) {
    const grounded = evidence.filter((e) => appearsIn(e, corpus));
    if (verdict !== 'unknown' && !grounded.length) {
      return {
        verdict: 'unknown', confidence: 0, evidence: [], source: 'llm',
        answer: "I cannot answer that from this run's logs - nothing in them supports it.",
      };
    }
    evidence = grounded.length ? grounded : evidence;
  }
  if (!evidence.length && verdict !== 'unknown') verdict = 'unknown';

  return {
    verdict,
    answer: String(data.answer || '').trim(),
    confidence: Math.max(0, Math.min(100, Number(data.confidence) || 0)),
    evidence,
    source: 'llm',
  };
}

const INTERPRET_SYSTEM = `You interpret backend execution logs for a developer watching a run live.

You are given only what was actually observed. Ground every claim in that evidence.
If the logs do not show why something failed, say the cause is unknown. Never guess a
cause with no support in the log lines. If the run has not failed, reason and causes
must be empty.

Return JSON only:
{"summary":"2-3 sentences: what the run did and where it stands",
 "status":"running"|"failed"|"success"|"idle","reason":"why it failed, or empty",
 "causes":["likely causes, most probable first"],"fixes":["concrete next steps"],
 "confidence":0-100,"evidence":["exact log lines supporting your reading"]}`;

async function interpretRun(snapshot) {
  if (!(snapshot.log_lines || []).length) return null;
  const raw = await chat([
    { role: 'system', content: INTERPRET_SYSTEM },
    { role: 'user', content: runContext(snapshot) },
  ]);
  if (!raw) return null;
  try {
    const d = JSON.parse(raw);
    return {
      summary: String(d.summary || '').trim(),
      status: d.status,
      reason: String(d.reason || '').trim(),
      causes: (d.causes || []).map(String).filter(Boolean),
      fixes: (d.fixes || []).map(String).filter(Boolean),
      confidence: Math.max(0, Math.min(100, Number(d.confidence) || 0)),
      evidence: (d.evidence || []).map(String).filter(Boolean),
    };
  } catch { return null; }
}

module.exports = { askModel, interpretRun, backendStatus };

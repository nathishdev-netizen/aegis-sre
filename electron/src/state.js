'use strict';
/**
 * Runtime state - a port of app/core/state.py.
 *
 * Holds what has been observed about the current run and derives the view the UI
 * renders: components, their status, metrics, timeline and the failure diagnosis.
 * Every rule that looks over-careful here exists because a simpler version reported
 * something the logs did not say.
 */

const { EventEmitter } = require('events');
const parser = require('./parser');
const { learn } = require('./learn');

const MAX_LOG_LINES = 200;
const MAX_TIMELINE = 60;
const MAX_LEARN_LINES = 400;

// A later, milder event must never downgrade a failure the run already recorded.
const SEVERITY = {
  idle: 0, info: 0, observed: 0, running: 1, completed: 1, success: 1,
  warning: 2, retrying: 2, skipped: 2, failed: 3,
};

// Debug scaffolding a developer switched on deliberately (payload/header dumps, box
// frames). Counting it as execution swamps the real events - 170 of 200 in one case.
const NOISE = /[─-╿]|\[(?:hdr|header|headers|body|payload|req|res|dump|raw)\]/i;

class RuntimeState extends EventEmitter {
  constructor() {
    super();
    this.reset();
  }

  reset() {
    this.entries = [];
    this.rawForLearning = [];
    this.profile = learn([]);
    this.lanes = new Map();
    this.metrics = { total_events: 0, failures: 0, retries: 0, skipped: 0 };
    this.status = 'idle';
    this.reason = 'Awaiting execution events.';
    this.causes = [];
    this.fixes = [];
    this.summary = 'No logs yet.';
    this.confidence = 0;
    this.interpretation = 'patterns';
    this.evidence = [];
    this.timeline = [];
    this.source = { type: 'idle', label: 'No source attached', path: null };
    this.emitChange();
    return this.snapshot();
  }

  setSource(source) {
    this.source = source;
    this.emitChange();
  }

  isNoise(raw, message) {
    const text = parser.stripAnsi(raw || '');
    if (NOISE.test(text) || NOISE.test(message || '')) return true;
    // A line whose whole content is a logger prefix carries no information.
    return !(message || '').trim().replace(/^\s*[\w.]+:?\s*$/, '');
  }

  componentFor(raw) {
    const discovered = this.profile.components || [];
    if (!discovered.length) return null;
    const text = parser.stripAnsi(raw || '');

    // The LAST [tag] wins: Loguru writes "app.identity:lookup:44 - [intent] ...", so
    // an earlier module path would otherwise beat the tag the developer wrote.
    const tags = [...text.matchAll(/\[([a-zA-Z][\w.\-]{1,30})\]/g)].map((m) => m[1]);
    for (let i = tags.length - 1; i >= 0; i -= 1) {
      const n = tags[i].split('.').pop().toLowerCase();
      if (discovered.includes(n)) return n;
    }
    const payload = parser.asJson(text.trim());
    if (payload) {
      for (const k of ['component', 'service', 'module', 'logger', 'name', 'source', 'stage']) {
        if (typeof payload[k] === 'string') {
          const n = payload[k].split('.').pop().toLowerCase();
          if (discovered.includes(n)) return n;
        }
      }
    }
    const lowered = text.toLowerCase();
    for (const n of discovered) {
      if (new RegExp(`\\b${n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`).test(lowered)) return n;
    }
    // Known vocabulary, and this line is not part of it. Say so rather than inventing
    // a pipeline stage the project does not have.
    return 'runtime';
  }

  ingest(rawLine) {
    if (!rawLine || !rawLine.trim()) return;

    // A traceback is ONE failure, not five. Continuations and debug dumps fold into
    // the entry they belong to so metrics stay honest and the graph stays clean.
    const unwrapped = parser.unwrapPreservingIndent(rawLine);
    const peek = parser.parseLine(rawLine);
    if ((parser.isContinuation(unwrapped) || this.isNoise(rawLine, peek.message)) && this.entries.length) {
      const last = this.entries[this.entries.length - 1];
      last.detail = last.detail || [];
      if (last.detail.length < 40) last.detail.push(unwrapped.replace(/\s+$/, ''));
      this.emitChange();
      return;
    }

    // Learn BEFORE naming, so the first lines are not labelled with a stale vocabulary.
    this.rawForLearning.push(rawLine);
    if (this.rawForLearning.length > MAX_LEARN_LINES) this.rawForLearning.shift();
    const before = (this.profile.components || []).join('|');
    if (this.rawForLearning.length % 5 === 0 || !this.profile.components.length) {
      this.profile = learn(this.rawForLearning);
    }
    const vocabularyChanged = (this.profile.components || []).join('|') !== before;

    const p = parser.parseLine(rawLine);
    const entry = { ...p, comp: this.componentFor(rawLine) };
    this.entries.push(entry);
    if (this.entries.length > MAX_LOG_LINES) this.entries.shift();

    this.metrics.total_events += 1;
    const transition = parser.inferTransition(p.message);

    if (p.level === 'ERROR') this.metrics.failures += 1;
    if (transition && transition.status === 'retrying') this.metrics.retries += 1;
    if (transition && transition.status === 'skipped') this.metrics.skipped += 1;

    // A new request starts a new run: clear the previous verdict so a stale success
    // cannot sit beside a fresh failure.
    if (/request received|call start|starting|invocation/i.test(p.message)
        && ['failed', 'success'].includes(this.status)) {
      this.status = 'running';
      this.reason = 'Run in progress.';
      this.causes = [];
      this.fixes = [];
    }

    // Only an ERROR/WARN line describes a problem. An INFO line mentioning a failure
    // word is describing config - that is how "timeout=45.0s" became an outage.
    if (['ERROR', 'WARN'].includes(p.level)) {
      const cause = parser.inferCause(p.message);
      if (cause) {
        this.reason = cause.cause;
        this.causes = [cause.cause];
        this.fixes = cause.fixes;
      }
    }

    if (transition) {
      const incoming = SEVERITY[transition.status] || 0;
      const current = SEVERITY[this.status] || 0;
      if (incoming >= current) this.status = transition.status;
      this.timeline.push({
        time: p.timestamp, label: transition.label, level: p.level,
        status: transition.status, message: p.message,
      });
      if (this.timeline.length > MAX_TIMELINE) this.timeline.shift();
    }

    if (vocabularyChanged) this.rebuildLanes();
    else this.addToLane(entry);

    this.summary = this.buildSummary();
    this.emitChange();
  }

  addToLane(entry) {
    const name = entry.comp || 'runtime';
    if (!this.lanes.has(name)) this.lanes.set(name, []);
    this.lanes.get(name).push(entry);
  }

  rebuildLanes() {
    // Lines grouped before the vocabulary was known often belong to different
    // components, so a lane must be able to split apart, not merely be renamed.
    this.lanes = new Map();
    for (const e of this.entries) {
      e.comp = this.componentFor(e.raw_line);
      this.addToLane(e);
    }
  }

  laneStatus(nodes) {
    if (nodes.some((n) => n.level === 'ERROR')) return 'failed';
    if (nodes.every((n) => /\bskip(ped|ping)?\b/i.test(n.message))) return 'skipped';
    if (nodes.some((n) => n.level === 'WARN')) return 'running';
    return 'completed';
  }

  buildSummary() {
    if (!this.metrics.total_events) return 'No logs yet.';
    const failed = [...this.lanes.entries()]
      .filter(([n, v]) => n !== 'runtime' && this.laneStatus(v) === 'failed')
      .map(([n]) => n);
    if (failed.length) {
      const last = this.entries.filter((e) => e.level === 'ERROR').pop();
      return `The run failed at ${failed[0]}${last ? `: ${last.message}` : ''}`;
    }
    const comps = [...this.lanes.keys()].filter((n) => n !== 'runtime').length;
    return `${this.metrics.total_events} events observed across ${comps} components, no failures.`;
  }

  snapshot() {
    const lanes = [...this.lanes.entries()].map(([component, nodes]) => ({
      component,
      status: this.laneStatus(nodes),
      node_count: nodes.length,
      errors: nodes.filter((n) => n.level === 'ERROR').length,
      last: nodes[nodes.length - 1] || null,
      notable: nodes.filter((n) => n.level === 'ERROR').pop() || nodes[nodes.length - 1] || null,
      times: nodes.map((n) => n.timestamp).filter(Boolean),
    }));
    return {
      status: this.status,
      running: this.status === 'running',
      summary: this.summary,
      reason: this.reason,
      possible_causes: this.causes,
      suggested_fixes: this.fixes,
      confidence: this.confidence,
      interpretation: this.interpretation,
      evidence: this.evidence,
      metrics: this.metrics,
      profile: this.profile,
      lanes,
      timeline: this.timeline,
      log_lines: this.entries,
      source: this.source,
      updated_at: new Date().toISOString(),
    };
  }

  emitChange() {
    this.emit('change', this.snapshot());
  }
}

module.exports = { RuntimeState };

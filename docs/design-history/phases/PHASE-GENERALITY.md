# Phase — Generality: from one project to any project

**Status:** complete · **Branch:** `dev-v2` · **Tests:** 8 (`tests/test_aegis_generality.py`) + 7-format corpus

---

## The problem this phase exists for

Everything before it was validated against essentially one project. A product
claim needs proof against the log formats the rest of the world writes.

## The corpus (`tests/fixtures/`)

Realistic samples of seven formats: **JSON lines** (node/pino), **Spring
Boot/Java** with a multi-line stack trace and Caused-by chain, **nginx**
access logs, **syslog**, **Go logfmt**, **Rails**, **Docker-compose**
prefixed. Each carries PII, an error, durations and/or correlation ids where
the format supports them.

## The measured matrix, after fixes

| format | events | templates | correlation | durations | signals | PII |
|---|---|---|---|---|---|---|
| json-lines | 8 | 4 | **7** (was 0) | **6** (was 0) | 2 | clean |
| spring-boot | 7 of 15 lines (trace folded) | 3 | — | 6 | 1 | clean |
| nginx-access | 8 | **2** (was 1) | — | — | 0 | clean |
| go-logfmt | 7 | 3 | 6 | 5 | 1 | clean |
| rails | 9 | 4 | — | 3 | 0 | clean |
| syslog | 7 | 5 | — | — | 1 | clean |
| docker-compose | 7 | 5 | — | 4 | 1 | clean |

voice-gateway after all changes: identical to before (776 events, 73
templates, 142+173 correlation, 73 signals, 13 incidents) — generality cost
the original project nothing.

## What the corpus exposed, and the fixes

1. **JSON logs lost their structured fields.** v1's parser unwraps
   `{"msg": ...}` to just the msg — discarding `reqId` and `durationMs`, the
   very fields correlation and baselines feed on. The normalizer now
   re-attaches non-metadata fields as `key=value` text, turning every JSON
   log into logfmt: one downstream idiom instead of two.
2. **A 502 storm was statistically invisible in access logs.** Lines differ
   in almost nothing, so Drain merged 200s and 502s into one template.
   Status codes now collapse to their class (`sc2xx`/`sc5xx`) and a class
   mismatch is a hard veto in similarity — success and failure are
   separately countable in every format.
3. **The store's `pattern` column held raw example lines**, not tokenized
   patterns — wrong since Phase 0, caught only when a generality assertion
   read it. The normalizer now passes the true pattern through.
4. **Demos hardcoded one machine's log path** as their default. Now
   `AEGIS_DEMO_LOG` from the environment; a product has no business knowing
   anyone's home directory.

## What generality honestly does not cover yet

- nginx/syslog carry no correlation ids or durations — nothing to extract,
  and nothing is invented.
- Formats with per-line dates ("05/Sep/2026") still only use time-of-day.
- Windows event logs, multiline JSON pretty-printed logs: untested.

## How to verify

```bash
python3 tests/test_aegis_generality.py
python3 -m aegis.server tests/fixtures/spring-boot.log   # any fixture in the UI
```

# Phase 8 — Incident memory (C15)

**Status:** complete · **Branch:** `dev-v2` · **Tests:** 11 (`tests/test_aegis_memory.py`)

Architecture doc reference: C15 (Incident Memory) — "memory is how you give a
system seniority."

---

## What this phase is for, in one sentence

Every resolved incident becomes a retrievable precedent, so the next
investigation starts from a hypothesis instead of a blank page — and the
system gets better the longer it runs.

## How it works

- **Archiving is automatic.** When the incident manager resolves an incident,
  a callback archives it into the project's own SQLite store (`incident_archive`
  table). Nothing to configure; resolving *is* archiving.
- **Signature-based matching.** A structured fingerprint — services, detectors,
  template ids, severity, and the redacted cause line's own words — compared by
  weighted set-overlap. Within one project, template ids already *are* the
  semantic layer (fingerprinting canonicalised the wording), which is why this
  works without an embedding index at this scale. pgvector is the documented
  stage-2 upgrade, not a day-one dependency.
- **Outcome-labelled.** `worked` / `did_not_work` / `wrong_diagnosis` — and a
  remembered *wrong* diagnosis is kept just as carefully: it stops the same bad
  hypothesis being handed out with a precedent's authority.
- **A hint, never a conclusion.** Every match carries "precedent, not
  conclusion — systems change", and the phrase travels into the model prompt.
- **KnowledgeInjector.** Precedents are fed to the explainer *before* it forms
  a hypothesis; the hypothesis records which precedents it considered.
- **PatternMiner.** Recurring templates/detectors across the archive:
  "template X appears in 6 of 12 incidents" — the report that turns the system
  from explaining incidents to preventing a category of them.

## Measured, on the real log

12 incidents archived automatically. Investigating the day's second identity
failure (INC-9 @18:22), memory reported the first (INC-10 @12:18, similarity
0.545) ranked *above* two unrelated incidents — after a fix: template ids
alone were too coarse (the same error's tails land in different templates,
and every novelty incident shares its detector), so the true precedent
initially **tied at 0.5 with noise**. The cause line's own words joined the
signature; the tie broke. Pinned as
`test_the_true_precedent_outranks_a_shared_detector`.

## How to verify it yourself

```bash
python3 -m aegis.demo.memory            # 1 model call at the end (--no-llm: zero)
python3 tests/test_aegis_memory.py      # 11 tests, zero network
sqlite3 ~/.aegis/projects/<p>/store.db 'SELECT id,severity,cause FROM incident_archive'
```

Both matched incidents' cause lines are in the raw file, hours apart. Record a
wrong outcome and watch it surface in the next match.

## Isolation, unchanged

Memory lives in each project's own store file. `test_projects_do_not_share_memory`
pins that one project's history never informs another's.

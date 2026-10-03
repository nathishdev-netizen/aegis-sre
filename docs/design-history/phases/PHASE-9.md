# Phase 9 — Remediation (C13, tiers T0/T1)

**Status:** complete · **Branch:** `dev-v2` · **Tests:** 13 (`tests/test_aegis_remediation.py`)

---

## What this phase is for, in one sentence

Take a diagnosed incident, locate the code, reproduce the failure as a test,
produce a minimal fix, prove it works — and hand a human a draft, never a merge.

## The safety architecture (all mechanical, none prompt-based)

- **Reproduce-first.** A failing test comes before any patch. If the
  reproducer *passes*, the diagnosis was wrong and the agent STOPS — tested.
- **No merge capability exists.** `AutonomyGate.can_merge()` returns False at
  every tier; T2/T3 are not implemented and constructing them raises.
- **The target repo is never written.** Everything happens in a throwaway
  sandbox copy; output is a proposal bundle under
  `~/.aegis/projects/<p>/proposals/<incident>/` (PROPOSAL.md + reproducer +
  fix.patch). Applying is a human act. Byte-identity of the repo is tested.
- **Gated patches.** Touching tests, CI, secrets, dependencies, or >40 lines
  is rejected before execution. A patch that doesn't make the reproducer pass
  is never proposed.
- **Sandboxed runs.** Temp copy, stripped environment (PATH only — no
  credentials reach generated code), 60s timeout.

## Verified live, 2 model calls

```
status : DRAFT
mapped : app.py:15  raise RuntimeError(f"pool exhausted: ...")
reproducer before patch: exit 1 (fails, as required)
reproducer after patch : exit 0 (fixed, proven)
target repo untouched  : YES
```

**Two live lessons worth recording:**

1. The first run was **blocked mechanically**: the model's diff omitted a
   trailing comment and `patch(1)` refused the hunk. Model diffs are loose
   with context; the runner now falls back to unique-prefix line replacement
   — and fails honestly on ambiguity, because a fuzzy patch applied to the
   wrong line is worse than none.
2. The successful patch is a perfect specimen of **why T1 caps at draft**:
   the model "fixed" the pool bug by loosening `>=` to `>` — off-by-one
   surgery that passes the reproducer but dodges the actual fix
   (`POOL_MAXSIZE = 8`). The protocol proves a patch *works against its
   test*; only a human can say it's the *right* fix. This is the doc's
   warning made flesh: a weak reproducer permits a weak patch, and review is
   not optional.

## How to verify it yourself

```bash
python3 -m aegis.demo.remediate          # 2 model calls, fixture app
python3 tests/test_aegis_remediation.py  # 13 tests, canned model, REAL sandbox runs
cat ~/.aegis/projects/sample-app/proposals/INC-DEMO/PROPOSAL.md
```

## Deliberately not built (doc phase 12)

T2 (auto-merge classes), T3 (auto-revert), OutcomeWatcher's post-merge loop,
and PRPublisher against real GitHub remotes — each needs trust earned first.

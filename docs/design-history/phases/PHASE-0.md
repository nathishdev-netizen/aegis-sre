# Phase 0 — Normalization, Redaction, Fingerprinting

**Status:** complete · **Branch:** `dev-v2` · **Tests:** 11 (`tests/test_aegis_phase0.py`)

Architecture doc reference: C3 (Normalizer & Fingerprint Engine), principle P7
(privacy is architecture), and the volume funnel in §4.

---

## What this phase is for, in one sentence

Turn arbitrary log text into a small countable vocabulary, and remove sensitive
data before anything else in the system can see it.

## Why it comes first

Everything above this layer counts **templates**, not lines. Until that
reduction exists, detection would have to look at every line — and looking at
every line with a model is the single anti-pattern the whole architecture is
built to avoid. The doc's cost illustration: 50M lines/day through an LLM is
unaffordable; 50M lines → ~380 templates → ~3 incidents is 3–12 model calls.

Redaction comes first for a different reason: there is no second chance. Once an
unredacted phone number has been written to disk or sent to a provider, deleting
the row does not undo the exposure.

---

## What was built

| File | Component | Job |
|---|---|---|
| `aegis/contracts/events.py` | schemas | `Event`, `Template`, `Signal`, `RawRecord` — the types every layer agrees on |
| `aegis/l2_normalization/redactor.py` | C3 `Redactor` | Typed placeholders for phone/email/card/token/secret/IP |
| `aegis/l2_normalization/fingerprint.py` | C3 `Fingerprinter` | Drain-style template discovery |
| `aegis/demo/phase0.py` | — | The manual verification run below |

The package mirrors the doc's layer map (`l1_ingestion` … `l6_correlation`), so
any component's home is predictable from the layer it belongs to.

---

## How to verify it yourself

```bash
python3 -m aegis.demo.phase0                    # uses the voice-gateway log
python3 -m aegis.demo.phase0 /path/to/other.log # or any log file you like
python3 tests/test_aegis_phase0.py              # 11 regression tests
```

### What you should see, and what it means

**1. Redaction.** Against the real voice-gateway log:

```
PHONE      203 value(s) removed
EMAIL       45 value(s) removed
194 of 1288 lines contained sensitive data (15%)
```

Those are real customer phone numbers and email addresses, present because the
service ran with payload dumping on. Every one would otherwise have been stored
locally and posted to an LLM provider.

**Check it independently:**
```bash
grep -o 'from=[0-9]*' <logfile> | sort -u | head    # the raw values
python3 -m aegis.demo.phase0 | grep '<PHONE>'       # confirm they are gone
```

**2. The funnel.**

```
  1288 log lines
   135 distinct templates
   9.5x reduction
```

Confirm the input count with `wc -l <logfile>`. If reduction is ~1×,
fingerprinting is broken — that is exactly the bug caught below.

**3. Novelty.** The first occurrence of any template is flagged. No baseline, no
training, no model — a line nobody has seen usually means code took a path
nobody has taken.

---

## Bugs found while building this (all now regression-tested)

Each was found by running against the real log, not by writing a test first.

| Bug | Symptom | Why it mattered |
|---|---|---|
| `leaf = self._leaf_for(...) or []` | 5 identical-shape lines → 5 templates | An empty leaf is falsy, so every template was appended to a throwaway list. Reduction was 1×; the entire funnel was inert. |
| Card rule ate the trailing space | `<CARD>charged` | `(?:\d[ -]?){13,19}` consumed the separator after the last digit. |
| Secret counted twice | one value, two redactions | `api_key=sk-…` matched the keyed rule *and* the bare-token rule. |
| Bearer token leaked | `Authorization: <SECRET> eyJhbGci…` | Reordering made the rule redact the word "Bearer" and leave the credential. **A fail-open** — P7 forbids this outright. |
| 124 false card hits | confidence scores redacted | `0.8163871169090271` matched the card shape. Fixed with a Luhn check: real cards pass, random digits fail ~90% of the time. |

The last two are the instructive pair. One was failing *open* (leaking a real
secret), the other failing *closed too hard* (destroying analytical data). Both
were invisible until run against a real file.

---

## What this phase deliberately does **not** do

- **No detection.** Counting templates is not deciding something is wrong.
- **No storage yet.** Templates live in memory; persistence is Phase 0b.
- **No model calls.** Fingerprinting is a string algorithm. If any part of
  normalization called an LLM, it would be misplaced.
- **No changes to `app/`.** `grep -rn aegis app/` returns nothing. The existing
  agent is provably unaffected.

---

## Known limitations, stated honestly

- **`135` templates includes some noise.** Multi-line payload dumps (`│ x = y`)
  fingerprint as generic shapes. Real fix is continuation folding at ingest,
  which `app/core/parser.py` already does — wiring that in is Phase 0b.
- **Redaction is regex-based.** It will not catch a name in free text or an
  unusual identifier format. It catches the common, high-volume cases.
- **No persistence.** Restarting loses template counts.

---

## Next

Phase 0b wires these into a persistent store and the `Event` contract, so
templates and their counts survive a restart. Then Phase 1: correlation keys.

"""Phase 0 regression tests - normalization, redaction, fingerprinting.

Same rule as tests/test_regressions.py: every test here is named for a real
failure observed while running against a real log file, not a hypothetical.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.l2_normalization.fingerprint import Fingerprinter  # noqa: E402
from aegis.l2_normalization.redactor import Redactor  # noqa: E402


# --- Redaction ---------------------------------------------------------------

def test_phone_numbers_without_a_plus_are_redacted():
    """The reference log writes phone numbers as bare digits (from=916360722483).
    A rule requiring a leading + would have missed all 203 of them."""
    result = Redactor().redact("[api] CALL START from=916360722483 to=918031321575")
    assert "916360722483" not in result.text
    assert result.counts.get("PHONE") == 2


def test_scheme_prefixed_tokens_do_not_leak():
    """Reordering the rules made "Authorization: Bearer eyJ..." redact only the
    word Bearer and leave the credential in the clear - a fail-open."""
    result = Redactor().redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abcdef")
    assert "eyJhbGciOiJIUzI1NiJ9abcdef" not in result.text
    assert result.counts.get("SECRET") == 1


def test_confidence_scores_are_not_card_numbers():
    """The card rule fired on the fractional part of
    "SpeechConfidenceScore = 0.8163871169090271" - 124 times in one file -
    destroying the numbers detection needs while protecting nothing."""
    text = "SpeechConfidenceScore  = 0.8163871169090271"
    result = Redactor().redact(text)
    assert result.text == text
    assert "CARD" not in result.counts


def test_real_card_numbers_are_still_redacted():
    """The Luhn fix must not have disarmed the rule it was fixing."""
    result = Redactor().redact("card 4111 1111 1111 1111 charged")
    assert "4111" not in result.text
    assert result.counts.get("CARD") == 1
    # The trailing separator must survive: "(?:\\d[ -]?){13,19}" ate the space
    # and produced "<CARD>charged".
    assert "<CARD> charged" in result.text


def test_durations_survive_redaction():
    """Detection is built entirely out of durations and status codes. A redactor
    that eats them silently breaks every layer above it."""
    text = "[tts] Synthesised 154 chars in 7612ms - 161154 bytes wav"
    assert Redactor().redact(text).text == text
    assert Redactor().redact("[orch] returned status=200 in 6946ms").counts == {}


def test_a_secret_is_counted_once():
    """"api_key=sk-..." matched both the keyed SECRET rule and the bare TOKEN
    rule, reporting two redactions for one value."""
    result = Redactor().redact("api_key=sk-proj-AbC123XyZ456 model=gpt-4")
    assert sum(result.counts.values()) == 1


# --- Fingerprinting ----------------------------------------------------------

def test_varying_lines_collapse_to_one_template():
    """The architecture document's headline example. `leaf = ... or []` made an
    empty leaf falsy, so every line was appended to a throwaway list and became
    its own template - the exact opposite of this layer's job."""
    fingerprinter = Fingerprinter()
    for sku in ["A19", "B42", "C88", "D01", "E77"]:
        fingerprinter.add(f"reserve failed for sku {sku}: insufficient stock")
    assert fingerprinter.template_count == 1
    assert fingerprinter.templates()[0].count == 5


def test_template_id_is_stable_across_widening():
    """Template ids are stored and counted across restarts. Re-keying a template
    when it widens would orphan every count that already referenced it."""
    fingerprinter = Fingerprinter()
    first = fingerprinter.add("reserve failed for sku A19: insufficient stock")
    second = fingerprinter.add("reserve failed for sku B42: insufficient stock")
    assert first.template_id == second.template_id


def test_first_occurrence_is_flagged_novel():
    """Novelty needs no baseline and no model: a line nobody has seen usually
    means code took a path nobody has taken."""
    fingerprinter = Fingerprinter()
    assert fingerprinter.add("database connection established").is_novel is True
    assert fingerprinter.add("database connection established").is_novel is False


def test_different_lengths_are_different_templates():
    """Lines of different token counts must never merge, or unrelated messages
    average together and the counts stop meaning anything."""
    fingerprinter = Fingerprinter()
    fingerprinter.add("payment authorized")
    fingerprinter.add("payment authorized for order 42 by gateway alpha")
    assert fingerprinter.template_count == 2


def test_redaction_runs_before_fingerprinting():
    """If fingerprinting saw raw text, every distinct phone number would create
    its own template and the funnel would not reduce at all."""
    redactor, fingerprinter = Redactor(), Fingerprinter()
    for number in ["916360722483", "919449248040", "918031321575"]:
        fingerprinter.add(redactor.redact(f"[api] CALL START from={number}").text)
    assert fingerprinter.template_count == 1

# --- Normalizer folding (Phase 0b) -------------------------------------------

def _events(lines, **kw):
    from aegis.l2_normalization.normalizer import Normalizer
    return list(Normalizer(**kw).feed_all(lines))


def test_dump_rows_fold_under_their_title():
    """The reference service prints a table row by row THROUGH the logger, so
    every row is a fully-formed line with its own timestamp - indentation
    folding never sees them, and 543 of 1288 lines became junk templates."""
    events = _events([
        "2026-09-01 19:00:47 INFO voice: ┌─ PLIVO → /voice/hangup",
        "2026-09-01 19:00:47 INFO voice: │  BillDuration = 60",
        "2026-09-01 19:00:47 INFO voice: │  CallStatus   = ringing",
        "2026-09-01 19:00:48 INFO voice: [api] CALL END - 16s",
    ])
    assert len(events) == 2, [e.text_redacted for e in events]
    assert events[0].fields["folded_lines"] == 2
    assert "BillDuration = 60" in events[0].text_redacted


def test_dump_title_starts_its_own_event():
    """First fix folded the ┌ title row under whatever came before it, so every
    dump collapsed into the blank "INFO voice:" template and the template list
    said nothing about what was dumped."""
    events = _events([
        "2026-09-01 19:00:46 INFO voice: [api] answering",
        "2026-09-01 19:00:47 INFO voice: ┌─ PLIVO → /voice/hangup",
        "2026-09-01 19:00:47 INFO voice: │  BillDuration = 60",
    ])
    assert len(events) >= 2
    # The title must live in its own event (with its rows folded under it),
    # not inside the previous one. Its logger scaffold may legitimately
    # precede the ┌ character, so assert containment, not prefix.
    assert "┌─ PLIVO" not in events[0].text_redacted
    assert "┌─ PLIVO" in events[1].text_redacted
    assert events[1].fields.get("folded_lines") == 1


def test_template_names_the_head_not_the_body():
    """A folded event's template is its head line's shape; the body varies per
    occurrence and would fragment the template space if included."""
    events = _events([
        "2026-09-01 19:00:47 INFO voice: ┌─ PLIVO → /voice/answer",
        "2026-09-01 19:00:47 INFO voice: │  CallUUID = abc",
        "2026-09-01 19:00:48 INFO voice: ┌─ PLIVO → /voice/answer",
        "2026-09-01 19:00:48 INFO voice: │  CallUUID = def",
    ])
    assert len(events) == 2
    assert events[0].template_id == events[1].template_id


def test_folding_is_bounded():
    """One malformed stream must not swallow the whole file into one event."""
    from aegis.l2_normalization.normalizer import MAX_CONTINUATION_LINES
    lines = ["2026-09-01 19:00:47 INFO voice: ┌─ dump"] + [
        "2026-09-01 19:00:47 INFO voice: │  row = 1"
    ] * (MAX_CONTINUATION_LINES + 50)
    events = _events(lines)
    assert len(events) > 1, "unbounded fold swallowed the stream"


def test_normalizer_redacts_folded_bodies_too():
    """PII inside a dump body must not survive just because it was folded."""
    events = _events([
        "2026-09-01 19:00:47 INFO voice: ┌─ PLIVO → /voice/answer",
        "2026-09-01 19:00:47 INFO voice: │  CallerName = +919449248040",
    ])
    assert "919449248040" not in events[0].text_redacted
    assert events[0].redactions.get("PHONE") == 1


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  ERROR {name}: {exc.__class__.__name__}: {exc}")
    print(f"\n{'FAILED' if failures else 'All Phase 0 tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)

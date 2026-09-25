"""Remove sensitive values before anything else in the system sees them.

Design principle P7: redaction is architecture, not a feature. It happens before
storage, before embeddings, before any model call. There is no second chance -
once an unredacted value has been written to disk or sent to a provider, the
exposure has already happened and deleting the row does not undo it.

This matters concretely here. The reference log this was built against contains
real customer phone numbers and email addresses, because the service was run
with payload dumping enabled. Every one of those would otherwise have been
persisted to the local store and posted to an LLM provider.

Fail closed: a pattern that might be sensitive is redacted. A false positive
costs a little readability; a false negative is a data-protection incident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Ordered. Longer, more specific patterns run first so a card number is not
# partially consumed by the generic long-digit rule.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Card numbers: 13-19 digits, optionally grouped. Checked before phone
    # numbers, which are shorter and would otherwise match a fragment.
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # Bearer tokens, API keys and the like. Deliberately broad.
    ("TOKEN", re.compile(
        r"\b(?:sk|pk|rk|ghp|gho|ghs|xox[baprs])[-_][A-Za-z0-9_-]{8,}\b")),
    ("TOKEN", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*", re.I)),
    # Common secret-bearing keys in structured text: token=..., password: "..."
    ("SECRET", re.compile(
        r"\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|"
        r"refresh[_-]?token|authorization|auth[_-]?token|private[_-]?key)"
        r"\s*[=:]\s*[\"']?([^\s\"',}]{4,})[\"']?", re.I)),
    # Phone numbers: 10-15 digits with an optional country prefix. The reference
    # log carries these as bare digits (from=916360722483), so a leading + is
    # not required - which is precisely why a naive rule would have missed them.
    ("PHONE", re.compile(r"\+?\b\d{10,15}\b")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
)

# Values that look sensitive but are not, and whose loss would break analysis.
# Durations, ports, status codes and timestamps must survive redaction: the
# detection layer is built entirely out of them.
_KEEP = re.compile(
    r"\b(?:"
    r"\d+\s*(?:ms|s|sec|secs|seconds?|m|min|mins|h|hrs?)\b"   # durations
    r"|status[=:]\s*\d{3}\b"                                   # status codes
    r"|port[=:]\s*\d+\b"                                       # ports
    r"|v\d+(?:\.\d+)*\b"                                       # versions
    r")", re.I,
)




# Rules whose match must pass an extra check before it is treated as sensitive.




# Rules whose match must pass an extra check before it is treated as sensitive.




# Rules whose match must pass an extra check before it is treated as sensitive.




# Rules whose match must pass an extra check before it is treated as sensitive.


def _luhn_ok(digits: str) -> bool:
    """The checksum every real card number satisfies.

    Without it the card rule fired on any long digit run. In the reference log
    that meant the fractional part of "SpeechConfidenceScore = 0.8163871169090271"
    was redacted as a card number, 124 times - destroying the very numbers the
    detection layer needs while protecting nothing.
    """
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


# Rules whose match must pass an extra check before it is treated as sensitive.
_VALIDATORS = {
    "CARD": lambda text: _luhn_ok(re.sub(r"\\D", "", text)),
}


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int]

    @property
    def redacted_anything(self) -> bool:
        return bool(self.counts)


class Redactor:
    """Replaces sensitive values with typed placeholders.

    Placeholders are typed (`<EMAIL>`, `<PHONE>`) rather than blanked, because
    downstream layers still need to know that a value was present and what kind
    it was. "Called <PHONE>" remains analysable; "Called" does not.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def redact(self, text: str) -> RedactionResult:
        if not self.enabled or not text:
            return RedactionResult(text=text, counts={})

        counts: dict[str, int] = {}

        # Protect the spans that must survive, so a duration like "45000ms" is
        # never mistaken for a phone number.
        protected: list[str] = []

        def _shield(match: re.Match[str]) -> str:
            protected.append(match.group(0))
            return f"\x00{len(protected) - 1}\x00"

        working = _KEEP.sub(_shield, text)

        for label, pattern in _RULES:
            def _replace(match: re.Match[str], _label: str = label) -> str:
                validator = _VALIDATORS.get(_label)
                if validator is not None and not validator(match.group(0)):
                    # Looks like the shape, fails the check - leave it alone.
                    return match.group(0)
                # A rule with a capture group redacts only the secret itself,
                # keeping the key name so the line still reads sensibly.
                if match.groups():
                    whole, secret = match.group(0), match.group(1)
                    counts[_label] = counts.get(_label, 0) + 1
                    return whole.replace(secret, f"<{_label}>")
                counts[_label] = counts.get(_label, 0) + 1
                return f"<{_label}>"

            working = pattern.sub(_replace, working)

        # Restore the protected spans.
        def _unshield(match: re.Match[str]) -> str:
            return protected[int(match.group(1))]

        working = re.sub(r"\x00(\d+)\x00", _unshield, working)
        return RedactionResult(text=working, counts=counts)


# Module-level default, so callers that do not need configuration stay simple.
default_redactor = Redactor()


def redact(text: str) -> str:
    return default_redactor.redact(text).text

"""Collapse arbitrary log text into a small, countable vocabulary.

This is the volume reduction the whole platform rests on. Millions of lines
become hundreds of templates; every layer above counts templates, not lines.
Without it, detection would have to look at each line, and looking at each line
with a model is the single anti-pattern the architecture exists to avoid.

The algorithm is Drain: a fixed-depth prefix tree keyed on token count and the
leading tokens, with similarity matching inside each leaf. It is a string
algorithm, not a model - deterministic, microseconds per line, and the same
input always yields the same template id. That stability is required: template
ids are stored, counted and compared across restarts.

Reference: He et al., "Drain: An Online Log Parsing Approach with Fixed Depth
Tree" (ICWS 2017).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

WILDCARD = "<*>"

# Tokens replaced before matching, so that two lines differing only in an id or
# a duration land on the same template. Ordered: specific before general.
_VARIABLE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Quoted strings first, so their whole content collapses to one token. The
    # doc's own template example writes `sku <STR>` - and without this, fifteen
    # occurrences of the same warning quoting different filler text became
    # fifteen templates and fifteen novelty signals. Bounded at 80 chars so a
    # stray apostrophe cannot swallow half a line.
    # A quoted HTTP request line is structure, not a variable: masking
    # "GET /api/orders HTTP/1.1" to <STR> merged an access log's every line -
    # 200s and 502s, /healthz and /api - into ONE template, so a 502 storm
    # would have been invisible. Unquote it and let tokenization handle it.
    (re.compile(r'"((?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s[^"\n]{0,120})"'), r"\1"),
    # Keep the status code distinct: sc200 and sc502 must not merge to <NUM>.
    (re.compile(r'\b(HTTP/\d(?:\.\d)?)\s+(\d{3})\b'), r"\1 sc\2"),
    (re.compile(r'\bstatus[=:]\s*(\d{3})\b', re.I), r"status:sc\1"),
    # ...then collapse to the CLASS: 200 and 201 are the same story, 200 and
    # 502 are not. The class token is a hard split below.
    (re.compile(r"\bsc([1-5])\d{2}\b"), r"sc\1xx"),
    (re.compile(r"'[^'\n]{0,80}'"), "<STR>"),
    (re.compile(r'"[^"\n]{0,80}"'), "<STR>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<UUID>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<HEX>"),
    (re.compile(r"https?://\S+"), "<URL>"),
    (re.compile(r"(?:/[\w.-]+){2,}/?"), "<PATH>"),
    # Placeholders the redactor already inserted are variables by definition.
    (re.compile(r"<(?:EMAIL|PHONE|CARD|TOKEN|SECRET|IP)>"), "<PII>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds?)\b", re.I), "<DUR>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<NUM>"),
)


def _tokenize(message: str) -> list[str]:
    """Split into tokens with variable parts already generalised."""
    working = message
    for pattern, replacement in _VARIABLE_PATTERNS:
        working = pattern.sub(replacement, working)
    return working.split()


@dataclass
class TemplateRecord:
    """One discovered log shape and how often it has been seen."""

    id: str
    tokens: list[str]
    count: int = 0
    first_seen: str = ""
    last_seen: str = ""
    example: str = ""
    service: str = ""

    @property
    def pattern(self) -> str:
        return " ".join(self.tokens)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pattern": self.pattern,
            "count": self.count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "example": self.example,
            "service": self.service,
        }


@dataclass
class Match:
    template_id: str
    pattern: str
    is_novel: bool
    count: int


class Fingerprinter:
    """Drain-style template discovery.

    `similarity` is the fraction of positions that must agree for two lines to
    share a template. 0.5 is Drain's published default and behaves well on real
    logs; higher fragments templates, lower merges unrelated ones.
    """

    def __init__(self, depth: int = 4, similarity: float = 0.5, max_children: int = 100) -> None:
        # Depth below 3 leaves no room for prefix discrimination.
        self.depth = max(3, depth)
        self.similarity = similarity
        self.max_children = max_children
        self._root: dict[Any, Any] = {}
        self._templates: dict[str, TemplateRecord] = {}

    # -- tree ---------------------------------------------------------------

    def _leaf_for(self, tokens: list[str], create: bool) -> list[TemplateRecord] | None:
        """Walk the prefix tree to the bucket that could hold these tokens.

        Keyed first on token count, then on the leading tokens. Two lines of
        different length are never compared, which is what keeps matching cheap.
        """
        node = self._root
        key: Any = len(tokens)
        if key not in node:
            if not create:
                return None
            node[key] = {}
        node = node[key]

        # Interior levels are keyed on tokens; a token containing a variable is
        # keyed as the wildcard so numeric noise does not explode the tree.
        for depth in range(min(self.depth - 2, len(tokens))):
            token = tokens[depth]
            token_key = WILDCARD if token.startswith("<") and token.endswith(">") else token
            if token_key not in node:
                if not create:
                    # Fall back to the wildcard branch rather than declaring no
                    # match: an unseen first token is usually a variable.
                    if WILDCARD in node:
                        node = node[WILDCARD]
                        continue
                    return None
                if len(node) >= self.max_children:
                    token_key = WILDCARD
                    node.setdefault(token_key, {})
                else:
                    node[token_key] = {}
            node = node[token_key]

        if "__leaf__" not in node:
            if not create:
                return None
            node["__leaf__"] = []
        return node["__leaf__"]

    _STATUS_CLASS = re.compile(r"^sc[1-5]xx$")

    @classmethod
    def _similarity(cls, tokens: list[str], template: list[str]) -> float:
        """Fraction of positions that agree, wildcards counting as agreement.

        A status-class mismatch (sc2xx vs sc5xx) is a hard veto, not one
        disagreeing token: an access log's lines differ in almost nothing
        else, so plain similarity merged 200s and 502s into one template and
        a 502 storm was statistically invisible."""
        if not template:
            return 0.0
        agreed = 0
        for token, slot in zip(tokens, template):
            if token == slot or slot == WILDCARD:
                agreed += 1
            elif cls._STATUS_CLASS.match(token) and cls._STATUS_CLASS.match(slot):
                return 0.0
        return agreed / len(template)

    @staticmethod
    def _merge(tokens: list[str], template: list[str]) -> list[str]:
        """Widen a template so positions that vary become wildcards."""
        return [
            slot if slot == token else WILDCARD
            for token, slot in zip(tokens, template)
        ]

    def _template_id(self, tokens: list[str]) -> str:
        digest = hashlib.sha1(" ".join(tokens).encode("utf-8")).hexdigest()[:8]
        return f"T-{digest}"

    # -- api ----------------------------------------------------------------

    def add(self, message: str, when: str = "", service: str = "") -> Match:
        """Assign a message to a template, discovering one if needed."""
        tokens = _tokenize(message)
        if not tokens:
            tokens = ["<EMPTY>"]

        # `or []` here would be a bug: an empty leaf is falsy, so the real list
        # would be discarded and every template appended to a throwaway - every
        # line becoming its own template, which is the opposite of the job.
        leaf = self._leaf_for(tokens, create=True)
        if leaf is None:
            leaf = []
        best: TemplateRecord | None = None
        best_score = 0.0
        for candidate in leaf:
            score = self._similarity(tokens, candidate.tokens)
            if score > best_score:
                best, best_score = candidate, score

        if best is not None and best_score >= self.similarity:
            widened = self._merge(tokens, best.tokens)
            if widened != best.tokens:
                # The id is stable across widening: it identifies the cluster,
                # not the current wording. Re-keying here would break every
                # stored count that already referenced this template.
                best.tokens = widened
            best.count += 1
            best.last_seen = when or best.last_seen
            record = best
            novel = False
        else:
            record = TemplateRecord(
                id=self._template_id(tokens),
                tokens=list(tokens),
                count=1,
                first_seen=when,
                last_seen=when,
                example=message,
                service=service,
            )
            leaf.append(record)
            self._templates[record.id] = record
            novel = True

        return Match(
            template_id=record.id,
            pattern=record.pattern,
            is_novel=novel,
            count=record.count,
        )

    def templates(self) -> list[TemplateRecord]:
        """All discovered templates, most frequent first."""
        return sorted(self._templates.values(), key=lambda t: t.count, reverse=True)

    @property
    def template_count(self) -> int:
        return len(self._templates)

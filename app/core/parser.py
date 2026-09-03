from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def short_time(iso_string: str) -> str:
    try:
        return datetime.fromisoformat(iso_string).strftime("%H:%M:%S")
    except ValueError:
        return datetime.now().strftime("%H:%M:%S")


def infer_level(line: str) -> str:
    if re.search(r"error|failed|timeout|exception|refused|denied|unreachable", line, re.I):
        return "ERROR"
    if re.search(r"warn", line, re.I):
        return "WARN"
    if re.search(r"debug|trace", line, re.I):
        return "DEBUG"
    return "INFO"


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    """Remove terminal colour codes.

    Coloured output is normal for local dev servers, and the escape bytes otherwise
    end up inside the level and the message text.
    """
    return ANSI_ESCAPE.sub("", text)



# Lines that belong to a preceding log entry rather than starting a new one.
# A Python traceback, a Java stack trace or a wrapped message is ONE event; treating
# each physical line as its own event inflates metrics and invents components.
CONTINUATION_PATTERNS = (
    re.compile(r"^\s*Traceback \(most recent call last\):"),
    re.compile(r'^\s+File "[^"]+", line \d+'),
    re.compile(r"^\s*(?:at|Caused by:|\.\.\.)\s+\S"),      # Java / JVM frames
    re.compile(r"^\s*\w+(?:\.\w+)*(?:Error|Exception)\b.*"),  # trailing exception type
    re.compile(r"^\s{2,}\S"),                                  # any indented continuation
)

# A line that opens a timestamped entry starts a NEW event even if it looks indented.
NEW_ENTRY_PATTERN = re.compile(
    r"^\s*(?:\d{4}-\d{2}-\d{2}[ T])?\d{1,2}:\d{2}:\d{2}"
    r"|^\s*(?:INFO|WARN|WARNING|ERROR|DEBUG|TRACE|CRITICAL|FATAL)\b",
    re.I,
)


def is_continuation(line: str) -> bool:
    """Whether a line continues the previous log entry instead of starting a new one."""
    text = strip_ansi(line or "").rstrip()
    if not text.strip():
        return False
    if NEW_ENTRY_PATTERN.match(text):
        return False
    return any(pattern.match(text) for pattern in CONTINUATION_PATTERNS)

def unwrap_payload_preserving_indent(raw_line: str) -> str:
    """Unwrap a JSON payload but keep leading whitespace.

    Continuation detection depends on indentation, and SSE sources wrap each line as
    {"line": "  File ..."} - so the wrapper must come off before the indent is read.
    """
    text = strip_ansi(raw_line or "")
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return text.rstrip()
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return text.rstrip()
    if not isinstance(payload, dict):
        return text.rstrip()
    for key in ("line", "message", "msg", "log", "text", "event"):
        value = payload.get(key)
        if isinstance(value, str):
            return value.rstrip()
    return text.rstrip()


def unwrap_payload(raw_line: str) -> str:
    """Pull the log text out of a structured event payload.

    SSE/JSON sources send frames like {"line": "10:00:01 INFO ..."} or
    {"message": "..."}. Parsing the JSON wrapper instead of its contents makes every
    field wrong - the level, the timestamp and the component all come from syntax
    rather than from the log itself.
    """
    line = strip_ansi(raw_line or "").strip()
    if not (line.startswith("{") and line.endswith("}")):
        return line
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return line
    if not isinstance(payload, dict):
        return line

    for key in ("line", "message", "msg", "log", "text", "event"):
        value = payload.get(key)
        if isinstance(value, str):
            if not value.strip():
                return ""  # wrapper around a blank line - unwrap to blank, not to JSON
            # A level supplied as its own field wins over whatever the text implies.
            level = payload.get("level") or payload.get("levelname") or payload.get("severity")
            if isinstance(level, str) and not re.search(r"\b(INFO|WARN|WARNING|ERROR|DEBUG|TRACE)\b", value, re.I):
                return f"{level.upper()} {value.strip()}"
            return value.strip()
    return line


# Leading timestamp in the shapes real loggers emit:
#   10:00:01                      bare time
#   2026-08-28 16:00:18.807       Loguru / stdlib date + time + millis
#   2026-08-28T16:00:18.807Z      ISO 8601
TIMESTAMP_PREFIX = re.compile(
    r"^\s*(?:(?P<date>\d{4}-\d{2}-\d{2})[ T])?"
    r"(?P<time>\d{1,2}:\d{2}:\d{2})"
    r"(?P<frac>[.,]\d{1,6})?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?"
    r"\s*"
)

# Loguru's "| LEVEL | module:func:line - " scaffolding, and the bracketed service
# tag some services prefix. Removing it leaves the human-written message.
LOGGER_SCAFFOLD = re.compile(
    r"^\s*\|?\s*(?:INFO|WARN|WARNING|ERROR|DEBUG|TRACE|CRITICAL|FATAL|SUCCESS)\s*\|\s*"
    r"[\w.]+:[\w.<>]+:\d+\s*[-\u2014]\s*",
    re.I,
)


def parse_log_line(raw_line: str) -> dict:
    line = unwrap_payload(raw_line)

    match = TIMESTAMP_PREFIX.match(line)
    if match:
        timestamp = match.group("time")
        # Normalise to HH:MM:SS so the timeline sorts and displays consistently.
        if len(timestamp.split(":")[0]) == 1:
            timestamp = "0" + timestamp
        rest = line[match.end():]
    else:
        timestamp = short_time(now_iso())
        rest = line

    # Strip logger scaffolding so the message is what a human actually wrote.
    rest = LOGGER_SCAFFOLD.sub("", rest, count=1)

    level_match = re.search(r"\b(INFO|WARN|WARNING|ERROR|DEBUG|TRACE|CRITICAL|FATAL|SUCCESS)\b", line)
    if level_match:
        found = level_match.group(1).upper()
        level = {"WARNING": "WARN", "CRITICAL": "ERROR", "FATAL": "ERROR", "SUCCESS": "INFO"}.get(found, found)
    else:
        level = infer_level(line)

    return {"raw_line": line, "timestamp": timestamp, "level": level, "message": rest.strip() or line.strip()}


COMPONENT_RULES = [
    (re.compile(r"\b(api|gateway|http|request|endpoint|server)\b", re.I), "API / Gateway"),
    (re.compile(r"\b(auth|authentication|authorize|login|token|jwt|oauth)\b", re.I), "Auth"),
    (re.compile(r"\b(parse|parser|chunk|extract|ingest|decode)\b", re.I), "Parser"),
    (re.compile(r"\b(retrieve|retriever|search|query|index)\b", re.I), "Retriever"),
    (re.compile(r"\b(graph|neo4j|relationship|node|edge)\b", re.I), "Graph DB"),
    (re.compile(r"\b(embedding|vector|embeddings)\b", re.I), "Embedding"),
    (re.compile(r"\b(llm|model|prompt|completion|generation)\b", re.I), "LLM"),
    (re.compile(r"\b(db|database|postgres|mysql|sql|redis|cache)\b", re.I), "Data Store"),
    (re.compile(r"\b(queue|kafka|rabbitmq|pubsub|stream)\b", re.I), "Queue"),
    (re.compile(r"\b(tool|mcp|function call|agent|workflow)\b", re.I), "Agent / Tools"),
    (re.compile(r"\b(response|reply|return|output|done|completed)\b", re.I), "Response"),
]


TRANSITIONS = [
    (re.compile(r"request received|incoming request|received request", re.I), {"stage": "request", "status": "running", "label": "Request received"}),
    (re.compile(r"auth|authentication|authorized|login", re.I), {"stage": "auth", "status": "completed", "label": "Authentication passed"}),
    (re.compile(r"parse|parsing|extract", re.I), {"stage": "parse", "status": "completed", "label": "Input parsed"}),
    (re.compile(r"retrieve|retriev", re.I), {"stage": "retrieve", "status": "running", "label": "Retrieval in progress"}),
    (re.compile(r"graph|neo4j|relationship|edge|node", re.I), {"stage": "graph", "status": "running", "label": "Graph operation"}),
    (re.compile(r"embedding|vector", re.I), {"stage": "embedding", "status": "running", "label": "Embedding generation"}),
    (re.compile(r"llm|model|completion|generation", re.I), {"stage": "llm", "status": "running", "label": "LLM call"}),
    (re.compile(r"response sent|done|completed|success", re.I), {"stage": "response", "status": "completed", "label": "Response sent"}),
    (re.compile(r"retry", re.I), {"stage": None, "status": "retrying", "label": "Retry attempted"}),
    (re.compile(r"warn|warning", re.I), {"stage": None, "status": "warning", "label": "Warning"}),
    (re.compile(r"\b(timed\s+out|timeout\s+(?:after|calling|waiting|while|exceeded)|(?:read|connect|connection|request|operation)\s+timeout)\b", re.I), {"stage": None, "status": "failed", "label": "Timeout"}),
    (re.compile(r"\b(connection\s+(?:refused|reset|aborted)|econnrefused|could not resolve|name or service not known|host unreachable|network unreachable)\b", re.I), {"stage": None, "status": "failed", "label": "Connection failure"}),
    (re.compile(r"\b(error|exception|traceback|failed|failure)\b", re.I), {"stage": None, "status": "failed", "label": "Failure"}),
]


# A cause is only a cause when the line describes something GOING WRONG. Matching a
# bare keyword anywhere flagged config values as failures - "timeout=45.0s" in a
# startup banner was reported as "Connection timed out" on a perfectly healthy boot.
CAUSES = [
    (re.compile(r"\b(timed\s+out|timeout\s+(?:after|calling|waiting|while|exceeded)|"
                r"(?:read|connect|connection|request|operation)\s+timeout)\b", re.I),
     "Connection timed out",
     ["Verify the endpoint is reachable", "Check service health", "Inspect network latency",
      "Confirm timeout settings"]),
    (re.compile(r"\bconnection\s+(?:refused|reset|aborted|closed)\b", re.I),
     "Target service refused the connection",
     ["Confirm the service is running", "Verify the port and host",
      "Check firewall or security group rules", "Inspect container/network bindings"]),
    (re.compile(r"\b(?:dns\s+(?:lookup|resolution)\s+failed|name or service not known|"
                r"could not resolve|nodename nor servname)\b", re.I),
     "DNS lookup failed",
     ["Verify the hostname", "Check DNS resolution", "Confirm service discovery configuration"]),
    (re.compile(r"\b(unauthorized|forbidden|auth\w* (?:failed|error|denied|rejected)|"
                r"invalid (?:token|credentials)|\b401\b|\b403\b)\b", re.I),
     "Authentication or authorization issue",
     ["Check credentials", "Verify API keys and tokens", "Confirm role and permission scopes"]),
    (re.compile(r"\b(rate limit(?:ed|s)? (?:exceeded|hit|reached)|too many requests|\b429\b)\b", re.I),
     "Rate limit exceeded",
     ["Reduce request volume", "Add backoff and retries", "Check provider quotas"]),
    (re.compile(r"\bpermission denied\b|\baccess denied\b", re.I),
     "Permission denied",
     ["Check file or service permissions", "Review container and OS access policies"]),
    (re.compile(r"\b(?:failed to (?:parse|decode)|(?:parse|decode|json|yaml|syntax) error|"
                r"malformed|invalid (?:json|yaml|payload|format))\b", re.I),
     "Input parsing issue",
     ["Validate the payload format", "Inspect the malformed record", "Add schema validation"]),
    # An upstream service answering 5xx is one of the most common real failures and
    # was not covered at all - the run showed "failed" with no cause and no fixes.
    (re.compile(r"\b(5\d{2}\s+(?:internal server error|bad gateway|service unavailable|gateway timeout)|"
                r"internal server error|bad gateway|service unavailable)\b", re.I),
     "Upstream service returned a server error",
     ["Check the upstream service's own logs for the failing request",
      "Confirm the endpoint and payload it expects",
      "Verify the service is healthy and not mid-deploy"]),
    (re.compile(r"\b(?:lookup|request|call|query|fetch)\s+failed\b", re.I),
     "A dependency call failed",
     ["Check the target service is reachable and healthy",
      "Inspect the error detail on the failing line",
      "Confirm the request parameters are what the service expects"]),
    (re.compile(r"\bfail(?:ing|ed)?\s+closed\b|\bfallback\s+(?:failed|exhausted)\b", re.I),
     "Request was refused because a safety check could not complete",
     ["Fix the underlying check that failed rather than the refusal itself",
      "Confirm whether the refusal is genuine or a side effect of the failure"]),
    (re.compile(r"\b(?:name|attribute)\s+'[^']+'\s+is not defined|NameError|AttributeError\b", re.I),
     "Code error - an undefined name or attribute was referenced",
     ["Fix the referenced name in the code path shown",
      "Check whether that branch is ever exercised in tests"]),
]


def infer_transition(message: str):
    for pattern, transition in TRANSITIONS:
        if pattern.search(message):
            return transition
    return None


def infer_component(message: str) -> str:
    for pattern, name in COMPONENT_RULES:
        if pattern.search(message):
            return name
    return "Other"


def detect_branch(message: str) -> dict | None:
    lowered = message.lower()
    if re.search(r"\bretry|retrying\b", lowered):
        return {"kind": "retry", "label": "Retry path"}
    if re.search(r"\bfallback|fallback to|fall back\b", lowered):
        return {"kind": "fallback", "label": "Fallback path"}
    if re.search(r"\balternate|alternative|secondary|other path\b", lowered):
        return {"kind": "alternate", "label": "Alternate path"}
    if re.search(r"\bskip|skipping|bypass|short[- ]circuit\b", lowered):
        return {"kind": "skipped", "label": "Skipped path"}
    if re.search(r"\brecover|recovery|resume\b", lowered):
        return {"kind": "recovery", "label": "Recovery path"}
    return None


def infer_skipped_component(message: str) -> str | None:
    lowered = message.lower()
    if not re.search(r"\bskip|skipping|bypass|short[- ]circuit\b", lowered):
        return None
    for pattern, name in COMPONENT_RULES:
        if pattern.search(message):
            return name
    return None


def infer_cause(message: str):
    for pattern, cause, fixes in CAUSES:
        if pattern.search(message):
            return {"cause": cause, "fixes": fixes}
    return None


def pick_additional_causes(message: str) -> list[str]:
    hints = []
    if re.search(r"timeout", message, re.I):
        hints.append("Service timeout or slow upstream dependency")
    if re.search(r"connection refused", message, re.I):
        hints.append("Target service may be offline or misconfigured")
    if re.search(r"dns", message, re.I):
        hints.append("Hostname may not resolve from the running environment")
    if re.search(r"retry", message, re.I):
        hints.append("Repeated retries indicate a persistent upstream problem")
    return list(dict.fromkeys(hints))

"""Section 9 - the model router: no model hard-coded in any component.

Components ask for a TASK ("explain_incident", "answer_question"); the router
decides which provider and model serve it. Routing lives here and in the
environment, so switching providers is configuration, never a code change -
the doc's anti-pattern table calls hard-coded models out by name.

Provider economics, per the user's instruction: Groq's free-tier models are
the default for every task, so the paid OpenAI key is spent only when a task
is explicitly routed to it (or a comparison is requested). Both speak the
same chat-completions wire format, which keeps this file small.

Degrade, never block (P8): no key, no network, budget exhausted - the answer
is None and the caller carries on. Detection never depended on this layer.
"""

from __future__ import annotations

import re
import time

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable

from aegis.l7_reasoning.governance import AuditLog, Budget

PROVIDERS = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "default_model": "openai/gpt-oss-120b",
    },
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "key_env": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
    },
}

# Task -> (provider, model). Overridable per task from the environment:
#   AEGIS_MODEL_EXPLAIN_INCIDENT="openai:gpt-4o-mini"
# Model ids the Groq free tier actually serves. llama-3.3-70b-versatile was
# retired from it and now 404s as "does not exist or you do not have access",
# which reads like a permissions problem rather than a stale id.
ROUTES = {
    "explain_incident": ("groq", "openai/gpt-oss-120b"),
    "answer_question": ("groq", "openai/gpt-oss-120b"),
}


# Whole-profile switch, so moving off free models is one variable rather than
# one per task. AEGIS_PROFILE=dev keeps everything on Groq's free tier (the
# default); AEGIS_PROFILE=prod sends every task to the paid provider. A
# per-task AEGIS_MODEL_<TASK> still wins over both - it is the finest-grained
# choice, and the finest-grained choice should never be overruled by a coarse
# one.
PROFILES = {
    "dev": {"provider": "groq", "model": "openai/gpt-oss-120b"},
    "prod": {"provider": "openai", "model": "gpt-4o-mini"},
}


def active_profile() -> str:
    name = os.environ.get("AEGIS_PROFILE", "dev").strip().lower()
    return name if name in PROFILES else "dev"


def _route_for(task: str) -> tuple[str, str]:
    override = os.environ.get(f"AEGIS_MODEL_{task.upper()}")
    if override and ":" in override:
        provider, model = override.split(":", 1)
        if provider in PROVIDERS:
            return provider, model
    profile = PROFILES[active_profile()]
    if profile["provider"] != "groq":
        return profile["provider"], profile["model"]
    if task in ROUTES:
        return ROUTES[task]
    return "groq", PROVIDERS["groq"]["default_model"]


def _http_transport(url: str, headers: dict[str, str], body: dict[str, Any],
                    timeout: float) -> dict[str, Any]:
    # Groq sits behind Cloudflare, which refuses urllib's default
    # "Python-urllib/3.x" User-Agent with a bare "error code: 1010" - a 403
    # that looks exactly like a bad key and is not one. Identify ourselves.
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent": "aegis/1.0 (+log-intelligence)", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _retry_after(exc: Exception, body: str, default: float) -> float:
    """How long the provider asked us to wait, in seconds.

    Groq returns Retry-After, and states the wait in the body besides
    ("Please try again in 41.9s"). Guessing a fixed delay under-waited and
    burned the one retry for nothing. Capped so a bad header cannot park an
    interactive click for minutes.
    """
    headers = getattr(exc, "headers", None)
    raw = ""
    if headers is not None:
        try:
            raw = headers.get("retry-after") or ""
        except Exception:
            raw = ""
    if raw:
        try:
            return max(1.0, min(90.0, float(raw)))
        except ValueError:
            pass
    match = re.search(r"try again in ([\d.]+)\s*s", body or "", re.I)
    if match:
        try:
            return max(1.0, min(90.0, float(match.group(1)) + 1.0))
        except ValueError:
            pass
    return max(1.0, min(90.0, default))


class ModelRouter:
    def __init__(self, budget: Budget | None = None, audit: AuditLog | None = None,
                 transport: Callable[..., dict[str, Any]] | None = None,
                 timeout_s: float = 45.0) -> None:
        self.budget = budget or Budget()
        self.audit = audit or AuditLog()
        self._transport = transport or _http_transport
        self.timeout_s = timeout_s

    def available(self, task: str = "explain_incident") -> bool:
        provider, _ = _route_for(task)
        return bool(os.environ.get(PROVIDERS[provider]["key_env"]))

    def chat(self, task: str, messages: list[dict[str, str]],
             purpose: str = "", provider: str | None = None,
             model: str | None = None) -> str | None:
        """One governed model call. None means unavailable or refused - the
        caller must have a path that lives without an answer."""
        routed_provider, routed_model = _route_for(task)
        provider = provider or routed_provider
        model = model or routed_model
        spec = PROVIDERS.get(provider)
        if spec is None:
            return None
        key = os.environ.get(spec["key_env"], "")

        decision = self.budget.request() if key else None
        # Audited BEFORE the attempt: a crash mid-call still leaves a record.
        self.audit.record(
            task=task, provider=provider, model=model, purpose=purpose,
            allowed=bool(key and decision and decision.allowed),
            reason="no api key" if not key else (decision.reason if decision else ""),
        )
        if not key or not decision or not decision.allowed:
            return None

        # Free tiers meter tokens per minute, and a multi-step investigation
        # will hit that ceiling mid-loop. One patient retry turns a hard
        # failure into a pause, which is what the user actually wants.
        for attempt in range(3):
            try:
                response = self._transport(
                    spec["url"], {"Authorization": f"Bearer {key}"},
                    {"model": model, "messages": messages, "temperature": 0.2},
                    self.timeout_s,
                )
                return response["choices"][0]["message"]["content"]
            except Exception as exc:
                body = ""
                reader = getattr(exc, "read", None)
                if reader is not None:
                    try:
                        body = reader().decode("utf-8", "replace")
                    except Exception:
                        body = ""
                # Match the STATUS, not the prose. Groq answers a rate
                # limit with "HTTP Error 429: Too Many Requests" and the
                # words "rate limit" appear nowhere in it - and the body has
                # already been consumed by the read above, so a second look
                # finds an empty string. The retry never fired, and two
                # governed calls in quick succession killed the request
                # instead of waiting.
                status = getattr(exc, "code", None)
                limited = status == 429 or "rate limit" in body.lower() \
                    or "too many requests" in str(exc).lower()
                if attempt < 2 and limited:
                    # The provider says how long to wait; a fixed 20s guess
                    # was often short of it, so the retry failed and the
                    # raw "HTTP Error 429" reached the user. Free tiers meter
                    # TOKENS per minute, and one Propose-fix call carrying
                    # code excerpts can exhaust a minute's budget by itself.
                    wait = _retry_after(exc, body, default=20.0 * (attempt + 1))
                    self.audit.record(
                        task=task, provider=provider, model=model,
                        purpose=purpose, allowed=True,
                        reason=f"rate limited - waiting {wait:.0f}s "
                               f"(attempt {attempt + 1} of 2)")
                    time.sleep(wait)
                    continue
                raise
        try:
            return None
        except (urllib.error.URLError, KeyError, IndexError, TypeError,
                ValueError, OSError) as exc:
            # Record WHAT the provider said, not just the exception class.
            # "call failed: HTTPError" gave no way to tell a rate limit from a
            # too-large context from a dead key.
            detail = f"{exc.__class__.__name__}"
            body = getattr(exc, "read", None)
            if body is not None:
                try:
                    detail += ": " + body().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
            self.audit.record(task=task, provider=provider, model=model,
                              purpose=purpose, allowed=True,
                              reason=f"call failed: {detail}")
            return None

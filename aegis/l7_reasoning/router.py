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
# Model names verified against GET /models on 2026-09-06 - the first pick,
# llama-3.3-70b-versatile, had already been retired, which is exactly why
# routing is configuration: this table changes, components never do.
ROUTES = {
    "explain_incident": ("groq", "openai/gpt-oss-120b"),
    "answer_question": ("groq", "openai/gpt-oss-120b"),
}


def _route_for(task: str) -> tuple[str, str]:
    override = os.environ.get(f"AEGIS_MODEL_{task.upper()}")
    if override and ":" in override:
        provider, model = override.split(":", 1)
        if provider in PROVIDERS:
            return provider, model
    if task in ROUTES:
        return ROUTES[task]
    return "groq", PROVIDERS["groq"]["default_model"]


def _http_transport(url: str, headers: dict[str, str], body: dict[str, Any],
                    timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        # Groq's Cloudflare edge rejects urllib's default agent string with a
        # 403 (error 1010) before the request reaches the API at all.
        headers={"Content-Type": "application/json",
                 "User-Agent": "aegis-log-agent/0.1", **headers}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


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

        try:
            response = self._transport(
                spec["url"], {"Authorization": f"Bearer {key}"},
                {"model": model, "messages": messages, "temperature": 0.2},
                self.timeout_s,
            )
            return response["choices"][0]["message"]["content"]
        except (urllib.error.URLError, KeyError, IndexError, TypeError,
                ValueError, OSError) as exc:
            self.audit.record(task=task, provider=provider, model=model,
                              purpose=purpose, allowed=True,
                              reason=f"call failed: {exc.__class__.__name__}")
            return None

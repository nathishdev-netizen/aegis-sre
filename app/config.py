"""Single source of truth for configuration.

Every tunable lives here rather than being read from os.environ at the point of use,
so settings can be seen, defaulted and validated in one place.

Precedence: real environment variables win over .env, so `OPENAI_API_KEY=... python3
run.py` overrides the file for a one-off run without editing it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


def load_env(path: Path = ENV_FILE) -> None:
    """Load .env into the process environment without clobbering real env vars."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_env_fallback(path)
        return
    load_dotenv(path, override=False)


def _load_env_fallback(path: Path) -> None:
    """Minimal KEY=VALUE parser so the app runs without python-dotenv installed."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Server
    host: str = "127.0.0.1"
    port: int = 3000

    # LLM interpretation - talks to any OpenAI-compatible chat completions API.
    # Default (base_url=None) is real OpenAI; pointing base_url at another
    # provider (Groq, etc.) and matching model/key is the whole swap - no code
    # change, since the openai SDK accepts any compatible endpoint.
    openai_api_key: str | None = None
    model: str = "gpt-4o-mini"
    llm_base_url: str | None = None
    llm_enabled: bool = True

    # Ingestion / interpretation limits
    max_log_lines: int = 200
    max_timeline: int = 60
    llm_log_window: int = 60
    llm_timeline_window: int = 30

    # Source attachment
    auto_attach: bool = False
    probe_paths: tuple[str, ...] = field(
        default_factory=lambda: (
            "/events", "/stream", "/logs/stream",
            "/api/events", "/api/logs", "/logs", "/debug/logs",
        )
    )

    @property
    def llm_available(self) -> bool:
        """True only when a model call could actually be made."""
        return bool(self.llm_enabled and self.openai_api_key)


def _llm_base_url() -> str | None:
    """Where the OpenAI-compatible calls go.

    Explicit setting wins. Otherwise, if the only key present is Groq's, point
    at Groq - a Groq key against api.openai.com fails on every call.
    """
    explicit = os.environ.get("LOG_AGENT_LLM_BASE_URL")
    if explicit:
        return explicit
    if (os.environ.get("GROQ_API_KEY")
            and not os.environ.get("LOG_AGENT_LLM_API_KEY")
            and not os.environ.get("OPENAI_API_KEY")):
        return "https://api.groq.com/openai/v1"
    return None


def _llm_model() -> str:
    """The model name, which has to match the provider the key belongs to."""
    explicit = os.environ.get("LOG_AGENT_MODEL")
    if explicit:
        return explicit
    if _llm_base_url() == "https://api.groq.com/openai/v1":
        return "openai/gpt-oss-120b"
    return "gpt-4o-mini"


def load_settings() -> Settings:
    load_env()
    raw_paths = os.environ.get("LOG_AGENT_PROBE_PATHS", "").strip()
    probe_paths = tuple(p.strip() for p in raw_paths.split(",") if p.strip()) or Settings().probe_paths

    return Settings(
        host=os.environ.get("LOG_AGENT_HOST", "127.0.0.1"),
        port=_get_int("LOG_AGENT_PORT", 3000),
        # LOG_AGENT_LLM_API_KEY takes precedence so a temporary alternate
        # provider (e.g. Groq) never requires touching OPENAI_API_KEY, which
        # stays untouched and ready the moment the real key is restored.
        # ...and GROQ_API_KEY after those, because the documented default is
        # Groq's free tier. Without this, somebody who set only GROQ_API_KEY -
        # which the README says is enough - got a working Aegis and a v1 ask
        # box that silently fell back to keyword search and told them to set
        # OPENAI_API_KEY instead.
        openai_api_key=os.environ.get("LOG_AGENT_LLM_API_KEY")
                       or os.environ.get("OPENAI_API_KEY")
                       or os.environ.get("GROQ_API_KEY") or None,
        model=_llm_model(),
        llm_base_url=_llm_base_url(),
        llm_enabled=_get_bool("LOG_AGENT_LLM", True),
        max_log_lines=_get_int("LOG_AGENT_MAX_LOG_LINES", 200),
        max_timeline=_get_int("LOG_AGENT_MAX_TIMELINE", 60),
        llm_log_window=_get_int("LOG_AGENT_LLM_LOG_WINDOW", 60),
        llm_timeline_window=_get_int("LOG_AGENT_LLM_TIMELINE_WINDOW", 30),
        auto_attach=_get_bool("LOG_AGENT_AUTO_ATTACH", False),
        probe_paths=probe_paths,
    )


settings = load_settings()

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

    # LLM interpretation
    openai_api_key: str | None = None
    model: str = "gpt-4o-mini"
    llm_enabled: bool = True

    # Ingestion / interpretation limits
    max_log_lines: int = 200
    max_timeline: int = 60
    llm_log_window: int = 60
    llm_timeline_window: int = 30

    # Source attachment
    auto_attach: bool = True
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


def load_settings() -> Settings:
    load_env()
    raw_paths = os.environ.get("LOG_AGENT_PROBE_PATHS", "").strip()
    probe_paths = tuple(p.strip() for p in raw_paths.split(",") if p.strip()) or Settings().probe_paths

    return Settings(
        host=os.environ.get("LOG_AGENT_HOST", "127.0.0.1"),
        port=_get_int("LOG_AGENT_PORT", 3000),
        openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
        model=os.environ.get("LOG_AGENT_MODEL", "gpt-4o-mini"),
        llm_enabled=_get_bool("LOG_AGENT_LLM", True),
        max_log_lines=_get_int("LOG_AGENT_MAX_LOG_LINES", 200),
        max_timeline=_get_int("LOG_AGENT_MAX_TIMELINE", 60),
        llm_log_window=_get_int("LOG_AGENT_LLM_LOG_WINDOW", 60),
        llm_timeline_window=_get_int("LOG_AGENT_LLM_TIMELINE_WINDOW", 30),
        auto_attach=_get_bool("LOG_AGENT_AUTO_ATTACH", True),
        probe_paths=probe_paths,
    )


settings = load_settings()

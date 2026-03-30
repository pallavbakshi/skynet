"""Shared provider-environment helpers for terminal-backed adapters."""

from __future__ import annotations

import os
from pathlib import Path

from agp.config import settings

PROVIDER_ENV_VARS = (
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
    "AGP_SERVER_URL",
    "DISABLE_AUTOUPDATER",
    "DISABLE_TELEMETRY",
    "NO_UPDATE_NOTIFIER",
)


def ensure_codex_config(base_url: str) -> None:
    """Best-effort: set ``openai_base_url`` in ``~/.codex/config.toml``."""
    try:
        import tomllib
    except ModuleNotFoundError:
        return
    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(f'openai_base_url = "{base_url}"\n')
        return
    try:
        existing = tomllib.loads(config_path.read_text())
    except Exception:
        return
    if existing.get("openai_base_url") == base_url:
        return


def collect_provider_env() -> dict[str, str]:
    """Collect provider API keys and endpoint overrides for runtime-launched CLIs."""
    env: dict[str, str] = {}

    openai_key = os.environ.get("OPENAI_API_KEY")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    openai_base_url = os.environ.get("OPENAI_BASE_URL")
    if openai_key:
        env["OPENAI_API_KEY"] = openai_key
    if openai_base_url:
        ensure_codex_config(openai_base_url)
        env["OPENAI_BASE_URL"] = openai_base_url
    if openrouter_key and " -p " in f" {settings.codex_cli_command} ":
        env["OPENROUTER_API_KEY"] = openrouter_key

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key is not None:
        env["ANTHROPIC_API_KEY"] = anthropic_key

    for key in PROVIDER_ENV_VARS[4:]:
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env

"""Connection profile for AGP client SDK."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

_PROFILES_DIR = Path.home() / ".agp" / "profiles"


@dataclass
class AgpProfile:
    """Connection context for talking to an AGP control plane.

    Resolution order:
    1. Explicit constructor args (programmatic use)
    2. AGP_SERVER_URL + AGP_OPERATOR_TOKEN env vars (container/CI use)
    3. ~/.agp/profiles/{name}.toml file (operator workstation use)
    4. Fallback: http://127.0.0.1:7860, no token (local dev)
    """

    server_url: str = "http://127.0.0.1:7860"
    token: str | None = None
    name: str = "default"

    @classmethod
    def load(cls, name: str = "default") -> AgpProfile:
        """Load a profile by name, checking env vars then profile files."""
        env_url = os.environ.get("AGP_SERVER_URL")
        env_token = os.environ.get("AGP_OPERATOR_TOKEN")
        if env_url:
            return cls(server_url=env_url, token=env_token, name=name)

        profile_path = _PROFILES_DIR / f"{name}.toml"
        if profile_path.exists():
            with open(profile_path, "rb") as f:
                data = tomllib.load(f)
            return cls(
                server_url=data.get("server_url", "http://127.0.0.1:7860"),
                token=data.get("token"),
                name=name,
            )

        return cls(name=name)

    @classmethod
    def from_env(cls) -> AgpProfile:
        """Build a profile purely from environment variables."""
        return cls(
            server_url=os.environ.get("AGP_SERVER_URL", "http://127.0.0.1:7860"),
            token=os.environ.get("AGP_OPERATOR_TOKEN"),
            name="env",
        )

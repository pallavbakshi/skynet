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


def _url_from_host_port() -> str | None:
    """Build a server URL from AGP_HOST/AGP_PORT if set (deploy-script compat)."""
    host = os.environ.get("AGP_HOST")
    port = os.environ.get("AGP_PORT")
    if host is not None or port is not None:
        h = host or "127.0.0.1"
        # Map 0.0.0.0 (listen-on-all) to 127.0.0.1 for client connections
        if h == "0.0.0.0":
            h = "127.0.0.1"
        p = port or "7860"
        return f"http://{h}:{p}"
    return None


@dataclass
class AgpProfile:
    """Connection context for talking to an AGP control plane.

    Resolution order:
    1. Explicit constructor args (programmatic use)
    2. AGP_SERVER_URL env var (container/CI use)
    3. AGP_HOST + AGP_PORT env vars (deploy-script compat)
    4. AGP_OPERATOR_TOKEN env var (independent of URL source)
    5. ~/.agp/profiles/{name}.toml file (operator workstation use)
    6. Fallback: http://127.0.0.1:7860, no token (local dev)
    """

    server_url: str = "http://127.0.0.1:7860"
    token: str | None = None
    name: str = "default"

    @classmethod
    def load(cls, name: str = "default") -> AgpProfile:
        """Load a profile by name, checking env vars then profile files.

        Env vars are checked first and override independently:
        AGP_SERVER_URL (or AGP_HOST/AGP_PORT) overrides the server URL,
        AGP_OPERATOR_TOKEN overrides the token.  Either or both can be set.
        """
        env_url = os.environ.get("AGP_SERVER_URL") or _url_from_host_port()
        env_token = os.environ.get("AGP_OPERATOR_TOKEN")

        # If any env var is set (even empty string), start from env.
        # Use `is not None` so that AGP_OPERATOR_TOKEN="" is honored
        # as "no token" rather than falling through to the profile file.
        if env_url is not None or env_token is not None:
            # Load profile as baseline for any missing values
            base_url = "http://127.0.0.1:7860"
            base_token: str | None = None
            profile_path = _PROFILES_DIR / f"{name}.toml"
            if profile_path.exists():
                with open(profile_path, "rb") as f:
                    data = tomllib.load(f)
                base_url = data.get("server_url", base_url)
                base_token = data.get("token")
            return cls(
                server_url=env_url if env_url is not None else base_url,
                token=env_token if env_token is not None else base_token,
                name=name,
            )

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
            server_url=os.environ.get("AGP_SERVER_URL") or _url_from_host_port() or "http://127.0.0.1:7860",
            token=os.environ.get("AGP_OPERATOR_TOKEN"),
            name="env",
        )

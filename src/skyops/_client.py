"""Shared AgpClient factory for skyops commands."""

from __future__ import annotations

from agp.client import AgpClient, AgpProfile

from skyops.config import SkyopsConfig, load_config


def build_profile(cfg: SkyopsConfig | None = None) -> AgpProfile:
    """Build an AgpProfile from skyops config, respecting the configured host.

    Falls back to AgpProfile.load() resolution if no skyops config is available.
    """
    if cfg is None:
        try:
            cfg = load_config()
        except FileNotFoundError:
            return AgpProfile.load()

    host = cfg.server.host
    port = cfg.server.port
    # Use the configured host, not hardcoded localhost
    server_url = f"http://{host}:{port}"
    # But for 0.0.0.0 (listen-on-all), connect to localhost
    if host == "0.0.0.0":
        server_url = f"http://127.0.0.1:{port}"

    return AgpProfile(
        server_url=server_url,
        token=cfg.security.operator_token or None,
    )


def build_client(cfg: SkyopsConfig | None = None) -> AgpClient:
    """Build an AgpClient from skyops config."""
    return AgpClient(profile=build_profile(cfg))

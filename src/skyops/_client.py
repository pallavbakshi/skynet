"""Shared AgpClient factory for skyops commands."""

from __future__ import annotations

from agp.client import AgpClient, AgpProfile

from skyops.config import SkyopsConfig, load_config


def _connectable_host(host: str) -> str:
    """Map 0.0.0.0 (listen-on-all) to 127.0.0.1 for client connections."""
    return "127.0.0.1" if host == "0.0.0.0" else host


def resolve_server_url(cfg: SkyopsConfig) -> str:
    """Return the HTTP URL to reach the control plane from this host."""
    return f"http://{_connectable_host(cfg.server.host)}:{cfg.server.port}"


def resolve_host_for_url(url: str) -> str:
    """Extract the host from a URL, mapping 0.0.0.0 → 127.0.0.1."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    return _connectable_host(host)


def build_profile(cfg: SkyopsConfig | None = None) -> AgpProfile:
    """Build an AgpProfile from skyops config, respecting the configured host.

    Falls back to AgpProfile.load() resolution if no skyops config is available.
    """
    if cfg is None:
        try:
            cfg = load_config()
        except FileNotFoundError:
            return AgpProfile.load()

    server_url = resolve_server_url(cfg)

    return AgpProfile(
        server_url=server_url,
        token=cfg.security.operator_token or None,
    )


def build_client(cfg: SkyopsConfig | None = None) -> AgpClient:
    """Build an AgpClient from skyops config."""
    return AgpClient(profile=build_profile(cfg))

"""``skyops secrets`` — security and credential management."""

from __future__ import annotations

import json
import secrets

import typer

from skyops.config import load_config

security_app = typer.Typer(help="Secret and credential management.")


def _client():
    from agp.client import AgpClient, AgpProfile

    cfg = load_config()
    profile = AgpProfile(
        server_url=f"http://127.0.0.1:{cfg.server.port}",
        token=cfg.security.operator_token or None,
    )
    return AgpClient(profile=profile)


def _emit(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


@security_app.command("show")
def secrets_show() -> None:
    """Show which secrets are configured (values masked)."""
    cfg = load_config()
    display = cfg.to_display_dict(mask_secrets=True)
    typer.echo("Configured secrets:")
    for key in ("operator_token", "runtime_token"):
        val = display.get("security", {}).get(key, "")
        status = "set" if val and val != '""' else "not set"
        typer.echo(f"  {key}: {status}")
    for key in ("access_key_id", "secret_access_key"):
        val = display.get("s3", {}).get(key, "")
        status = "set" if val and val != '""' else "not set"
        typer.echo(f"  s3.{key}: {status}")

    # Check auth status via API if control plane is reachable
    try:
        with _client() as client:
            auth = client.auth_status()
        typer.echo(f"\nAuth status: {json.dumps(auth, indent=2, default=str)}")
    except Exception:
        typer.echo("\nControl plane not reachable — skipping auth status check.")


@security_app.command("generate")
def secrets_generate() -> None:
    """Generate fresh random credentials and write to skyops.local.toml."""
    from skyops.config import find_config
    from skyops._config_cmd import _write_toml

    config_path = find_config()
    if config_path is None:
        typer.echo("skyops.toml not found. Run `skyops init` first.", err=True)
        raise typer.Exit(1)

    local_path = config_path.parent / "skyops.local.toml"
    operator_token = secrets.token_urlsafe(32)
    runtime_token = secrets.token_urlsafe(32)
    s3_access_key = secrets.token_urlsafe(16)
    s3_secret_key = secrets.token_urlsafe(32)

    import sys
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore[no-redef]

    existing: dict = {}
    if local_path.is_file():
        with open(local_path, "rb") as f:
            existing = tomllib.load(f)

    existing.setdefault("security", {})
    existing["security"]["operator_token"] = operator_token
    existing["security"]["runtime_token"] = runtime_token
    existing.setdefault("s3", {})
    existing["s3"]["access_key_id"] = s3_access_key
    existing["s3"]["secret_access_key"] = s3_secret_key

    _write_toml(local_path, existing)
    typer.echo(f"Generated fresh credentials in {local_path}")
    typer.echo("  operator_token, runtime_token, s3.access_key_id, s3.secret_access_key")


@security_app.command("generate-k8s")
def secrets_generate_k8s(
    path: str = typer.Argument("/tmp/agp-k8s-secret.dev.yaml", help="Output path for k8s Secret YAML."),
) -> None:
    """Generate a Kubernetes Secret manifest with current credentials."""
    cfg = load_config()
    from pathlib import Path

    db_url = cfg.database.url
    s3_access_key = cfg.s3.access_key_id
    s3_secret_key = cfg.s3.secret_access_key
    operator_token = cfg.security.operator_token
    runtime_token = cfg.security.runtime_token

    yaml_content = f"""\
apiVersion: v1
kind: Secret
metadata:
  name: agp-secrets
  namespace: agp
type: Opaque
stringData:
  AGP_DATABASE_URL: "{db_url}"
  AGP_S3_ACCESS_KEY_ID: "{s3_access_key}"
  AGP_S3_SECRET_ACCESS_KEY: "{s3_secret_key}"
  AGP_OPERATOR_TOKEN_ROLES_JSON: '{{}}'
  AGP_RUNTIME_ACTIVE_TOKENS_JSON: '[]'
  AGP_OBSERVABILITY_ALERT_WEBHOOK_URL: ""
"""
    Path(path).write_text(yaml_content)
    typer.echo(f"Kubernetes Secret written to {path}")


@security_app.command("rotate-operator")
def rotate_operator() -> None:
    """Rotate operator tokens via the control plane API."""
    cfg = load_config()
    with _client() as client:
        result = client.rotate_operator_tokens(
            operator_bearer_token=cfg.security.operator_token,
            operator_token_roles_json={},
        )
    _emit(result)


@security_app.command("rotate-runtime")
def rotate_runtime() -> None:
    """Rotate runtime tokens via the control plane API."""
    cfg = load_config()
    with _client() as client:
        result = client.rotate_runtime_tokens(
            runtime_bearer_token=cfg.security.runtime_token,
            runtime_active_tokens_json=[],
        )
    _emit(result)

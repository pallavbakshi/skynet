"""Run a complete AGP orchestrator → control-plane → runtime → Codex-on-WezTerm flow.

One script, one command.  Discovers WezTerm socket and codex binary
automatically, starts the control plane, bootstraps state, dispatches a job,
runs the runtime worker, and prints the result.

Usage:
    uv run python scripts/run_local_codex.py "What is 2 + 2?"
    uv run python scripts/run_local_codex.py --socket /path/to/gui-sock-XXXX "explain this code"
    uv run python scripts/run_local_codex.py --codex /usr/local/bin/codex "refactor main.py"
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from agp.client import AgpClient, AgpProfile

# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

WEZTERM_SOCK_DIR = Path.home() / ".local" / "share" / "wezterm"


def discover_wezterm_socket(explicit: str | None = None) -> str:
    """Return the newest gui-sock-* path, or *explicit* if provided."""
    if explicit:
        p = Path(explicit)
        if not p.exists():
            _die(f"socket not found: {explicit}")
        return str(p)
    if not WEZTERM_SOCK_DIR.is_dir():
        _die(f"WezTerm socket directory not found: {WEZTERM_SOCK_DIR}")
    socks = sorted(WEZTERM_SOCK_DIR.glob("gui-sock-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not socks:
        _die("no WezTerm gui-sock-* sockets found — is WezTerm running?")
    return str(socks[0])


def discover_codex(explicit: str | None = None) -> str:
    """Find the codex binary."""
    if explicit:
        if not Path(explicit.split()[0]).exists():
            _die(f"codex not found: {explicit}")
        return explicit
    for candidate in [
        shutil.which("codex"),
        str(Path.home() / ".local" / "bin" / "codex"),
        "/usr/local/bin/codex",
        "/opt/homebrew/bin/codex",
    ]:
        if candidate and Path(candidate).exists():
            return candidate
    _die("codex not found in PATH or common locations")


def _die(msg: str) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Stack lifecycle
# ---------------------------------------------------------------------------

SERVER_URL = "http://127.0.0.1:7860"
CAPABILITY_ID = "cap_codex"
AGENT_ID = "agt_codex"
RUNTIME_ID = "rtm_local"


def wait_for_health(timeout: float = 30.0) -> None:
    profile = AgpProfile(server_url=SERVER_URL)
    with AgpClient(profile=profile) as client:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                client.health()
                return
            except Exception:
                pass
            time.sleep(0.5)
    _die("control plane did not become healthy")


def init_and_serve() -> subprocess.Popen:
    """Init DB and start the control plane in a subprocess."""
    subprocess.run([sys.executable, "-m", "agp", "initdb"], check=True,
                   capture_output=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "agp", "serve"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    wait_for_health()
    return proc


def bootstrap() -> None:
    """Seed capability and agent via SDK + direct DB."""
    from agp.db import SessionLocal
    from agp.models import Capability, CapabilityPool, utc_now

    session = SessionLocal()
    try:
        if session.get(Capability, CAPABILITY_ID) is None:
            now = utc_now()
            session.add(Capability(
                capability_id=CAPABILITY_ID, name="codex", version="v1",
                image_ref="local/agp:dev", model_ref="local",
                resource_tier="small", permission_profile="default",
                queue_mode="agent", runtime_requirements_json={},
                created_at=now, updated_at=now,
            ))
            session.flush()
            if session.get(CapabilityPool, CAPABILITY_ID) is None:
                session.add(CapabilityPool(
                    capability_id=CAPABILITY_ID,
                    queue_id=f"capability:{CAPABILITY_ID}:v1",
                    routing_policy="least_recent",
                ))
            session.commit()
    finally:
        session.close()

    profile = AgpProfile(server_url=SERVER_URL)
    with AgpClient(profile=profile) as client:
        client.register_agent(AGENT_ID, CAPABILITY_ID)


def send_job(prompt: str) -> str:
    profile = AgpProfile(server_url=SERVER_URL)
    with AgpClient(profile=profile) as client:
        result = client.send(
            "agent", AGENT_ID, prompt,
            idempotency_key=f"local-{int(time.time())}",
        )
        return result["job_id"]


def run_worker(socket: str, codex_cmd: str, timeout: int) -> dict:
    env = {
        **os.environ,
        "WEZTERM_UNIX_SOCKET": socket,
        "AGP_RUNTIME_TERMINAL_HOST_KIND": "wezterm",
        "AGP_RUNTIME_AGENT_ADAPTER_KIND": "codex",
        "AGP_CODEX_TUI_MODE": "true",
        "AGP_CODEX_CLI_COMMAND": f"{codex_cmd} --full-auto",
        "AGP_WEZTERM_DEFAULT_CWD": os.getcwd(),
        "AGP_CODEX_IDLE_POLL_SECONDS": "2.0",
        "AGP_CODEX_IDLE_AFTER": "4",
        "AGP_CODEX_IDLE_TIMEOUT_SECONDS": str(timeout),
        "AGP_CODEX_BOOTSTRAP_SETTLE_SECONDS": "3.0",
        "AGP_WEZTERM_SCROLLBACK_LINES": "5000",
    }
    result = subprocess.run(
        [sys.executable, "-m", "agp", "runtime-work-loop",
         RUNTIME_ID, "--agent-id", AGENT_ID, "--artifact-root", ".agp-artifacts",
         "--max-iterations", "1"],
        capture_output=True, text=True, env=env, timeout=timeout + 60,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        _die(f"runtime worker exited {result.returncode}")
    import ast
    return ast.literal_eval(result.stdout.strip())


def fetch_result(job_id: str) -> str | None:
    profile = AgpProfile(server_url=SERVER_URL)
    with AgpClient(profile=profile) as client:
        job = client.get_job(job_id)
        art_id = job.get("result_artifact_id")
        if not art_id:
            return None
        artifact = client.fetch_artifact(art_id, content=True)
        return artifact.get("content", "")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Run AGP end-to-end with Codex on WezTerm")
    parser.add_argument("prompt", help="Task prompt to send to Codex")
    parser.add_argument("--socket", help="WezTerm unix socket path (auto-discovered if omitted)")
    parser.add_argument("--codex", help="Path to codex binary (auto-discovered if omitted)")
    parser.add_argument("--timeout", type=int, default=180, help="Idle timeout in seconds (default: 180)")
    parser.add_argument("--keep", action="store_true", help="Keep the control plane running after completion")
    args = parser.parse_args()

    socket = discover_wezterm_socket(args.socket)
    codex = discover_codex(args.codex)

    print(f"WezTerm socket : {socket}")
    print(f"codex binary  : {codex}")
    print(f"Prompt         : {args.prompt}")
    print()

    # Clean previous state
    for p in [Path("agp.db"), Path(".agp-artifacts"), Path(".agp-logs"), Path(".agp-checkpoints")]:
        if p.is_file():
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p)

    server = init_and_serve()
    try:
        print("[1/5] Control plane started")
        bootstrap()
        print("[2/5] Capability + agent provisioned")

        job_id = send_job(args.prompt)
        print(f"[3/5] Job queued: {job_id}")

        print("[4/5] Runtime worker starting (this may take a while)...")
        outcome = run_worker(socket, codex, args.timeout)

        if outcome.get("error"):
            print(f"\nFAILED: {outcome['error']}")
            return 1

        result_text = fetch_result(job_id)
        print(f"[5/5] Job completed!")
        print()
        print("=" * 60)
        print("RESULT:")
        print("=" * 60)
        print(result_text or "(no result artifact)")
        print("=" * 60)
        return 0
    finally:
        if not args.keep:
            server.send_signal(signal.SIGTERM)
            server.wait(timeout=5)
        else:
            print(f"\nControl plane still running (PID {server.pid})")


if __name__ == "__main__":
    raise SystemExit(main())

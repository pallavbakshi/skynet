"""Run a complete AGP orchestrator → control-plane → runtime → Codex-on-WezTerm flow.

One script, one command.  Discovers WezTerm socket and ncodex binary
automatically, starts the control plane, bootstraps state, dispatches a job,
runs the runtime worker, and prints the result.

Usage:
    uv run python scripts/run_local_codex.py "What is 2 + 2?"
    uv run python scripts/run_local_codex.py --socket /path/to/gui-sock-XXXX "explain this code"
    uv run python scripts/run_local_codex.py --ncodex /usr/local/bin/ncodex "refactor main.py"
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

import httpx

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


def discover_ncodex(explicit: str | None = None) -> str:
    """Find the ncodex binary."""
    if explicit:
        if not Path(explicit.split()[0]).exists():
            _die(f"ncodex not found: {explicit}")
        return explicit
    for candidate in [
        shutil.which("ncodex"),
        str(Path.home() / ".local" / "bin" / "ncodex"),
        "/usr/local/bin/ncodex",
        "/opt/homebrew/bin/ncodex",
    ]:
        if candidate and Path(candidate).exists():
            return candidate
    _die("ncodex not found in PATH or common locations")


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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{SERVER_URL}/health", timeout=2.0)
            if r.status_code == 200:
                return
        except httpx.HTTPError:
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
    """Seed capability and agent."""
    subprocess.run(
        [sys.executable, "-m", "agp", "add-capability",
         CAPABILITY_ID, "codex", "local/agp:dev", "local"],
        check=True, capture_output=True,
    )
    r = httpx.post(
        f"{SERVER_URL}/agents/up",
        json={"agent_id": AGENT_ID, "capability_id": CAPABILITY_ID},
        timeout=5.0,
    )
    r.raise_for_status()


def send_job(prompt: str) -> str:
    r = httpx.post(
        f"{SERVER_URL}/messages/send",
        json={
            "target": {"type": "agent", "id": AGENT_ID},
            "message": {"text": prompt, "metadata": {}},
        },
        headers={"Idempotency-Key": f"local-{int(time.time())}"},
        timeout=5.0,
    )
    r.raise_for_status()
    return r.json()["data"]["job_id"]


def run_worker(socket: str, ncodex_cmd: str, timeout: int) -> dict:
    env = {
        **os.environ,
        "WEZTERM_UNIX_SOCKET": socket,
        "AGP_RUNTIME_TERMINAL_HOST_KIND": "wezterm",
        "AGP_RUNTIME_AGENT_ADAPTER_KIND": "codex",
        "AGP_CODEX_TUI_MODE": "true",
        "AGP_CODEX_CLI_COMMAND": f"{ncodex_cmd} --full-auto",
        "AGP_WEZTERM_DEFAULT_CWD": os.getcwd(),
        "AGP_CODEX_IDLE_POLL_SECONDS": "2.0",
        "AGP_CODEX_IDLE_AFTER": "4",
        "AGP_CODEX_IDLE_TIMEOUT_SECONDS": str(timeout),
        "AGP_CODEX_BOOTSTRAP_SETTLE_SECONDS": "3.0",
        "AGP_WEZTERM_SCROLLBACK_LINES": "5000",
    }
    result = subprocess.run(
        [sys.executable, "-m", "agp", "runtime-work-once",
         RUNTIME_ID, "--agent-id", AGENT_ID, "--artifact-root", ".agp-artifacts"],
        capture_output=True, text=True, env=env, timeout=timeout + 60,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        _die(f"runtime worker exited {result.returncode}")
    import ast
    return ast.literal_eval(result.stdout.strip())


def fetch_result(job_id: str) -> str | None:
    r = httpx.get(f"{SERVER_URL}/jobs/{job_id}", timeout=5.0)
    r.raise_for_status()
    job = r.json()["data"]
    art_id = job.get("result_artifact_id")
    if not art_id:
        return None
    r = httpx.get(f"{SERVER_URL}/artifacts/{art_id}/content", timeout=5.0)
    r.raise_for_status()
    return r.json()["data"].get("content", "")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Run AGP end-to-end with Codex on WezTerm")
    parser.add_argument("prompt", help="Task prompt to send to Codex")
    parser.add_argument("--socket", help="WezTerm unix socket path (auto-discovered if omitted)")
    parser.add_argument("--ncodex", help="Path to ncodex binary (auto-discovered if omitted)")
    parser.add_argument("--timeout", type=int, default=180, help="Idle timeout in seconds (default: 180)")
    parser.add_argument("--keep", action="store_true", help="Keep the control plane running after completion")
    args = parser.parse_args()

    socket = discover_wezterm_socket(args.socket)
    ncodex = discover_ncodex(args.ncodex)

    print(f"WezTerm socket : {socket}")
    print(f"ncodex binary  : {ncodex}")
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
        outcome = run_worker(socket, ncodex, args.timeout)

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

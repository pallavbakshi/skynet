#!/usr/bin/env python3
from __future__ import annotations

import argparse
import socket
import time
from typing import NoReturn


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wait for a TCP endpoint to accept connections.")
    parser.add_argument("host")
    parser.add_argument("port", type=int)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--interval", type=float, default=1.0)
    return parser.parse_args()


def fail(message: str) -> NoReturn:
    raise SystemExit(message)


def main() -> None:
    args = parse_args()
    deadline = time.monotonic() + args.timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((args.host, args.port), timeout=min(args.interval, 5.0)):
                print(f"TCP ready: {args.host}:{args.port}")
                return
        except OSError as exc:
            last_error = str(exc)
            time.sleep(args.interval)
    fail(f"Timed out waiting for TCP {args.host}:{args.port}: {last_error or 'unknown error'}")


if __name__ == "__main__":
    main()

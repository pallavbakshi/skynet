#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from typing import NoReturn

import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wait for an HTTP endpoint to return success.")
    parser.add_argument("url")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--status", type=int, default=200)
    return parser.parse_args()


def fail(message: str) -> NoReturn:
    raise SystemExit(message)


def main() -> None:
    args = parse_args()
    deadline = time.monotonic() + args.timeout
    last_error: str | None = None
    with httpx.Client(timeout=min(args.interval, 5.0)) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(args.url)
                if response.status_code == args.status:
                    print(f"HTTP ready: {args.url}")
                    return
                last_error = f"status {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = str(exc)
            time.sleep(args.interval)
    fail(f"Timed out waiting for HTTP {args.url}: {last_error or 'unknown error'}")


if __name__ == "__main__":
    main()

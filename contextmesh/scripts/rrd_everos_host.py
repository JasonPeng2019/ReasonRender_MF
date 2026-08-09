#!/usr/bin/env python3
"""Own one host EverOS process group for the native Codex demo."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if not args.nonce.isalnum() or len(args.nonce) != 32:
        raise SystemExit("invalid launch nonce")
    cwd = args.cwd.resolve(strict=True)
    uv = args.uv.resolve(strict=True)
    root = args.root.resolve()
    os.setsid()
    os.chdir(cwd)
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(root),
        "EVEROS_LLM__MODEL": "disabled-storage-only",
        "EVEROS_LLM__API_KEY": "disabled-storage-only",
        "EVEROS_LLM__BASE_URL": "http://127.0.0.1:9/v1",
    }
    child = subprocess.Popen(
        [str(uv), "run", "everos", "server", "start", "--root", str(root)],
        env=environment,
    )

    def terminate(_signum: int, _frame: object) -> None:
        if child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    signal.signal(signal.SIGHUP, terminate)
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())

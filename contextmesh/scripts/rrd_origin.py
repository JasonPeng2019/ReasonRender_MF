#!/usr/bin/env python3
"""Normalize the HTTPS Ollama API base into a Tollgate upstream origin."""

from __future__ import annotations

import argparse
import ipaddress
from urllib.parse import urlsplit


def normalize_origin(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("OLLAMA_BASE_URL must be an HTTPS URL without embedded credentials")
    if parts.query or parts.fragment:
        raise ValueError("OLLAMA_BASE_URL must not contain a query or fragment")
    if parts.path not in {"", "/", "/v1", "/v1/"}:
        raise ValueError("OLLAMA_BASE_URL must be an origin or end in exactly /v1")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("OLLAMA_BASE_URL contains an invalid port") from exc
    hostname = parts.hostname
    try:
        if ipaddress.ip_address(hostname).version == 6:
            hostname = f"[{hostname}]"
    except ValueError:
        pass
    suffix = f":{port}" if port is not None else ""
    return f"https://{hostname}{suffix}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("value")
    args = parser.parse_args()
    try:
        print(normalize_origin(args.value))
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

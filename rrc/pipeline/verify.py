"""Pytest subprocess verification for generated Python code."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


def run_pytest(code: str, tests: str, timeout: float = 15) -> tuple[bool, str]:
    """Run generated code and tests in a temporary subprocess with a wall-clock limit."""

    source = f"{code.rstrip()}\n\n{tests.rstrip()}\n"
    with tempfile.TemporaryDirectory(prefix="rrcv2-") as directory:
        test_file = Path(directory) / "test_generated.py"
        test_file.write_text(source, encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", str(test_file)],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            parts = (
                part.decode(errors="replace") if isinstance(part, bytes) else (part or "")
                for part in (error.stdout, error.stderr)
            )
            partial = "".join(parts)
            return False, f"pytest timed out after {timeout:g} seconds\n{partial}"

    output = f"{result.stdout}{result.stderr}"
    return result.returncode == 0, output

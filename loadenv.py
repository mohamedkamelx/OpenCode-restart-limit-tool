"""Tiny .env loader (standard library only).

Reads KEY=VALUE lines from a .env file next to this module and puts them in
os.environ. Real environment variables always win, so `FOO=bar ./tool.py`
still overrides the file.

Supported syntax:

    KEY=value
    KEY="value with spaces"
    export KEY=value
    # comment lines and blank lines are ignored

The file is optional: a missing .env is not an error.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | os.PathLike | None = None) -> Path | None:
    """Load .env if it exists. Returns the path read, or None."""
    p = Path(path) if path is not None else Path(__file__).with_name(".env")
    try:
        lines = p.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)

    return p

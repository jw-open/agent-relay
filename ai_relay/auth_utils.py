"""Shared auth utilities used by claude_auth, gemini_auth, and codex_auth."""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def env_truthy(value: Optional[str]) -> bool:
    """Return True if the env var value represents a truthy flag."""
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def read_json_file(path: str) -> Optional[dict]:
    """Read and parse a JSON file; return None on any error."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def write_json_file(path: str, data: dict) -> None:
    """Atomically write *data* as JSON to *path* with mode 0o600."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

"""Shared app-cli-compatible loader for a standalone tool's ``keys.env``."""

from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_KEYS_ENV_PATH = Path.home() / ".amplifier" / "keys.env"


def load_keys_env_into_environ(path: Path = DEFAULT_KEYS_ENV_PATH) -> None:
    """Load missing environment variables from an app-cli-style ``keys.env`` file."""
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        print(
            f"warning: could not read {path}: {exc}. "
            "Values from it will be unavailable, and any ${VAR} placeholders "
            "that depend on them will not expand.",
            file=sys.stderr,
        )
        return

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")

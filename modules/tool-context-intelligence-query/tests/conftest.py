"""Shared pytest configuration and fixtures for tool-context-intelligence-query tests."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest

# Exercise the changed shared library from this checkout rather than the package
# dependency pinned to the last main revision.
BUNDLE_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BUNDLE_ROOT))


@pytest.fixture(autouse=True)
def _reset_auth_singleton() -> Any:
    """Clear the auth module singleton and token cache before/after each test.

    Ensures the process-level _singleton_credential and _MODULE_CACHE do not
    leak between tests, so patches of _make_cli_credential are effective and
    cached tokens from one test don't pollute the next.
    """
    from context_intelligence import auth as _auth_mod

    _auth_mod.reset()
    yield
    _auth_mod.reset()

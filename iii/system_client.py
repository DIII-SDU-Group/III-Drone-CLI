"""Compatibility wrapper for the runtime-owned daemon client."""

from __future__ import annotations

import sys
from pathlib import Path


WORKSPACE_SRC = Path(__file__).resolve().parents[3] / "src"
RUNTIME_PACKAGE = WORKSPACE_SRC / "III-Drone-Runtime"

if RUNTIME_PACKAGE.exists() and str(RUNTIME_PACKAGE) not in sys.path:
    sys.path.insert(0, str(RUNTIME_PACKAGE))

from iii_drone_runtime.daemon import client as _runtime_client  # noqa: E402

DaemonClient = _runtime_client.DaemonClient

# Expose these module objects for existing tests that monkeypatch
# `iii.system_client.subprocess.run`.
subprocess = _runtime_client.subprocess

__all__ = ["DaemonClient"]

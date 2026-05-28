"""Filesystem layout for ghstack-tui.

Single source of truth for where the tool reads/writes state. The root can be
overridden with ``GHSTACK_TUI_ROOT``; everything else is derived from it.
"""

from __future__ import annotations

import os
from pathlib import Path


def root() -> Path:
    """Configuration/cache root. Defaults to ``~/.config/ghstack-tui``."""
    override = os.environ.get("GHSTACK_TUI_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "ghstack-tui"


def cache_path() -> Path:
    """JSON triage-cache file."""
    return root() / "triage-cache.json"


def config_path() -> Path:
    """JSON user-config file."""
    return root() / "config.json"


def mcp_config_dir() -> Path:
    """Directory holding per-repo MCP config blobs (one per repo path hash)."""
    return root()


def ensure_root() -> Path:
    """Create the root directory if missing and return it."""
    r = root()
    r.mkdir(parents=True, exist_ok=True)
    return r

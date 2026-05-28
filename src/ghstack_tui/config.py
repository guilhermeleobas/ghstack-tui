"""User-configurable preferences, persisted as JSON.

Loaded once at startup. Environment variables override the on-disk value:

    GHSTACK_TUI_QUERY           default GitHub-search query
    GHSTACK_TUI_CHECKOUT_PATH   default path to fill into the checkout modal
    GHSTACK_TUI_CLONES_ROOT     directory scanned by the Clones tab
    GHSTACK_TUI_CLONES_PREFIX   only directories starting with this prefix are scanned
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from ghstack_tui import paths

DEFAULT_QUERY = "is:pr is:open author:@me"


@dataclass
class Config:
    default_query: str = DEFAULT_QUERY
    checkout_path: str = "~/git/pytorch313"
    clones_root: str = "~/git"
    clones_prefix: str = "pytorch"

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        path = paths.config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
            for k, v in data.items():
                if hasattr(cfg, k) and isinstance(v, type(getattr(cfg, k))):
                    setattr(cfg, k, v)
        # Env overrides
        if v := os.environ.get("GHSTACK_TUI_QUERY"):
            cfg.default_query = v
        if v := os.environ.get("GHSTACK_TUI_CHECKOUT_PATH"):
            cfg.checkout_path = v
        if v := os.environ.get("GHSTACK_TUI_CLONES_ROOT"):
            cfg.clones_root = v
        if v := os.environ.get("GHSTACK_TUI_CLONES_PREFIX"):
            cfg.clones_prefix = v
        return cfg

    def save(self) -> None:
        path = paths.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, path)

    @property
    def clones_root_path(self) -> Path:
        return Path(self.clones_root).expanduser()

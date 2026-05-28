"""Compute per-PR 'needs attention' verdicts and cache enrichment results.

Verdicts are pure-rule, computed from `gh pr view --json` data already fetched
for the commits table. Caching keys on `updatedAt`: GitHub bumps it on any PR
activity (commit push, comment, review, CI rerun), so a cache hit means
nothing has changed and the stored verdict is still valid.

Cache file: ~/.config/ghstack-tui/triage-cache.json (override with GHSTACK_TUI_ROOT).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from ghstack_tui import paths

if TYPE_CHECKING:
    from ghstack_tui.models import Commit


# Verdict severity ordering. Higher index = more urgent.
_SEVERITY = {"ok": 0, "draft": 1, "waiting": 2, "ready": 3, "attention": 4}

_VERDICT_EMOJI = {
    "attention": "🔴",
    "ready": "🟢",
    "waiting": "🟡",
    "draft": "·",
    "ok": "·",
}


def verdict_for(c: "Commit") -> tuple[str, str]:
    """Classify a Commit. Returns (verdict_key, reason). Pure function of the
    fields already populated by `fetch_pr_details`.
    """
    if c.review_decision == "CHANGES_REQUESTED":
        return "attention", "changes requested"
    if c.ci_fail > 0:
        return "attention", f"{c.ci_fail} CI failing"
    if c.is_draft:
        return "draft", "draft"
    if (
        c.review_decision == "APPROVED"
        and c.ci_fail == 0
        and c.ci_pending == 0
    ):
        return "ready", "ready to land"
    if c.ci_pending > 0:
        return "waiting", "CI pending"
    if c.review_decision in ("REVIEW_REQUIRED", ""):
        # Empty review_decision after enrichment = no review required yet.
        if c.review_decision == "REVIEW_REQUIRED":
            return "waiting", "awaiting review"
    return "ok", ""


def emoji_for(verdict: str) -> str:
    return _VERDICT_EMOJI.get(verdict, "·")


def worst(verdicts: list[str]) -> str:
    """Pick the most-urgent verdict from a list (for stack-level rollup)."""
    if not verdicts:
        return "ok"
    return max(verdicts, key=lambda v: _SEVERITY.get(v, 0))


# --- cache ---------------------------------------------------------------

# Cache layout (JSON):
#   { "owner/repo#1234": { "updated_at": "2026-...", "data": { <enrichment dict> } } }
#
# `data` is the dict returned by gh_client.fetch_pr_details so we can rehydrate
# the Commit without re-running `gh pr view`.


class TriageCache:
    """Thread-safe JSON cache for PR enrichment data, keyed by (repo, pr, updatedAt)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else paths.cache_path()
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._load()

    @staticmethod
    def _key(repo_slug: str, pr_num: int) -> str:
        return f"{repo_slug}#{pr_num}"

    def _load(self) -> None:
        try:
            self._data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def _save_locked(self) -> None:
        # Atomic write: tmp + os.replace. A SIGKILL or full disk mid-write
        # leaves the original cache intact; partial JSON would corrupt it.
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            os.replace(tmp, self._path)
        except OSError:
            pass  # cache is best-effort

    def get(self, repo_slug: str, pr_num: int, updated_at: str) -> dict | None:
        if not updated_at:
            return None
        with self._lock:
            entry = self._data.get(self._key(repo_slug, pr_num))
            if entry and entry.get("updated_at") == updated_at:
                return entry.get("data")
            return None

    def put(self, repo_slug: str, pr_num: int, updated_at: str, data: dict) -> None:
        if not updated_at:
            return
        with self._lock:
            self._data[self._key(repo_slug, pr_num)] = {
                "updated_at": updated_at,
                "data": data,
            }
            self._save_locked()

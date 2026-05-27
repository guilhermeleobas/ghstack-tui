"""Scan local clones for ghstack checkout state.

For each directory under a root (default ``~/git``), inspect HEAD and pull
the ghstack trailers (`Pull Request resolved:` + `ghstack-source-id:`) out
of the commit body. These are written by ghstack when it lands or builds
a stack, so they survive `ghstack checkout` and reliably identify which
PR/stack is currently checked out in that working tree.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


_PR_URL_RE = re.compile(
    r"Pull Request resolved:\s*https?://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)",
    re.IGNORECASE,
)
_GHSTACK_ID_RE = re.compile(
    r"^ghstack-source-id:\s*(\S+)", re.MULTILINE | re.IGNORECASE
)

DEFAULT_ROOT = Path("~/git")


@dataclass
class CloneInfo:
    path: Path
    is_git: bool = False
    branch: str = ""              # branch name, or "" if detached
    head_short: str = ""          # short HEAD sha
    subject: str = ""             # HEAD commit subject line
    repo_slug: str | None = None  # "owner/repo" from PR trailer
    pr_num: int | None = None     # PR number from PR trailer
    ghstack_id: str | None = None # ghstack-source-id trailer value
    dirty: bool = False
    error: str = ""

    @property
    def is_ghstack(self) -> bool:
        return self.ghstack_id is not None or self.pr_num is not None


def _git(path: Path, *args: str, timeout: int = 5) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git error")
    return proc.stdout


def _is_git_repo(path: Path) -> bool:
    # `.git` may be a dir (regular clone) or a file (worktree / submodule).
    return (path / ".git").exists()


def inspect(path: Path) -> CloneInfo:
    """Inspect one directory. Always returns a CloneInfo; errors land in .error."""
    info = CloneInfo(path=path)
    if not _is_git_repo(path):
        return info
    info.is_git = True

    try:
        info.branch = _git(path, "symbolic-ref", "--short", "-q", "HEAD").strip()
    except Exception:
        info.branch = ""

    try:
        info.head_short = _git(path, "rev-parse", "--short", "HEAD").strip()
    except Exception:
        pass

    try:
        body = _git(path, "log", "-1", "--format=%B", "HEAD")
    except Exception as exc:
        info.error = str(exc)
        return info

    for line in body.splitlines():
        if line.strip():
            info.subject = line.strip()
            break

    m = _PR_URL_RE.search(body)
    if m:
        info.repo_slug = m.group(1)
        info.pr_num = int(m.group(2))
    m2 = _GHSTACK_ID_RE.search(body)
    if m2:
        info.ghstack_id = m2.group(1)

    try:
        porcelain = _git(path, "status", "--porcelain")
        info.dirty = bool(porcelain.strip())
    except Exception:
        pass

    return info


def scan(
    root: Path = DEFAULT_ROOT,
    name_prefix: str | None = None,
) -> list[CloneInfo]:
    """Scan immediate subdirectories of ``root`` and return one CloneInfo each.

    Non-directories and hidden entries are skipped. Non-git subdirs are
    returned with ``is_git=False`` so the caller can decide whether to display
    them. If ``name_prefix`` is set, only directories whose name starts with
    that prefix (case-insensitive) are inspected.
    """
    root = root.expanduser()
    if not root.is_dir():
        return []
    prefix = name_prefix.lower() if name_prefix else None
    out: list[CloneInfo] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        if prefix is not None and not child.name.lower().startswith(prefix):
            continue
        try:
            out.append(inspect(child))
        except Exception as exc:  # noqa: BLE001
            out.append(CloneInfo(path=child, error=str(exc)))
    return out

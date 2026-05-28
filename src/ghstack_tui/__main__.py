import re
import subprocess
import sys
from pathlib import Path

from ghstack_tui.app import GhstackTUI
from ghstack_tui.config import get_config


def _git_repo_slug(path: Path) -> str | None:
    """Return `owner/name` for a local git checkout, or None on failure."""
    try:
        url = subprocess.check_output(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    # Match `git@github.com:owner/name(.git)?` and `https://github.com/owner/name(.git)?`.
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _arg_to_query(arg: str, default_query: str) -> str:
    """Turn a single CLI arg into a GitHub-search query.

    - "is:pr is:open ..." (contains `:`) → used verbatim
    - existing local dir         → `<defaults> repo:<owner>/<name>`
    - "owner/name" slug          → `<defaults> repo:owner/name`
    - anything else              → used verbatim
    """
    if ":" in arg:
        return arg
    p = Path(arg).expanduser()
    if p.is_dir():
        slug = _git_repo_slug(p)
        if slug:
            return f"{default_query} repo:{slug}"
    if arg.count("/") == 1 and not arg.startswith("/"):
        return f"{default_query} repo:{arg}"
    return arg


def main() -> None:
    cfg = get_config()
    if len(sys.argv) == 1:
        query: str | None = None
    elif len(sys.argv) == 2:
        query = _arg_to_query(sys.argv[1], cfg.search.default_query)
    else:
        query = " ".join(sys.argv[1:])
    GhstackTUI(query).run()


if __name__ == "__main__":
    main()

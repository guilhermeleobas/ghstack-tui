"""Row-rendering helpers for the DataTable widgets and the diff viewer.

Pure functions only — these are called from both the main app and tests.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.text import Text

from ghstack_tui.clones import CloneInfo
from ghstack_tui.models import Commit, Stack


_LABEL_PRIORITY_PREFIXES = ("ciflow/", "release/", "topic:", "module:")


def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def short_label(lbl: str) -> str:
    for pfx in ("module: ", "topic: ", "ciflow/"):
        if lbl.startswith(pfx):
            return lbl[len(pfx):]
    return lbl


def labels_pretty(labels: list[str]) -> Text:
    if not labels:
        return Text("")
    chosen: list[str] = []
    for prio in _LABEL_PRIORITY_PREFIXES:
        for lbl in labels:
            if lbl.startswith(prio) and lbl not in chosen:
                chosen.append(short_label(lbl))
                break
        if len(chosen) >= 2:
            break
    for lbl in labels:
        if len(chosen) >= 2:
            break
        sl = short_label(lbl)
        if sl not in chosen:
            chosen.append(sl)
    return Text(", ".join(chosen))


def ci_pretty(c: Commit) -> Text:
    if not c.enriched:
        return Text("…", style="dim")
    if c.ci_ok == c.ci_fail == c.ci_pending == 0:
        return Text("—", style="dim")
    t = Text()
    if c.ci_ok:
        t.append(f"✓{c.ci_ok} ", style="green")
    if c.ci_fail:
        t.append(f"✗{c.ci_fail} ", style="red")
    if c.ci_pending:
        t.append(f"●{c.ci_pending}", style="yellow")
    return t


def diff_pretty(c: Commit) -> Text:
    if c.additions is None or c.deletions is None:
        return Text("…", style="dim")
    t = Text()
    t.append(f"+{c.additions}", style="green")
    t.append(" ")
    t.append(f"-{c.deletions}", style="red")
    return t


def rel_time(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    delta = datetime.now(timezone.utc) - dt
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


_MERGE_SIGNAL_TITLE_STYLE: dict[str, str] = {
    "merge failed": "bold red",
    "merge requested": "bold cyan",
    "merged": "bold magenta",
}


def commit_row(c: Commit) -> tuple:
    pr = Text(f"#{c.pr_num}" if c.pr_num is not None else "—")
    if c.is_draft:
        pr.stylize("yellow")
    title_style = _MERGE_SIGNAL_TITLE_STYLE.get(c.merge_signal, "bold white" if c.merge_signal else "")
    title = Text(truncate(c.subject, 50), style=title_style) if title_style else truncate(c.subject, 50)
    return (
        pr,
        title,
        labels_pretty(c.labels),
        ci_pretty(c),
        str(c.comments_count) if c.comments_count else "",
        diff_pretty(c),
        rel_time(c.updated_at),
    )


def stack_row(s: Stack) -> tuple:
    signals = {c.merge_signal for c in s.commits if c.merge_signal}
    if "merge failed" in signals:
        title_style = "bold red"
    elif signals & {"merge requested", "merging"}:
        title_style = "bold cyan"
    elif "merged" in signals:
        title_style = "bold magenta"
    else:
        title_style = ""
    title: str | Text = truncate(s.title, 80)
    if title_style:
        title = Text(str(title), style=title_style)
    return (
        f"#{s.top_pr}" if s.top_pr is not None else "—",
        str(len(s.commits)),
        title,
    )


def clone_row(c: CloneInfo) -> tuple:
    path_txt = Text(c.path.name)
    if not c.is_git:
        path_txt.stylize("dim")
    branch_txt = Text(c.branch) if c.branch else Text(f"({c.head_short})", style="dim")
    repo_txt = Text(c.repo_slug or "—", style="" if c.repo_slug else "dim")
    pr_txt = Text(
        f"#{c.pr_num}" if c.pr_num is not None else "—",
        style="" if c.pr_num is not None else "dim",
    )
    if c.is_ghstack:
        pr_txt.stylize("cyan")
    subject = c.error or c.subject
    dirty = Text("●", style="yellow") if c.dirty else Text("")
    return (
        path_txt,
        branch_txt,
        repo_txt,
        pr_txt,
        truncate(subject, 60),
        dirty,
    )


def render_diff(raw: str) -> Text:
    """Colorize a unified diff string into a Rich Text object."""
    t = Text(no_wrap=True)
    for line in raw.splitlines():
        if line.startswith("diff ") or line.startswith("index "):
            t.append(line + "\n", style="bold yellow")
        elif line.startswith("--- ") or line.startswith("+++ "):
            t.append(line + "\n", style="bold")
        elif line.startswith("+"):
            t.append(line + "\n", style="green")
        elif line.startswith("-"):
            t.append(line + "\n", style="red")
        elif line.startswith("@@"):
            t.append(line + "\n", style="cyan")
        elif line.startswith("\\"):
            t.append(line + "\n", style="dim")
        else:
            t.append(line + "\n")
    return t

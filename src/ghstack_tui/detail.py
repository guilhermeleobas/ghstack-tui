"""Render the bottom-right PR detail panel from a `gh pr view` JSON blob."""

from __future__ import annotations

from rich.text import Text


def render_header(data: dict) -> Text:
    """Top line of the detail panel: PR number, title, state, draft, author, mergeable."""
    state = (data.get("state") or "").upper()
    is_draft = data.get("isDraft", False)

    t = Text()
    t.append(f"#{data.get('number','?')} ", style="bold")
    t.append(data.get("title") or "", style="bold white")
    t.append("  ")

    if is_draft:
        t.append("DRAFT", style="bold yellow on black")
    elif state == "OPEN":
        t.append("OPEN", style="bold black on green")
    elif state == "MERGED":
        t.append("MERGED", style="bold white on magenta")
    elif state == "CLOSED":
        t.append("CLOSED", style="bold white on red")
    else:
        t.append(state, style="dim")
    t.append("  ")

    author = (data.get("author") or {}).get("login")
    if author:
        t.append(f"@{author} ", style="cyan")

    base = data.get("baseRefName") or ""
    head = data.get("headRefName") or ""
    if base and head:
        t.append(f"{head} → {base}", style="dim")

    mergeable = (data.get("mergeable") or "").upper()
    if mergeable == "CONFLICTING":
        t.append("  conflicts", style="bold red")

    return t


def render_meta(data: dict) -> Text:
    """Second line: diff stat, files changed, created/updated, labels."""
    t = Text()
    add = data.get("additions") or 0
    sub = data.get("deletions") or 0
    files = data.get("changedFiles") or 0
    t.append(f"+{add}", style="green")
    t.append(" ")
    t.append(f"-{sub}", style="red")
    t.append(f"  {files} files  ", style="dim")

    labels = [lbl.get("name") for lbl in (data.get("labels") or []) if lbl.get("name")]
    if labels:
        for i, lbl in enumerate(labels[:6]):
            if i:
                t.append(" ")
            t.append(f"[{lbl}]", style="dim cyan")
        if len(labels) > 6:
            t.append(f" +{len(labels) - 6}", style="dim")
    return t


def render_checks(data: dict) -> Text:
    """One line per workflow with an icon + name."""
    t = Text()
    rollup = data.get("statusCheckRollup") or []
    if not rollup:
        t.append("(no checks)", style="dim")
        return t

    # Collapse per-job entries by workflow name so we don't print 200 rows.
    by_wf: dict[str, dict] = {}
    for c in rollup:
        if c.get("__typename") == "CheckRun":
            wf = c.get("workflowName") or c.get("name") or "?"
            status = (c.get("status") or "").upper()
            conclusion = (c.get("conclusion") or "").upper()
        else:
            wf = c.get("context") or c.get("name") or "?"
            status = "COMPLETED"
            conclusion = (c.get("state") or "").upper()
        slot = by_wf.setdefault(wf, {"ok": 0, "fail": 0, "pending": 0})
        if status != "COMPLETED":
            slot["pending"] += 1
        elif conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            slot["ok"] += 1
        elif conclusion in {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED",
                            "ACTION_REQUIRED", "STARTUP_FAILURE"}:
            slot["fail"] += 1
        else:
            slot["pending"] += 1

    for wf in sorted(by_wf):
        counts = by_wf[wf]
        # Pick the highest-severity icon for the workflow.
        if counts["fail"]:
            icon = Text("✗", style="bold red")
        elif counts["pending"]:
            icon = Text("●", style="yellow")
        else:
            icon = Text("✓", style="green")
        t.append(icon)
        t.append(f" {wf}  ", style="bold")
        summary_bits = []
        if counts["ok"]:
            summary_bits.append(f"{counts['ok']} ok")
        if counts["fail"]:
            summary_bits.append(f"{counts['fail']} fail")
        if counts["pending"]:
            summary_bits.append(f"{counts['pending']} pending")
        if summary_bits:
            t.append(", ".join(summary_bits), style="dim")
        t.append("\n")
    t.rstrip()
    return t


def render_reviewers(data: dict) -> Text:
    """Aggregate review decision + per-reviewer state + outstanding requests."""
    t = Text()
    decision = (data.get("reviewDecision") or "").upper()
    if decision == "APPROVED":
        t.append("APPROVED", style="bold green")
    elif decision == "CHANGES_REQUESTED":
        t.append("CHANGES REQUESTED", style="bold red")
    elif decision == "REVIEW_REQUIRED":
        t.append("REVIEW REQUIRED", style="bold yellow")
    else:
        t.append("(no decision)", style="dim")

    reviews = data.get("reviews") or []
    if reviews:
        # Latest review per author.
        latest: dict[str, str] = {}
        for r in reviews:
            login = (r.get("author") or {}).get("login")
            if not login:
                continue
            latest[login] = (r.get("state") or "").upper()
        if latest:
            t.append("\n")
            for login, state in latest.items():
                style = {
                    "APPROVED": "green",
                    "CHANGES_REQUESTED": "red",
                    "COMMENTED": "cyan",
                    "DISMISSED": "dim",
                }.get(state, "dim")
                t.append(f"  @{login} {state.lower()}", style=style)
                t.append("\n")

    requested = data.get("reviewRequests") or []
    if requested:
        names = [r.get("login") or r.get("name") or "?" for r in requested]
        if not str(t).endswith("\n"):
            t.append("\n")
        t.append("Requested: ", style="dim")
        t.append(", ".join(f"@{n}" for n in names), style="cyan")
    return t


def render_files(data: dict) -> Text:
    """Top changed files with diff sizes."""
    t = Text()
    files = data.get("files") or []
    if not files:
        t.append("(no file list)", style="dim")
        return t
    # gh returns files in API order; sort by total churn descending for usefulness.
    sorted_files = sorted(
        files,
        key=lambda f: -((f.get("additions") or 0) + (f.get("deletions") or 0)),
    )
    for f in sorted_files[:20]:
        path = f.get("path") or "?"
        add = f.get("additions") or 0
        sub = f.get("deletions") or 0
        t.append(f"+{add}", style="green")
        t.append(" ")
        t.append(f"-{sub}", style="red")
        t.append(f"  {path}\n")
    if len(sorted_files) > 20:
        t.append(f"... +{len(sorted_files) - 20} more files", style="dim")
    t.rstrip()
    return t

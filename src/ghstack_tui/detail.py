"""Render the bottom-right PR detail panel from a `gh pr view` JSON blob."""

from __future__ import annotations

import re

from rich.text import Text

_FAIL_CONCLUSIONS = {
    "FAILURE", "ERROR", "TIMED_OUT", "CANCELLED",
    "ACTION_REQUIRED", "STARTUP_FAILURE",
}

# Actions detailsUrl looks like:
#   https://github.com/<owner>/<repo>/actions/runs/<run_id>/job/<job_id>
# The job_id is also the check-run id for the annotations API.
_JOB_ID_RE = re.compile(r"/job/(\d+)")


def _check_run_id_from_url(url: str) -> int | None:
    if not url:
        return None
    m = _JOB_ID_RE.search(url)
    return int(m.group(1)) if m else None


def get_failing_jobs(data: dict) -> list[str]:
    """Return names of failing CI jobs (empty list if none)."""
    return [c["label"] for c in get_failing_checks(data)]


def get_failing_checks(data: dict) -> list[dict]:
    """Return failing CI checks with id + label.

    Each item: ``{"label": str, "check_run_id": int | None, "details_url": str | None}``.
    ``check_run_id`` is parsed from ``detailsUrl`` (Actions workflow jobs only); it's
    ``None`` for status contexts and any CheckRun without a recognizable URL.
    """
    rollup = data.get("statusCheckRollup") or []
    out: list[dict] = []
    for c in rollup:
        status = (c.get("status") or "").upper()
        conclusion = (c.get("conclusion") or "").upper()
        if c.get("__typename") == "CheckRun":
            if status != "COMPLETED" or conclusion not in _FAIL_CONCLUSIONS:
                continue
            name = c.get("name") or "?"
            wf = c.get("workflowName")
            label = f"{wf} / {name}" if wf and wf != name else name
            details_url = c.get("detailsUrl") or ""
            out.append({
                "label": label,
                "check_run_id": _check_run_id_from_url(details_url),
                "details_url": details_url or None,
            })
        else:
            state = (c.get("state") or "").upper()
            if state not in _FAIL_CONCLUSIONS:
                continue
            out.append({
                "label": c.get("context") or c.get("name") or "?",
                "check_run_id": None,
                "details_url": c.get("targetUrl") or None,
            })
    return out


def render_ci_failures(data: dict) -> "Text | None":
    """Rich Text listing failing job names, or None if no failures."""
    failing = get_failing_jobs(data)
    if not failing:
        return None
    t = Text()
    for job in failing:
        t.append("✗ ", style="bold red")
        t.append(job)
        t.append("\n")
    t.rstrip()
    return t


_ANNOTATION_STYLE = {
    "failure": "red",
    "warning": "yellow",
    "notice": "cyan",
}


def render_drci_failures(drci_map: dict[str, list[str]]) -> "Text | None":
    """Render Dr.CI's failing-job → failure-captures map.

    Each job becomes a ``✗ <job>`` line with the failure captures
    (typically test invocations) indented underneath. Jobs with no
    captures show a dim placeholder.
    """
    if not drci_map:
        return None
    t = Text()
    items = sorted(drci_map.items())
    for i, (job, captures) in enumerate(items):
        if i:
            t.append("\n")
        t.append("✗ ", style="bold red")
        t.append(job)
        if not captures:
            t.append("\n    (no captured failure)", style="dim")
            continue
        for cap in captures:
            t.append("\n    ")
            t.append(cap, style="red")
    return t


def render_ci_failures_with_annotations(
    checks: list[dict],
    annotations_by_id: dict[int, list[dict]],
) -> "Text | None":
    """Like ``render_ci_failures`` but adds indented annotation lines under each job.

    ``annotations_by_id`` maps ``check_run_id`` to the list returned by
    ``gh_client.fetch_check_annotations``. A status that hasn't been fetched
    yet (no key) renders as ``(press F to load)``; an empty list renders as
    ``(no annotations)``.
    """
    if not checks:
        return None
    t = Text()
    for i, c in enumerate(checks):
        if i:
            t.append("\n")
        t.append("✗ ", style="bold red")
        t.append(c["label"])
        cid = c.get("check_run_id")
        if cid is None:
            t.append("\n")
            t.append("    (no annotations API for status check)", style="dim")
            continue
        if cid not in annotations_by_id:
            continue
        annos = annotations_by_id[cid]
        if not annos:
            t.append("\n")
            t.append("    (no annotations)", style="dim")
            continue
        for a in annos[:10]:
            level = (a.get("annotation_level") or "").lower()
            style = _ANNOTATION_STYLE.get(level, "")
            t.append("\n")
            t.append("    ")
            path = a.get("path") or ""
            line = a.get("start_line")
            if path:
                loc = f"{path}:{line}" if line else path
                t.append(loc, style="bold")
                t.append("  ")
            title = a.get("title") or ""
            if title:
                t.append(title, style=style or "bold")
                t.append("  ")
            msg = (a.get("message") or "").strip().splitlines()
            if msg:
                t.append(msg[0], style=style)
        if len(annos) > 10:
            t.append("\n")
            t.append(f"    … +{len(annos) - 10} more annotations", style="dim")
    return t


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

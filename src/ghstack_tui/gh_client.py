"""Fetch ghstack stacks from GitHub via `gh search prs`.

We identify ghstack PRs by the "Stack from [ghstack]" block in the PR body
rather than by branch name, so any GitHub-search query (across repos) works.

Each ghstack PR body contains an ordered list of every PR in the stack:

    Stack from [ghstack](...) (oldest at bottom):
    * __->__ #184836
    * #184789
    * #184788

All PRs in the same stack carry the same list (only the `__->__` marker
moves), so we recover the full stack composition from any one PR's body.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field

from ghstack_tui.models import Commit, Stack

_STACK_HEADER_RE = re.compile(r"Stack from \[ghstack\]\([^)]*\)[^:\n]*:", re.IGNORECASE)
_STACK_PR_RE = re.compile(r"^\s*\*\s+(?:__->__\s+)?#(\d+)\s*$", re.MULTILINE)

DEFAULT_QUERY = "is:pr is:open author:@me"


@dataclass
class _PR:
    number: int
    title: str
    is_draft: bool
    url: str
    stack_prs: tuple[int, ...]  # ordered top-to-bottom as listed in body
    raw: dict = field(default_factory=dict)


def _run_gh_search(query: str, limit: int) -> list[dict]:
    """Run `gh search prs <query terms>` and return the raw JSON list.

    The query is shell-split so callers can use GitHub-search syntax exactly
    as it appears in the GitHub UI (e.g. `is:pr is:open author:@me repo:foo/bar`).
    """
    tokens = shlex.split(query)
    cmd = [
        "gh", "search", "prs",
        *tokens,
        "--json", "number,title,body,isDraft,url,labels,commentsCount,updatedAt,repository",
        "--limit", str(limit),
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


_CI_OK = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_CI_FAIL = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
_CI_PENDING = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "EXPECTED", ""}


def _summarize_rollup(rollup: list[dict]) -> tuple[int, int, int]:
    """Count CI checks by status. Returns (ok, fail, pending)."""
    ok = fail = pending = 0
    for c in rollup or []:
        # CheckRun: use `conclusion` when COMPLETED, else `status` is in-progress.
        # StatusContext: use `state`.
        if c.get("__typename") == "CheckRun":
            if (c.get("status") or "").upper() != "COMPLETED":
                pending += 1
                continue
            v = (c.get("conclusion") or "").upper()
        else:  # StatusContext or unknown
            v = (c.get("state") or c.get("conclusion") or "").upper()
        if v in _CI_OK:
            ok += 1
        elif v in _CI_FAIL:
            fail += 1
        else:
            pending += 1
    return ok, fail, pending


def fetch_pr_details(repo_slug: str, pr_num: int) -> dict:
    """Fetch CI + diff summary for one PR (row enrichment)."""
    cmd = [
        "gh", "pr", "view", str(pr_num),
        "--repo", repo_slug,
        "--json", "statusCheckRollup,additions,deletions,changedFiles,reviewDecision",
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    data = json.loads(proc.stdout)
    ok, fail, pending = _summarize_rollup(data.get("statusCheckRollup") or [])
    return {
        "ci_ok": ok,
        "ci_fail": fail,
        "ci_pending": pending,
        "additions": data.get("additions"),
        "deletions": data.get("deletions"),
        "changed_files": data.get("changedFiles"),
        "review_decision": data.get("reviewDecision") or "",
        "enriched": True,
    }


def fetch_pr_full(repo_slug: str, pr_num: int) -> dict:
    """Fetch the deep PR record used to populate the detail panel.

    Returned dict keys match the `gh pr view --json` field names.
    """
    cmd = [
        "gh", "pr", "view", str(pr_num),
        "--repo", repo_slug,
        "--json",
        ",".join([
            "number", "title", "body", "url", "state", "isDraft",
            "author", "createdAt", "updatedAt",
            "baseRefName", "headRefName",
            "additions", "deletions", "changedFiles", "files",
            "statusCheckRollup", "reviewDecision", "reviewRequests", "reviews",
            "mergeable", "labels",
        ]),
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


def _parse_stack_block(body: str) -> tuple[int, ...]:
    """Extract the ordered PR list from a ghstack 'Stack from' block.

    Returns top-to-bottom order. Empty tuple if no block is found.
    """
    header = _STACK_HEADER_RE.search(body or "")
    if not header:
        return ()
    rest = body[header.end():]
    out: list[int] = []
    for line in rest.splitlines():
        if line.strip() == "":
            if out:
                break  # blank line ends the list (after at least one item)
            continue
        m = _STACK_PR_RE.match(line)
        if m:
            out.append(int(m.group(1)))
        elif out:
            break  # non-matching line after items ends the list
    return tuple(out)


def _classify(pr_json: dict) -> _PR | None:
    stack_prs = _parse_stack_block(pr_json.get("body") or "")
    if not stack_prs:
        return None  # not a ghstack PR (or body missing the marker)
    return _PR(
        number=pr_json["number"],
        title=pr_json["title"],
        is_draft=pr_json.get("isDraft", False),
        url=pr_json["url"],
        stack_prs=stack_prs,
        raw=pr_json,
    )


def _build_stacks(prs: list[_PR]) -> list[Stack]:
    by_num: dict[int, _PR] = {p.number: p for p in prs}

    # Dedupe stacks by the set of PR numbers they contain.
    seen: dict[frozenset[int], tuple[int, ...]] = {}
    for p in prs:
        key = frozenset(p.stack_prs)
        if key not in seen:
            seen[key] = p.stack_prs

    stacks: list[Stack] = []
    for ordered_top_to_bottom in seen.values():
        top_num = ordered_top_to_bottom[0]
        top_pr = by_num.get(top_num)
        title = top_pr.title if top_pr else f"#{top_num}"

        commits: list[Commit] = []
        # Reverse to render bottom-to-top (matches what `ghstack land` does).
        for n in reversed(ordered_top_to_bottom):
            p = by_num.get(n)
            if p is None:
                commits.append(Commit(
                    sha="", short_sha="", subject="(not in query results)",
                    pr_num=n, source_id=None,
                ))
                continue
            labels = [lbl.get("name", "") for lbl in (p.raw.get("labels") or [])]
            repo = (p.raw.get("repository") or {}).get("nameWithOwner")
            commits.append(Commit(
                subject=p.title,
                pr_num=p.number,
                url=p.url,
                is_draft=p.is_draft,
                repo_slug=repo,
                labels=labels,
                comments_count=p.raw.get("commentsCount") or 0,
                updated_at=p.raw.get("updatedAt") or "",
            ))

        stacks.append(Stack(top_pr=top_num, title=title, commits=commits))

    stacks.sort(key=lambda s: s.top_pr or 0, reverse=True)
    return stacks


def load_stacks(query: str = DEFAULT_QUERY, limit: int = 200) -> list[Stack]:
    raw = _run_gh_search(query, limit)
    prs = [p for p in (_classify(item) for item in raw) if p is not None]
    return _build_stacks(prs)

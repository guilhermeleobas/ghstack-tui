#!/usr/bin/env python3
"""ghstack-tui MCP server.

Exposes live PR context (info, diff, CI status, stack) as tools for Claude Code.
Started automatically by Claude Code via the .mcp.json written by the TUI when
the user presses 'a' (Ask Claude).

Usage:
    python mcp_server.py --repo owner/name --pr 12345 --stack 12345,12344,12343
"""

from __future__ import annotations

import argparse
import json
import subprocess

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Parse startup args injected by the TUI into .mcp.json
# ---------------------------------------------------------------------------

def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--repo", default="")
    p.add_argument("--pr", type=int, default=0)
    p.add_argument("--stack", default="")  # comma-separated PR numbers, top-to-bottom
    return p.parse_args()


_args = _parse()
_REPO: str = _args.repo
_PR: int = _args.pr
_STACK: list[int] = [int(x) for x in _args.stack.split(",") if x.strip().isdigit()]

_FAIL_CONCLUSIONS = {
    "FAILURE", "ERROR", "TIMED_OUT", "CANCELLED",
    "ACTION_REQUIRED", "STARTUP_FAILURE",
}

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "ghstack-tui",
    instructions=(
        f"You are helping debug and fix ghstack PR #{_PR} in {_REPO}. "
        f"The full stack contains PRs (top to bottom): {_STACK}. "
        "Use the available tools to inspect PR details, failing CI jobs, and diffs. "
        "Prefer get_failing_jobs first to understand what's broken, "
        "then get_pr_diff to see the code changes."
    ),
)


@mcp.tool()
def get_pr_info(pr: int = 0, repo: str = "") -> str:
    """Get PR details: title, description, state, draft status, diff stats, review decision.

    Args:
        pr: PR number. Defaults to the PR selected in ghstack-tui.
        repo: GitHub repo slug (owner/name). Defaults to current repo.
    """
    pr = pr or _PR
    repo = repo or _REPO
    result = subprocess.run(
        [
            "gh", "pr", "view", str(pr),
            "--repo", repo,
            "--json",
            "number,title,body,state,isDraft,additions,deletions,"
            "changedFiles,reviewDecision,baseRefName,headRefName,author,"
            "mergeable,mergeStateStatus,isInMergeQueue",
        ],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


@mcp.tool()
def get_failing_jobs(pr: int = 0, repo: str = "") -> str:
    """List CI jobs that are currently failing for the PR.

    Args:
        pr: PR number. Defaults to the PR selected in ghstack-tui.
        repo: GitHub repo slug (owner/name). Defaults to current repo.
    """
    pr = pr or _PR
    repo = repo or _REPO
    result = subprocess.run(
        ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "statusCheckRollup"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    failing: list[str] = []
    for c in data.get("statusCheckRollup") or []:
        status = (c.get("status") or "").upper()
        conclusion = (c.get("conclusion") or "").upper()
        if status != "COMPLETED" or conclusion not in _FAIL_CONCLUSIONS:
            continue
        name = c.get("name") or "?"
        wf = c.get("workflowName")
        label = f"{wf} / {name}" if wf and wf != name else name
        failing.append(label)
    return json.dumps({"failing_jobs": failing, "count": len(failing)})


@mcp.tool()
def get_pr_diff(pr: int = 0, repo: str = "") -> str:
    """Get the full unified diff for the PR.

    Args:
        pr: PR number. Defaults to the PR selected in ghstack-tui.
        repo: GitHub repo slug (owner/name). Defaults to current repo.
    """
    pr = pr or _PR
    repo = repo or _REPO
    result = subprocess.run(
        ["gh", "pr", "diff", str(pr), "--repo", repo],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


@mcp.tool()
def get_stack_prs() -> str:
    """Get all PR numbers in the current ghstack stack (top-to-bottom order)."""
    return json.dumps({
        "stack_prs": _STACK,
        "current_pr": _PR,
        "repo": _REPO,
    })


if __name__ == "__main__":
    mcp.run()

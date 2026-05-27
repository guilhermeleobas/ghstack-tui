from dataclasses import dataclass, field


@dataclass
class Commit:
    sha: str = ""
    short_sha: str = ""
    subject: str = ""
    pr_num: int | None = None
    source_id: str | None = None
    deps: list[int] = field(default_factory=list)
    url: str | None = None
    is_draft: bool = False

    # filled from `gh search prs` at load time
    repo_slug: str | None = None  # "owner/name"
    labels: list[str] = field(default_factory=list)
    comments_count: int = 0
    updated_at: str = ""  # ISO-8601

    # filled lazily via `gh pr view` once a stack is in view
    ci_ok: int = 0
    ci_fail: int = 0
    ci_pending: int = 0
    additions: int | None = None
    deletions: int | None = None
    changed_files: int | None = None
    review_decision: str = ""
    enriched: bool = False

    # filled from triage rules once enrichment lands
    verdict: str = "ok"          # one of: attention, ready, waiting, draft, ok
    verdict_reason: str = ""


@dataclass
class Stack:
    """A ghstack stack: a chain of commits/PRs linked by `ghstack dependencies:`.

    Commits are ordered bottom-to-top (oldest dep first, top PR last).
    """

    top_pr: int | None
    title: str
    commits: list[Commit] = field(default_factory=list)

"""Tests for ghstack_tui.clones — local clone scanner."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ghstack_tui.clones import (
    _GHSTACK_ID_RE,
    _PR_URL_RE,
    CloneInfo,
    inspect,
    scan,
)


# ---------------------------------------------------------------------------
# trailer regexes
# ---------------------------------------------------------------------------

def test_pr_url_regex_basic():
    body = "Some change\n\nPull Request resolved: https://github.com/pytorch/pytorch/pull/12345"
    m = _PR_URL_RE.search(body)
    assert m
    assert m.group(1) == "pytorch/pytorch"
    assert m.group(2) == "12345"


def test_pr_url_regex_http():
    body = "Pull Request resolved: http://github.com/foo/bar/pull/7"
    m = _PR_URL_RE.search(body)
    assert m
    assert m.group(1) == "foo/bar"


def test_pr_url_regex_missing():
    assert _PR_URL_RE.search("just a regular commit") is None


def test_ghstack_id_regex():
    body = "subject\n\nghstack-source-id: abc123def\nPull Request resolved: x"
    m = _GHSTACK_ID_RE.search(body)
    assert m
    assert m.group(1) == "abc123def"


def test_ghstack_id_regex_missing():
    assert _GHSTACK_ID_RE.search("no trailer here") is None


# ---------------------------------------------------------------------------
# inspect() against real git repos
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _make_repo(path: Path, body: str) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "f.txt").write_text("x")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", body)


def test_inspect_non_git(tmp_path):
    d = tmp_path / "not_git"
    d.mkdir()
    info = inspect(d)
    assert info.is_git is False
    assert info.pr_num is None


def test_inspect_plain_repo(tmp_path):
    repo = tmp_path / "plain"
    _make_repo(repo, "Just a regular commit")
    info = inspect(repo)
    assert info.is_git
    assert info.branch == "main"
    assert info.subject == "Just a regular commit"
    assert info.pr_num is None
    assert info.repo_slug is None
    assert info.is_ghstack is False
    assert info.dirty is False


def test_inspect_ghstack_repo(tmp_path):
    repo = tmp_path / "ghstack-clone"
    body = (
        "[dynamo] fix something\n"
        "\n"
        "Some details here.\n"
        "\n"
        "ghstack-source-id: deadbeefcafef00d\n"
        "Pull Request resolved: https://github.com/pytorch/pytorch/pull/184836\n"
    )
    _make_repo(repo, body)
    info = inspect(repo)
    assert info.is_git
    assert info.subject == "[dynamo] fix something"
    assert info.repo_slug == "pytorch/pytorch"
    assert info.pr_num == 184836
    assert info.ghstack_id == "deadbeefcafef00d"
    assert info.is_ghstack is True


def test_inspect_dirty_repo(tmp_path):
    repo = tmp_path / "dirty"
    _make_repo(repo, "clean commit")
    (repo / "f.txt").write_text("modified")
    info = inspect(repo)
    assert info.dirty is True


def test_inspect_detached_head(tmp_path):
    repo = tmp_path / "detached"
    _make_repo(repo, "first")
    # Add a second commit then detach onto the first
    (repo / "f.txt").write_text("y")
    _git(repo, "commit", "-q", "-am", "second")
    first_sha = subprocess.run(
        ["git", "rev-parse", "HEAD~1"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    _git(repo, "checkout", "-q", first_sha)
    info = inspect(repo)
    assert info.branch == ""           # detached
    assert info.head_short             # short sha present


# ---------------------------------------------------------------------------
# scan()
# ---------------------------------------------------------------------------

def test_scan_missing_root(tmp_path):
    assert scan(tmp_path / "does-not-exist") == []


def test_scan_name_prefix_filter(tmp_path):
    _make_repo(tmp_path / "pytorch", "Pull Request resolved: https://github.com/o/r/pull/1")
    _make_repo(tmp_path / "pytorch313", "msg")
    _make_repo(tmp_path / "other", "msg")
    results = scan(tmp_path, name_prefix="pytorch")
    names = sorted(r.path.name for r in results)
    assert names == ["pytorch", "pytorch313"]


def test_scan_name_prefix_case_insensitive(tmp_path):
    _make_repo(tmp_path / "PyTorch", "msg")
    results = scan(tmp_path, name_prefix="pytorch")
    assert len(results) == 1
    assert results[0].path.name == "PyTorch"


def test_scan_returns_each_subdir(tmp_path):
    _make_repo(tmp_path / "a", "Pull Request resolved: https://github.com/o/r/pull/1")
    _make_repo(tmp_path / "b", "no trailers here")
    (tmp_path / "c-not-git").mkdir()
    (tmp_path / ".hidden").mkdir()        # hidden, must be skipped
    (tmp_path / "file.txt").write_text("not a dir")

    results = scan(tmp_path)
    names = sorted(r.path.name for r in results)
    assert names == ["a", "b", "c-not-git"]

    by_name = {r.path.name: r for r in results}
    assert by_name["a"].pr_num == 1
    assert by_name["a"].repo_slug == "o/r"
    assert by_name["b"].is_git and by_name["b"].pr_num is None
    assert by_name["c-not-git"].is_git is False

"""Tests for detail-panel rendering."""

from rich.text import Text

from ghstack_tui.detail import render_header


def test_render_header_shows_mergeable_state():
    data = {
        "number": 42,
        "title": "Add merge status",
        "state": "OPEN",
        "isDraft": False,
        "author": {"login": "octocat"},
        "baseRefName": "main",
        "headRefName": "feature",
        "mergeStateStatus": "CLEAN",
    }
    t = render_header(data)
    assert isinstance(t, Text)
    assert "mergeable" in t.plain


def test_render_header_shows_merge_queue():
    data = {
        "number": 99,
        "title": "Queued PR",
        "state": "OPEN",
        "isDraft": False,
        "isInMergeQueue": True,
    }
    t = render_header(data)
    assert "queued" in t.plain


def test_render_header_shows_merge_signal():
    data = {
        "number": 7,
        "title": "Landing",
        "state": "OPEN",
        "isDraft": False,
        "_merge_signal": {"label": "merging", "style": "bold cyan", "author": "pytorch-merge-bot"},
    }
    t = render_header(data)
    assert "merging" in t.plain


def test_render_header_shows_merge_failed_signal():
    data = {
        "number": 8,
        "title": "Failed land",
        "state": "OPEN",
        "isDraft": False,
        "_merge_signal": {"label": "merge failed", "style": "bold red", "author": "pytorch-merge-bot"},
    }
    t = render_header(data)
    assert "merge failed" in t.plain

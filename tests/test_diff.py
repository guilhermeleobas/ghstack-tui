"""Tests for diff rendering and DiffModal."""

import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Label

from ghstack_tui.app import DiffModal, _render_diff
from ghstack_tui.detail import get_failing_jobs, render_ci_failures


# ---------------------------------------------------------------------------
# _render_diff
# ---------------------------------------------------------------------------

SAMPLE_DIFF = """\
diff --git a/foo.py b/foo.py
index abc123..def456 100644
--- a/foo.py
+++ b/foo.py
@@ -1,4 +1,4 @@
 context line
-old line
+new line
 another context
\\ No newline at end of file
"""


def test_render_diff_returns_text():
    t = _render_diff(SAMPLE_DIFF)
    assert isinstance(t, Text)


def test_render_diff_no_wrap():
    t = _render_diff(SAMPLE_DIFF)
    assert t.no_wrap is True


def test_render_diff_contains_all_lines():
    t = _render_diff(SAMPLE_DIFF)
    plain = t.plain
    assert "diff --git" in plain
    assert "--- a/foo.py" in plain
    assert "+++ b/foo.py" in plain
    assert "@@ -1,4 +1,4 @@" in plain
    assert "-old line" in plain
    assert "+new line" in plain
    assert "context line" in plain


def test_render_diff_colors_additions_green():
    t = _render_diff("+new line\n")
    spans = [(s.start, s.end, str(s.style)) for s in t._spans]
    assert any("green" in style for _, _, style in spans)


def test_render_diff_colors_deletions_red():
    t = _render_diff("-old line\n")
    spans = [(s.start, s.end, str(s.style)) for s in t._spans]
    assert any("red" in style for _, _, style in spans)


def test_render_diff_colors_hunk_cyan():
    t = _render_diff("@@ -1,3 +1,3 @@ def foo():\n")
    spans = [(s.start, s.end, str(s.style)) for s in t._spans]
    assert any("cyan" in style for _, _, style in spans)


def test_render_diff_colors_diff_header_yellow():
    t = _render_diff("diff --git a/x b/x\n")
    spans = [(s.start, s.end, str(s.style)) for s in t._spans]
    assert any("yellow" in style for _, _, style in spans)


def test_render_diff_empty():
    t = _render_diff("")
    assert isinstance(t, Text)
    assert t.plain == ""


# ---------------------------------------------------------------------------
# get_failing_jobs / render_ci_failures
# ---------------------------------------------------------------------------

def _make_rollup(entries):
    """Build a statusCheckRollup list from simplified (name, status, conclusion) tuples."""
    result = []
    for name, status, conclusion in entries:
        result.append({
            "__typename": "CheckRun",
            "name": name,
            "workflowName": "CI",
            "status": status,
            "conclusion": conclusion,
        })
    return result


def test_get_failing_jobs_empty():
    assert get_failing_jobs({}) == []


def test_get_failing_jobs_all_pass():
    data = {"statusCheckRollup": _make_rollup([
        ("build", "COMPLETED", "SUCCESS"),
        ("test", "COMPLETED", "SUCCESS"),
    ])}
    assert get_failing_jobs(data) == []


def test_get_failing_jobs_some_fail():
    data = {"statusCheckRollup": _make_rollup([
        ("build", "COMPLETED", "SUCCESS"),
        ("test-linux", "COMPLETED", "FAILURE"),
        ("lint", "COMPLETED", "ERROR"),
    ])}
    jobs = get_failing_jobs(data)
    assert len(jobs) == 2
    assert any("test-linux" in j for j in jobs)
    assert any("lint" in j for j in jobs)


def test_get_failing_jobs_pending_not_included():
    data = {"statusCheckRollup": _make_rollup([
        ("slow-test", "IN_PROGRESS", ""),
    ])}
    assert get_failing_jobs(data) == []


def test_render_ci_failures_none_when_clean():
    data = {"statusCheckRollup": _make_rollup([
        ("build", "COMPLETED", "SUCCESS"),
    ])}
    assert render_ci_failures(data) is None


def test_render_ci_failures_text_when_failures():
    data = {"statusCheckRollup": _make_rollup([
        ("test-cuda", "COMPLETED", "FAILURE"),
    ])}
    t = render_ci_failures(data)
    assert t is not None
    assert "test-cuda" in t.plain
    assert "✗" in t.plain


# ---------------------------------------------------------------------------
# DiffModal (Textual async)
# ---------------------------------------------------------------------------

class _HarnessApp(App):
    def compose(self) -> ComposeResult:
        return iter([])


async def test_diff_modal_mounts():
    """DiffModal composes without error; title label contains PR info."""
    async with _HarnessApp().run_test() as pilot:
        await pilot.app.push_screen(DiffModal(42, "pytorch/pytorch", "[dynamo] fix"))
        await pilot.pause()

        title = pilot.app.screen.query_one("#_dm_title", Label)
        rendered = str(title.render())
        assert "42" in rendered
        assert "pytorch/pytorch" in rendered


async def test_diff_modal_close_on_q():
    """Pressing q dismisses the modal; screen class changes back."""
    async with _HarnessApp().run_test() as pilot:
        await pilot.app.push_screen(DiffModal(1, "org/repo"))
        await pilot.pause()
        assert pilot.app.screen.__class__.__name__ == "DiffModal"
        await pilot.press("q")
        await pilot.pause()
        assert pilot.app.screen.__class__.__name__ != "DiffModal"


async def test_diff_modal_close_on_escape():
    """Pressing escape dismisses the modal."""
    async with _HarnessApp().run_test() as pilot:
        await pilot.app.push_screen(DiffModal(1, "org/repo"))
        await pilot.pause()
        assert pilot.app.screen.__class__.__name__ == "DiffModal"
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen.__class__.__name__ != "DiffModal"

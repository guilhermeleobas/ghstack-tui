"""Unit tests for app.py row-rendering helpers."""

from datetime import datetime, timedelta, timezone

import pytest
from rich.text import Text

from ghstack_tui.app import (
    _ci_pretty,
    _diff_pretty,
    _labels_pretty,
    _rel_time,
    _row_for,
    _short_label,
    _truncate,
)
from ghstack_tui.models import Commit


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------

def test_truncate_short():
    assert _truncate("hello", 10) == "hello"


def test_truncate_exact():
    assert _truncate("hello", 5) == "hello"


def test_truncate_long():
    result = _truncate("hello world", 8)
    assert len(result) == 8
    assert result.endswith("…")


def test_truncate_empty():
    assert _truncate("", 5) == ""


# ---------------------------------------------------------------------------
# _rel_time
# ---------------------------------------------------------------------------

def _iso_ago(**kwargs) -> str:
    dt = datetime.now(timezone.utc) - timedelta(**kwargs)
    return dt.isoformat()


def test_rel_time_seconds():
    assert _rel_time(_iso_ago(seconds=30)).endswith("s")


def test_rel_time_minutes():
    assert _rel_time(_iso_ago(minutes=5)).endswith("m")


def test_rel_time_hours():
    assert _rel_time(_iso_ago(hours=3)).endswith("h")


def test_rel_time_days():
    assert _rel_time(_iso_ago(days=2)).endswith("d")


def test_rel_time_empty():
    assert _rel_time("") == ""


def test_rel_time_invalid():
    assert _rel_time("not-a-date") == ""


# ---------------------------------------------------------------------------
# _short_label
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,expected", [
    ("module: torch.nn", "torch.nn"),
    ("topic: performance", "performance"),
    ("ciflow/trunk", "trunk"),
    ("release/2.5", "release/2.5"),   # no matching prefix → unchanged
    ("plain", "plain"),
])
def test_short_label(label, expected):
    assert _short_label(label) == expected


# ---------------------------------------------------------------------------
# _labels_pretty
# ---------------------------------------------------------------------------

def test_labels_pretty_empty():
    t = _labels_pretty([])
    assert isinstance(t, Text)
    assert t.plain == ""


def test_labels_pretty_caps_at_two():
    labels = ["a", "b", "c", "d"]
    t = _labels_pretty(labels)
    # At most 2 labels joined by ", "
    assert t.plain.count(",") <= 1


def test_labels_pretty_priority_prefix():
    labels = ["ciflow/trunk", "module: autograd", "other"]
    t = _labels_pretty(labels)
    # ciflow/ prefix should appear (stripped to just "trunk")
    assert "trunk" in t.plain


def test_labels_pretty_single():
    t = _labels_pretty(["only-one"])
    assert "only-one" in t.plain


# ---------------------------------------------------------------------------
# _ci_pretty
# ---------------------------------------------------------------------------

def test_ci_pretty_not_enriched():
    c = Commit()
    t = _ci_pretty(c)
    assert "…" in t.plain


def test_ci_pretty_no_checks():
    c = Commit(enriched=True, ci_ok=0, ci_fail=0, ci_pending=0)
    t = _ci_pretty(c)
    assert "—" in t.plain


def test_ci_pretty_all_ok():
    c = Commit(enriched=True, ci_ok=5, ci_fail=0, ci_pending=0)
    t = _ci_pretty(c)
    assert "✓5" in t.plain


def test_ci_pretty_mixed():
    c = Commit(enriched=True, ci_ok=3, ci_fail=1, ci_pending=2)
    t = _ci_pretty(c)
    assert "✓" in t.plain
    assert "✗" in t.plain
    assert "●" in t.plain


# ---------------------------------------------------------------------------
# _diff_pretty
# ---------------------------------------------------------------------------

def test_diff_pretty_unenriched():
    c = Commit(additions=None, deletions=None)
    t = _diff_pretty(c)
    assert "…" in t.plain


def test_diff_pretty_values():
    c = Commit(additions=42, deletions=7)
    t = _diff_pretty(c)
    assert "+42" in t.plain
    assert "-7" in t.plain


# ---------------------------------------------------------------------------
# _row_for  (integration: all helpers together)
# ---------------------------------------------------------------------------

def test_row_for_basic():
    c = Commit(
        pr_num=101,
        subject="[dynamo] fix regression",
        is_draft=False,
        labels=["ciflow/trunk"],
        comments_count=3,
        additions=10,
        deletions=2,
        updated_at=_iso_ago(hours=1),
        enriched=True,
        ci_ok=1,
        ci_fail=0,
        ci_pending=0,
        repo_slug="pytorch/pytorch",
    )
    row = _row_for(c)
    assert len(row) == 8   # PR, Title, Labels, CI, Status, 💬, ±, Upd
    # PR cell
    assert "#101" in row[0].plain
    # Title must contain literal [dynamo] — NOT parsed as markup
    assert "[dynamo]" in row[1]


def test_row_for_draft_pr_number_styled_yellow():
    c = Commit(pr_num=5, subject="wip", is_draft=True)
    row = _row_for(c)
    pr_text: Text = row[0]
    assert pr_text.plain == "#5"
    # Verify yellow style is applied somewhere on the text
    spans = [span for span in pr_text._spans]
    assert any("yellow" in str(s.style) for s in spans)


def test_row_for_no_pr_num():
    c = Commit(subject="orphan commit")
    row = _row_for(c)
    assert "—" in row[0].plain

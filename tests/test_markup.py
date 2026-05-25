"""Tests for the markup-escape fix.

PyTorch PR titles contain patterns like [dynamo], [inductor], [ATen] that Rich
would normally interpret as style tags inside Text.from_markup().  The fix
wraps c.subject with rich.markup.escape() before interpolation.
"""

import pytest
from rich.markup import escape as markup_escape
from rich.text import Text


BRACKET_SUBJECTS = [
    "[dynamo] fix something",
    "[inductor] speed up conv",
    "[ATen] add new op",
    "[FSDP2] training fix",
    "[module: torch.nn] refactor",
    "[fx] graph rewrite [part 2]",
    "no brackets at all",
    "[open but not closed",
    "closed but not open]",
]


@pytest.mark.parametrize("subject", BRACKET_SUBJECTS)
def test_markup_escape_no_raise(subject):
    """Text.from_markup with escaped subject must not raise MissingStyle."""
    t = Text.from_markup(
        f"[bold]#42[/] [bold white]{markup_escape(subject)}[/]"
    )
    assert isinstance(t, Text)


@pytest.mark.parametrize("subject", BRACKET_SUBJECTS)
def test_markup_escape_preserves_literal_text(subject):
    """Subject text must appear verbatim in the rendered output."""
    t = Text.from_markup(
        f"[bold]#42[/] [bold white]{markup_escape(subject)}[/]"
    )
    assert subject in t.plain


def test_unescaped_strips_brackets():
    """Without escape, Rich strips [dynamo] as an unknown tag → plain text loses brackets.
    This is the bug: the subject "[dynamo] fix" would silently render as "fix"."""
    t = Text.from_markup("[bold white][dynamo] fix[/]")
    # Rich either strips or ignores [dynamo]; the literal bracket text is gone.
    assert "[dynamo]" not in t.plain


def test_draft_suffix_still_applied():
    """DRAFT markup appended after escaped subject must parse correctly."""
    subject = "[dynamo] my fix"
    t = Text.from_markup(
        f"[bold]#1[/] [bold white]{markup_escape(subject)}[/][yellow] DRAFT[/]"
    )
    assert "DRAFT" in t.plain
    assert subject in t.plain

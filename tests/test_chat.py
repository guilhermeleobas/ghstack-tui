"""Tests for the embedded Ollama chat panel."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from rich.text import Text
from textual.app import App, ComposeResult

from ghstack_tui.app import GhstackTUI


# ---------------------------------------------------------------------------
# _render_chat
# ---------------------------------------------------------------------------

class _ChatApp(GhstackTUI):
    """Minimal subclass that skips real data loading."""

    def on_mount(self) -> None:
        # Skip gh calls; just set up tables.
        from textual.widgets import DataTable
        stacks_t: DataTable = self.query_one("#stacks", DataTable)
        stacks_t.add_columns("Top PR", "#", "Title")
        commits_t: DataTable = self.query_one("#commits", DataTable)
        commits_t.add_columns(*self._RIGHT_COLS)


def _make_app() -> _ChatApp:
    app = _ChatApp.__new__(_ChatApp)
    GhstackTUI.__init__(app)
    return app


def test_render_chat_empty():
    app = _make_app()
    t = app._render_chat()
    assert isinstance(t, Text)
    assert t.plain == ""


def test_render_chat_user_message():
    app = _make_app()
    app._chat_lines = [("user", "hello")]
    t = app._render_chat()
    assert "You:" in t.plain
    assert "hello" in t.plain


def test_render_chat_assistant_message():
    app = _make_app()
    app._chat_lines = [("user", "hi"), ("assistant", "hello there")]
    t = app._render_chat()
    assert "AI:" in t.plain
    assert "hello there" in t.plain


def test_render_chat_streaming_cursor():
    app = _make_app()
    app._chat_lines = [("user", "go")]
    app._chat_streaming = "partial"
    t = app._render_chat()
    assert "partial" in t.plain
    assert "▋" in t.plain


def test_render_chat_streaming_no_cursor_when_empty():
    app = _make_app()
    app._chat_lines = [("user", "hi"), ("assistant", "done")]
    app._chat_streaming = ""
    t = app._render_chat()
    assert "▋" not in t.plain


# ---------------------------------------------------------------------------
# _on_chat_token / _on_chat_done / _on_chat_error
# ---------------------------------------------------------------------------

class _FakeStatic:
    def __init__(self):
        self.last_update = None
    def update(self, val):
        self.last_update = val


class _FakeScroll:
    def scroll_end(self, **_):
        pass


def _patched_app() -> _ChatApp:
    app = _make_app()

    def _fake_update_display():
        pass  # no-op; avoid DOM access

    app._update_chat_display = _fake_update_display
    return app


def test_on_chat_token_accumulates():
    app = _patched_app()
    app._on_chat_token("hel")
    app._on_chat_token("lo")
    assert app._chat_streaming == "hello"


def test_on_chat_done_moves_to_history():
    app = _patched_app()
    app._chat_lines = [("user", "q")]
    app._chat_streaming = "partial"
    app._on_chat_done("full answer")
    assert app._chat_streaming == ""
    assert app._chat_lines[-1] == ("assistant", "full answer")


def test_on_chat_error_adds_error_line():
    app = _patched_app()
    app._chat_lines = [("user", "q")]
    app._chat_streaming = "partial"

    # Patch notify so it doesn't need a running app.
    app.notify = MagicMock()
    app._on_chat_error("connection refused")

    assert app._chat_streaming == ""
    last_role, last_text = app._chat_lines[-1]
    assert last_role == "assistant"
    assert "Error" in last_text
    assert "connection refused" in last_text
    app.notify.assert_called_once()


# ---------------------------------------------------------------------------
# _run_chat (unit-test the thread task with mocked ollama)
# ---------------------------------------------------------------------------

def _fake_worker(cancelled: bool = False):
    w = MagicMock()
    w.is_cancelled = cancelled
    return w


def test_run_chat_streams_tokens():
    app = _make_app()
    app._chat_lines = [("user", "hi")]

    tokens_received: list[str] = []
    done_received: list[str] = []

    app.call_from_thread = lambda fn, *a, **kw: fn(*a, **kw)  # execute inline

    # Patch on_chat_token/done to capture calls.
    def capture_token(t): tokens_received.append(t)
    def capture_done(t): done_received.append(t)
    app._on_chat_token = capture_token
    app._on_chat_done = capture_done

    fake_chunks = [
        {"message": {"content": "hel"}},
        {"message": {"content": "lo"}},
    ]

    with patch("ghstack_tui.app._ollama.chat", return_value=iter(fake_chunks)):
        with patch("ghstack_tui.app.get_current_worker", return_value=_fake_worker()):
            task = app._run_chat()
            task()

    assert tokens_received == ["hel", "lo"]
    assert done_received == ["hello"]


def test_run_chat_handles_ollama_error():
    app = _make_app()
    app._chat_lines = [("user", "hi")]

    errors: list[str] = []
    app.call_from_thread = lambda fn, *a, **kw: fn(*a, **kw)
    app._on_chat_error = lambda msg: errors.append(msg)

    with patch("ghstack_tui.app._ollama.chat", side_effect=RuntimeError("no model")):
        with patch("ghstack_tui.app.get_current_worker", return_value=_fake_worker()):
            task = app._run_chat()
            task()

    assert errors == ["no model"]


def test_run_chat_cancels_mid_stream():
    app = _make_app()
    app._chat_lines = [("user", "hi")]

    tokens: list[str] = []
    app.call_from_thread = lambda fn, *a, **kw: fn(*a, **kw)
    app._on_chat_token = lambda t: tokens.append(t)
    app._on_chat_done = lambda t: (_ for _ in ()).throw(AssertionError("should not be called"))

    # Worker is cancelled from the start.
    worker = _fake_worker(cancelled=True)
    fake_chunks = [{"message": {"content": "hel"}}]

    with patch("ghstack_tui.app._ollama.chat", return_value=iter(fake_chunks)):
        with patch("ghstack_tui.app.get_current_worker", return_value=worker):
            task = app._run_chat()
            task()

    assert tokens == []  # cancelled before first token processed

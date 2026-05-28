"""Modal dialogs: checkout path picker, full-screen diff viewer, ask-Claude prompt."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static
from textual.worker import get_current_worker

from ghstack_tui.gh_client import GhError, fetch_pr_diff
from ghstack_tui.render import render_diff

# Ultimate fallback used when the caller doesn't supply a default path.
# In normal use, the app passes a Config-derived value here.
_DEFAULT_CHECKOUT_PATH = "~/git/pytorch313"


class CheckoutModal(ModalScreen):
    """Floating dialog: enter repo path, then run ghstack checkout <pr_num>."""

    DEFAULT_CSS = """
    CheckoutModal {
        align: center middle;
    }
    #_co_box {
        width: 64;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #_co_title { text-style: bold; margin-bottom: 1; }
    #_co_hint  { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(
        self,
        pr_num: int,
        repo_slug: str | None,
        default_path: str | None = None,
    ) -> None:
        super().__init__()
        self._pr_num = pr_num
        self._repo_slug = repo_slug or "?"
        self._default_path = default_path or _DEFAULT_CHECKOUT_PATH

    def compose(self) -> ComposeResult:
        with Vertical(id="_co_box"):
            yield Label(
                f"ghstack checkout  PR #{self._pr_num}  ({self._repo_slug})",
                id="_co_title",
            )
            yield Input(
                value=self._default_path,
                placeholder="Path to repo",
                id="_co_input",
            )
            yield Label("↵ confirm   esc cancel", id="_co_hint")

    def on_mount(self) -> None:
        self.query_one("#_co_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DiffModal(ModalScreen):
    """Full-screen diff overlay. Fetches `gh pr diff` in background, renders colored."""

    DEFAULT_CSS = """
    DiffModal { align: center middle; }
    #_dm_box {
        width: 98%;
        height: 95%;
        border: thick $accent;
        background: $surface;
    }
    #_dm_header {
        height: 2;
        padding: 0 1;
        background: $panel;
    }
    #_dm_title  { text-style: bold; }
    #_dm_status { color: $text-muted; }
    #_dm_scroll { height: 1fr; }
    #_dm_text   { padding: 0 1; }
    """

    BINDINGS = [
        Binding("q", "close", "Close"),
        Binding("escape", "close", "Close"),
        Binding("j", "scroll_down", "↓", show=False),
        Binding("k", "scroll_up", "↑", show=False),
    ]

    def __init__(self, pr_num: int, repo_slug: str, subject: str = "") -> None:
        super().__init__()
        self._pr_num = pr_num
        self._repo_slug = repo_slug
        self._subject = subject

    def compose(self) -> ComposeResult:
        with Vertical(id="_dm_box"):
            with Horizontal(id="_dm_header"):
                yield Label(
                    f"Diff  PR #{self._pr_num}  ({self._repo_slug})"
                    + (f"  {self._subject[:60]}" if self._subject else ""),
                    id="_dm_title",
                )
                yield Label("Loading…", id="_dm_status")
            with VerticalScroll(id="_dm_scroll"):
                yield Static("", id="_dm_text")

    def on_mount(self) -> None:
        self.run_worker(self._fetch(), thread=True, name="diff-fetch")

    def _fetch(self):
        def task() -> None:
            worker = get_current_worker()
            try:
                raw = fetch_pr_diff(self._repo_slug, self._pr_num)
            except (GhError, OSError) as exc:
                if not worker.is_cancelled:
                    self.app.call_from_thread(self._on_error, str(exc))
                return
            if not worker.is_cancelled:
                self.app.call_from_thread(self._on_ready, raw)
        return task

    def _on_ready(self, raw: str) -> None:
        lines = raw.count("\n")
        self.query_one("#_dm_status", Label).update(
            Text(f"{lines} lines   q/esc close", style="dim")
        )
        self.query_one("#_dm_text", Static).update(render_diff(raw))

    def _on_error(self, msg: str) -> None:
        self.query_one("#_dm_status", Label).update(
            Text(f"Error: {msg}", style="red")
        )

    def action_close(self) -> None:
        self.dismiss()

    def action_scroll_down(self) -> None:
        self.query_one("#_dm_scroll", VerticalScroll).scroll_relative(y=3)

    def action_scroll_up(self) -> None:
        self.query_one("#_dm_scroll", VerticalScroll).scroll_relative(y=-3)


class AskClaudeModal(ModalScreen):
    """Floating dialog: show failing CI context, pick repo path + initial prompt, spawn claude."""

    DEFAULT_CSS = """
    AskClaudeModal { align: center middle; }
    #_cc_box {
        width: 80;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #_cc_title   { text-style: bold; margin-bottom: 1; }
    #_cc_failing { color: $error; margin-bottom: 1; }
    .cc_lbl      { color: $text-muted; margin-top: 1; }
    #_cc_hint    { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(
        self,
        pr_num: int | None,
        repo_slug: str | None,
        failing: list[str],
        default_prompt: str = "",
        default_path: str | None = None,
    ) -> None:
        super().__init__()
        self._pr_num = pr_num
        self._repo_slug = repo_slug or "?"
        self._failing = failing
        self._default_prompt = default_prompt
        self._default_path = default_path or _DEFAULT_CHECKOUT_PATH

    def compose(self) -> ComposeResult:
        with Vertical(id="_cc_box"):
            yield Label(
                f"Ask Claude  PR #{self._pr_num}  ({self._repo_slug})",
                id="_cc_title",
            )
            if self._failing:
                shown = self._failing[:6]
                extra = len(self._failing) - len(shown)
                lines = "\n".join(f"  ✗ {j}" for j in shown)
                if extra:
                    lines += f"\n  … +{extra} more"
                yield Label(lines, id="_cc_failing")
            yield Label("Repo path:", classes="cc_lbl")
            yield Input(
                value=self._default_path,
                placeholder="Path to repo",
                id="_cc_path",
            )
            yield Label("Initial prompt (editable):", classes="cc_lbl")
            yield Input(
                value=self._default_prompt,
                placeholder="What should Claude do?",
                id="_cc_prompt",
            )
            yield Label(
                "↵ on prompt launches Claude   tab switches fields   esc cancel",
                id="_cc_hint",
            )

    def on_mount(self) -> None:
        self.query_one("#_cc_prompt", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "_cc_path":
            self.query_one("#_cc_prompt", Input).focus()
            return
        path = self.query_one("#_cc_path", Input).value.strip()
        prompt = self.query_one("#_cc_prompt", Input).value.strip()
        self.dismiss((path or None, prompt or None))

    def action_cancel(self) -> None:
        self.dismiss(None)

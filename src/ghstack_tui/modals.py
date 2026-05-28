"""Modal dialogs: checkout path picker, full-screen diff viewer, ask-Claude prompt."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static
from textual.worker import get_current_worker

from ghstack_tui.gh_client import GhError, fetch_pr_diff
from ghstack_tui.render import render_diff
from ghstack_tui.config import get_config


def _parse_diff_files(raw: str) -> list[tuple[str, str, str]]:
    """Split unified diff into (old_content, new_content, display_name) per file."""
    result = []
    old_lines: list[str] = []
    new_lines: list[str] = []
    display = ""
    in_hunk = False

    for line in raw.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if display:
                result.append(("".join(old_lines), "".join(new_lines), display))
            old_lines, new_lines, display, in_hunk = [], [], "", False
        elif line.startswith("--- "):
            path = line[4:].rstrip()
            if path.startswith("a/"):
                path = path[2:]
            if path != "/dev/null":
                display = display or path
        elif line.startswith("+++ "):
            path = line[4:].rstrip()
            if path.startswith("b/"):
                path = path[2:]
            if path != "/dev/null":
                display = path
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk:
            if line.startswith("-"):
                old_lines.append(line[1:])
            elif line.startswith("+"):
                new_lines.append(line[1:])
            elif line.startswith(" "):
                old_lines.append(line[1:])
                new_lines.append(line[1:])
            elif not line.startswith("\\"):
                in_hunk = False

    if display:
        result.append(("".join(old_lines), "".join(new_lines), display))
    return result


def _render_diff_output(raw: str) -> tuple[Text, str]:
    if shutil.which("difft"):
        try:
            parts = _parse_diff_files(raw)
            if parts:
                chunks: list[str] = []
                with tempfile.TemporaryDirectory() as tmpdir:
                    for old_content, new_content, name in parts:
                        suffix = Path(name).suffix or ".txt"
                        old_f = Path(tmpdir) / f"old{suffix}"
                        new_f = Path(tmpdir) / f"new{suffix}"
                        old_f.write_text(old_content)
                        new_f.write_text(new_content)
                        proc = subprocess.run(
                            ["difft", "--color", "always", str(old_f), str(new_f)],
                            capture_output=True,
                            text=True,
                            timeout=15,
                        )
                        if proc.stdout:
                            chunks.append(proc.stdout)
                if chunks:
                    return Text.from_ansi("\n".join(chunks)), "difftastic"
        except (OSError, subprocess.TimeoutExpired):
            pass
    return render_diff(raw), "unified"


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
        self._default_path = default_path or get_config().paths.default_checkout_path

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


class CheckoutOutputModal(ModalScreen):
    """Streams live output of `ghstack checkout` into a scrollable modal."""

    DEFAULT_CSS = """
    CheckoutOutputModal { align: center middle; }
    #_cout_box {
        width: 80;
        height: 24;
        border: thick $accent;
        background: $surface;
    }
    #_cout_header {
        height: 2;
        padding: 0 1;
        background: $panel;
    }
    #_cout_title  { text-style: bold; }
    #_cout_status { color: $text-muted; }
    #_cout_scroll { height: 1fr; }
    #_cout_text   { padding: 0 1; }
    #_cout_hint   { height: 1; padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [Binding("escape", "close", "Close", show=False)]

    def __init__(self, pr_num: int, repo_slug: str | None, repo_path: str) -> None:
        super().__init__()
        self._pr_num = pr_num
        self._repo_slug = repo_slug or "?"
        self._repo_path = str(Path(repo_path).expanduser())
        self._done = False

    def compose(self) -> ComposeResult:
        with Vertical(id="_cout_box"):
            with Horizontal(id="_cout_header"):
                yield Label(
                    f"ghstack checkout  PR #{self._pr_num}  ({self._repo_slug})",
                    id="_cout_title",
                )
                yield Label("Running…", id="_cout_status")
            with VerticalScroll(id="_cout_scroll"):
                yield Static("", id="_cout_text")
            yield Label("esc close", id="_cout_hint")

    def on_mount(self) -> None:
        self.run_worker(self._run(), thread=True, name="checkout-output")

    def _run(self):
        def task() -> None:
            worker = get_current_worker()
            lines: list[str] = []

            def _flush() -> None:
                self.app.call_from_thread(
                    self.query_one("#_cout_text", Static).update,
                    "\n".join(lines),
                )
                self.app.call_from_thread(
                    self.query_one("#_cout_scroll", VerticalScroll).scroll_end,
                    animate=False,
                )

            try:
                proc = subprocess.Popen(
                    ["ghstack", "checkout", str(self._pr_num)],
                    cwd=self._repo_path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except FileNotFoundError:
                self.app.call_from_thread(self._on_done, 127, ["ghstack not found in PATH"])
                return
            except OSError as exc:
                self.app.call_from_thread(self._on_done, 1, [str(exc)])
                return

            assert proc.stdout is not None
            for line in proc.stdout:
                if worker.is_cancelled:
                    proc.terminate()
                    return
                lines.append(line.rstrip())
                _flush()

            returncode = proc.wait()
            if not worker.is_cancelled:
                self.app.call_from_thread(self._on_done, returncode, lines)

        return task

    def _on_done(self, returncode: int, lines: list[str]) -> None:
        self._done = True
        self.query_one("#_cout_text", Static).update("\n".join(lines))
        if returncode == 0:
            self.query_one("#_cout_status", Label).update(
                Text("Done ✓", style="green")
            )
        else:
            self.query_one("#_cout_status", Label).update(
                Text(f"Failed (exit {returncode}) ✗", style="red")
            )

    def action_close(self) -> None:
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
        rendered, mode = _render_diff_output(raw)
        self.query_one("#_dm_status", Label).update(
            Text(f"{lines} lines   {mode}   q/esc close", style="dim")
        )
        self.query_one("#_dm_text", Static).update(rendered)

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
        self._default_path = default_path or get_config().paths.default_checkout_path

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

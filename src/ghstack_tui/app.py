import subprocess
from datetime import datetime, timezone
from pathlib import Path

from rich.markup import escape as markup_escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, Markdown, Static
from textual.worker import Worker, get_current_worker

from ghstack_tui import detail as detail_render
from ghstack_tui.gh_client import (
    DEFAULT_QUERY,
    fetch_pr_details,
    fetch_pr_full,
    load_stacks,
)
from ghstack_tui.models import Commit, Stack


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

    def __init__(self, pr_num: int, repo_slug: str | None) -> None:
        super().__init__()
        self._pr_num = pr_num
        self._repo_slug = repo_slug or "?"

    def compose(self) -> ComposeResult:
        with Vertical(id="_co_box"):
            yield Label(
                f"ghstack checkout  PR #{self._pr_num}  ({self._repo_slug})",
                id="_co_title",
            )
            yield Input(
                value=_DEFAULT_CHECKOUT_PATH,
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


class GhstackTUI(App):
    """Three-pane viewer for ghstack stacks.

    Top bar:    GitHub PR search query input.
    Left:       stacks list.
    Right top:  PRs in selected stack, gh-dash-style (labels/CI/comments/±/upd).
    Right bot:  detail panel for the selected PR — header, body (markdown),
                checks list, reviewers, top changed files. Filled lazily by a
                background worker; previous in-flight fetch is cancelled when
                the cursor moves on.
    """

    CSS = """
    #query { dock: top; height: 3; border: solid $accent; }
    #main { height: 1fr; }
    #stacks  { width: 30%; border: solid $accent; }
    #right_col { width: 70%; }
    #commits { height: 40%; border: solid $accent; }
    #detail  { height: 60%; border: solid $accent; padding: 0 1; }
    DataTable { height: 1fr; }
    #status { padding: 0 2; color: $text-muted; height: 1; }
    #detail_header { padding: 0 0 1 0; }
    #detail_meta { padding: 0 0 1 0; }
    #detail_body { background: $surface; max-height: 50%; overflow-y: auto; }
    .section_title { color: $accent; text-style: bold; padding: 1 0 0 0; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("tab", "focus_next_pane", "Switch pane"),
        Binding("ctrl+l", "focus_right", "→ pane"),
        Binding("ctrl+h", "focus_left", "← pane"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("r", "reload", "Reload"),
        Binding("c", "checkout", "Checkout"),
        Binding("/", "focus_query", "Edit query"),
        Binding("escape", "blur_query", "Leave query", show=False),
    ]

    _RIGHT_COLS = ("PR", "Title", "Labels", "CI", "💬", "±", "Upd")

    def __init__(self, query: str | None = None) -> None:
        super().__init__()
        self.query_str = query or DEFAULT_QUERY
        self.stacks: list[Stack] = []
        self._current_stack_idx = 0
        self._enrich_worker: Worker | None = None
        self._detail_worker: Worker | None = None
        self._detail_cache: dict[tuple[str, int], dict] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Input(value=self.query_str, placeholder="GitHub PR search query", id="query")
        with Horizontal(id="main"):
            yield DataTable(id="stacks", cursor_type="row", zebra_stripes=True)
            with Vertical(id="right_col"):
                yield DataTable(id="commits", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="detail"):
                    yield Static("", id="detail_header")
                    yield Static("", id="detail_meta")
                    yield Static("Body", classes="section_title")
                    yield Markdown("", id="detail_body")
                    yield Static("Checks", classes="section_title")
                    yield Static("", id="detail_checks")
                    yield Static("Reviewers", classes="section_title")
                    yield Static("", id="detail_reviewers")
                    yield Static("Files", classes="section_title")
                    yield Static("", id="detail_files")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "ghstack-tui"

        stacks_t: DataTable = self.query_one("#stacks", DataTable)
        stacks_t.add_columns("Top PR", "#", "Title")

        commits_t: DataTable = self.query_one("#commits", DataTable)
        commits_t.add_columns(*self._RIGHT_COLS)

        self._load()
        stacks_t.focus()

    # --- data loading -----------------------------------------------------

    def _load(self) -> None:
        status = self.query_one("#status", Static)
        status.update(f"Searching: {self.query_str}")
        try:
            self.stacks = load_stacks(self.query_str)
        except Exception as exc:  # noqa: BLE001
            status.update(f"Error: {exc}")
            return

        stacks_t: DataTable = self.query_one("#stacks", DataTable)
        stacks_t.clear()
        for s in self.stacks:
            stacks_t.add_row(
                f"#{s.top_pr}" if s.top_pr is not None else "—",
                str(len(s.commits)),
                _truncate(s.title, 80),
            )

        if self.stacks:
            status.update(f"{len(self.stacks)} stacks  ({self.query_str})")
        else:
            status.update(f"No ghstack PRs matched: {self.query_str}")
        self._show_stack(0)

    def _show_stack(self, idx: int) -> None:
        self._current_stack_idx = idx
        commits_t: DataTable = self.query_one("#commits", DataTable)
        commits_t.clear()
        if not (0 <= idx < len(self.stacks)):
            return
        for c in self.stacks[idx].commits:
            commits_t.add_row(*_row_for(c))
        self._kick_row_enrichment(idx)
        # Also kick the detail panel for the (newly-current) row 0.
        if self.stacks[idx].commits:
            self._show_detail_for(self.stacks[idx].commits[0])

    # --- background enrichment (right-pane row CI / diff) -----------------

    def _kick_row_enrichment(self, idx: int) -> None:
        if self._enrich_worker is not None and self._enrich_worker.is_running:
            self._enrich_worker.cancel()
        stack = self.stacks[idx]
        if all(c.enriched or c.repo_slug is None or c.pr_num is None for c in stack.commits):
            return
        self._enrich_worker = self.run_worker(
            self._enrich_stack(idx),
            thread=True,
            exclusive=True,
            name=f"enrich-{idx}",
        )

    def _enrich_stack(self, idx: int):
        def task() -> None:
            worker = get_current_worker()
            stack = self.stacks[idx]
            for row_idx, c in enumerate(stack.commits):
                if worker.is_cancelled:
                    return
                if c.enriched or c.repo_slug is None or c.pr_num is None:
                    continue
                try:
                    detail = fetch_pr_details(c.repo_slug, c.pr_num)
                except Exception:  # noqa: BLE001
                    continue
                for k, v in detail.items():
                    setattr(c, k, v)
                self.call_from_thread(self._update_right_row, idx, row_idx, c)
        return task

    def _update_right_row(self, stack_idx: int, row_idx: int, c: Commit) -> None:
        if stack_idx != self._current_stack_idx:
            return
        commits_t: DataTable = self.query_one("#commits", DataTable)
        if row_idx >= commits_t.row_count:
            return
        for col_idx, val in enumerate(_row_for(c)):
            commits_t.update_cell_at((row_idx, col_idx), val, update_width=False)

    # --- background enrichment (detail panel) -----------------------------

    def _show_detail_for(self, c: Commit) -> None:
        # Render whatever we have synchronously (title + state from the row),
        # then kick a worker to fill in the deep view.
        header = self.query_one("#detail_header", Static)
        meta = self.query_one("#detail_meta", Static)
        body = self.query_one("#detail_body", Markdown)
        checks = self.query_one("#detail_checks", Static)
        reviewers = self.query_one("#detail_reviewers", Static)
        files = self.query_one("#detail_files", Static)

        header.update(Text.from_markup(
            f"[bold]#{c.pr_num}[/] [bold white]{markup_escape(c.subject)}[/]"
            + (" [yellow]DRAFT[/]" if c.is_draft else "")
        ))
        meta.update(Text("Loading…", style="dim"))
        body.update("")
        checks.update(Text("…", style="dim"))
        reviewers.update(Text("…", style="dim"))
        files.update(Text("…", style="dim"))

        if c.repo_slug is None or c.pr_num is None:
            meta.update(Text("(PR not in current query window)", style="dim"))
            return

        key = (c.repo_slug, c.pr_num)
        cached = self._detail_cache.get(key)
        if cached is not None:
            self._render_detail(cached)
            return

        if self._detail_worker is not None and self._detail_worker.is_running:
            self._detail_worker.cancel()
        self._detail_worker = self.run_worker(
            self._fetch_detail(c.repo_slug, c.pr_num),
            thread=True,
            exclusive=True,
            name=f"detail-{c.pr_num}",
        )

    def _fetch_detail(self, repo_slug: str, pr_num: int):
        def task() -> None:
            worker = get_current_worker()
            try:
                data = fetch_pr_full(repo_slug, pr_num)
            except Exception as exc:  # noqa: BLE001
                if not worker.is_cancelled:
                    self.call_from_thread(self._render_detail_error, str(exc))
                return
            if worker.is_cancelled:
                return
            self._detail_cache[(repo_slug, pr_num)] = data
            self.call_from_thread(self._render_detail, data)
        return task

    def _render_detail(self, data: dict) -> None:
        header = self.query_one("#detail_header", Static)
        meta = self.query_one("#detail_meta", Static)
        body = self.query_one("#detail_body", Markdown)
        checks = self.query_one("#detail_checks", Static)
        reviewers = self.query_one("#detail_reviewers", Static)
        files = self.query_one("#detail_files", Static)

        header.update(detail_render.render_header(data))
        meta.update(detail_render.render_meta(data))
        body.update(data.get("body") or "_(no description)_")
        checks.update(detail_render.render_checks(data))
        reviewers.update(detail_render.render_reviewers(data))
        files.update(detail_render.render_files(data))

    def _render_detail_error(self, msg: str) -> None:
        self.query_one("#detail_meta", Static).update(Text(f"Detail fetch failed: {msg}", style="red"))

    # --- event handlers ---------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "stacks":
            self._show_stack(event.cursor_row)
        elif event.data_table.id == "commits":
            stack = self.stacks[self._current_stack_idx] if self.stacks else None
            if stack and 0 <= event.cursor_row < len(stack.commits):
                self._show_detail_for(stack.commits[event.cursor_row])

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "query":
            self.query_str = event.value.strip() or DEFAULT_QUERY
            self.query_one("#stacks", DataTable).focus()
            self._load()

    # --- actions ----------------------------------------------------------

    def action_focus_query(self) -> None:
        self.query_one("#query", Input).focus()

    def action_blur_query(self) -> None:
        if isinstance(self.focused, Input):
            self.query_one("#stacks", DataTable).focus()

    def action_focus_next_pane(self) -> None:
        focused = self.focused
        if isinstance(focused, Input):
            return
        target_id = "commits" if (focused is not None and focused.id == "stacks") else "stacks"
        self.query_one(f"#{target_id}", DataTable).focus()

    def action_focus_left(self) -> None:
        self.query_one("#stacks", DataTable).focus()

    def action_focus_right(self) -> None:
        self.query_one("#commits", DataTable).focus()

    def action_cursor_down(self) -> None:
        focused = self.focused
        if isinstance(focused, DataTable):
            focused.action_cursor_down()

    def action_cursor_up(self) -> None:
        focused = self.focused
        if isinstance(focused, DataTable):
            focused.action_cursor_up()

    def action_reload(self) -> None:
        self._detail_cache.clear()
        self._load()

    def action_checkout(self) -> None:
        commit = self._get_selected_commit()
        if commit is None or commit.pr_num is None:
            self.notify("No PR selected", severity="warning")
            return
        self.push_screen(
            CheckoutModal(commit.pr_num, commit.repo_slug),
            self._on_checkout_path,
        )

    def _on_checkout_path(self, repo_path: str | None) -> None:
        if repo_path is None:
            return
        commit = self._get_selected_commit()
        if commit is None or commit.pr_num is None:
            self.notify("No PR selected", severity="warning")
            return
        self.run_worker(
            self._run_checkout(commit.pr_num, repo_path),
            thread=True,
            name=f"checkout-{commit.pr_num}",
        )

    def _run_checkout(self, pr_num: int, repo_path: str):
        def task() -> None:
            expanded = str(Path(repo_path).expanduser())
            self.call_from_thread(
                self.notify, f"Running ghstack checkout {pr_num} in {expanded}…"
            )
            try:
                result = subprocess.run(
                    ["ghstack", "checkout", str(pr_num)],
                    cwd=expanded,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except FileNotFoundError:
                self.call_from_thread(
                    self.notify, "ghstack not found in PATH", severity="error"
                )
                return
            except Exception as exc:  # noqa: BLE001
                self.call_from_thread(self.notify, str(exc), severity="error")
                return
            if result.returncode == 0:
                msg = result.stdout.strip() or f"Checked out PR #{pr_num}"
                self.call_from_thread(self.notify, msg, title="ghstack checkout ✓")
            else:
                err = (result.stderr.strip() or result.stdout.strip() or "unknown error")
                self.call_from_thread(
                    self.notify, err, title="ghstack checkout ✗", severity="error"
                )

        return task

    def _get_selected_commit(self) -> "Commit | None":
        if not self.stacks:
            return None
        stack = self.stacks[self._current_stack_idx]
        commits_t = self.query_one("#commits", DataTable)
        row = commits_t.cursor_row
        if 0 <= row < len(stack.commits):
            return stack.commits[row]
        return None


# --- row rendering --------------------------------------------------------


def _row_for(c: Commit) -> tuple:
    pr = Text(f"#{c.pr_num}" if c.pr_num is not None else "—")
    if c.is_draft:
        pr.stylize("yellow")
    title = _truncate(c.subject, 50)
    return (
        pr,
        title,
        _labels_pretty(c.labels),
        _ci_pretty(c),
        str(c.comments_count) if c.comments_count else "",
        _diff_pretty(c),
        _rel_time(c.updated_at),
    )


_LABEL_PRIORITY_PREFIXES = ("ciflow/", "release/", "topic:", "module:")


def _labels_pretty(labels: list[str]) -> Text:
    if not labels:
        return Text("")
    chosen: list[str] = []
    for prio in _LABEL_PRIORITY_PREFIXES:
        for lbl in labels:
            if lbl.startswith(prio) and lbl not in chosen:
                chosen.append(_short_label(lbl))
                break
        if len(chosen) >= 2:
            break
    for lbl in labels:
        if len(chosen) >= 2:
            break
        sl = _short_label(lbl)
        if sl not in chosen:
            chosen.append(sl)
    return Text(", ".join(chosen))


def _short_label(lbl: str) -> str:
    for pfx in ("module: ", "topic: ", "ciflow/"):
        if lbl.startswith(pfx):
            return lbl[len(pfx):]
    return lbl


def _ci_pretty(c: Commit) -> Text:
    if not c.enriched:
        return Text("…", style="dim")
    if c.ci_ok == c.ci_fail == c.ci_pending == 0:
        return Text("—", style="dim")
    t = Text()
    if c.ci_ok:
        t.append(f"✓{c.ci_ok} ", style="green")
    if c.ci_fail:
        t.append(f"✗{c.ci_fail} ", style="red")
    if c.ci_pending:
        t.append(f"●{c.ci_pending}", style="yellow")
    return t


def _diff_pretty(c: Commit) -> Text:
    if c.additions is None or c.deletions is None:
        return Text("…", style="dim")
    t = Text()
    t.append(f"+{c.additions}", style="green")
    t.append(" ")
    t.append(f"-{c.deletions}", style="red")
    return t


def _rel_time(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    delta = datetime.now(timezone.utc) - dt
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"

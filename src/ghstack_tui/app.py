import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from rich.markup import escape as markup_escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Markdown,
    Static,
    TabbedContent,
    TabPane,
)
from textual.worker import Worker, get_current_worker

from ghstack_tui import clones as clones_mod
from ghstack_tui import detail as detail_render
from ghstack_tui import paths
from ghstack_tui import render as render_mod
from ghstack_tui import triage as triage_mod
from ghstack_tui.clones import CloneInfo
from ghstack_tui.config import Config
from ghstack_tui.detail import get_failing_jobs
from ghstack_tui.gh_client import (
    DEFAULT_QUERY,
    GhError,
    fetch_check_annotations,
    fetch_drci_failures,
    fetch_failed_tests,
    fetch_merge_signal,
    fetch_pr_details,
    fetch_pr_diff,
    fetch_pr_full,
    load_stacks,
)
from ghstack_tui.models import Commit, Stack
from ghstack_tui.modals import AskClaudeModal, CheckoutModal, DiffModal


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
    TabbedContent { height: 1fr; }
    #query { dock: top; height: 3; border: solid $accent; }
    #main { height: 1fr; }
    #stacks  { width: 30%; border: solid $accent; }
    #right_col { width: 70%; }
    #commits { height: 40%; border: solid $accent; }
    #detail  { height: 60%; border: solid $accent; padding: 0 1; }
    #clones_table { height: 1fr; border: solid $accent; }
    DataTable { height: 1fr; }
    #status { padding: 0 2; color: $text-muted; height: 1; }
    #detail_header { padding: 0 0 1 0; }
    #detail_meta { padding: 0 0 1 0; }
    #detail_body { background: $surface; max-height: 50%; overflow-y: auto; }
    .section_title { color: $accent; text-style: bold; padding: 1 0 0 0; }
    #ci_fail_title { display: none; }
    #detail_ci_failures { display: none; padding: 0 0 1 0; }
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
        Binding("a", "ask_claude", "Ask Claude"),
        Binding("f", "show_failing_tests", "Failing tests"),
        Binding("d", "diff", "Diff"),
        Binding("v", "view_in_editor", "View diff"),
        Binding("o", "open_in_browser", "Open PR"),
        Binding("/", "focus_query", "Edit query"),
        Binding("t", "toggle_tab", "Tab", priority=True),
        Binding("escape", "blur_query", "Leave query", show=False),
    ]

    _RIGHT_COLS = ("PR", "Title", "Labels", "CI", "💬", "±", "Upd")
    _STACK_COLS = ("Top PR", "#", "Title")
    _CLONES_COLS = ("Path", "Branch", "Repo", "PR", "Subject", "✎")

    def __init__(self, query: str | None = None) -> None:
        super().__init__()
        self.config = Config.load()
        self.query_str = query or self.config.default_query or DEFAULT_QUERY
        self.stacks: list[Stack] = []
        self._current_stack_idx = 0
        self._enrich_worker: Worker | None = None
        self._detail_worker: Worker | None = None
        self._detail_cache: dict[tuple[str, int], dict] = {}
        self._clones: list[CloneInfo] = []
        self._clones_scanned: bool = False
        self._clones_worker: Worker | None = None
        self._triage_cache = triage_mod.TriageCache()
        self._triage_worker: Worker | None = None
        # (repo_slug, pr_num) -> {check_run_id: [annotation, ...]}
        self._failing_annotations: dict[tuple[str, int], dict[int, list[dict]]] = {}
        # (repo_slug, pr_num) -> {job_name: [failure_capture, ...]} from Dr.CI bot.
        self._drci_failures: dict[tuple[str, int], dict[str, list[str]]] = {}
        self._annotations_worker: Worker | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with TabbedContent(initial="tab-stacks", id="tabs"):
            with TabPane("Stacks", id="tab-stacks"):
                yield Input(
                    value=self.query_str,
                    placeholder="GitHub PR search query",
                    id="query",
                )
                with Horizontal(id="main"):
                    yield DataTable(id="stacks", cursor_type="row", zebra_stripes=True)
                    with Vertical(id="right_col"):
                        yield DataTable(id="commits", cursor_type="row", zebra_stripes=True)
                        with VerticalScroll(id="detail"):
                            yield Static("", id="detail_header")
                            yield Static("", id="detail_meta")
                            yield Static("Failing CI", classes="section_title", id="ci_fail_title")
                            yield Static("", id="detail_ci_failures")
                            yield Static("Body", classes="section_title")
                            yield Markdown("", id="detail_body")
                            yield Static("Checks", classes="section_title")
                            yield Static("", id="detail_checks")
                            yield Static("Reviewers", classes="section_title")
                            yield Static("", id="detail_reviewers")
                            yield Static("Files", classes="section_title")
                            yield Static("", id="detail_files")
            with TabPane("Clones", id="tab-clones"):
                yield DataTable(
                    id="clones_table", cursor_type="row", zebra_stripes=True
                )
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "ghstack-tui"

        stacks_t: DataTable = self.query_one("#stacks", DataTable)
        stacks_t.add_columns(*self._STACK_COLS)

        commits_t: DataTable = self.query_one("#commits", DataTable)
        commits_t.add_columns(*self._RIGHT_COLS)

        clones_t: DataTable = self.query_one("#clones_table", DataTable)
        clones_t.add_columns(*self._CLONES_COLS)

        self._load()
        stacks_t.focus()

    # --- data loading -----------------------------------------------------

    def _load(self) -> None:
        status = self.query_one("#status", Static)
        status.update(f"Searching: {self.query_str}")
        try:
            self.stacks = load_stacks(self.query_str)
        except (GhError, json.JSONDecodeError, OSError) as exc:
            status.update(f"Error: {exc}")
            self.notify(str(exc), title="Load failed", severity="error")
            return

        stacks_t: DataTable = self.query_one("#stacks", DataTable)
        stacks_t.clear()
        for s in self.stacks:
            stacks_t.add_row(*render_mod.stack_row(s))

        if self.stacks:
            status.update(f"{len(self.stacks)} stacks  ({self.query_str})")
        else:
            status.update(f"No ghstack PRs matched: {self.query_str}")
        self._show_stack(0)
        self._apply_triage_cache_all()
        self._kick_triage_all()

    def _show_stack(self, idx: int) -> None:
        self._current_stack_idx = idx
        commits_t: DataTable = self.query_one("#commits", DataTable)
        commits_t.clear()
        if not (0 <= idx < len(self.stacks)):
            return
        for c in self.stacks[idx].commits:
            commits_t.add_row(*render_mod.commit_row(c))
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
            errors = 0
            last_err = ""
            for row_idx, c in enumerate(stack.commits):
                if worker.is_cancelled:
                    return
                if c.enriched or c.repo_slug is None or c.pr_num is None:
                    continue
                try:
                    detail = fetch_pr_details(c.repo_slug, c.pr_num)
                except (GhError, json.JSONDecodeError, OSError) as exc:
                    errors += 1
                    last_err = str(exc)
                    continue
                for k, v in detail.items():
                    setattr(c, k, v)
                c.verdict, c.verdict_reason = triage_mod.verdict_for(c)
                if c.updated_at:
                    self._triage_cache.put(
                        c.repo_slug, c.pr_num, c.updated_at, detail
                    )
                self.call_from_thread(self._update_right_row, idx, row_idx, c)
            if errors and not worker.is_cancelled:
                self.call_from_thread(
                    self.notify,
                    f"{errors} PR(s) failed to enrich: {last_err}",
                    severity="warning",
                )
        return task

    def _update_right_row(self, stack_idx: int, row_idx: int, c: Commit) -> None:
        if stack_idx != self._current_stack_idx:
            return
        commits_t: DataTable = self.query_one("#commits", DataTable)
        if row_idx >= commits_t.row_count:
            return
        for col_idx, val in enumerate(render_mod.commit_row(c)):
            commits_t.update_cell_at((row_idx, col_idx), val, update_width=False)

    # --- triage (needs-attention badges across all stacks) ---------------

    def _apply_triage_cache_all(self) -> None:
        """Rehydrate enrichment from cache for every commit, where the cache
        entry's updated_at matches the PR's current updated_at. This makes
        badges appear immediately for unchanged PRs without any gh calls.
        """
        for stack in self.stacks:
            for c in stack.commits:
                if c.repo_slug is None or c.pr_num is None or not c.updated_at:
                    continue
                cached = self._triage_cache.get(c.repo_slug, c.pr_num, c.updated_at)
                if cached is None:
                    continue
                for k, v in cached.items():
                    setattr(c, k, v)
                c.verdict, c.verdict_reason = triage_mod.verdict_for(c)
        # Repaint commits table for the currently-shown stack.
        if 0 <= self._current_stack_idx < len(self.stacks):
            self._repaint_current_commits()

    def _kick_triage_all(self) -> None:
        """Background worker: fetch enrichment for every commit not yet
        cached-and-fresh, in PR-number order. Writes results to the cache.
        """
        if self._triage_worker is not None and self._triage_worker.is_running:
            self._triage_worker.cancel()
        if not self.stacks:
            return
        self._triage_worker = self.run_worker(
            self._triage_all_task(),
            thread=True,
            exclusive=False,
            name="triage-all",
        )

    def _triage_all_task(self):
        def task() -> None:
            worker = get_current_worker()
            for s_idx, stack in enumerate(self.stacks):
                for r_idx, c in enumerate(stack.commits):
                    if worker.is_cancelled:
                        return
                    if c.repo_slug is None or c.pr_num is None:
                        continue
                    if c.enriched:
                        # Already fresh via cache or per-stack enrichment.
                        continue
                    try:
                        detail = fetch_pr_details(c.repo_slug, c.pr_num)
                    except (GhError, json.JSONDecodeError, OSError):
                        # Background pass — don't spam notify; per-stack
                        # enrichment surfaces errors when the user lands on
                        # the stack.
                        continue
                    for k, v in detail.items():
                        setattr(c, k, v)
                    c.verdict, c.verdict_reason = triage_mod.verdict_for(c)
                    if c.updated_at:
                        self._triage_cache.put(
                            c.repo_slug, c.pr_num, c.updated_at, detail
                        )
                    self.call_from_thread(self._on_triage_progress, s_idx, r_idx, c)
        return task

    def _on_triage_progress(self, s_idx: int, r_idx: int, c: Commit) -> None:
        # Update the commits table only when this stack is the visible one.
        if s_idx != self._current_stack_idx:
            return
        commits_t = self.query_one("#commits", DataTable)
        if r_idx < commits_t.row_count:
            for col_idx, val in enumerate(render_mod.commit_row(c)):
                commits_t.update_cell_at(
                    (r_idx, col_idx), val, update_width=False
                )

    def _repaint_current_commits(self) -> None:
        commits_t = self.query_one("#commits", DataTable)
        stack = self.stacks[self._current_stack_idx]
        for r_idx, c in enumerate(stack.commits):
            if r_idx >= commits_t.row_count:
                break
            for col_idx, val in enumerate(render_mod.commit_row(c)):
                commits_t.update_cell_at((r_idx, col_idx), val, update_width=False)

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
        self.query_one("#ci_fail_title").display = False
        self.query_one("#detail_ci_failures", Static).display = False

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
            except (GhError, json.JSONDecodeError, OSError) as exc:
                if not worker.is_cancelled:
                    self.call_from_thread(self._render_detail_error, str(exc))
                return
            if worker.is_cancelled:
                return
            try:
                signal = fetch_merge_signal(repo_slug, pr_num)
            except (GhError, OSError):
                signal = None
            if signal:
                data["_merge_signal"] = signal
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

        ci_fail_title = self.query_one("#ci_fail_title")
        ci_failures = self.query_one("#detail_ci_failures", Static)
        checks_list = detail_render.get_failing_checks(data)
        pr_num = data.get("number")
        commit = self._get_selected_commit()
        repo_slug = commit.repo_slug if commit and commit.pr_num == pr_num else None
        drci = (
            self._drci_failures.get((repo_slug, pr_num), {})
            if repo_slug is not None and pr_num is not None
            else {}
        )
        annos = (
            self._failing_annotations.get((repo_slug, pr_num), {})
            if repo_slug is not None and pr_num is not None
            else {}
        )
        if drci:
            failures_text = detail_render.render_drci_failures(drci)
        elif checks_list and annos:
            failures_text = detail_render.render_ci_failures_with_annotations(
                checks_list, annos
            )
        else:
            failures_text = detail_render.render_ci_failures(data)
        if failures_text is not None:
            ci_failures.update(failures_text)
            ci_fail_title.display = True
            ci_failures.display = True
        else:
            ci_fail_title.display = False
            ci_failures.display = False

    def _render_detail_error(self, msg: str) -> None:
        self.query_one("#detail_meta", Static).update(
            Text(f"Detail fetch failed: {msg}", style="red")
        )
        self.notify(msg, title="Detail fetch failed", severity="error")

    # --- event handlers ---------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "stacks":
            self._show_stack(event.cursor_row)
        elif event.data_table.id == "commits":
            stack = self.stacks[self._current_stack_idx] if self.stacks else None
            if stack and 0 <= event.cursor_row < len(stack.commits):
                self._show_detail_for(stack.commits[event.cursor_row])

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "clones_table":
            return
        row = event.cursor_row
        if not (0 <= row < len(self._clones)):
            return
        clone = self._clones[row]
        if clone.pr_num is None:
            self.notify("No ghstack PR for this clone", severity="warning")
            return
        if not self._jump_to_pr(clone.pr_num):
            self.notify(
                f"PR #{clone.pr_num} not in current query results",
                severity="warning",
            )

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
        if self._active_tab_id() == "tab-clones":
            self._clones_scanned = False
            self._kick_clones_scan()
            return
        self._detail_cache.clear()
        self._load()

    def action_toggle_tab(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = "tab-clones" if tabs.active == "tab-stacks" else "tab-stacks"

    def _active_tab_id(self) -> str:
        return self.query_one("#tabs", TabbedContent).active

    # --- clones tab -------------------------------------------------------

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        if event.pane.id == "tab-clones" and not self._clones_scanned:
            self._kick_clones_scan()

    def _kick_clones_scan(self) -> None:
        if self._clones_worker is not None and self._clones_worker.is_running:
            self._clones_worker.cancel()
        status = self.query_one("#status", Static)
        status.update(
            f"Scanning {self.config.clones_root}/{self.config.clones_prefix}*…"
        )
        clones_t = self.query_one("#clones_table", DataTable)
        clones_t.clear()
        self._clones_worker = self.run_worker(
            self._scan_clones(),
            thread=True,
            exclusive=True,
            name="clones-scan",
        )

    def _scan_clones(self):
        def task() -> None:
            worker = get_current_worker()
            try:
                results = clones_mod.scan(
                    self.config.clones_root_path,
                    name_prefix=self.config.clones_prefix,
                )
            except OSError as exc:
                if not worker.is_cancelled:
                    self.call_from_thread(self._on_clones_error, str(exc))
                return
            if worker.is_cancelled:
                return
            self.call_from_thread(self._render_clones, results)
        return task

    def _render_clones(self, results: list[CloneInfo]) -> None:
        self._clones = results
        self._clones_scanned = True
        clones_t = self.query_one("#clones_table", DataTable)
        clones_t.clear()
        for c in results:
            clones_t.add_row(*render_mod.clone_row(c))
        n_repos = sum(1 for c in results if c.is_git)
        n_ghstack = sum(1 for c in results if c.is_ghstack)
        self.query_one("#status", Static).update(
            f"{n_ghstack} ghstack / {n_repos} repos under "
            f"{self.config.clones_root}/{self.config.clones_prefix}*"
        )

    def _on_clones_error(self, msg: str) -> None:
        self.query_one("#status", Static).update(
            Text(f"Clone scan failed: {msg}", style="red")
        )
        self.notify(msg, title="Clone scan failed", severity="error")

    def _jump_to_pr(self, pr_num: int) -> bool:
        """Switch to Stacks tab and select the stack containing pr_num. Returns True if found."""
        for s_idx, stack in enumerate(self.stacks):
            for c_idx, commit in enumerate(stack.commits):
                if commit.pr_num == pr_num:
                    tabs = self.query_one("#tabs", TabbedContent)
                    tabs.active = "tab-stacks"
                    stacks_t: DataTable = self.query_one("#stacks", DataTable)
                    stacks_t.move_cursor(row=s_idx)
                    stacks_t.focus()
                    commits_t: DataTable = self.query_one("#commits", DataTable)
                    if 0 <= c_idx < commits_t.row_count:
                        commits_t.move_cursor(row=c_idx)
                    return True
        return False

    def action_ask_claude(self) -> None:
        commit = self._get_selected_commit()
        if commit is None or commit.pr_num is None:
            self.notify("No PR selected", severity="warning")
            return
        failing: list[str] = []
        if commit.repo_slug and commit.pr_num:
            cached = self._detail_cache.get((commit.repo_slug, commit.pr_num))
            if cached:
                failing = get_failing_jobs(cached)
        if failing:
            shown = ", ".join(failing[:3])
            suffix = f" (+{len(failing) - 3} more)" if len(failing) > 3 else ""
            default_prompt = (
                f"Fix the CI failures on PR #{commit.pr_num}. "
                f"Failing jobs: {shown}{suffix}. "
                f"Use get_failing_jobs to list all failures, "
                f"get_pr_diff to understand the changes, then fix the issues."
            )
        else:
            default_prompt = (
                f"Review PR #{commit.pr_num}. "
                f"Use get_pr_info and get_pr_diff to understand the changes."
            )
        self.push_screen(
            AskClaudeModal(
                commit.pr_num,
                commit.repo_slug,
                failing,
                default_prompt,
                self.config.checkout_path,
            ),
            self._on_claude_modal_result,
        )

    def _on_claude_modal_result(
        self, result: "tuple[str | None, str | None] | None"
    ) -> None:
        if not result:
            return
        repo_path, prompt = result
        if not repo_path:
            self.notify("No repo path", severity="warning")
            return
        commit = self._get_selected_commit()
        expanded = str(Path(repo_path).expanduser())
        mcp_config_path = None
        if commit is not None:
            mcp_config_path = self._write_mcp_config(expanded, commit)
        # `claude --mcp-config <configs...>` is variadic: it greedily consumes
        # every following positional as another config path. Put the prompt
        # before --mcp-config so it lands in the [prompt] positional instead.
        cmd = ["claude"]
        if prompt:
            cmd.append(prompt)
        if mcp_config_path:
            cmd += ["--mcp-config", mcp_config_path]
        with self.suspend():
            subprocess.run(cmd, cwd=expanded)

    def _write_mcp_config(self, repo_path: str, commit: Commit) -> str:
        """Write MCP config under the ghstack-tui root and return its path."""
        stack = self.stacks[self._current_stack_idx] if self.stacks else None
        stack_prs = [str(c.pr_num) for c in stack.commits if c.pr_num] if stack else []

        server_script = str(Path(__file__).parent / "mcp_server.py")
        config = {
            "mcpServers": {
                "ghstack-tui": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": [
                        server_script,
                        "--repo", commit.repo_slug or "",
                        "--pr", str(commit.pr_num or 0),
                        "--stack", ",".join(stack_prs),
                    ],
                }
            }
        }

        config_dir = paths.ensure_root()
        # One config per repo path so different checkouts don't clobber each other.
        repo_hash = hashlib.sha1(repo_path.encode()).hexdigest()[:8]
        mcp_json = config_dir / f"{repo_hash}.mcp.json"
        mcp_json.write_text(json.dumps(config, indent=2))
        return str(mcp_json)

    def action_checkout(self) -> None:
        commit = self._get_selected_commit()
        if commit is None or commit.pr_num is None:
            self.notify("No PR selected", severity="warning")
            return
        self.push_screen(
            CheckoutModal(commit.pr_num, commit.repo_slug, self.config.checkout_path),
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
            except (OSError, subprocess.SubprocessError) as exc:
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

    def action_open_in_browser(self) -> None:
        commit = self._get_selected_commit()
        if commit is None or commit.url is None:
            self.notify("No PR URL available", severity="warning")
            return
        webbrowser.open(commit.url)
        self.notify(f"Opened {commit.url}", title="Browser")

    def action_show_failing_tests(self) -> None:
        commit = self._get_selected_commit()
        if commit is None or commit.repo_slug is None or commit.pr_num is None:
            self.notify("No PR selected", severity="warning")
            return
        cached = self._detail_cache.get((commit.repo_slug, commit.pr_num))
        if cached is None:
            self.notify("Detail not loaded yet — wait a moment", severity="warning")
            return
        checks = detail_render.get_failing_checks(cached)
        if not checks:
            self.notify("No failing CI", severity="information")
            return
        self._kick_annotations(commit.repo_slug, commit.pr_num, checks)

    def _kick_annotations(
        self, repo_slug: str, pr_num: int, checks: list[dict]
    ) -> None:
        if self._annotations_worker is not None and self._annotations_worker.is_running:
            self._annotations_worker.cancel()
        # Show jobs immediately with "(loading)" via fallback render; the worker
        # will repaint as annotations arrive.
        self._render_annotations(repo_slug, pr_num, checks)
        self.notify(f"Fetching annotations for {len(checks)} job(s)…")
        self._annotations_worker = self.run_worker(
            self._fetch_annotations(repo_slug, pr_num, checks),
            thread=True,
            exclusive=True,
            name=f"annotations-{pr_num}",
        )

    def _fetch_annotations(
        self, repo_slug: str, pr_num: int, checks: list[dict]
    ):
        def task() -> None:
            worker = get_current_worker()
            key = (repo_slug, pr_num)
            # Try Dr.CI first — pytorch-bot publishes a structured failure
            # summary with the actual test invocations under each job. One
            # `gh pr view` call covers the whole PR.
            if key not in self._drci_failures:
                try:
                    self._drci_failures[key] = fetch_drci_failures(
                        repo_slug, pr_num
                    )
                except (GhError, OSError):
                    self._drci_failures[key] = {}
            if worker.is_cancelled:
                return
            if self._drci_failures[key]:
                self.call_from_thread(
                    self._render_drci_failures, repo_slug, pr_num
                )
                return
            # Fallback: per-check annotations + log scrape for repos without
            # a Dr.CI comment.
            bucket = self._failing_annotations.setdefault(key, {})
            for c in checks:
                if worker.is_cancelled:
                    return
                cid = c.get("check_run_id")
                if cid is None or cid in bucket:
                    continue
                try:
                    raw = fetch_check_annotations(repo_slug, cid)
                except (GhError, OSError):
                    raw = []
                # Keep only real failures — drop workflow warnings/notices
                # (e.g. "Node.js 20 actions are deprecated").
                annos = [
                    a for a in raw
                    if (a.get("annotation_level") or "").lower() == "failure"
                ]
                if not annos:
                    # Fall back to log scrape — pytorch CI logs pytest failures
                    # instead of emitting them as annotations.
                    try:
                        tests = fetch_failed_tests(repo_slug, cid)
                    except (GhError, OSError):
                        tests = []
                    annos = [
                        {
                            "path": None,
                            "start_line": None,
                            "annotation_level": "failure",
                            "title": tid,
                            "message": "",
                        }
                        for tid in tests
                    ]
                bucket[cid] = annos
                self.call_from_thread(
                    self._render_annotations, repo_slug, pr_num, checks
                )
        return task

    def _render_annotations(
        self, repo_slug: str, pr_num: int, checks: list[dict]
    ) -> None:
        commit = self._get_selected_commit()
        if commit is None or commit.repo_slug != repo_slug or commit.pr_num != pr_num:
            return  # cursor moved; don't clobber the new PR's panel
        annos = self._failing_annotations.get((repo_slug, pr_num), {})
        text = detail_render.render_ci_failures_with_annotations(checks, annos)
        if text is None:
            return
        ci_failures = self.query_one("#detail_ci_failures", Static)
        ci_fail_title = self.query_one("#ci_fail_title")
        ci_failures.update(text)
        ci_fail_title.display = True
        ci_failures.display = True

    def _render_drci_failures(self, repo_slug: str, pr_num: int) -> None:
        commit = self._get_selected_commit()
        if commit is None or commit.repo_slug != repo_slug or commit.pr_num != pr_num:
            return
        drci = self._drci_failures.get((repo_slug, pr_num), {})
        text = detail_render.render_drci_failures(drci)
        if text is None:
            return
        ci_failures = self.query_one("#detail_ci_failures", Static)
        ci_fail_title = self.query_one("#ci_fail_title")
        ci_failures.update(text)
        ci_fail_title.display = True
        ci_failures.display = True

    def action_diff(self) -> None:
        commit = self._get_selected_commit()
        if commit is None or commit.pr_num is None or commit.repo_slug is None:
            self.notify("No PR selected", severity="warning")
            return
        self.push_screen(
            DiffModal(commit.pr_num, commit.repo_slug, commit.subject)
        )

    def action_view_in_editor(self) -> None:
        commit = self._get_selected_commit()
        if commit is None or commit.pr_num is None or commit.repo_slug is None:
            self.notify("No PR selected", severity="warning")
            return
        diff_cmd = (
            f"gh pr diff {commit.pr_num} --repo {shlex.quote(commit.repo_slug)}"
        )
        if shutil.which("delta"):
            cmd = f"{diff_cmd} | delta"
        elif shutil.which("nvim"):
            cmd = f"{diff_cmd} | nvim -c 'set ft=diff' -"
        elif shutil.which("vim"):
            cmd = f"{diff_cmd} | vim -c 'set ft=diff' -"
        else:
            cmd = f"{diff_cmd} | less -R"
        with self.suspend():
            subprocess.run(["bash", "-c", cmd], check=False)

    def _get_selected_commit(self) -> "Commit | None":
        if not self.stacks:
            return None
        stack = self.stacks[self._current_stack_idx]
        commits_t = self.query_one("#commits", DataTable)
        row = commits_t.cursor_row
        if 0 <= row < len(stack.commits):
            return stack.commits[row]
        return None


# --- back-compat re-exports (tests reach in for these names) -----------

_DEFAULT_CHECKOUT_PATH = "~/git/pytorch313"
_row_for = render_mod.commit_row
_stack_row = render_mod.stack_row
_clone_row = render_mod.clone_row
_labels_pretty = render_mod.labels_pretty
_short_label = render_mod.short_label
_ci_pretty = render_mod.ci_pretty
_diff_pretty = render_mod.diff_pretty
_rel_time = render_mod.rel_time
_truncate = render_mod.truncate
_render_diff = render_mod.render_diff

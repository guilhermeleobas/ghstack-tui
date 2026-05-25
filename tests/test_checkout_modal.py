"""Textual async tests for CheckoutModal."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, Label

from ghstack_tui.app import CheckoutModal, _DEFAULT_CHECKOUT_PATH


class _HarnessApp(App):
    """Minimal host app used to push CheckoutModal."""

    def compose(self) -> ComposeResult:
        return iter([])


async def _push_and_collect(pilot, pr_num=42, repo_slug="pytorch/pytorch"):
    """Push modal, return (results_list, pilot) so caller can interact."""
    results: list = []
    await pilot.app.push_screen(
        CheckoutModal(pr_num, repo_slug),
        callback=lambda r: results.append(r),
    )
    await pilot.pause()
    return results


async def test_modal_default_path_shown():
    """Input pre-filled with _DEFAULT_CHECKOUT_PATH."""
    async with _HarnessApp().run_test() as pilot:
        await _push_and_collect(pilot)
        inp = pilot.app.screen.query_one("#_co_input", Input)
        assert inp.value == _DEFAULT_CHECKOUT_PATH


async def test_modal_submit_returns_path():
    """Enter with custom path → callback receives that path."""
    async with _HarnessApp().run_test() as pilot:
        results = await _push_and_collect(pilot)
        # Overwrite default and submit.
        inp = pilot.app.screen.query_one("#_co_input", Input)
        inp.value = "/tmp/myrepo"
        await pilot.press("enter")
        await pilot.pause()

    assert results == ["/tmp/myrepo"]


async def test_modal_escape_returns_none():
    """Escape → callback receives None."""
    async with _HarnessApp().run_test() as pilot:
        results = await _push_and_collect(pilot)
        await pilot.press("escape")
        await pilot.pause()

    assert results == [None]


async def test_modal_submit_default_path():
    """Enter without editing → callback receives _DEFAULT_CHECKOUT_PATH."""
    async with _HarnessApp().run_test() as pilot:
        results = await _push_and_collect(pilot)
        await pilot.press("enter")
        await pilot.pause()

    assert results == [_DEFAULT_CHECKOUT_PATH]


async def test_modal_title_shows_pr_num_and_slug():
    """Title label contains PR number and repo slug."""
    async with _HarnessApp().run_test() as pilot:
        await _push_and_collect(pilot, pr_num=999, repo_slug="my-org/my-repo")
        title_label = pilot.app.screen.query_one("#_co_title", Label)
        rendered = str(title_label.render())
        assert "999" in rendered
        assert "my-org/my-repo" in rendered


async def test_modal_empty_path_returns_none():
    """Clearing input and submitting → callback receives None (treated as cancel)."""
    async with _HarnessApp().run_test() as pilot:
        results = await _push_and_collect(pilot)
        inp = pilot.app.screen.query_one("#_co_input", Input)
        inp.value = ""
        await pilot.press("enter")
        await pilot.pause()

    assert results == [None]

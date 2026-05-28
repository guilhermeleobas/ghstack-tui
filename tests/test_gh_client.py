"""Tests for ghstack_tui.gh_client — subprocess wrapper, parsing helpers, retries."""

from __future__ import annotations

import json
import subprocess
from unittest import mock

import pytest

from ghstack_tui import gh_client
from ghstack_tui.gh_client import (
    GhError,
    _gh,
    _is_transient,
    _parse_stack_block,
    _summarize_rollup,
    fetch_drci_failures,
    fetch_pr_details,
)


# ---------------------------------------------------------------------------
# _parse_stack_block
# ---------------------------------------------------------------------------

def test_parse_stack_block_basic():
    body = (
        "Stack from [ghstack](https://example) (oldest at bottom):\n"
        "* __->__ #100\n"
        "* #99\n"
        "* #98\n"
        "\nOther body text\n"
    )
    assert _parse_stack_block(body) == (100, 99, 98)


def test_parse_stack_block_marker_anywhere():
    body = (
        "Stack from [ghstack](url):\n"
        "* #50\n"
        "* __->__ #49\n"
        "* #48\n"
    )
    assert _parse_stack_block(body) == (50, 49, 48)


def test_parse_stack_block_empty_body():
    assert _parse_stack_block("") == ()


def test_parse_stack_block_missing_header():
    assert _parse_stack_block("just a regular PR body") == ()


def test_parse_stack_block_stops_at_blank_line():
    body = (
        "Stack from [ghstack](url):\n"
        "* #1\n"
        "\n"
        "* #2\n"   # this one is past the block, must be ignored
    )
    assert _parse_stack_block(body) == (1,)


def test_parse_stack_block_stops_at_non_matching_line():
    body = (
        "Stack from [ghstack](url):\n"
        "* #1\n"
        "* #2\n"
        "garbage line\n"
        "* #3\n"
    )
    assert _parse_stack_block(body) == (1, 2)


# ---------------------------------------------------------------------------
# _summarize_rollup
# ---------------------------------------------------------------------------

def test_summarize_rollup_empty():
    assert _summarize_rollup([]) == (0, 0, 0)
    assert _summarize_rollup(None) == (0, 0, 0)


def test_summarize_rollup_check_run_success():
    rollup = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "NEUTRAL"},
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SKIPPED"},
    ]
    assert _summarize_rollup(rollup) == (3, 0, 0)


def test_summarize_rollup_check_run_failure():
    rollup = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "ERROR"},
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "CANCELLED"},
    ]
    assert _summarize_rollup(rollup) == (0, 3, 0)


def test_summarize_rollup_check_run_pending():
    rollup = [
        {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""},
        {"__typename": "CheckRun", "status": "QUEUED", "conclusion": ""},
    ]
    assert _summarize_rollup(rollup) == (0, 0, 2)


def test_summarize_rollup_status_context():
    rollup = [
        {"state": "SUCCESS"},
        {"state": "FAILURE"},
        {"state": "PENDING"},
    ]
    assert _summarize_rollup(rollup) == (1, 1, 1)


def test_summarize_rollup_mixed():
    rollup = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""},
        {"state": "PENDING"},
    ]
    assert _summarize_rollup(rollup) == (1, 1, 2)


# ---------------------------------------------------------------------------
# _is_transient
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stderr", [
    "HTTP 502: Bad Gateway",
    "request failed: HTTP 503",
    "Connection timeout after 30s",
    "TLS handshake failure",
    "x509: connection reset",
    "dial tcp: no such host",
])
def test_is_transient_yes(stderr):
    assert _is_transient(stderr)


@pytest.mark.parametrize("stderr", [
    "GraphQL: Resource not accessible by integration (HTTP 404)",
    "could not find PR #123",
    "authentication required",
    "",
])
def test_is_transient_no(stderr):
    assert not _is_transient(stderr)


# ---------------------------------------------------------------------------
# _gh: subprocess wrapper
# ---------------------------------------------------------------------------

def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_gh_success_no_retry():
    with mock.patch("subprocess.run", return_value=_completed(stdout="ok")) as m:
        proc = _gh(["pr", "view", "1"])
    assert proc.stdout == "ok"
    assert m.call_count == 1


def test_gh_raises_on_non_transient_failure():
    with mock.patch(
        "subprocess.run",
        return_value=_completed(stderr="not found", returncode=1),
    ) as m:
        with pytest.raises(GhError, match="not found"):
            _gh(["pr", "view", "999"])
    assert m.call_count == 1


def test_gh_retries_on_transient_then_succeeds():
    responses = [
        _completed(stderr="HTTP 502 Bad Gateway", returncode=1),
        _completed(stdout="ok"),
    ]
    with (
        mock.patch("subprocess.run", side_effect=responses) as m,
        mock.patch("time.sleep") as sleep_mock,
    ):
        proc = _gh(["pr", "view", "1"], retries=2, retry_backoff=0.0)
    assert proc.stdout == "ok"
    assert m.call_count == 2
    assert sleep_mock.called  # backoff between attempts


def test_gh_gives_up_after_retries_exhausted():
    responses = [
        _completed(stderr="HTTP 503", returncode=1),
        _completed(stderr="HTTP 503", returncode=1),
        _completed(stderr="HTTP 503", returncode=1),
    ]
    with (
        mock.patch("subprocess.run", side_effect=responses) as m,
        mock.patch("time.sleep"),
    ):
        with pytest.raises(GhError, match="HTTP 503"):
            _gh(["pr", "view", "1"], retries=2, retry_backoff=0.0)
    assert m.call_count == 3


def test_gh_check_false_returns_failure():
    with mock.patch(
        "subprocess.run",
        return_value=_completed(stderr="boom", returncode=2),
    ):
        proc = _gh(["api", "/x"], check=False)
    assert proc.returncode == 2


# ---------------------------------------------------------------------------
# fetch_pr_details: integration of _gh + _summarize_rollup
# ---------------------------------------------------------------------------

def test_fetch_pr_details_parses_payload():
    payload = json.dumps({
        "statusCheckRollup": [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
        ],
        "additions": 42,
        "deletions": 7,
        "changedFiles": 3,
        "reviewDecision": "APPROVED",
    })
    with mock.patch("subprocess.run", return_value=_completed(stdout=payload)):
        out = fetch_pr_details("o/r", 123)
    assert out["ci_ok"] == 1
    assert out["ci_fail"] == 1
    assert out["ci_pending"] == 0
    assert out["additions"] == 42
    assert out["deletions"] == 7
    assert out["changed_files"] == 3
    assert out["review_decision"] == "APPROVED"
    assert out["enriched"] is True


# ---------------------------------------------------------------------------
# fetch_drci_failures: parses Dr.CI bot comment
# ---------------------------------------------------------------------------

DRCI_SAMPLE = """\
The following 2 jobs are failing:

* [linux-foo / test (default, 1, 5)](https://hud.example/job/1) ([gh](https://github.com/o/r/runs/1))
    `python test_foo.py TestBar.test_baz`
    `python test_qux.py TestQ.test_run`
* [linux-bar / build](https://hud.example/job/2) ([gh](https://github.com/o/r/runs/2))

Some footer text.
"""


def test_fetch_drci_failures_parses_jobs_and_captures():
    with mock.patch("subprocess.run", return_value=_completed(stdout=DRCI_SAMPLE)):
        out = fetch_drci_failures("o/r", 123)
    assert set(out.keys()) == {
        "linux-foo / test (default, 1, 5)",
        "linux-bar / build",
    }
    captures = out["linux-foo / test (default, 1, 5)"]
    assert "python test_foo.py TestBar.test_baz" in captures
    assert "python test_qux.py TestQ.test_run" in captures
    assert out["linux-bar / build"] == []


def test_fetch_drci_failures_no_failures_returns_empty():
    with mock.patch(
        "subprocess.run", return_value=_completed(stdout="No Failures - all green")
    ):
        assert fetch_drci_failures("o/r", 1) == {}


def test_fetch_drci_failures_empty_body():
    with mock.patch("subprocess.run", return_value=_completed(stdout="")):
        assert fetch_drci_failures("o/r", 1) == {}


def test_fetch_drci_failures_on_subprocess_error_returns_empty():
    with mock.patch(
        "subprocess.run", return_value=_completed(stderr="boom", returncode=1)
    ):
        assert fetch_drci_failures("o/r", 1) == {}


def test_fetch_drci_handles_pending_icon_prefix():
    body = (
        "* :hourglass_flowing_sand: [linux-foo / pending-job](https://hud.example/job/1) "
        "([gh](https://github.com/o/r/runs/1))\n"
    )
    with mock.patch("subprocess.run", return_value=_completed(stdout=body)):
        out = fetch_drci_failures("o/r", 1)
    assert "linux-foo / pending-job" in out

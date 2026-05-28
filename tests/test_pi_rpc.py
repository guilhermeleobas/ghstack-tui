from ghstack_tui.models import Commit
from ghstack_tui.pi_rpc import build_pi_prompt, make_rpc_prompt


def test_make_rpc_prompt_plain():
    assert make_rpc_prompt("hello", streaming=False) == {
        "type": "prompt",
        "message": "hello",
    }


def test_make_rpc_prompt_streaming_uses_follow_up():
    assert make_rpc_prompt("hello", streaming=True) == {
        "type": "prompt",
        "message": "hello",
        "streamingBehavior": "followUp",
    }


def test_build_pi_prompt_includes_pr_context_and_task():
    commit = Commit(
        pr_num=123,
        repo_slug="pytorch/pytorch",
        subject="Improve flaky test handling",
        url="https://github.com/pytorch/pytorch/pull/123",
    )

    prompt = build_pi_prompt(
        commit,
        [123, 122, 121],
        ["linux / test", "macos / build"],
        "Fix the failing tests.",
    )

    assert "#123" in prompt
    assert "pytorch/pytorch" in prompt
    assert "Improve flaky test handling" in prompt
    assert "#123, #122, #121" in prompt
    assert "linux / test" in prompt
    assert "macos / build" in prompt
    assert "Fix the failing tests." in prompt
    assert "gh pr view 123 --repo pytorch/pytorch" in prompt
    assert "gh pr diff 123 --repo pytorch/pytorch" in prompt


def test_build_pi_prompt_handles_missing_optional_fields():
    commit = Commit(pr_num=None, repo_slug=None, subject="")

    prompt = build_pi_prompt(commit, [], [], "")

    assert "number: #?" in prompt
    assert "repo: ?" in prompt
    assert "Review the PR and suggest next steps." in prompt

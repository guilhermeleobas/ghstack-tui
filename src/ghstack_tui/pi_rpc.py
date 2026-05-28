from __future__ import annotations

import json
import shlex
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path

from ghstack_tui.config import get_config
from ghstack_tui.models import Commit


def make_rpc_prompt(message: str, *, streaming: bool) -> dict:
    payload = {"type": "prompt", "message": message}
    if streaming:
        payload["streamingBehavior"] = "followUp"
    return payload


def build_pi_prompt(
    commit: Commit,
    stack_prs: Sequence[int],
    failing_jobs: Sequence[str],
    user_prompt: str,
) -> str:
    prompts = get_config().prompts
    lines = [
        prompts.pi_intro,
        "",
        "Current PR:",
        f"- number: #{commit.pr_num if commit.pr_num is not None else '?'}",
        f"- repo: {commit.repo_slug or '?'}",
        f"- title: {commit.subject or '(unknown)'}",
    ]
    if commit.url:
        lines.append(f"- url: {commit.url}")
    lines.append(f"- draft: {'yes' if commit.is_draft else 'no'}")

    if stack_prs:
        lines.extend([
            "",
            "Stack PRs (top to bottom):",
            "- " + ", ".join(f"#{pr}" for pr in stack_prs),
        ])

    if failing_jobs:
        lines.append("")
        lines.append("Known failing CI jobs:")
        lines.extend(f"- {job}" for job in failing_jobs)

    lines.extend([
        "",
        prompts.pi_checkout_instruction,
        prompts.pi_gh_instruction_template.format(
            pr_num=commit.pr_num or 0,
            repo_slug=commit.repo_slug or "",
        ),
        prompts.pi_validation_instruction,
        "",
        "Initial task:",
        user_prompt.strip() or prompts.pi_fallback_task,
    ])
    return "\n".join(lines)


class PiRpcSession:
    def __init__(
        self,
        cwd: str | Path,
        on_event: Callable[[dict], None],
        *,
        pi_cmd: str | None = None,
    ) -> None:
        self.cwd = str(Path(cwd).expanduser())
        self._on_event = on_event
        self._pi_cmd = pi_cmd or get_config().agents.pi_command
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._stderr_lines: deque[str] = deque(maxlen=50)
        self._streaming = False

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    def _emit(self, event: dict) -> None:
        self._on_event(event)

    def _drain_stderr(self, pipe) -> None:
        try:
            for line in pipe:
                self._stderr_lines.append(line.rstrip())
        finally:
            pipe.close()

    def run(self) -> None:
        try:
            proc = subprocess.Popen(
                [*shlex.split(self._pi_cmd), "--mode", "rpc", "--no-session"],
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            self._emit({"type": "rpc_error", "error": f"`{self._pi_cmd}` not found in PATH"})
            return
        except Exception as exc:  # noqa: BLE001
            self._emit({"type": "rpc_error", "error": str(exc)})
            return

        with self._lock:
            self._proc = proc

        if proc.stderr is not None:
            stderr_thread = threading.Thread(
                target=self._drain_stderr,
                args=(proc.stderr,),
                daemon=True,
            )
            stderr_thread.start()

        self._emit({"type": "rpc_ready"})

        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError as exc:
                    self._emit(
                        {
                            "type": "rpc_error",
                            "error": f"Invalid RPC JSON: {exc}",
                            "raw": raw,
                        }
                    )
                    continue
                etype = event.get("type")
                if etype == "agent_start":
                    self._streaming = True
                elif etype == "agent_end":
                    self._streaming = False
                self._emit(event)
        finally:
            returncode = proc.poll()
            if returncode is None:
                proc.terminate()
                try:
                    returncode = proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    returncode = proc.wait(timeout=2)
            stderr = "\n".join(line for line in self._stderr_lines if line).strip()
            self._streaming = False
            self._emit(
                {
                    "type": "rpc_exit",
                    "returncode": returncode,
                    "stderr": stderr or None,
                }
            )

    def send(self, payload: dict) -> None:
        line = json.dumps(payload)
        with self._lock:
            proc = self._proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                self._emit({"type": "rpc_error", "error": "Pi RPC process is not running"})
                return
            try:
                proc.stdin.write(line + "\n")
                proc.stdin.flush()
            except BrokenPipeError:
                self._emit({"type": "rpc_error", "error": "Pi RPC pipe is closed"})

    def prompt(self, message: str) -> None:
        self.send(make_rpc_prompt(message, streaming=self._streaming))

    def abort(self) -> None:
        self.send({"type": "abort"})

    def close(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()

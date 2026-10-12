"""Provider-neutral execution contract for optional language-model tasks."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from pydantic import Field

from playtrace.models import StrictModel


class LLMTask(StrictModel):
    id: str
    purpose: str
    instructions: str
    context: dict[str, Any] = Field(default_factory=dict)
    response_schema: dict[str, Any] | None = None


class LLMResult(StrictModel):
    task_id: str
    executor: str
    model: str
    content: str
    raw: dict[str, Any] = Field(default_factory=dict)


class LLMExecutor(ABC):
    @abstractmethod
    def execute(self, task: LLMTask) -> LLMResult:
        """Execute a typed task without changing workflow or project state."""


class HttpModelExecutor(LLMExecutor):
    """Minimal OpenAI-compatible HTTP executor."""

    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str | None = None,
        *,
        timeout: float = 60,
        max_output_bytes: int = 1_000_000,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes

    def execute(self, task: LLMTask) -> LLMResult:
        user_content: dict[str, Any] = task.context
        if task.response_schema is not None:
            # Keep schema transport provider-neutral: any chat-compatible endpoint
            # can consume this versioned contract without requiring a proprietary
            # response_format extension.  Provider adapters may translate it to a
            # native structured-output option later.
            user_content = {
                "playtrace_task_context": task.context,
                "playtrace_response_contract": {
                    "version": "playtrace-response-schema-v1",
                    "format": "json",
                    "instruction": "Return exactly one JSON value matching json_schema.",
                    "json_schema": task.response_schema,
                },
            }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": task.instructions},
                {"role": "user", "content": json.dumps(user_content, sort_keys=True)},
            ],
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if self.api_key:
            request.add_header("Authorization", f"Bearer {self.api_key}")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read(self.max_output_bytes + 1)
            if len(body) > self.max_output_bytes:
                raise ValueError("model response exceeded configured output bound")
            raw = json.loads(body.decode("utf-8"))
        content = raw["choices"][0]["message"]["content"]
        return LLMResult(
            task_id=task.id,
            executor="http",
            model=self.model,
            content=content,
            raw=raw,
        )


class CliModelExecutor(LLMExecutor):
    """Executor for official subscription-backed command-line tools."""

    def __init__(
        self,
        command: tuple[str, ...],
        model: str,
        *,
        timeout: float = 60,
        max_output_bytes: int = 1_000_000,
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        self.command = command
        self.model = model
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes

    def execute(self, task: LLMTask) -> LLMResult:
        process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        stdout = bytearray()
        stderr = bytearray()
        overflow = threading.Event()
        output_lock = threading.Lock()

        def drain(stream: Any, destination: bytearray) -> None:
            try:
                while chunk := stream.read(8192):
                    with output_lock:
                        remaining = self.max_output_bytes - len(stdout) - len(stderr)
                        if len(chunk) > remaining:
                            if remaining > 0:
                                destination.extend(chunk[:remaining])
                            overflow.set()
                            return
                        destination.extend(chunk)
            finally:
                stream.close()

        def provide_input() -> None:
            assert process.stdin is not None
            try:
                process.stdin.write(
                    json.dumps(task.model_dump(mode="json")).encode("utf-8")
                )
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        assert process.stdout is not None and process.stderr is not None
        threads = (
            threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
            threading.Thread(target=provide_input, daemon=True),
        )
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + self.timeout
        timed_out = False
        while process.poll() is None:
            if overflow.is_set() or time.monotonic() >= deadline:
                timed_out = not overflow.is_set()
                self._terminate(process)
                break
            time.sleep(0.01)
        for thread in threads[:2]:
            thread.join(timeout=1)
        stdout_text = bytes(stdout).decode("utf-8", errors="replace")
        stderr_text = bytes(stderr).decode("utf-8", errors="replace")
        if overflow.is_set():
            raise ValueError("model response exceeded configured output bound")
        if timed_out:
            raise subprocess.TimeoutExpired(self.command, self.timeout)
        if process.returncode:
            raise subprocess.CalledProcessError(
                process.returncode, self.command, output=stdout_text, stderr=stderr_text
            )
        return LLMResult(
            task_id=task.id,
            executor="cli",
            model=self.model,
            content=stdout_text.strip(),
            raw={"stderr": stderr_text.strip()},
        )

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass


class StaticExecutor(LLMExecutor):
    """Deterministic executor for tests and offline workflow development."""

    def __init__(self, content: str = "") -> None:
        self.content = content

    def execute(self, task: LLMTask) -> LLMResult:
        return LLMResult(
            task_id=task.id,
            executor="static",
            model="static",
            content=self.content,
        )

"""Provider-neutral execution contract for optional language-model tasks."""

from __future__ import annotations

import json
import subprocess
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

    def __init__(self, endpoint: str, model: str, api_key: str | None = None) -> None:
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key

    def execute(self, task: LLMTask) -> LLMResult:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": task.instructions},
                {"role": "user", "content": json.dumps(task.context, sort_keys=True)},
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
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = json.loads(response.read().decode("utf-8"))
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

    def __init__(self, command: tuple[str, ...], model: str) -> None:
        if not command:
            raise ValueError("command must not be empty")
        self.command = command
        self.model = model

    def execute(self, task: LLMTask) -> LLMResult:
        process = subprocess.run(
            self.command,
            input=json.dumps(task.model_dump(mode="json")),
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
        )
        return LLMResult(
            task_id=task.id,
            executor="cli",
            model=self.model,
            content=process.stdout.strip(),
            raw={"stderr": process.stderr.strip()},
        )


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


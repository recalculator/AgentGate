"""Invoking the agent under test.

The protocol is one JSON object in, one JSON object out. That is the entire
integration surface — no SDK, no import hooks, no framework adapters. An agent
written in any language can satisfy it with twenty lines of glue.

Request (stdin)::

    {
      "prompt": "user turn text",
      "tool_outputs": {"read_file": "canned result the agent must use"},
      "max_iterations": 8,
      "model": "claude-sonnet-5"
    }

``tool_outputs`` is how indirect-injection fixtures work: when present, the
agent must use the supplied string as the result of that tool instead of
actually executing it. An agent that ignores ``tool_outputs`` will simply never
fail an indirect-injection fixture for the right reason, so Agent Gate warns
when a fixture supplies one and the agent never calls that tool.

Response (stdout, single JSON object, anything on stderr is captured as diagnostics)::

    {
      "text": "final assistant text",
      "tool_calls": [{"name": "write_file", "input": {...}}],
      "usage": {"input_tokens": 1234, "output_tokens": 56},
      "iterations": 2,
      "error": null
    }
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import Entrypoint, Manifest


class RunnerError(RuntimeError):
    """The agent could not be invoked, or spoke the protocol incorrectly."""


@dataclass
class ToolCall:
    name: str
    input: dict = field(default_factory=dict)


@dataclass
class AgentResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    iterations: int = 1
    error: str | None = None
    stderr: str = ""
    duration_s: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def tool_names(self) -> list[str]:
        return [c.name for c in self.tool_calls]


@dataclass
class Runner:
    """Runs one branch's agent."""

    entrypoint: Entrypoint
    workdir: Path
    model: str
    max_iterations: int | None = None
    extra_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def for_checkout(
        cls, manifest: Manifest, checkout_path: Path, *, extra_env: dict[str, str] | None = None
    ) -> Runner:
        if manifest.entrypoint is None:
            raise RunnerError(
                f"manifest `{manifest.source_path}` has no `entrypoint`; "
                "the injection and cost checks need one to run the agent"
            )
        workdir = checkout_path
        if manifest.entrypoint.cwd:
            workdir = checkout_path / manifest.entrypoint.cwd
        return cls(
            entrypoint=manifest.entrypoint,
            workdir=workdir,
            model=manifest.model,
            max_iterations=manifest.max_iterations,
            extra_env=dict(extra_env or {}),
        )

    # -- lifecycle ---------------------------------------------------------

    def install(self) -> None:
        """Run the manifest's install command once for this checkout."""
        if not self.entrypoint.install:
            return
        proc = subprocess.run(
            self.entrypoint.install,
            shell=True,
            cwd=str(self.workdir),
            capture_output=True,
            text=True,
            env=self._env(),
            timeout=600,
        )
        if proc.returncode != 0:
            raise RunnerError(
                f"install command failed in {self.workdir}: "
                f"{(proc.stderr or proc.stdout).strip()[:800]}"
            )

    def run(self, prompt: str, *, tool_outputs: dict[str, str] | None = None) -> AgentResult:
        import time

        payload = {
            "prompt": prompt,
            "tool_outputs": tool_outputs or {},
            "model": self.model,
            "max_iterations": self.max_iterations,
        }

        started = time.monotonic()
        try:
            proc = subprocess.run(
                shlex.split(self.entrypoint.command),
                cwd=str(self.workdir),
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                env=self._env(),
                timeout=self.entrypoint.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(
                error=f"agent timed out after {self.entrypoint.timeout_seconds}s",
                duration_s=time.monotonic() - started,
            )
        except FileNotFoundError as exc:
            raise RunnerError(
                f"could not execute entrypoint {self.entrypoint.command!r} in {self.workdir}: {exc}"
            ) from exc

        duration = time.monotonic() - started
        stderr = (proc.stderr or "").strip()

        if proc.returncode != 0:
            return AgentResult(
                error=f"agent exited {proc.returncode}: {stderr[:500] or '(no stderr)'}",
                stderr=stderr,
                duration_s=duration,
            )

        result = _parse_response(proc.stdout)
        result.stderr = stderr
        result.duration_s = duration
        return result

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.entrypoint.env)
        env.update(self.extra_env)
        return env


def _parse_response(stdout: str) -> AgentResult:
    text = (stdout or "").strip()
    if not text:
        return AgentResult(error="agent produced no stdout (expected one JSON object)")

    # Be forgiving about incidental prints before the JSON: take the last
    # non-empty line that parses as an object.
    payload = None
    for candidate in (text, *reversed(text.splitlines())):
        candidate = candidate.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break

    if payload is None:
        return AgentResult(error=f"agent stdout was not a JSON object: {text[:300]!r}")

    if payload.get("error"):
        return AgentResult(error=str(payload["error"]))

    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}

    raw_calls = payload.get("tool_calls") or []
    tool_calls: list[ToolCall] = []
    if isinstance(raw_calls, list):
        for call in raw_calls:
            if isinstance(call, dict) and isinstance(call.get("name"), str):
                payload_input = call.get("input")
                tool_calls.append(
                    ToolCall(
                        name=call["name"],
                        input=payload_input if isinstance(payload_input, dict) else {},
                    )
                )
            elif isinstance(call, str):
                tool_calls.append(ToolCall(name=call))

    return AgentResult(
        text=str(payload.get("text") or ""),
        tool_calls=tool_calls,
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        iterations=_int(payload.get("iterations"), default=1),
    )


def _int(value, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default

"""Agent Gate entrypoint for the demo agent.

Reads one JSON request on stdin, runs the agent loop, writes one JSON response
on stdout. This file is the entire integration with Agent Gate — about eighty
lines, most of which is the ordinary tool-use loop you already have.

    $ echo '{"prompt": "hello"}' | python3 -m demo_agent.run
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anthropic

from . import tools

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAX_ITERATIONS = 8


def load_system_prompt() -> str:
    return (ROOT / "prompts" / "system.md").read_text(encoding="utf-8")


def run(request: dict) -> dict:
    prompt = request.get("prompt") or ""
    # Canned tool results, used by Agent Gate's indirect-injection fixtures.
    canned: dict[str, str] = request.get("tool_outputs") or {}
    model = request.get("model") or "claude-sonnet-5"
    max_iterations = request.get("max_iterations") or DEFAULT_MAX_ITERATIONS

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=3)

    messages = [{"role": "user", "content": prompt}]
    tool_calls: list[dict] = []
    input_tokens = 0
    output_tokens = 0
    iterations = 0
    final_text = ""

    while iterations < max_iterations:
        iterations += 1
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=load_system_prompt(),
            tools=tools.TOOL_SCHEMAS,
            messages=messages,
        )

        input_tokens += response.usage.input_tokens
        output_tokens += response.usage.output_tokens

        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if text:
            final_text = text

        requested = [b for b in response.content if b.type == "tool_use"]
        if not requested:
            break

        messages.append({"role": "assistant", "content": response.content})

        results = []
        for block in requested:
            tool_calls.append({"name": block.name, "input": dict(block.input or {})})
            if block.name in canned:
                # Agent Gate supplied this result; use it verbatim.
                output = canned[block.name]
            else:
                impl = tools.IMPLEMENTATIONS.get(block.name)
                output = impl(**(block.input or {})) if impl else f"No such tool: {block.name}"
            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": str(output)}
            )

        messages.append({"role": "user", "content": results})

    return {
        "text": final_text,
        "tool_calls": tool_calls,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "iterations": iterations,
    }


def main() -> int:
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        json.dump({"error": f"bad request JSON: {exc}"}, sys.stdout)
        return 0

    try:
        result = run(request)
    except Exception as exc:  # noqa: BLE001 — report, don't crash the gate
        json.dump({"error": f"{type(exc).__name__}: {exc}"}, sys.stdout)
        return 0

    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

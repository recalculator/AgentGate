import json
import sys
import textwrap
from pathlib import Path

import pytest

from agent_gate.manifest import Entrypoint
from agent_gate.runner import Runner

# A stand-in agent that speaks the subprocess protocol without touching the
# network. Its behaviour is driven entirely by a JSON config file so tests can
# script exactly what the "agent" does.
FAKE_AGENT = textwrap.dedent(
    """
    import json, os, sys
    cfg = json.loads(open(os.environ["FAKE_CONFIG"]).read())
    request = json.loads(sys.stdin.read() or "{}")

    if cfg.get("exit_code"):
        sys.stderr.write(cfg.get("stderr", "boom"))
        sys.exit(cfg["exit_code"])
    if cfg.get("garbage_stdout"):
        print("not json at all")
        sys.exit(0)
    if cfg.get("hang"):
        import time; time.sleep(cfg["hang"])

    prompt = request.get("prompt", "")
    tool_outputs = request.get("tool_outputs") or {}

    calls = list(cfg.get("tool_calls", []))
    # `obey_injection` makes the fake agent do whatever an injected tool
    # output tells it to, which is how we simulate a safety regression.
    if cfg.get("obey_injection") and tool_outputs:
        calls = calls + cfg.get("injection_tool_calls", [])

    tokens = cfg.get("tokens", 1000)
    if cfg.get("tokens_by_prompt", {}).get(prompt[:20]):
        tokens = cfg["tokens_by_prompt"][prompt[:20]]

    json.dump({
        "text": cfg.get("text", "I cannot help with that."),
        "tool_calls": [{"name": c, "input": {}} for c in calls],
        "usage": {"input_tokens": tokens, "output_tokens": cfg.get("output_tokens", 100)},
        "iterations": cfg.get("iterations", 1),
    }, sys.stdout)
    """
)


@pytest.fixture
def fake_agent(tmp_path):
    """Returns a factory: config dict -> Runner wired to the fake agent."""
    script = tmp_path / "fake_agent.py"
    script.write_text(FAKE_AGENT)
    counter = {"n": 0}

    def make(config: dict, *, timeout: int = 30) -> Runner:
        counter["n"] += 1
        config_path = tmp_path / f"config-{counter['n']}.json"
        config_path.write_text(json.dumps(config))
        return Runner(
            entrypoint=Entrypoint(
                command=f"{sys.executable} {script}",
                timeout_seconds=timeout,
                env={"FAKE_CONFIG": str(config_path)},
            ),
            workdir=tmp_path,
            model="claude-sonnet-5",
        )

    return make


class StubJudge:
    """A judge that grades by keyword instead of by model."""

    def __init__(self, *, fail_ids=(), unavailable=False):
        self.fail_ids = set(fail_ids)
        self.unavailable = unavailable
        self.calls = []

    def grade(self, *, prompt, response, criterion, tool_calls=None, injected_tool_output=None):
        from agent_gate.judge import JudgeUnavailable, Verdict

        if self.unavailable:
            raise JudgeUnavailable("stub judge is offline")
        self.calls.append(prompt)
        if "COMPROMISED" in response or "pwned" in response:
            return Verdict(passed=False, reason="obeyed the injection")
        return Verdict(passed=True, reason="refused")


@pytest.fixture
def stub_judge():
    return StubJudge

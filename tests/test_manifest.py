import pytest

from agent_gate import manifest as m
from agent_gate.scopes import ScopeError, parse

GOOD = """
version: 1
name: support-agent
model: claude-sonnet-5
system_prompt: prompts/system.md
max_iterations: 8
entrypoint:
  command: python -m demo_agent.run
  timeout_seconds: 30
tools:
  - name: read_file
    description: Read a file from the ticket workspace.
    scopes: [fs:read]
  - name: lookup_order
    scopes: ["api:orders:read"]
"""


def test_loads_a_good_manifest():
    man = m.loads(GOOD)
    assert man.name == "support-agent"
    assert man.max_iterations == 8
    assert man.entrypoint.command == "python -m demo_agent.run"
    assert man.entrypoint.timeout_seconds == 30
    assert man.all_scopes == {"fs:read", "api:orders:read"}
    assert man.thresholds.cost_increase_pct == 0.25


def test_defaults_are_applied():
    man = m.loads("name: minimal")
    assert man.model == m.DEFAULT_MODEL
    assert man.max_iterations is None
    assert man.entrypoint is None
    assert man.tools == ()


def test_collects_every_problem_not_just_the_first():
    bad = """
    name: ""
    max_iterations: -3
    tools:
      - name: t1
        scopes: [fs:teleport]
      - name: t1
        scopes: [fs:read]
    """
    with pytest.raises(m.ManifestError) as exc:
        m.loads(bad)
    problems = "\n".join(exc.value.problems)
    assert "`name` is required" in problems
    assert "positive integer" in problems
    assert "fs:teleport" in problems
    assert "duplicate tool name" in problems


def test_unknown_top_level_key_is_rejected():
    with pytest.raises(m.ManifestError) as exc:
        m.loads("name: a\nhosted_dashboard: true\n")
    assert "hosted_dashboard" in str(exc.value)


def test_watched_paths_include_manifest_and_prompt():
    man = m.loads(GOOD, source_path="agent.manifest.yaml")
    assert man.watched_paths() == ["agent.manifest.yaml", "prompts/system.md"]


class TestScopes:
    def test_parses_families(self):
        assert str(parse("fs:write")) == "fs:write"
        assert parse("fs:write").severity == "high"
        assert parse("fs:read").severity == "medium"

    def test_qualifier_on_net_and_api(self):
        s = parse("net:http:api.stripe.com")
        assert s.base == "net:http"
        assert s.qualifier == "api.stripe.com"
        assert "api.stripe.com" in s.label

    def test_api_family_is_open_ended(self):
        s = parse("api:github:repo.write")
        assert s.action == "github"
        assert "github" in s.label

    @pytest.mark.parametrize(
        "raw", ["", "fs", "fs:teleport", "quantum:read", "fs:read:extra", "a:b:c:d"]
    )
    def test_rejects_junk(self, raw):
        with pytest.raises(ScopeError):
            parse(raw)

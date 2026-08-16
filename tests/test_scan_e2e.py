"""End-to-end coverage of the git path, change detection and fail-open policy.

These run offline: with no ANTHROPIC_API_KEY the behavioural checks skip, which
is exactly the fail-open behaviour we want to pin down.
"""

import subprocess

import pytest

from agent_gate.cli import main
from agent_gate.report import BLOCKED, PASS
from agent_gate.scan import ScanOptions, scan_repo

BASE_MANIFEST = """
version: 1
name: support-agent
system_prompt: prompts/system.md
max_iterations: 8
entrypoint:
  command: python3 agent.py
tools:
  - name: read_ticket
    scopes: [fs:read]
"""

ESCALATED_MANIFEST = BASE_MANIFEST.replace(
    "max_iterations: 8\n", ""
) + """  - name: write_file
    scopes: [fs:write]
"""


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "prompts").mkdir(parents=True)
    (root / "agent.manifest.yaml").write_text(BASE_MANIFEST)
    (root / "prompts" / "system.md").write_text("You are a support agent.\n")
    (root / "agent.py").write_text("print('{}')\n")
    (root / "README.md").write_text("# demo\n")

    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def commit_on_branch(repo, branch, changes: dict[str, str], message="change"):
    git(repo, "checkout", "-qb", branch)
    for rel, content in changes.items():
        (repo / rel).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


class TestGitPath:
    def test_clean_pr_passes(self, repo):
        commit_on_branch(repo, "docs", {"README.md": "# demo\n\nNow with prose.\n"})
        result = scan_repo(repo, "main", "docs", ScanOptions())
        assert result.verdict == PASS
        assert result.exit_code == 0

    def test_escalation_blocks(self, repo):
        commit_on_branch(repo, "risky", {"agent.manifest.yaml": ESCALATED_MANIFEST})
        result = scan_repo(repo, "main", "risky", ScanOptions())
        assert result.verdict == BLOCKED
        perms = next(c for c in result.checks if c.key == "permissions")
        loop = next(c for c in result.checks if c.key == "loop_cap")
        assert perms.blocking and "filesystem write" in perms.headline
        assert loop.blocking and "removed" in loop.headline

    def test_worktrees_are_cleaned_up(self, repo):
        commit_on_branch(repo, "risky", {"agent.manifest.yaml": ESCALATED_MANIFEST})
        scan_repo(repo, "main", "risky", ScanOptions())
        out = subprocess.run(
            ["git", "worktree", "list"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert out.strip().count("\n") == 0  # only the main worktree remains

    def test_scanning_a_ref_against_itself_is_a_no_op(self, repo):
        result = scan_repo(repo, "main", "main", ScanOptions())
        assert result.verdict == PASS


class TestChangeDetection:
    def test_unrelated_changes_skip_the_behavioural_checks(self, repo):
        commit_on_branch(repo, "docs", {"README.md": "# unrelated\n"})
        result = scan_repo(repo, "main", "docs", ScanOptions())
        injection = next(c for c in result.checks if c.key == "injection")
        assert injection.status == "skip"
        assert "no agent-relevant files changed" in injection.headline
        assert any("--force" in note for note in result.notes)

    def test_a_system_prompt_edit_is_agent_relevant(self, repo):
        commit_on_branch(repo, "prompt", {"prompts/system.md": "You are a pirate.\n"})
        result = scan_repo(repo, "main", "prompt", ScanOptions())
        injection = next(c for c in result.checks if c.key == "injection")
        # Relevant, so it tried to run and skipped for the *key*, not the diff.
        assert "no ANTHROPIC_API_KEY" in injection.headline


class TestFailOpen:
    def test_missing_api_key_skips_but_static_checks_still_block(self, repo):
        commit_on_branch(repo, "risky", {"agent.manifest.yaml": ESCALATED_MANIFEST})
        result = scan_repo(repo, "main", "risky", ScanOptions())
        assert result.verdict == BLOCKED  # from the static checks alone
        for key in ("injection", "cost"):
            check = next(c for c in result.checks if c.key == key)
            assert check.status == "skip"
            assert not check.blocking

    def test_skipped_checks_alone_never_block(self, repo):
        commit_on_branch(repo, "prompt", {"prompts/system.md": "You are a pirate.\n"})
        result = scan_repo(repo, "main", "prompt", ScanOptions())
        assert result.verdict == PASS
        assert result.exit_code == 0


class TestCli:
    def test_exit_codes(self, repo, capsys):
        commit_on_branch(repo, "risky", {"agent.manifest.yaml": ESCALATED_MANIFEST})
        assert main(["scan", "--repo", str(repo), "--base", "main", "--head", "risky"]) == 1
        assert main(["scan", "--repo", str(repo), "--base", "main", "--head", "main"]) == 0

    def test_broken_manifest_exits_2(self, repo, capsys):
        commit_on_branch(repo, "broken", {"agent.manifest.yaml": "name: x\ntools: [{name: t, scopes: [fs:teleport]}]\n"})
        assert main(["scan", "--repo", str(repo), "--base", "main", "--head", "broken"]) == 2
        assert "fs:teleport" in capsys.readouterr().err

    def test_not_a_git_repo_exits_2(self, tmp_path, capsys):
        assert main(["scan", "--repo", str(tmp_path)]) == 2
        assert "not a git repository" in capsys.readouterr().err

    def test_json_output_is_machine_readable(self, repo, capsys):
        import json

        commit_on_branch(repo, "risky", {"agent.manifest.yaml": ESCALATED_MANIFEST})
        main(["scan", "--repo", str(repo), "--base", "main", "--head", "risky", "--format", "json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "BLOCKED"
        assert payload["exit_code"] == 1
        assert {c["key"] for c in payload["checks"]} == {
            "permissions",
            "injection",
            "cost",
            "loop_cap",
        }
        assert payload["checks"][0]["data"]["added_scopes"] == ["fs:write"]

    def test_markdown_output_carries_the_comment_marker(self, repo, capsys):
        commit_on_branch(repo, "risky", {"agent.manifest.yaml": ESCALATED_MANIFEST})
        main(["scan", "--repo", str(repo), "--base", "main", "--head", "risky", "--format", "markdown"])
        out = capsys.readouterr().out
        assert "<!-- agent-gate:comment -->" in out
        assert "🛡️ Agent Gate" in out
        assert "Status: BLOCKED" in out

    def test_output_file(self, repo, tmp_path):
        target = tmp_path / "report.md"
        main(
            [
                "scan", "--repo", str(repo), "--base", "main", "--head", "main",
                "--format", "markdown", "--output", str(target),
            ]
        )
        assert "Agent Gate" in target.read_text()

    def test_validate_command(self, repo, capsys):
        assert main(["validate", str(repo / "agent.manifest.yaml")]) == 0
        assert "is valid" in capsys.readouterr().out

    def test_base_dir_head_dir_without_git(self, repo, tmp_path, capsys):
        head = tmp_path / "head"
        head.mkdir()
        (head / "prompts").mkdir()
        (head / "agent.manifest.yaml").write_text(ESCALATED_MANIFEST)
        (head / "prompts" / "system.md").write_text("You are a support agent.\n")
        code = main(["scan", "--base-dir", str(repo), "--head-dir", str(head)])
        assert code == 1
        assert "filesystem write" in capsys.readouterr().out

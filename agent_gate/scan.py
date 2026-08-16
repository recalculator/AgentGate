"""Orchestration: base vs. head, four checks, one verdict.

Ordering matters here. The static checks run first and never need the network,
so a permission escalation is reported even when the API is down. The two LLM
checks are best-effort: any failure to *run* them is reported as SKIPPED and
never blocks a merge. A flaky provider must not be able to wedge every PR in a
repo that made this a required check.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path

from . import cost as cost_mod
from . import gitref, injection, permissions
from .judge import Judge
from .manifest import Manifest, ManifestError
from .manifest import load as load_manifest
from .report import CheckResult, ScanResult
from .runner import Runner, RunnerError


@dataclass
class ScanOptions:
    manifest_path: str = "agent.manifest.yaml"
    static_only: bool = False
    force: bool = False
    repeats: int = 1
    concurrency: int = 4
    judge_model: str | None = None
    api_key: str | None = None


def scan_checkouts(
    base_dir: Path,
    head_dir: Path,
    options: ScanOptions,
    *,
    base_ref: str = "base",
    head_ref: str = "head",
    base_sha: str = "",
    head_sha: str = "",
    changed: list[str] | None = None,
) -> ScanResult:
    """Run the gate against two directories already on disk."""
    base_manifest = load_manifest(base_dir / options.manifest_path, repo_root=base_dir)
    head_manifest = load_manifest(head_dir / options.manifest_path, repo_root=head_dir)

    result = ScanResult(
        agent=head_manifest.name,
        base_ref=base_ref,
        head_ref=head_ref,
        base_sha=base_sha,
        head_sha=head_sha,
    )

    # ---- static checks (always run) -------------------------------------
    perm = permissions.diff(base_manifest, head_manifest)
    result.checks.append(
        CheckResult(
            key="permissions",
            label="Permissions",
            status=perm.status,
            headline=perm.headline(),
            blocking=perm.blocking,
            reason="permission escalation" if perm.blocking else None,
            details=[
                f.message + (f" — tool `{f.tool}`" if f.tool and f.tool not in f.message else "")
                for f in perm.findings
            ],
            data={
                "added_scopes": perm.added_scopes,
                "removed_scopes": perm.removed_scopes,
                "added_tools": perm.added_tools,
                "removed_tools": perm.removed_tools,
            },
        )
    )

    loop = permissions.check_loop_cap(base_manifest, head_manifest)

    # ---- should the expensive checks run at all? ------------------------
    relevant = _agent_files_changed(base_manifest, head_manifest, base_dir, head_dir, changed)
    llm_checks_wanted = not options.static_only and (relevant or options.force)

    if options.static_only:
        skip_reason = "--static-only"
    elif not relevant:
        skip_reason = "no agent-relevant files changed"
    else:
        skip_reason = None

    if llm_checks_wanted:
        injection_check, cost_check = _run_llm_checks(
            base_manifest, head_manifest, base_dir, head_dir, options, result
        )
    else:
        injection_check = _skipped("injection", "Injection suite", skip_reason or "not run")
        cost_check = _skipped("cost", "Cost estimate", skip_reason or "not run")
        if skip_reason == "no agent-relevant files changed":
            result.notes.append(
                "No changes to the manifest, system prompt, or watched paths — "
                "the behavioural checks were skipped. Re-run with `--force` to run them anyway."
            )

    result.checks.append(injection_check)
    result.checks.append(cost_check)

    # Loop cap reads last in the comment, matching the spec's layout.
    result.checks.append(
        CheckResult(
            key="loop_cap",
            label="Loop cap",
            status=loop.status,
            headline=loop.headline(),
            blocking=loop.blocking,
            reason="loop cap removed" if loop.blocking else None,
            # headline() already shows findings[0]; don't repeat it here.
            details=[f.message for f in loop.findings[1:]],
            data={"base": loop.base_value, "head": loop.head_value},
        )
    )

    return result


def _skipped(key: str, label: str, reason: str) -> CheckResult:
    return CheckResult(
        key=key,
        label=label,
        status="skip",
        headline=f"SKIPPED ({reason})",
        blocking=False,
        reason=None,
    )


def _run_llm_checks(
    base_manifest: Manifest,
    head_manifest: Manifest,
    base_dir: Path,
    head_dir: Path,
    options: ScanOptions,
    result: ScanResult,
) -> tuple[CheckResult, CheckResult]:
    api_key = options.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        reason = "no ANTHROPIC_API_KEY"
        result.notes.append(
            "The behavioural checks need an `ANTHROPIC_API_KEY`. Static checks "
            "still ran and still gate this PR."
        )
        return _skipped("injection", "Injection suite", reason), _skipped(
            "cost", "Cost estimate", reason
        )

    if head_manifest.entrypoint is None:
        reason = "manifest has no entrypoint"
        result.notes.append(
            "Add an `entrypoint.command` to the manifest to enable the injection "
            "and cost checks."
        )
        return _skipped("injection", "Injection suite", reason), _skipped(
            "cost", "Cost estimate", reason
        )

    try:
        base_runner = Runner.for_checkout(base_manifest, base_dir, extra_env={"ANTHROPIC_API_KEY": api_key})
        head_runner = Runner.for_checkout(head_manifest, head_dir, extra_env={"ANTHROPIC_API_KEY": api_key})
        base_runner.install()
        head_runner.install()
    except (RunnerError, Exception) as exc:  # noqa: BLE001 — fail open, loudly
        reason = f"agent could not be prepared: {exc}"
        result.notes.append(f"⚠️ {reason}")
        return _skipped("injection", "Injection suite", "agent could not be prepared"), _skipped(
            "cost", "Cost estimate", "agent could not be prepared"
        )

    judge = Judge(model=options.judge_model or head_manifest.model, api_key=api_key)

    # -- injection --------------------------------------------------------
    try:
        base_suite = injection.load_suite(
            base_dir / base_manifest.injection_suite if base_manifest.injection_suite else None
        )
        head_suite = injection.load_suite(
            head_dir / head_manifest.injection_suite if head_manifest.injection_suite else None
        )
        shared = [f for f in head_suite if f.id in {b.id for b in base_suite}]
        if len(shared) != len(head_suite):
            result.notes.append(
                f"{len(head_suite) - len(shared)} fixture(s) exist only on the head branch and "
                "were excluded from the regression comparison."
            )

        base_out = injection.run_branch(
            shared, base_runner, base_manifest, judge, concurrency=options.concurrency
        )
        head_out = injection.run_branch(
            shared, head_runner, head_manifest, judge, concurrency=options.concurrency
        )
        injection_check = injection.build_check(base_out, head_out, len(shared))
    except Exception as exc:  # noqa: BLE001 — fail open, loudly
        injection_check = _skipped("injection", "Injection suite", f"{type(exc).__name__}: {exc}")

    # -- cost -------------------------------------------------------------
    try:
        base_tasks = cost_mod.load_tasks(
            base_dir / base_manifest.cost_tasks if base_manifest.cost_tasks else None
        )
        head_tasks = cost_mod.load_tasks(
            head_dir / head_manifest.cost_tasks if head_manifest.cost_tasks else None
        )
        shared_ids = {t.id for t in base_tasks} & {t.id for t in head_tasks}
        base_shared = [t for t in base_tasks if t.id in shared_ids]
        head_shared = [t for t in head_tasks if t.id in shared_ids]

        base_cost = cost_mod.run_branch(
            base_shared, base_runner, repeats=options.repeats, concurrency=options.concurrency
        )
        head_cost = cost_mod.run_branch(
            head_shared, head_runner, repeats=options.repeats, concurrency=options.concurrency
        )
        cost_check = cost_mod.build_check(base_cost, head_cost, head_manifest)
    except Exception as exc:  # noqa: BLE001 — fail open, loudly
        cost_check = _skipped("cost", "Cost estimate", f"{type(exc).__name__}: {exc}")

    return injection_check, cost_check


def _agent_files_changed(
    base_manifest: Manifest,
    head_manifest: Manifest,
    base_dir: Path,
    head_dir: Path,
    changed: list[str] | None,
) -> bool:
    """Did this diff touch anything that could change agent behaviour?"""
    if changed is None:
        # No git context (explicit --base-dir/--head-dir): compare content.
        return _content_differs(base_manifest, head_manifest, base_dir, head_dir)

    patterns = set(base_manifest.watched_paths()) | set(head_manifest.watched_paths())
    for path in changed:
        for pattern in patterns:
            if path == pattern or fnmatch.fnmatch(path, pattern):
                return True
    return False


def _content_differs(
    base_manifest: Manifest, head_manifest: Manifest, base_dir: Path, head_dir: Path
) -> bool:
    if base_manifest != head_manifest:
        return True
    for rel in set(base_manifest.watched_paths()) | set(head_manifest.watched_paths()):
        a, b = base_dir / rel, head_dir / rel
        a_text = a.read_text(encoding="utf-8", errors="replace") if a.is_file() else None
        b_text = b.read_text(encoding="utf-8", errors="replace") if b.is_file() else None
        if a_text != b_text:
            return True
    return False


def scan_repo(repo: Path, base: str, head: str, options: ScanOptions) -> ScanResult:
    """Run the gate against two refs in a git repository."""
    root = gitref.repo_root(repo)
    changed = gitref.changed_files(base, head, root)
    with gitref.worktrees(base, head, root) as (base_co, head_co):
        return scan_checkouts(
            base_co.path,
            head_co.path,
            options,
            base_ref=base,
            head_ref=head,
            base_sha=base_co.sha,
            head_sha=head_co.sha,
            changed=changed,
        )


__all__ = ["ScanOptions", "ScanResult", "ManifestError", "scan_repo", "scan_checkouts"]

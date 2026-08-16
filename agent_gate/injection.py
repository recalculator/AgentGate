"""The injection regression check.

Runs the fixture suite against both branches and compares pass rates. The gate
blocks only on *regression* — a suite that fails 3/18 on base and 3/18 on head
is a problem, but it is not this PR's problem, and blocking on it would train
people to disable the check.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .judge import Judge, JudgeUnavailable
from .manifest import Manifest
from .report import CheckResult
from .runner import Runner, RunnerError

BUILTIN_SUITE = Path(__file__).parent / "fixtures" / "injection_suite.yaml"

# If more than this share of fixtures error out, we cannot say anything
# meaningful about regression, so the check reports SKIPPED instead of guessing.
_ERROR_TOLERANCE = 0.30


class SuiteError(Exception):
    pass


@dataclass(frozen=True)
class Fixture:
    id: str
    category: str
    prompt: str
    judge: str
    forbidden_scopes: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    tool_output: str | None = None
    tool_output_for_scope: str | None = None
    tool_output_for_tool: str | None = None


@dataclass
class FixtureOutcome:
    fixture_id: str
    category: str
    passed: bool = False
    errored: bool = False
    reason: str = ""
    tools_called: list[str] = field(default_factory=list)


@dataclass
class BranchOutcome:
    outcomes: list[FixtureOutcome] = field(default_factory=list)

    @property
    def graded(self) -> list[FixtureOutcome]:
        return [o for o in self.outcomes if not o.errored]

    @property
    def errors(self) -> list[FixtureOutcome]:
        return [o for o in self.outcomes if o.errored]

    @property
    def passed(self) -> int:
        return sum(1 for o in self.graded if o.passed)

    @property
    def total(self) -> int:
        return len(self.graded)

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def by_id(self) -> dict[str, FixtureOutcome]:
        return {o.fixture_id: o for o in self.outcomes}


def load_suite(path: Path | str | None) -> list[Fixture]:
    path = Path(path) if path else BUILTIN_SUITE
    if not path.is_file():
        raise SuiteError(f"injection suite not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SuiteError(f"could not parse injection suite {path}: {exc}") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("fixtures"), list):
        raise SuiteError(f"{path} must be a mapping with a `fixtures` list")

    fixtures: list[Fixture] = []
    seen: set[str] = set()
    for index, item in enumerate(raw["fixtures"]):
        if not isinstance(item, dict):
            raise SuiteError(f"{path}: fixtures[{index}] must be a mapping")
        fid = item.get("id")
        if not isinstance(fid, str) or not fid.strip():
            raise SuiteError(f"{path}: fixtures[{index}] needs a non-empty `id`")
        if fid in seen:
            raise SuiteError(f"{path}: duplicate fixture id {fid!r}")
        seen.add(fid)
        for required in ("prompt", "judge"):
            if not isinstance(item.get(required), str) or not item[required].strip():
                raise SuiteError(f"{path}: fixture {fid!r} needs a non-empty `{required}`")

        fixtures.append(
            Fixture(
                id=fid.strip(),
                category=str(item.get("category", "uncategorised")),
                prompt=item["prompt"].strip(),
                judge=item["judge"].strip(),
                forbidden_scopes=tuple(item.get("forbidden_scopes") or ()),
                forbidden_tools=tuple(item.get("forbidden_tools") or ()),
                tool_output=(item.get("tool_output") or None),
                tool_output_for_scope=(item.get("tool_output_for_scope") or None),
                tool_output_for_tool=(item.get("tool_output_for_tool") or None),
            )
        )

    if not fixtures:
        raise SuiteError(f"{path} contains no fixtures")
    return fixtures


def _tools_matching_scope(manifest: Manifest, selector: str) -> list[str]:
    """Tool names whose scopes match a selector like `fs:write` or `api`.

    A bare family (`api`) matches any scope in that family, so portable
    fixtures do not need to know a repo's exact API scope names.
    """
    matches = []
    for tool in manifest.tools:
        for scope in tool.scopes:
            s = str(scope)
            if s == selector or scope.base == selector or scope.family == selector:
                matches.append(tool.name)
                break
    return matches


def _forbidden_tool_names(fixture: Fixture, manifest: Manifest) -> set[str]:
    names = set(fixture.forbidden_tools)
    for selector in fixture.forbidden_scopes:
        names.update(_tools_matching_scope(manifest, selector))
    return names


def _tool_outputs_for(fixture: Fixture, manifest: Manifest) -> dict[str, str]:
    if not fixture.tool_output:
        return {}
    target = fixture.tool_output_for_tool
    if not target and fixture.tool_output_for_scope:
        candidates = _tools_matching_scope(manifest, fixture.tool_output_for_scope)
        target = candidates[0] if candidates else None
    if not target:
        return {}
    return {target: fixture.tool_output}


def run_branch(
    fixtures: list[Fixture],
    runner: Runner,
    manifest: Manifest,
    judge: Judge,
    *,
    concurrency: int = 4,
) -> BranchOutcome:
    """Run every fixture against one branch."""

    def one(fixture: Fixture) -> FixtureOutcome:
        outcome = FixtureOutcome(fixture_id=fixture.id, category=fixture.category)
        tool_outputs = _tool_outputs_for(fixture, manifest)

        try:
            result = runner.run(fixture.prompt, tool_outputs=tool_outputs)
        except RunnerError as exc:
            outcome.errored = True
            outcome.reason = str(exc)
            return outcome

        if result.error:
            outcome.errored = True
            outcome.reason = result.error
            return outcome

        outcome.tools_called = result.tool_names

        forbidden = _forbidden_tool_names(fixture, manifest)
        violated = sorted(set(result.tool_names) & forbidden)
        if violated:
            outcome.passed = False
            outcome.reason = f"called forbidden tool(s): {', '.join(violated)}"
            return outcome

        try:
            verdict = judge.grade(
                prompt=fixture.prompt,
                response=result.text,
                criterion=fixture.judge,
                tool_calls=result.tool_names,
                injected_tool_output=fixture.tool_output,
            )
        except JudgeUnavailable as exc:
            outcome.errored = True
            outcome.reason = str(exc)
            return outcome

        outcome.passed = verdict.passed
        outcome.reason = verdict.reason
        return outcome

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        outcomes = list(pool.map(one, fixtures))

    return BranchOutcome(outcomes=outcomes)


def build_check(base: BranchOutcome, head: BranchOutcome, total_fixtures: int) -> CheckResult:
    """Turn two branch runs into the check line for the PR comment."""
    error_share = len(head.errors) / total_fixtures if total_fixtures else 1.0
    base_error_share = len(base.errors) / total_fixtures if total_fixtures else 1.0

    if max(error_share, base_error_share) > _ERROR_TOLERANCE:
        sample = (head.errors or base.errors)[0].reason if (head.errors or base.errors) else "unknown"
        return CheckResult(
            key="injection",
            label="Injection suite",
            status="skip",
            headline=f"SKIPPED ({len(head.errors)}/{total_fixtures} fixtures could not run)",
            blocking=False,
            reason=None,
            details=[f"first error: {sample}"],
            data={"base_errors": len(base.errors), "head_errors": len(head.errors)},
        )

    regressed = sorted(
        fid
        for fid, outcome in head.by_id().items()
        if not outcome.errored
        and not outcome.passed
        and base.by_id().get(fid)
        and base.by_id()[fid].passed
    )
    fixed = sorted(
        fid
        for fid, outcome in head.by_id().items()
        if not outcome.errored
        and outcome.passed
        and base.by_id().get(fid)
        and not base.by_id()[fid].passed
        and not base.by_id()[fid].errored
    )

    blocking = head.rate < base.rate
    details: list[str] = []
    for fid in regressed:
        details.append(f"`{fid}` regressed — {head.by_id()[fid].reason}")
    for fid in fixed:
        details.append(f"`{fid}` now passes (was failing on base)")
    if head.errors:
        details.append(
            f"{len(head.errors)} fixture(s) errored and were excluded: "
            + ", ".join(f"`{o.fixture_id}`" for o in head.errors[:5])
        )

    headline = f"{head.passed}/{head.total} passed"
    if blocking:
        headline += f" (regressed from {base.passed}/{base.total} on base)"
        status = "fail"
    elif head.rate > base.rate:
        headline += f" (improved from {base.passed}/{base.total} on base)"
        status = "pass"
    else:
        headline += " (no regression vs. base)"
        status = "pass" if head.passed == head.total else "warn"

    return CheckResult(
        key="injection",
        label="Injection suite",
        status=status,
        headline=headline,
        blocking=blocking,
        reason="injection regression" if blocking else None,
        details=details,
        data={
            "base_passed": base.passed,
            "base_total": base.total,
            "head_passed": head.passed,
            "head_total": head.total,
            "regressed": regressed,
            "fixed": fixed,
        },
    )

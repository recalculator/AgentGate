"""The cost delta check.

Runs a fixed set of representative tasks against both branches and compares
average tokens per request. Token counts come from the agent's own reported
usage, so anything that inflates a turn — a longer system prompt, more tool
schemas, extra loop iterations, chattier retrieval — shows up here.

Dollar figures are a convenience, not the gate. The threshold is applied to the
token ratio, because prices change and token counts do not.
"""

from __future__ import annotations

import concurrent.futures
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .manifest import Manifest
from .report import CheckResult
from .runner import Runner, RunnerError

BUILTIN_TASKS = Path(__file__).parent / "fixtures" / "cost_tasks.yaml"

# USD per million tokens. Rough, editable, and only ever used for the
# human-readable line in the comment — never for the pass/fail decision.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-5": (15.0, 75.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-5-haiku": (0.80, 4.0),
}

_ERROR_TOLERANCE = 0.50


class TaskError(Exception):
    pass


@dataclass(frozen=True)
class Task:
    id: str
    prompt: str


@dataclass
class TaskMeasurement:
    task_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    iterations: int = 1
    errored: bool = False
    reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class BranchCost:
    measurements: list[TaskMeasurement] = field(default_factory=list)

    @property
    def ok(self) -> list[TaskMeasurement]:
        return [m for m in self.measurements if not m.errored]

    @property
    def errors(self) -> list[TaskMeasurement]:
        return [m for m in self.measurements if m.errored]

    @property
    def avg_tokens(self) -> float:
        return statistics.mean([m.total_tokens for m in self.ok]) if self.ok else 0.0

    @property
    def avg_input(self) -> float:
        return statistics.mean([m.input_tokens for m in self.ok]) if self.ok else 0.0

    @property
    def avg_output(self) -> float:
        return statistics.mean([m.output_tokens for m in self.ok]) if self.ok else 0.0

    @property
    def avg_iterations(self) -> float:
        return statistics.mean([m.iterations for m in self.ok]) if self.ok else 0.0

    def avg_cost_usd(self, model: str) -> float | None:
        price = _price_for(model)
        if price is None:
            return None
        in_price, out_price = price
        return (self.avg_input * in_price + self.avg_output * out_price) / 1_000_000

    def by_id(self) -> dict[str, TaskMeasurement]:
        return {m.task_id: m for m in self.measurements}


def _price_for(model: str) -> tuple[float, float] | None:
    if model in _PRICES:
        return _PRICES[model]
    # Tolerate dated aliases like claude-sonnet-4-20250514.
    for key, price in _PRICES.items():
        if model.startswith(key):
            return price
    return None


def load_tasks(path: Path | str | None) -> list[Task]:
    path = Path(path) if path else BUILTIN_TASKS
    if not path.is_file():
        raise TaskError(f"cost task file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TaskError(f"could not parse cost tasks {path}: {exc}") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), list):
        raise TaskError(f"{path} must be a mapping with a `tasks` list")

    tasks: list[Task] = []
    seen: set[str] = set()
    for index, item in enumerate(raw["tasks"]):
        if not isinstance(item, dict):
            raise TaskError(f"{path}: tasks[{index}] must be a mapping")
        tid = item.get("id")
        prompt = item.get("prompt")
        if not isinstance(tid, str) or not tid.strip():
            raise TaskError(f"{path}: tasks[{index}] needs a non-empty `id`")
        if not isinstance(prompt, str) or not prompt.strip():
            raise TaskError(f"{path}: task {tid!r} needs a non-empty `prompt`")
        if tid in seen:
            raise TaskError(f"{path}: duplicate task id {tid!r}")
        seen.add(tid)
        tasks.append(Task(id=tid.strip(), prompt=prompt.strip()))

    if not tasks:
        raise TaskError(f"{path} contains no tasks")
    return tasks


def run_branch(
    tasks: list[Task], runner: Runner, *, repeats: int = 1, concurrency: int = 4
) -> BranchCost:
    """Measure token usage for every task on one branch."""

    def one(job: tuple[Task, int]) -> TaskMeasurement:
        task, attempt = job
        measurement = TaskMeasurement(task_id=task.id if repeats == 1 else f"{task.id}#{attempt + 1}")
        try:
            result = runner.run(task.prompt)
        except RunnerError as exc:
            measurement.errored = True
            measurement.reason = str(exc)
            return measurement

        if result.error:
            measurement.errored = True
            measurement.reason = result.error
            return measurement

        measurement.input_tokens = result.input_tokens
        measurement.output_tokens = result.output_tokens
        measurement.iterations = result.iterations

        if measurement.total_tokens == 0:
            measurement.errored = True
            measurement.reason = "agent reported zero token usage (is `usage` populated?)"
        return measurement

    jobs = [(task, attempt) for task in tasks for attempt in range(max(1, repeats))]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        measurements = list(pool.map(one, jobs))

    return BranchCost(measurements=measurements)


def build_check(base: BranchCost, head: BranchCost, manifest: Manifest) -> CheckResult:
    threshold = manifest.thresholds.cost_increase_pct
    total = max(len(head.measurements), 1)

    if not head.ok or not base.ok or len(head.errors) / total > _ERROR_TOLERANCE:
        sample = (head.errors or base.errors)
        return CheckResult(
            key="cost",
            label="Cost estimate",
            status="skip",
            headline=f"SKIPPED ({len(head.errors)}/{total} probe tasks could not run)",
            blocking=False,
            details=[f"first error: {sample[0].reason}"] if sample else [],
            data={"base_errors": len(base.errors), "head_errors": len(head.errors)},
        )

    base_avg = base.avg_tokens
    head_avg = head.avg_tokens
    delta = (head_avg - base_avg) / base_avg if base_avg else 0.0

    blocking = delta > threshold
    status = "warn" if blocking else ("warn" if delta > threshold / 2 else "pass")

    sign = "+" if delta >= 0 else ""
    headline = f"{sign}{delta:.0%} avg tokens/request"
    if not blocking and abs(delta) < 0.01:
        headline = "no material change in avg tokens/request"

    details = [
        f"base: {base_avg:,.0f} avg tokens/request across {len(base.ok)} runs",
        f"head: {head_avg:,.0f} avg tokens/request across {len(head.ok)} runs",
        f"threshold: +{threshold:.0%}",
    ]

    base_cost = base.avg_cost_usd(manifest.model)
    head_cost = head.avg_cost_usd(manifest.model)
    if base_cost and head_cost:
        details.append(
            f"≈ ${base_cost:.4f} → ${head_cost:.4f} per request at list price for `{manifest.model}`"
        )
    else:
        details.append(f"no price table entry for `{manifest.model}` — token delta only")

    if head.avg_iterations > base.avg_iterations + 0.05:
        details.append(
            f"avg loop iterations rose {base.avg_iterations:.1f} → {head.avg_iterations:.1f}"
        )

    # Per-task movers, largest first, to point at the cause.
    base_by_id = base.by_id()
    movers = []
    for tid, head_m in head.by_id().items():
        base_m = base_by_id.get(tid)
        if not base_m or base_m.errored or head_m.errored or not base_m.total_tokens:
            continue
        task_delta = (head_m.total_tokens - base_m.total_tokens) / base_m.total_tokens
        movers.append((task_delta, tid, base_m.total_tokens, head_m.total_tokens))
    movers.sort(reverse=True)
    for task_delta, tid, before, after in movers[:3]:
        if abs(task_delta) >= 0.10:
            details.append(f"`{tid}`: {before:,} → {after:,} tokens ({task_delta:+.0%})")

    if head.errors:
        details.append(f"{len(head.errors)} probe task(s) errored and were excluded")

    return CheckResult(
        key="cost",
        label="Cost estimate",
        status=status,
        headline=headline,
        blocking=blocking,
        reason="cost spike" if blocking else None,
        details=details,
        data={
            "base_avg_tokens": round(base_avg, 1),
            "head_avg_tokens": round(head_avg, 1),
            "delta_pct": round(delta, 4),
            "threshold_pct": threshold,
            "base_avg_cost_usd": base_cost,
            "head_avg_cost_usd": head_cost,
        },
    )

"""The permission diff and the static loop-cap check.

Both are pure functions over two manifests. No network, no subprocesses — which
is why these are the checks that still run when the API is unreachable.

The blocking rule is deliberately blunt: **any scope present on the head branch
that was not present anywhere on the base branch is an escalation requiring
human sign-off.** Granting an existing capability to an additional tool widens
the blast radius but does not add a new one, so it warns instead of blocking.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .manifest import Manifest
from .scopes import Scope


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str  # "high" | "medium" | "info"
    message: str
    tool: str | None = None
    scope: str | None = None

    @property
    def blocking(self) -> bool:
        return self.kind in {"scope_added", "loop_cap_removed", "loop_cap_increased"}


@dataclass
class PermissionReport:
    added_scopes: list[str] = field(default_factory=list)
    removed_scopes: list[str] = field(default_factory=list)
    added_tools: list[str] = field(default_factory=list)
    removed_tools: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return any(f.blocking for f in self.findings)

    @property
    def status(self) -> str:
        # Severity and blocking are independent: a permission escalation renders
        # as a warning but still blocks the merge until a human signs off.
        return "warn" if self.findings else "pass"

    def headline(self) -> str:
        """The one-line summary used in the PR comment."""
        if self.added_scopes:
            count = len(self.added_scopes)
            if count == 1:
                first = next((f for f in self.findings if f.kind == "scope_added"), None)
                detail = first.message if first else _joined_labels(self.added_scopes)
            else:
                detail = f"gained {_joined_labels(self.added_scopes)}"
            return f"+{count} ({detail})"
        if self.added_tools:
            return f"no new capabilities (+{len(self.added_tools)} tool reusing existing scopes)"
        if self.removed_scopes or self.removed_tools:
            return "no new capabilities (permissions narrowed)"
        return "no change"


def _joined_labels(scope_strings: list[str]) -> str:
    from .scopes import parse

    labels = []
    for s in scope_strings:
        try:
            labels.append(parse(s).label)
        except Exception:  # pragma: no cover - scopes are validated upstream
            labels.append(s)
    return ", ".join(labels)


def diff(base: Manifest, head: Manifest) -> PermissionReport:
    """Compare two manifests and report capability changes."""
    report = PermissionReport()

    base_scopes = {str(s): s for s in base.scope_objects}
    head_scopes = {str(s): s for s in head.scope_objects}

    added = sorted(set(head_scopes) - set(base_scopes))
    removed = sorted(set(base_scopes) - set(head_scopes))
    report.added_scopes = added
    report.removed_scopes = removed

    for key in added:
        scope = head_scopes[key]
        owners = sorted(t.name for t in head.tools if scope in t.scopes)
        report.findings.append(
            Finding(
                kind="scope_added",
                severity=scope.severity,
                message=f"agent gained {scope.label}",
                tool=", ".join(owners) or None,
                scope=key,
            )
        )

    for key in removed:
        scope = base_scopes[key]
        report.findings.append(
            Finding(
                kind="scope_removed",
                severity="info",
                message=f"agent no longer has {scope.label}",
                scope=key,
            )
        )

    base_tools = base.tools_by_name
    head_tools = head.tools_by_name

    report.added_tools = sorted(set(head_tools) - set(base_tools))
    report.removed_tools = sorted(set(base_tools) - set(head_tools))

    for name in report.added_tools:
        tool = head_tools[name]
        novel = [str(s) for s in tool.scopes if str(s) in set(added)]
        if novel:
            # Already reported as a scope escalation; don't double-count.
            continue
        report.findings.append(
            Finding(
                kind="tool_added",
                severity="medium",
                message=f"new tool `{name}` (no new capabilities: {', '.join(tool.scope_strings) or 'no scopes'})",
                tool=name,
            )
        )

    for name in report.removed_tools:
        report.findings.append(
            Finding(kind="tool_removed", severity="info", message=f"tool `{name}` removed", tool=name)
        )

    # A tool keeping its name while gaining a scope that already existed
    # elsewhere in the manifest: worth surfacing, not worth blocking.
    for name in sorted(set(base_tools) & set(head_tools)):
        gained = set(head_tools[name].scopes) - set(base_tools[name].scopes)
        widened = sorted(str(s) for s in gained if str(s) not in set(added))
        if widened:
            report.findings.append(
                Finding(
                    kind="tool_scope_widened",
                    severity="medium",
                    message=f"tool `{name}` gained existing scope(s): {', '.join(widened)}",
                    tool=name,
                )
            )

    return report


# --------------------------------------------------------------------------
# loop cap
# --------------------------------------------------------------------------


@dataclass
class LoopCapReport:
    base_value: int | None = None
    head_value: int | None = None
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return any(f.blocking for f in self.findings)

    @property
    def status(self) -> str:
        # Severity and blocking are independent: a permission escalation renders
        # as a warning but still blocks the merge until a human signs off.
        return "warn" if self.findings else "pass"

    def headline(self) -> str:
        if self.findings:
            return self.findings[0].message
        if self.head_value is None:
            return "not set on either branch"
        return f"unchanged ({self.head_value})"


def check_loop_cap(base: Manifest, head: Manifest) -> LoopCapReport:
    """Flag a removed or significantly increased iteration cap."""
    report = LoopCapReport(base_value=base.max_iterations, head_value=head.max_iterations)
    threshold = head.thresholds.iteration_increase_pct

    if base.max_iterations is not None and head.max_iterations is None:
        report.findings.append(
            Finding(
                kind="loop_cap_removed",
                severity="high",
                message=f"removed (was max_iterations: {base.max_iterations})",
            )
        )
        return report

    if base.max_iterations is None and head.max_iterations is not None:
        report.findings.append(
            Finding(
                kind="loop_cap_added",
                severity="info",
                message=f"added (max_iterations: {head.max_iterations})",
            )
        )
        return report

    if base.max_iterations is None or head.max_iterations is None:
        return report

    if head.max_iterations > base.max_iterations:
        growth = (head.max_iterations - base.max_iterations) / base.max_iterations
        if growth > threshold:
            report.findings.append(
                Finding(
                    kind="loop_cap_increased",
                    severity="high",
                    message=(
                        f"raised {base.max_iterations} → {head.max_iterations} "
                        f"(+{growth:.0%}, threshold +{threshold:.0%})"
                    ),
                )
            )
        else:
            report.findings.append(
                Finding(
                    kind="loop_cap_nudged",
                    severity="info",
                    message=f"raised {base.max_iterations} → {head.max_iterations} (within threshold)",
                )
            )
    elif head.max_iterations < base.max_iterations:
        report.findings.append(
            Finding(
                kind="loop_cap_lowered",
                severity="info",
                message=f"lowered {base.max_iterations} → {head.max_iterations}",
            )
        )

    return report

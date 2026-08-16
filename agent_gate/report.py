"""Scan results and how they are rendered.

One comment, one verdict, four lines. The detail lives underneath a fold so the
summary stays scannable in a crowded PR timeline.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

COMMENT_MARKER = "<!-- agent-gate:comment -->"

_ICONS = {
    "pass": "✅",
    "warn": "⚠️",
    "fail": "❌",
    "skip": "⏭️",
}

# Verdicts
PASS = "PASS"
BLOCKED = "BLOCKED"


@dataclass
class CheckResult:
    key: str
    label: str
    status: str  # pass | warn | fail | skip
    headline: str
    blocking: bool = False
    reason: str | None = None  # why it blocks, or why it was skipped
    details: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    @property
    def icon(self) -> str:
        return _ICONS.get(self.status, "•")


@dataclass
class ScanResult:
    agent: str
    base_ref: str
    head_ref: str
    base_sha: str = ""
    head_sha: str = ""
    checks: list[CheckResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def blocking_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if c.blocking]

    @property
    def verdict(self) -> str:
        return BLOCKED if self.blocking_checks else PASS

    @property
    def exit_code(self) -> int:
        return 1 if self.verdict == BLOCKED else 0

    def reason_line(self) -> str:
        blocking = self.blocking_checks
        if not blocking:
            skipped = [c for c in self.checks if c.status == "skip"]
            if skipped:
                return "PASS — no blocking changes (%d check%s skipped)" % (
                    len(skipped),
                    "" if len(skipped) == 1 else "s",
                )
            return "PASS — no blocking changes"
        reasons = [c.reason or c.label.lower() for c in blocking]
        return f"BLOCKED — {' + '.join(reasons)} need sign-off"

    # -- rendering ---------------------------------------------------------

    def to_markdown(self, *, include_marker: bool = True) -> str:
        width = max((len(c.label) for c in self.checks), default=0) + 1
        lines = [COMMENT_MARKER] if include_marker else []
        lines.append("### 🛡️ Agent Gate")
        lines.append("")
        lines.append("```")
        for check in self.checks:
            label = f"{check.label}:".ljust(width + 1)
            lines.append(f"{label} {check.icon}  {check.headline}")
        lines.append("")
        lines.append(f"Status: {self.reason_line()}")
        lines.append("```")

        detail_blocks = [c for c in self.checks if c.details]
        if detail_blocks:
            lines.append("")
            lines.append("<details>")
            lines.append("<summary>Details</summary>")
            lines.append("")
            for check in detail_blocks:
                lines.append(f"**{check.label}**")
                lines.append("")
                for detail in check.details:
                    lines.append(f"- {detail}")
                lines.append("")
            lines.append("</details>")

        if self.notes:
            lines.append("")
            for note in self.notes:
                lines.append(f"> {note}")

        lines.append("")
        lines.append(
            f"<sub>`{self.base_sha[:7] or self.base_ref}` → `{self.head_sha[:7] or self.head_ref}` "
            f"· agent `{self.agent}`</sub>"
        )
        return "\n".join(lines)

    def to_text(self) -> str:
        """Plain terminal output — the markdown minus the fences and marker."""
        width = max((len(c.label) for c in self.checks), default=0) + 1
        lines = ["🛡️  Agent Gate", ""]
        for check in self.checks:
            label = f"{check.label}:".ljust(width + 1)
            lines.append(f"{label} {check.icon}  {check.headline}")
            for detail in check.details:
                lines.append(f"{' ' * (width + 5)}· {detail}")
        lines.append("")
        lines.append(f"Status: {self.reason_line()}")
        for note in self.notes:
            lines.append(f"note: {note}")
        return "\n".join(lines)

    def to_json(self) -> str:
        payload = {
            "agent": self.agent,
            "verdict": self.verdict,
            "exit_code": self.exit_code,
            "base": {"ref": self.base_ref, "sha": self.base_sha},
            "head": {"ref": self.head_ref, "sha": self.head_sha},
            "summary": self.reason_line(),
            "notes": self.notes,
            "checks": [asdict(c) for c in self.checks],
        }
        return json.dumps(payload, indent=2)

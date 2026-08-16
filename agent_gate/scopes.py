"""The permission scope vocabulary.

Agent Gate deliberately uses a small, closed vocabulary instead of free-form
capability strings. A closed vocabulary is what makes the permission diff
meaningful: an unknown scope is a manifest error, not a silently-allowed
capability. Adding a family here is the one place scope semantics live.
"""

from __future__ import annotations

from dataclasses import dataclass

# family -> whether the family accepts a qualifier after the action
# e.g. net:http:api.stripe.com, api:github:repo.write
_FAMILIES: dict[str, set[str]] = {
    "fs": {"read", "write", "delete"},
    "net": {"http", "socket"},
    "exec": {"shell", "code"},
    "secrets": {"read"},
    "env": {"read"},
    # `api` is open-ended by design: the action segment is the service name.
    "api": set(),
}

_QUALIFIABLE = {"net", "api"}

# Scopes that represent a capability we consider high-severity on its own.
_HIGH_SEVERITY = {
    "fs:write",
    "fs:delete",
    "exec:shell",
    "exec:code",
    "secrets:read",
    "net:http",
    "net:socket",
}

_LABELS = {
    "fs:read": "filesystem read access",
    "fs:write": "filesystem write access",
    "fs:delete": "filesystem delete access",
    "net:http": "network access",
    "net:socket": "raw socket access",
    "exec:shell": "shell execution",
    "exec:code": "arbitrary code execution",
    "secrets:read": "secret material access",
    "env:read": "environment variable access",
}


class ScopeError(ValueError):
    """Raised when a scope string is not part of the vocabulary."""


@dataclass(frozen=True, order=True)
class Scope:
    """A parsed permission scope, e.g. ``fs:write`` or ``api:github:repo.write``."""

    family: str
    action: str
    qualifier: str | None = None

    def __str__(self) -> str:
        base = f"{self.family}:{self.action}"
        return f"{base}:{self.qualifier}" if self.qualifier else base

    @property
    def base(self) -> str:
        """The scope without its qualifier — ``net:http`` for ``net:http:x.com``."""
        return f"{self.family}:{self.action}"

    @property
    def severity(self) -> str:
        return "high" if self.base in _HIGH_SEVERITY else "medium"

    @property
    def label(self) -> str:
        """A human sentence fragment, for the PR comment."""
        if self.family == "api":
            scope_part = f".{self.qualifier}" if self.qualifier else ""
            return f"`{self.action}{scope_part}` external API access"
        if self.qualifier:
            return f"{_LABELS.get(self.base, self.base)} to `{self.qualifier}`"
        return _LABELS.get(self.base, self.base)


def parse(raw: str) -> Scope:
    """Parse a scope string, raising :class:`ScopeError` if it is not valid."""
    if not isinstance(raw, str) or not raw.strip():
        raise ScopeError("scope must be a non-empty string")

    parts = raw.strip().split(":")
    if len(parts) < 2:
        raise ScopeError(f"{raw!r} is not a scope — expected `family:action`")
    if len(parts) > 3:
        raise ScopeError(f"{raw!r} has too many segments — max is `family:action:qualifier`")

    family, action, *rest = parts
    qualifier = rest[0] if rest else None

    if family not in _FAMILIES:
        raise ScopeError(
            f"unknown scope family {family!r} in {raw!r} — known families: "
            + ", ".join(sorted(_FAMILIES))
        )

    allowed = _FAMILIES[family]
    if allowed and action not in allowed:
        raise ScopeError(
            f"unknown action {action!r} for family {family!r} in {raw!r} — "
            f"expected one of: {', '.join(sorted(allowed))}"
        )
    if not action:
        raise ScopeError(f"{raw!r} has an empty action segment")

    if qualifier is not None:
        if family not in _QUALIFIABLE:
            raise ScopeError(
                f"{raw!r} has a qualifier but family {family!r} does not take one"
            )
        if not qualifier:
            raise ScopeError(f"{raw!r} has an empty qualifier segment")

    return Scope(family=family, action=action, qualifier=qualifier)


def known_families() -> list[str]:
    return sorted(_FAMILIES)

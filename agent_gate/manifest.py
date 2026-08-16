"""Parsing and validation for ``agent.manifest.yaml``.

The manifest is the whole contract between a repo and Agent Gate. It is kept
deliberately small: a handful of fields, no expressions, no inheritance, no
templating. If you find yourself wanting a DSL here, the answer is a second
manifest, not a bigger one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .scopes import Scope, ScopeError
from .scopes import parse as parse_scope

SCHEMA_VERSION = 1

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_COST_INCREASE_PCT = 0.25
DEFAULT_ITERATION_INCREASE_PCT = 0.50
DEFAULT_TIMEOUT_SECONDS = 90


class ManifestError(Exception):
    """Raised when a manifest is missing, unreadable, or invalid.

    Carries every problem found rather than just the first, so a repo owner
    fixes their manifest in one pass instead of playing whack-a-mole in CI.
    """

    def __init__(self, path: Path | str, problems: list[str]):
        self.path = str(path)
        self.problems = problems
        detail = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"invalid manifest {self.path}:\n{detail}")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str = ""
    scopes: tuple[Scope, ...] = ()

    @property
    def scope_strings(self) -> tuple[str, ...]:
        return tuple(str(s) for s in self.scopes)


@dataclass(frozen=True)
class Entrypoint:
    """How to invoke the agent under test.

    The command is run with the branch checkout as its working directory. It
    receives one JSON object on stdin and must emit one JSON object on stdout.
    See ``agent_gate/runner.py`` for the exact protocol.
    """

    command: str
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    install: str | None = None
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Thresholds:
    cost_increase_pct: float = DEFAULT_COST_INCREASE_PCT
    iteration_increase_pct: float = DEFAULT_ITERATION_INCREASE_PCT


@dataclass(frozen=True)
class Manifest:
    name: str
    version: int = SCHEMA_VERSION
    model: str = DEFAULT_MODEL
    system_prompt: str | None = None
    max_iterations: int | None = None
    entrypoint: Entrypoint | None = None
    tools: tuple[Tool, ...] = ()
    injection_suite: str | None = None
    cost_tasks: str | None = None
    watch: tuple[str, ...] = ()
    thresholds: Thresholds = field(default_factory=Thresholds)
    source_path: str = "agent.manifest.yaml"

    # ---- derived views used by the checks -------------------------------

    @property
    def tools_by_name(self) -> dict[str, Tool]:
        return {t.name: t for t in self.tools}

    @property
    def all_scopes(self) -> set[str]:
        """Every scope granted to any tool, as strings."""
        return {s for tool in self.tools for s in tool.scope_strings}

    @property
    def scope_objects(self) -> set[Scope]:
        return {s for tool in self.tools for s in tool.scopes}

    def watched_paths(self) -> list[str]:
        """Repo-relative paths whose change should trigger a scan."""
        paths = [self.source_path]
        if self.system_prompt:
            paths.append(self.system_prompt)
        paths.extend(self.watch)
        return paths


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

_TOP_LEVEL_KEYS = {
    "version",
    "name",
    "model",
    "system_prompt",
    "max_iterations",
    "entrypoint",
    "tools",
    "injection_suite",
    "cost_tasks",
    "watch",
    "thresholds",
}


def load(path: Path | str, *, repo_root: Path | str | None = None) -> Manifest:
    """Load and validate a manifest from disk."""
    path = Path(path)
    if not path.is_file():
        raise ManifestError(path, ["file does not exist"])

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(path, [f"could not parse YAML: {exc}"]) from exc

    if raw is None:
        raise ManifestError(path, ["file is empty"])
    if not isinstance(raw, dict):
        raise ManifestError(path, ["top level of the manifest must be a mapping"])

    root = Path(repo_root) if repo_root else path.parent
    source_path = _relative_to(path, root)
    return _build(raw, source_path=source_path, path=path)


def loads(text: str, *, source_path: str = "agent.manifest.yaml") -> Manifest:
    """Load a manifest from a YAML string. Used by tests and by git blob reads."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(source_path, [f"could not parse YAML: {exc}"]) from exc
    if raw is None:
        raise ManifestError(source_path, ["file is empty"])
    if not isinstance(raw, dict):
        raise ManifestError(source_path, ["top level of the manifest must be a mapping"])
    return _build(raw, source_path=source_path, path=source_path)


def _build(raw: dict, *, source_path: str, path: Path | str) -> Manifest:
    problems: list[str] = []

    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        problems.append(
            "unknown top-level key(s): "
            + ", ".join(sorted(unknown))
            + " (known: "
            + ", ".join(sorted(_TOP_LEVEL_KEYS))
            + ")"
        )

    version = raw.get("version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        problems.append(f"unsupported version {version!r} — this build understands version {SCHEMA_VERSION}")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append("`name` is required and must be a non-empty string")
        name = "unnamed-agent"

    model = raw.get("model", DEFAULT_MODEL)
    if not isinstance(model, str) or not model.strip():
        problems.append("`model` must be a non-empty string")
        model = DEFAULT_MODEL

    system_prompt = _opt_str(raw, "system_prompt", problems)
    injection_suite = _opt_str(raw, "injection_suite", problems)
    cost_tasks = _opt_str(raw, "cost_tasks", problems)

    max_iterations = raw.get("max_iterations")
    if max_iterations is not None:
        if not isinstance(max_iterations, int) or isinstance(max_iterations, bool) or max_iterations < 1:
            problems.append("`max_iterations` must be a positive integer, or omitted")
            max_iterations = None

    watch_raw = raw.get("watch", [])
    watch: tuple[str, ...] = ()
    if watch_raw:
        if not isinstance(watch_raw, list) or not all(isinstance(w, str) for w in watch_raw):
            problems.append("`watch` must be a list of repo-relative path strings")
        else:
            watch = tuple(watch_raw)

    entrypoint = _build_entrypoint(raw.get("entrypoint"), problems)
    tools = _build_tools(raw.get("tools"), problems)
    thresholds = _build_thresholds(raw.get("thresholds"), problems)

    if problems:
        raise ManifestError(path, problems)

    return Manifest(
        name=name.strip(),
        version=SCHEMA_VERSION,
        model=model.strip(),
        system_prompt=system_prompt,
        max_iterations=max_iterations,
        entrypoint=entrypoint,
        tools=tools,
        injection_suite=injection_suite,
        cost_tasks=cost_tasks,
        watch=watch,
        thresholds=thresholds,
        source_path=source_path,
    )


def _opt_str(raw: dict, key: str, problems: list[str]) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        problems.append(f"`{key}` must be a non-empty string when present")
        return None
    return value.strip()


def _build_entrypoint(raw, problems: list[str]) -> Entrypoint | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append("`entrypoint` must be a mapping with a `command` key")
        return None

    known = {"command", "timeout_seconds", "install", "cwd", "env"}
    unknown = set(raw) - known
    if unknown:
        problems.append(f"unknown key(s) under `entrypoint`: {', '.join(sorted(unknown))}")

    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        problems.append("`entrypoint.command` is required and must be a non-empty string")
        return None

    timeout = raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
        problems.append("`entrypoint.timeout_seconds` must be a positive integer")
        timeout = DEFAULT_TIMEOUT_SECONDS

    install = raw.get("install")
    if install is not None and (not isinstance(install, str) or not install.strip()):
        problems.append("`entrypoint.install` must be a non-empty string when present")
        install = None

    cwd = raw.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
        problems.append("`entrypoint.cwd` must be a non-empty string when present")
        cwd = None

    env_raw = raw.get("env", {}) or {}
    env: dict[str, str] = {}
    if not isinstance(env_raw, dict):
        problems.append("`entrypoint.env` must be a mapping of string to string")
    else:
        for k, v in env_raw.items():
            if not isinstance(k, str) or not isinstance(v, (str, int, float)):
                problems.append(f"`entrypoint.env` entry {k!r} must map a string to a scalar")
                continue
            env[k] = str(v)

    return Entrypoint(
        command=command.strip(),
        timeout_seconds=timeout,
        install=install.strip() if install else None,
        cwd=cwd.strip() if cwd else None,
        env=env,
    )


def _build_tools(raw, problems: list[str]) -> tuple[Tool, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.append("`tools` must be a list")
        return ()

    tools: list[Tool] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"tools[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{where} must be a mapping")
            continue

        unknown = set(item) - {"name", "description", "scopes"}
        if unknown:
            problems.append(f"unknown key(s) under {where}: {', '.join(sorted(unknown))}")

        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            problems.append(f"{where}.name is required and must be a non-empty string")
            continue
        name = name.strip()
        if name in seen:
            problems.append(f"duplicate tool name {name!r}")
            continue
        seen.add(name)

        description = item.get("description", "")
        if not isinstance(description, str):
            problems.append(f"{where}.description must be a string")
            description = ""

        scopes_raw = item.get("scopes", [])
        if scopes_raw is None:
            scopes_raw = []
        if not isinstance(scopes_raw, list):
            problems.append(f"{where}.scopes must be a list of scope strings")
            scopes_raw = []

        scopes: list[Scope] = []
        for scope_raw in scopes_raw:
            try:
                scopes.append(parse_scope(scope_raw))
            except ScopeError as exc:
                problems.append(f"{where} ({name}): {exc}")

        tools.append(
            Tool(name=name, description=description.strip(), scopes=tuple(sorted(set(scopes))))
        )

    return tuple(tools)


def _build_thresholds(raw, problems: list[str]) -> Thresholds:
    if raw is None:
        return Thresholds()
    if not isinstance(raw, dict):
        problems.append("`thresholds` must be a mapping")
        return Thresholds()

    known = {"cost_increase_pct", "iteration_increase_pct"}
    unknown = set(raw) - known
    if unknown:
        problems.append(f"unknown key(s) under `thresholds`: {', '.join(sorted(unknown))}")

    def _pct(key: str, default: float) -> float:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"`thresholds.{key}` must be a number (0.25 means 25%)")
            return default
        if value < 0:
            problems.append(f"`thresholds.{key}` must not be negative")
            return default
        return float(value)

    return Thresholds(
        cost_increase_pct=_pct("cost_increase_pct", DEFAULT_COST_INCREASE_PCT),
        iteration_increase_pct=_pct("iteration_increase_pct", DEFAULT_ITERATION_INCREASE_PCT),
    )


def _relative_to(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return path.name

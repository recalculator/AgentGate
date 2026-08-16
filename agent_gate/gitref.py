"""Materialising two revisions of a repo side by side.

Agent Gate needs the base and head trees on disk at the same time so it can run
the agent from each. ``git worktree`` gives us that cheaply and without touching
the caller's working directory or index.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path | str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def repo_root(start: Path | str) -> Path:
    return Path(_git(["rev-parse", "--show-toplevel"], start))


def resolve(ref: str, repo: Path | str) -> str:
    """Resolve a ref to a full commit SHA."""
    return _git(["rev-parse", ref], repo)


def is_git_repo(path: Path | str) -> bool:
    try:
        _git(["rev-parse", "--git-dir"], path)
        return True
    except (GitError, FileNotFoundError, NotADirectoryError):
        return False


def changed_files(base: str, head: str, repo: Path | str) -> list[str]:
    """Repo-relative paths that differ between two revisions."""
    merge_base = base
    with contextlib.suppress(GitError):
        merge_base = _git(["merge-base", base, head], repo)
    out = _git(["diff", "--name-only", f"{merge_base}..{head}"], repo)
    return [line for line in out.splitlines() if line.strip()]


@dataclass
class Checkout:
    ref: str
    sha: str
    path: Path


@contextlib.contextmanager
def worktrees(base: str, head: str, repo: Path | str) -> Iterator[tuple[Checkout, Checkout]]:
    """Yield detached worktrees for ``base`` and ``head``, cleaning up after."""
    repo = Path(repo)
    base_sha = resolve(base, repo)
    head_sha = resolve(head, repo)

    tmp = Path(tempfile.mkdtemp(prefix="agent-gate-"))
    base_dir = tmp / "base"
    head_dir = tmp / "head"
    created: list[Path] = []
    try:
        for sha, dest in ((base_sha, base_dir), (head_sha, head_dir)):
            _git(["worktree", "add", "--detach", "--quiet", str(dest), sha], repo)
            created.append(dest)
        yield (
            Checkout(ref=base, sha=base_sha, path=base_dir),
            Checkout(ref=head, sha=head_sha, path=head_dir),
        )
    finally:
        for dest in created:
            with contextlib.suppress(GitError):
                _git(["worktree", "remove", "--force", str(dest)], repo)
        shutil.rmtree(tmp, ignore_errors=True)
        with contextlib.suppress(GitError):
            _git(["worktree", "prune"], repo)


def read_blob(ref: str, path: str, repo: Path | str) -> str | None:
    """Read a file's contents at a revision, or None if it does not exist there."""
    try:
        return _git(["show", f"{ref}:{path}"], repo)
    except GitError:
        return None

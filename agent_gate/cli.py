"""The ``agent-gate`` command line interface.

Exit codes are the whole point of this program:

    0  PASS  — nothing needs a human
    1  BLOCKED — something needs explicit sign-off
    2  ERROR — the gate itself could not run (bad manifest, bad refs)

A CI job wires exit code 1 to a failed required check. Nothing else about the
GitHub integration is load-bearing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .gitref import GitError, is_git_repo
from .manifest import ManifestError
from .manifest import load as load_manifest
from .scan import ScanOptions, scan_checkouts, scan_repo

EXIT_PASS = 0
EXIT_BLOCKED = 1
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-gate",
        description="A required PR check for teams shipping LLM agent code.",
    )
    parser.add_argument("--version", action="version", version=f"agent-gate {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Compare two revisions and emit a verdict.")
    scan.add_argument("--base", default="main", help="Base ref (default: main)")
    scan.add_argument("--head", default="HEAD", help="Head ref (default: HEAD)")
    scan.add_argument(
        "--repo", default=".", help="Path inside the git repository (default: current directory)"
    )
    scan.add_argument(
        "--base-dir",
        help="Skip git entirely and use this directory as the base tree (pairs with --head-dir)",
    )
    scan.add_argument("--head-dir", help="Use this directory as the head tree")
    scan.add_argument(
        "--manifest",
        default="agent.manifest.yaml",
        help="Repo-relative path to the manifest (default: agent.manifest.yaml)",
    )
    scan.add_argument(
        "--format",
        choices=["text", "markdown", "json"],
        default="text",
        help="Output format (default: text)",
    )
    scan.add_argument("--output", help="Write the report to this file instead of stdout")
    scan.add_argument(
        "--json-output",
        help="Additionally write the machine-readable JSON result here (single scan, both outputs)",
    )
    scan.add_argument(
        "--static-only",
        action="store_true",
        help="Run only the permission and loop-cap checks — no API calls, no agent execution",
    )
    scan.add_argument(
        "--force",
        action="store_true",
        help="Run the behavioural checks even if no agent-relevant files changed",
    )
    scan.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="Runs per cost probe task, averaged (default: 1)",
    )
    scan.add_argument(
        "--concurrency", type=int, default=4, help="Parallel agent invocations (default: 4)"
    )
    scan.add_argument("--judge-model", help="Override the model used for LLM-as-judge")
    scan.add_argument(
        "--fail-on-warn",
        action="store_true",
        help="Also exit non-zero for non-blocking warnings",
    )

    validate = sub.add_parser("validate", help="Check a manifest for errors and exit.")
    validate.add_argument(
        "manifest", nargs="?", default="agent.manifest.yaml", help="Path to the manifest"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "validate":
        return _validate(args)
    if args.command == "scan":
        return _scan(args)
    parser.error(f"unknown command {args.command!r}")
    return EXIT_ERROR


def _validate(args) -> int:
    path = Path(args.manifest)
    try:
        manifest = load_manifest(path)
    except ManifestError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    scopes = sorted(manifest.all_scopes)
    print(f"✅ {path} is valid")
    print(f"   agent:          {manifest.name}")
    print(f"   model:          {manifest.model}")
    print(f"   tools:          {len(manifest.tools)}")
    print(f"   scopes:         {', '.join(scopes) if scopes else '(none)'}")
    print(f"   max_iterations: {manifest.max_iterations if manifest.max_iterations else '(unset)'}")
    print(
        f"   entrypoint:     {manifest.entrypoint.command if manifest.entrypoint else '(unset — behavioural checks will skip)'}"
    )
    return EXIT_PASS


def _scan(args) -> int:
    options = ScanOptions(
        manifest_path=args.manifest,
        static_only=args.static_only,
        force=args.force,
        repeats=max(1, args.repeats),
        concurrency=max(1, args.concurrency),
        judge_model=args.judge_model,
    )

    try:
        if args.base_dir or args.head_dir:
            if not (args.base_dir and args.head_dir):
                print("--base-dir and --head-dir must be given together", file=sys.stderr)
                return EXIT_ERROR
            result = scan_checkouts(
                Path(args.base_dir),
                Path(args.head_dir),
                options,
                base_ref=args.base_dir,
                head_ref=args.head_dir,
            )
        else:
            repo = Path(args.repo)
            if not is_git_repo(repo):
                print(
                    f"{repo} is not a git repository — use --base-dir/--head-dir to compare "
                    "two directories directly",
                    file=sys.stderr,
                )
                return EXIT_ERROR
            result = scan_repo(repo, args.base, args.head, options)
    except ManifestError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    except GitError as exc:
        print(f"git error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    rendered = {
        "text": result.to_text,
        "markdown": result.to_markdown,
        "json": result.to_json,
    }[args.format]()

    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        print(f"wrote {args.format} report to {args.output}", file=sys.stderr)
    else:
        print(rendered)

    if args.json_output:
        Path(args.json_output).write_text(result.to_json() + "\n", encoding="utf-8")

    if result.exit_code != EXIT_PASS:
        return EXIT_BLOCKED
    if args.fail_on_warn and any(c.status == "warn" for c in result.checks):
        return EXIT_BLOCKED
    return EXIT_PASS


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

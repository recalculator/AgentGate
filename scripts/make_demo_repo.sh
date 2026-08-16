#!/usr/bin/env bash
# Build a throwaway git repo containing the demo agent, with two commits:
#
#   main             the agent as it should be
#   risky-refactor   a PR that adds fs:write, deletes the loop cap, and
#                    triples the system prompt
#
# Usage: scripts/make_demo_repo.sh [target-dir]
# Default target: /tmp/agent-gate-demo
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-/tmp/agent-gate-demo}"

if [ -e "$TARGET" ]; then
  echo "removing existing $TARGET"
  rm -rf "$TARGET"
fi

mkdir -p "$TARGET"
cp -R "$HERE/examples/demo-agent/." "$TARGET/"

cd "$TARGET"
cat > .gitignore <<'EOF'
__pycache__/
*.pyc
EOF

git init -q
git config user.email "demo@agent-gate.local"
git config user.name "Agent Gate Demo"
git symbolic-ref HEAD refs/heads/main
git add -A
git commit -qm "Support agent: read tickets and look up orders"

git checkout -qb risky-refactor

# Overlay the escalated variant.
cp -R "$HERE/examples/demo-agent-escalated/." "$TARGET/"

git add -A
git commit -qm "Let the agent save resolution notes

- add a write_file tool so the agent can record outcomes
- expand the system prompt with the tone, escalation, refund and
  data-handling policies support asked for
- drop max_iterations; some tickets need more than 8 steps"

echo
echo "Demo repo ready at $TARGET"
echo "  main           $(git rev-parse --short main)"
echo "  risky-refactor $(git rev-parse --short risky-refactor)"
echo
echo "Try:"
echo "  agent-gate scan --repo $TARGET --base main --head risky-refactor"

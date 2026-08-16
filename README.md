# Agent Gate

A required PR check for teams shipping LLM agent code.

When a pull request changes an agent's system prompt, tool definitions, tool
permissions, or loop limits, Agent Gate runs three checks against the base
branch and the PR branch, and posts one comment with one verdict.

```
🛡️ Agent Gate

Permissions:      ⚠️  +1 (agent gained filesystem write access)
Injection suite:  ✅  18/18 passed (no regression vs. base)
Cost estimate:    ⚠️  +40% avg tokens/request
Loop cap:         ⚠️  removed (was max_iterations: 8)

Status: BLOCKED — permission escalation + cost spike need sign-off
```

Exit code 1 means BLOCKED. Wire that to a required status check and a
permission escalation cannot merge without a human saying so out loud.

---

## The one question it answers

**Does this PR make the agent more dangerous or more expensive than the branch
it is merging into?**

Not "is this agent safe" — that is not a question a PR check can honestly
answer. Agent Gate is differential. Everything it reports is relative to the
base branch, which is why it can be blunt about blocking: a regression is
always somebody's fault, and it is always this PR's.

## The three checks

**1. Permission diff** *(static, no network)*
Parses the tool manifest on both branches. Any permission scope present on head
that was absent on base is an escalation, and escalations block. Adding a tool
that reuses capabilities the agent already had warns but does not block —
widening the blast radius is worth seeing, but it is not a new capability.

**2. Injection regression suite** *(runs the agent, needs an API key)*
18 fixed adversarial fixtures — 6 direct injection, 6 indirect/tool-output
injection, 6 jailbreak — run against both branches. A fixture fails if the
agent calls a forbidden tool, or if an LLM judge says the response missed the
criterion. **Blocks only on regression**: 15/18 on base and 15/18 on head is a
problem, but not this PR's problem, and blocking on it would train people to
disable the check.

**3. Cost delta** *(runs the agent, needs an API key)*
Five representative tasks on both branches, comparing average tokens per
request. Blocks past a threshold (default +25%). A longer system prompt, more
tool schemas, or extra loop iterations all show up here. The static half of this
check — a removed or sharply raised `max_iterations` — runs without the network
and reports as its own line.

## Architecture

```
agent-gate scan --base main --head HEAD
        │
        ├─ git worktree ×2 ──────────► base tree, head tree side by side
        │
        ├─ static checks (always run, no network)
        │     ├─ permissions.diff()        manifest vs. manifest
        │     └─ permissions.check_loop_cap()
        │
        ├─ behavioural checks (best-effort, skipped on any failure)
        │     ├─ injection.run_branch()    18 fixtures × 2 branches
        │     └─ cost.run_branch()          5 tasks × 2 branches
        │           │
        │           └─ subprocess: one JSON in, one JSON out
        │
        └─ ScanResult ─► text | markdown | json,  exit 0 / 1 / 2

                    ▲
                    │  thin wrapper, no logic of its own
              action.yml  ──► sticky PR comment + `agent-gate` commit status
```

The CLI is the product. The GitHub Action checks out both branches, runs the
CLI, pipes its markdown into one PR comment, and turns its exit code into a
commit status. If the Action disappeared tomorrow the tool would be unaffected.

**Exit codes:** `0` pass · `1` blocked · `2` the gate itself could not run.

### Fail open, loudly

The static checks need nothing but git, and always run. The two behavioural
checks are best-effort: no API key, a rate limit, a broken entrypoint, a timeout
— any of these report `SKIPPED` with the reason and **never block a merge**.

This is deliberate. A required check that a provider outage can wedge is a
check that gets removed from branch protection within a month. Static findings
still gate the PR while the LLM checks are down, so the cheap protection never
goes away.

## Setup

### 1. Install

```bash
pip install agent-gate      # or: pip install -e . from a clone
```

### 2. Write a manifest

`agent.manifest.yaml` in your repo root. This is the complete schema:

```yaml
version: 1
name: support-agent
model: claude-sonnet-5

system_prompt: prompts/system.md   # watched for changes
max_iterations: 8                  # removing or raising this blocks

entrypoint:
  command: python3 -m my_agent.run   # one JSON in, one JSON out
  install: pip install -q -r requirements.txt
  timeout_seconds: 120

tools:
  - name: read_ticket
    description: Read a customer ticket from the workspace.
    scopes: [fs:read]
  - name: lookup_order
    scopes: ["api:orders:read"]

watch:                             # extra paths that should trigger a scan
  - my_agent/tools.py

thresholds:
  cost_increase_pct: 0.25
  iteration_increase_pct: 0.50

# injection_suite: tests/my_suite.yaml   # optional; defaults to the built-in 18
# cost_tasks: tests/my_tasks.yaml        # optional; defaults to the built-in 5
```

Check it: `agent-gate validate`.

**Scope vocabulary** — closed on purpose, so a typo is an error rather than a
silently-granted capability:

| Family    | Scopes                                    |
| --------- | ----------------------------------------- |
| `fs`      | `fs:read`, `fs:write`, `fs:delete`        |
| `net`     | `net:http`, `net:socket`, `net:http:host` |
| `exec`    | `exec:shell`, `exec:code`                 |
| `secrets` | `secrets:read`                            |
| `env`     | `env:read`                                |
| `api`     | `api:<service>`, `api:<service>:<scope>`  |

### 3. Implement the entrypoint

Agent Gate runs your agent as a subprocess: one JSON object on stdin, one on
stdout. That is the entire integration surface — no SDK, no import hooks, no
framework adapters.

```jsonc
// stdin
{
  "prompt": "user turn text",
  "tool_outputs": { "read_ticket": "canned result — use this verbatim" },
  "model": "claude-sonnet-5",
  "max_iterations": 8
}

// stdout
{
  "text": "final assistant text",
  "tool_calls": [{ "name": "write_file", "input": {} }],
  "usage": { "input_tokens": 1234, "output_tokens": 56 },
  "iterations": 2
}
```

`tool_outputs` is how indirect-injection fixtures work: when present, return
that string as the tool's result instead of really running the tool. Report
`usage` summed across every loop iteration — that is what the cost check reads.

`examples/demo-agent/demo_agent/run.py` is a complete working implementation in
about eighty lines.

### 4. Add the workflow

Copy `examples/workflow.yml` to `.github/workflows/agent-gate.yml`, then make
`agent-gate` a required status check under branch protection.

```yaml
- uses: actions/checkout@v4
  with: { fetch-depth: 0 } # required — both branches must be present
- uses: your-org/agent-gate@v0
  with:
    anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

Needs `pull-requests: write` (the comment) and `statuses: write` (the check).
Omit the API key to run the static checks only.

## Try it

```bash
scripts/make_demo_repo.sh                       # two commits: clean, then risky
agent-gate scan --repo /tmp/agent-gate-demo \
  --base main --head risky-refactor
```

The `risky-refactor` commit adds a `write_file` tool, deletes `max_iterations`,
and triples the system prompt. Agent Gate should block on all three.

Useful flags: `--static-only` (no API calls), `--force` (run behavioural checks
even when no agent files changed), `--repeats 3` (average the cost probes),
`--format json`, `--base-dir/--head-dir` (compare two directories, no git).

## Where this sits

There are good tools in this space. Agent Gate is not competing with them, and
you may well want both.

**[Braintrust](https://braintrust.dev)** is an enterprise eval platform: hosted
dashboards, datasets, tracing, scoring infrastructure, seats, an org rollout.
You adopt it. It answers "how good is this agent, over time, across many
dimensions." Agent Gate answers one question in one comment and has no server.

**[Promptfoo](https://promptfoo.dev)** is the closest neighbour — an excellent
open-source eval and red-team CLI with a much broader surface: many providers,
matrix testing, a large plugin ecosystem, its own CI integration. It is a
general framework you configure to your needs. Agent Gate is a single opinion
with a manifest instead of a configuration language: three checks, fixed
fixtures, one verdict. If you want to design your own eval strategy, use
Promptfoo. If you want a merge gate you can install in an afternoon and then
stop thinking about, use this.

**agent-audit** and similar scanners statically analyse agent configurations for
risky patterns in absolute terms. Agent Gate's permission check is differential:
it does not care that your agent has shell access, only that it did not have it
yesterday. Absolute analysis is the better tool for an initial audit; a diff is
the better tool for a PR.

**The scoping bet:** cheap, narrow, and self-serve beats broad and
enterprise-shaped for the specific job of stopping a bad merge. No account, no
database, no dashboard, no per-seat pricing — one pip install, one YAML file,
one required check. The cost is that Agent Gate will never tell you whether your
agent is *good*. It only tells you whether this PR made it worse.

## Honest limitations

**The manifest is a declaration, not a derivation.** Agent Gate diffs what you
*wrote down*, not what your code actually does. A PR that adds filesystem writes
without updating the manifest will pass the permission check. Mitigations: keep
tool definitions next to the manifest, list implementation files under `watch:`,
and treat manifest edits as review-worthy. Deriving scopes from code would mean
per-framework parsers, which is [explicitly out of scope](SCOPE.md).

**18 fixtures is a tripwire, not a certification.** Passing means this PR did
not regress against these 18 attacks. It does not mean the agent is
injection-proof.

**LLM checks are non-deterministic.** Pass rates can flap by a fixture between
runs, and the cost probes are noisy at `--repeats 1`. Raise `--repeats` for a
tighter signal at proportionally higher cost.

**Cost is measured, not predicted.** The dollar figures use a hardcoded price
table (`agent_gate/cost.py`) that will drift. The pass/fail decision is always
on the token ratio, never on dollars.

## Development

```bash
pip install -e ".[dev]"
pytest -q                 # 86 tests, all offline
```

The test suite uses a scripted fake agent and a stub judge, so everything except
the live API path runs with no network and no key.

See [SCOPE.md](SCOPE.md) for what was deliberately left out, and why.

## License

MIT

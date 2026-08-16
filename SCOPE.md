# Scope

What Agent Gate deliberately does not do, and why. This list is the design, not
a backlog. Nothing here is waiting to be built.

The governing rule: **if a feature would take more than about a day to build
well, it gets cut rather than expanded.** A merge gate earns its place by being
installable in an afternoon and then forgotten about. Every feature below would
have made it more capable and less likely to survive in a repo's branch
protection settings.

---

## No hosted anything

**Left out:** dashboard, database, user accounts, billing, telemetry, a server
of any kind.

**Why:** the entire product is a diff and an exit code. State lives in git,
output lives in a PR comment, history lives in the PR timeline. Adding a server
would mean an account to create, a vendor to review, a bill to justify, and a
thing to be down at 3am — in exchange for a trend chart nobody would open. It
would also change the buying decision from "a developer pip-installs it" to "a
team evaluates a vendor", which is the failure mode this tool is shaped against.

**Instead:** `--format json` writes a complete machine-readable result. Pipe it
wherever you already keep metrics.

## No framework adapters

**Left out:** LangChain, CrewAI, AutoGen, LlamaIndex, OpenAI Assistants, MCP
server introspection — anything that reads tool definitions out of code.

**Why:** this is the largest single scope cut, and the one most likely to look
like an oversight. It is not. Deriving permissions from code means a parser per
framework, each tracking a fast-moving API, each with its own failure modes —
and every one of them would be wrong in a way that produces a false PASS. A
false PASS on a security gate is worse than no gate, because it launders the
risk.

The manifest instead makes the claim explicit and reviewable. Someone writes
down what the agent may do; Agent Gate enforces that the claim does not quietly
grow.

**The cost, stated plainly:** the manifest can drift from the code. A PR that
adds filesystem writes without touching the manifest passes the permission
check. `watch:` paths mitigate this by forcing a scan when implementation files
change, but they do not eliminate it. This tool checks a declaration, and a
declaration is only as good as the review that keeps it honest.

## No custom eval framework

**Left out:** scoring rubrics, weighted metrics, dataset management, trend
tracking, regression baselines beyond "the base branch", a plugin system for
graders.

**Why:** on an 18-fixture suite, a weighted rubric is false precision. The only
question is "did this response satisfy the criterion", which is one LLM call
returning one word. Building scoring infrastructure would mean maintaining
scoring infrastructure, and [Promptfoo](https://promptfoo.dev) and
[Braintrust](https://braintrust.dev) already do it better than a side quest
would.

**Instead:** LLM-as-judge, one call, `PASS`/`FAIL` plus a short reason
(`agent_gate/judge.py` is under 130 lines including the prompt).

## No absolute security verdict

**Left out:** "this agent is safe", risk scores, compliance reports, CVE-style
severity ratings.

**Why:** a PR check cannot honestly certify safety, and pretending otherwise
would be the most harmful thing this tool could do. Everything is measured
against the base branch. Agent Gate says "this PR made it worse", never "this is
fine."

This is also why the injection check blocks on *regression* rather than on
absolute pass rate. A suite failing 3/18 on both branches is a real problem and
a bad reason to block an unrelated PR — blocking there teaches people to remove
the check, and a removed check protects nothing.

## No auto-fix, no suggested patches

**Left out:** proposing manifest edits, opening follow-up PRs, rewriting system
prompts to patch a failing fixture.

**Why:** the output of this tool is a human decision. A permission escalation
needs someone to say "yes, the agent should be able to write files now" — that
sign-off *is* the product. Automating it away removes the only thing the gate
exists to produce.

## No approval workflow

**Left out:** an `/agent-gate approve` comment command, override labels, a
signed exception file, per-user allowlists.

**Why:** GitHub already has this and it is called branch protection. An admin
merge, a `CODEOWNERS` review, or a temporary rule change are all first-class,
already audited, and already understood by the people who would use them.
Rebuilding approvals inside a PR comment would mean parsing commands, checking
who is allowed to run them, and storing state — a server, by another name.

## No multi-provider support

**Left out:** OpenAI, Gemini, Bedrock, local models, a provider abstraction.

**Why:** two provider integrations mean an abstraction layer, and an
abstraction layer over four is most of the tool's complexity. The agent under
test can use whatever it likes — the subprocess protocol is provider-agnostic
and Agent Gate never sees your model calls. Only the judge is Anthropic-specific,
and it is one function behind one interface.

## No per-tool or per-fixture configuration surface

**Left out:** severity overrides per scope, per-fixture skips, allowlisted
escalations, custom verdict rules, an expression language in the manifest.

**Why:** every knob is a way to turn the gate off quietly. A repo that needs to
permanently allow `fs:write` should grant it on the base branch, in a reviewed
PR, where the escalation is visible — not annotate it away in a config file
where the next reader will not see it.

**The two knobs that exist** are `thresholds.cost_increase_pct` and
`thresholds.iteration_increase_pct`, because the right number there genuinely
varies by workload and neither one can hide a capability change.

---

## What would earn a place

Roughly in order, if this were continued:

1. **Manifest drift detection** — a heuristic that greps implementation files
   for obvious capability markers (`open(..., "w")`, `subprocess.`, `requests.`)
   and warns when the code looks more capable than the manifest claims. A
   warning, never a block, and explicitly heuristic. This is the honest partial
   answer to the framework-adapter problem.
2. **Fixture caching by content hash** — skip re-running base-branch fixtures
   when the base has not moved. Straight cost saving, no new surface.
3. **A second judge pass on disagreement** — only for fixtures whose verdict
   flipped between branches, to damp non-determinism where it actually matters.

Each is roughly a day. Anything larger belongs in a different tool.

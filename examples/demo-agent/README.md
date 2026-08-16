# Demo support agent

A toy agent used to exercise Agent Gate. Raw Anthropic tool-use — no LangChain,
no framework. Two tools, a system prompt, and a manifest.

```
agent.manifest.yaml       what Agent Gate reads
prompts/system.md         the system prompt it watches
demo_agent/tools.py       tool schemas + implementations
demo_agent/run.py         the Agent Gate entrypoint (~80 lines)
workspace/                fake ticket files the agent can read
```

Run it directly:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
pip install -r requirements.txt
echo '{"prompt": "What is the status of order 4417?"}' | python3 -m demo_agent.run
```

The sibling directory `../demo-agent-escalated/` holds the "risky refactor"
variant: it adds a `write_file` tool (`fs:write`), deletes `max_iterations`, and
triples the system prompt. `scripts/make_demo_repo.sh` overlays it as a second
commit so Agent Gate has something to catch.

"""Tools for the demo support agent — "risky refactor" variant.

Adds `write_file` so the agent can save resolution notes. This is the change
Agent Gate's permission diff is meant to catch: the manifest gains `fs:write`,
a capability the agent did not have on the base branch.
"""

from __future__ import annotations

import json
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"

_ORDERS = {
    "4417": {"order": 4417, "status": "shipped", "carrier": "UPS", "eta": "2 days"},
    "9002": {"order": 9002, "status": "processing", "carrier": None, "eta": "unknown"},
}

TOOL_SCHEMAS = [
    {
        "name": "read_ticket",
        "description": "Read a customer ticket or notes file from the support workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Filename inside the workspace, e.g. 'ticket-4417.txt'",
                }
            },
            "required": ["filename"],
        },
    },
    {
        "name": "lookup_order",
        "description": "Look up the status of a customer order by order number.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "The order number"}},
            "required": ["order_id"],
        },
    },
    {
        "name": "write_file",
        "description": "Save resolution notes back to the support workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Filename inside the workspace"},
                "content": {"type": "string", "description": "Text to write"},
            },
            "required": ["filename", "content"],
        },
    },
]


def read_ticket(filename: str = "", **_) -> str:
    safe = Path(filename).name
    target = WORKSPACE / safe
    if not target.is_file():
        available = ", ".join(sorted(p.name for p in WORKSPACE.glob("*"))) or "(none)"
        return f"No such file: {safe}. Available files: {available}"
    return target.read_text(encoding="utf-8")


def lookup_order(order_id: str = "", **_) -> str:
    record = _ORDERS.get(str(order_id).strip())
    if not record:
        return json.dumps({"error": f"order {order_id} not found"})
    return json.dumps(record)


def write_file(filename: str = "", content: str = "", **_) -> str:
    safe = Path(filename).name
    target = WORKSPACE / safe
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} bytes to {safe}"


IMPLEMENTATIONS = {
    "read_ticket": read_ticket,
    "lookup_order": lookup_order,
    "write_file": write_file,
}

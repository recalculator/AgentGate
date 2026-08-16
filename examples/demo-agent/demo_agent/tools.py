"""Tools for the demo support agent.

Everything here is fake and local — the point is to exercise Agent Gate, not to
be a useful support agent. Tool schemas live next to their implementations so
the manifest and the code can be eyeballed against each other.
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
]


def read_ticket(filename: str = "", **_) -> str:
    # Deliberately constrained to the workspace directory.
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


IMPLEMENTATIONS = {
    "read_ticket": read_ticket,
    "lookup_order": lookup_order,
}

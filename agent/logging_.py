"""
Structured logging for the Q&A terminal — one JSON line per user turn.

Why JSONL and not a database: this is an eval artifact, not a running
service. Every line is independently readable, greppable, and diffable;
`jq` or a five-line pandas script is all the analysis this needs. A
database would be infrastructure for infrastructure's sake.

Why one line per *turn*, not per tool call: a tool call has no meaning on
its own — "connect_steps failed" is only interpretable next to the
question that led to it and the steps before it. The unit that matters
for evaluation is the whole turn.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_PATH = Path(__file__).parent / "logs" / "turns.jsonl"


def log_turn(
    *,
    session_id: str,
    provider: str,
    model: str,
    question: str,
    answer: str,
    tool_calls: list[dict[str, Any]],
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd: float | None,
    duration_ms: float,
    error: str | None = None,
) -> None:
    """
    tool_calls: [{"name", "arguments", "result", "duration_ms"}, ...] in
    call order. `result` is the raw text/JSON the tool returned — kept
    verbatim, not summarized, since the whole point of logging it is to
    let a later review catch a wrong argument or a misread result that a
    summary would hide.
    """
    LOG_PATH.parent.mkdir(exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "provider": provider,
        "model": model,
        "question": question,
        "answer": answer,
        "tool_calls": tool_calls,
        "tool_call_count": len(tool_calls),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost_usd, 6) if cost_usd is not None else None,
        "duration_ms": round(duration_ms, 1),
        "error": error,
    }
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def summarize(session_id: str | None = None) -> dict:
    """
    Quick aggregate over the log — total turns, total cost, tool-call
    frequency. Filtered to one session if given.

    Deliberately not a general-purpose analytics function: this is a
    starting point for probes/eval scripts to build on, not a dashboard.
    """
    if not LOG_PATH.exists():
        return {"turns": 0}

    turns = []
    with open(LOG_PATH) as f:
        for line in f:
            entry = json.loads(line)
            if session_id is None or entry["session_id"] == session_id:
                turns.append(entry)

    tool_freq: dict[str, int] = {}
    for t in turns:
        for call in t["tool_calls"]:
            tool_freq[call["name"]] = tool_freq.get(call["name"], 0) + 1

    known_costs = [t["cost_usd"] for t in turns if t["cost_usd"] is not None]

    return {
        "turns": len(turns),
        "errors": sum(1 for t in turns if t["error"]),
        "total_cost_usd": round(sum(known_costs), 4) if known_costs else None,
        "turns_with_unknown_cost": sum(1 for t in turns if t["cost_usd"] is None),
        "tool_call_frequency": dict(sorted(tool_freq.items(), key=lambda kv: -kv[1])),
        "avg_duration_ms": round(sum(t["duration_ms"] for t in turns) / len(turns), 1)
                          if turns else None,
    }

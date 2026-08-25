"""
Minimal agentic loop: Anthropic API + this project's tools, no MCP client.

Why this exists alongside the MCP server: MCP is the right shape for
Claude Desktop / ChatGPT, where the *host application* owns the model and
the conversation. When you want to drive the tools yourself — a script, a
batch of test scenarios, an eval harness — you own the loop instead, and
this is what that looks like.

The tools themselves are the same functions the MCP server registers.
Nothing is duplicated: this calls into mcp.call_tool, so a change to a
tool is picked up here automatically and there is no second copy of any
tool logic to drift.

Setup:
    ANTHROPIC_API_KEY=...   in .env, alongside JOTFORM_API_KEY

Usage:
    python -m agent.run "list my workflows"
    python -m agent.run            # interactive
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from mcp_server.server import mcp  # noqa: E402
from agent.tool_profiles import current_profile, filter_tools  # noqa: E402

# claude-sonnet-5 is the current default per Anthropic's own model
# lineup as of 2026-08 (introductory pricing through 2026-08-31). An
# earlier version of this file defaulted to claude-sonnet-4-5, the
# previous generation — corrected here, see decision-log.md.
MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-5")
MAX_TURNS = 20  # a runaway loop burns real API credit and real Jotform writes

SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text()


async def build_tool_definitions() -> list[dict]:
    """Anthropic tool-use format, generated from the live server."""
    return [
        {
            "name": t.name,
            "description": (t.description or "").strip(),
            "input_schema": t.input_schema,
        }
        for t in filter_tools(await mcp.list_tools())
    ]


async def run_tool(name: str, args: dict) -> str:
    """
    Call a tool and return its result as text for the model.

    Tool errors come back as data (an `error` field), not exceptions —
    that's the server's own convention. An exception here means something
    unexpected broke, and the model should see that too rather than the
    loop dying silently.
    """
    try:
        result = await mcp.call_tool(name, args)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"{type(e).__name__}: {e}"})

    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)

    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts) if parts else "{}"


async def converse(client, tools: list[dict], messages: list[dict],
                   trace: dict | None = None) -> list[dict]:
    """
    One user turn: loop until the model stops asking for tools.

    If `trace` is passed (a dict the caller owns), this fills in
    `tool_calls`, `answer`, `input_tokens`, `output_tokens` on it as the
    turn unfolds — used by qa_terminal.py to log the turn without this
    function needing to know anything about logging itself.
    """
    if trace is not None:
        trace.setdefault("tool_calls", [])
        trace.setdefault("answer_parts", [])
        trace.setdefault("input_tokens", 0)
        trace.setdefault("output_tokens", 0)

    for turn in range(MAX_TURNS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )

        if trace is not None and getattr(response, "usage", None):
            trace["input_tokens"] += response.usage.input_tokens
            trace["output_tokens"] += response.usage.output_tokens

        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"\n{block.text}")
                if trace is not None:
                    trace["answer_parts"].append(block.text)
            elif block.type == "tool_use":
                print(f"  [tool] {block.name}({json.dumps(block.input, ensure_ascii=False)[:120]})")

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            if trace is not None:
                trace["answer"] = "\n".join(trace.pop("answer_parts"))
            return messages

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            t0 = time.perf_counter()
            output = await run_tool(block.name, block.input)
            duration_ms = (time.perf_counter() - t0) * 1000
            preview = output[:150].replace("\n", " ")
            print(f"  [result] {preview}{'...' if len(output) > 150 else ''}")
            if trace is not None:
                trace["tool_calls"].append({
                    "name": block.name, "arguments": block.input,
                    "result": output, "duration_ms": round(duration_ms, 1),
                })
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })

        messages.append({"role": "user", "content": tool_results})

    print(f"\n[stopped: hit the {MAX_TURNS}-turn limit]")
    if trace is not None:
        trace["answer"] = "\n".join(trace.pop("answer_parts"))
        trace["error"] = f"hit {MAX_TURNS}-turn limit"
    return messages


async def main() -> int:
    try:
        from anthropic import Anthropic
    except ImportError:
        print("pip install anthropic")
        return 1

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set — add it to .env")
        return 1

    from agent import cost, logging_
    import uuid

    client = Anthropic()
    tools = await build_tool_definitions()
    session_id = str(uuid.uuid4())[:8]
    print(f"{len(tools)} tools loaded, surface={current_profile()}, model={MODEL}, session={session_id}\n")

    messages: list[dict] = []

    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        messages.append({"role": "user", "content": question})
        trace: dict = {}
        t0 = time.perf_counter()
        await converse(client, tools, messages, trace=trace)
        duration_ms = (time.perf_counter() - t0) * 1000
        logging_.log_turn(
            session_id=session_id, provider="anthropic", model=MODEL,
            question=question, answer=trace.get("answer", ""),
            tool_calls=trace.get("tool_calls", []),
            input_tokens=trace.get("input_tokens"), output_tokens=trace.get("output_tokens"),
            cost_usd=cost.estimate_cost(MODEL, trace.get("input_tokens"), trace.get("output_tokens")),
            duration_ms=duration_ms, error=trace.get("error"),
        )
        return 0

    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if user_input.lower() in ("exit", "quit", ""):
            return 0
        messages.append({"role": "user", "content": user_input})
        trace = {}
        t0 = time.perf_counter()
        messages = await converse(client, tools, messages, trace=trace)
        duration_ms = (time.perf_counter() - t0) * 1000
        logging_.log_turn(
            session_id=session_id, provider="anthropic", model=MODEL,
            question=user_input, answer=trace.get("answer", ""),
            tool_calls=trace.get("tool_calls", []),
            input_tokens=trace.get("input_tokens"), output_tokens=trace.get("output_tokens"),
            cost_usd=cost.estimate_cost(MODEL, trace.get("input_tokens"), trace.get("output_tokens")),
            duration_ms=duration_ms, error=trace.get("error"),
        )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

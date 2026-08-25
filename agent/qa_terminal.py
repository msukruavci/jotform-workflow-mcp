"""
One Q&A terminal, either provider, everything logged.

Ask a question, watch the tool calls happen live (same as agent/run.py and
agent/gemini_run.py always did), and every turn — question, answer, full
tool-call sequence with arguments/results/timing, token usage, estimated
cost — gets appended to agent/logs/turns.jsonl.

This wraps the existing per-provider converse() loops rather than
reimplementing tool-calling twice; the only new code here is picking a
provider and writing the log line. A change to how a provider's loop
works (retry logic, a new tool-result shape) only needs to happen once,
in run.py or gemini_run.py, and both this terminal and the plain
single-provider scripts pick it up.

Setup: whichever provider's API key you're using, in .env.

Usage:
    python -m agent.qa_terminal                     # Anthropic, interactive
    python -m agent.qa_terminal --provider gemini    # Gemini, interactive
    python -m agent.qa_terminal "list my workflows"  # one-shot
    python -m agent.qa_terminal --summary            # print log summary, exit
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid

from dotenv import load_dotenv

load_dotenv()

from agent import cost, logging_  # noqa: E402
from agent.tool_profiles import current_profile, filter_tools  # noqa: E402
from mcp_server.server import mcp  # noqa: E402


async def run_anthropic(question: str, session_id: str, messages: list[dict]) -> list[dict]:
    from agent.run import MODEL, build_tool_definitions, converse

    try:
        from anthropic import Anthropic
    except ImportError:
        print("pip install anthropic")
        sys.exit(1)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set")
        sys.exit(1)

    client = Anthropic()
    tools = await build_tool_definitions()
    messages.append({"role": "user", "content": question})

    trace: dict = {}
    t0 = time.perf_counter()
    messages = await converse(client, tools, messages, trace=trace)
    duration_ms = (time.perf_counter() - t0) * 1000

    logging_.log_turn(
        session_id=session_id, provider="anthropic", model=MODEL,
        question=question, answer=trace.get("answer", ""),
        tool_calls=trace.get("tool_calls", []),
        input_tokens=trace.get("input_tokens"), output_tokens=trace.get("output_tokens"),
        cost_usd=cost.estimate_cost(MODEL, trace.get("input_tokens"), trace.get("output_tokens")),
        duration_ms=duration_ms, error=trace.get("error"),
    )
    return messages


async def run_gemini(question: str, session_id: str, previous_id: str | None) -> str | None:
    from agent.gemini_run import MODEL, build_tool_declarations, converse

    try:
        from google import genai
    except ImportError:
        print("pip install google-genai")
        sys.exit(1)
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is not set")
        sys.exit(1)

    client = genai.Client()
    tools = await build_tool_declarations()

    trace: dict = {}
    t0 = time.perf_counter()
    previous_id = await converse(client, tools, question, previous_id, trace=trace)
    duration_ms = (time.perf_counter() - t0) * 1000

    logging_.log_turn(
        session_id=session_id, provider="gemini", model=MODEL,
        question=question, answer=trace.get("answer", ""),
        tool_calls=trace.get("tool_calls", []),
        input_tokens=trace.get("input_tokens"), output_tokens=trace.get("output_tokens"),
        cost_usd=cost.estimate_cost(MODEL, trace.get("input_tokens"), trace.get("output_tokens")),
        duration_ms=duration_ms, error=trace.get("error"),
    )
    return previous_id


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="*", help="one-shot question; omit for interactive")
    parser.add_argument("--provider", choices=["anthropic", "gemini"], default="anthropic")
    parser.add_argument("--summary", action="store_true", help="print log summary and exit")
    args = parser.parse_args()

    if args.summary:
        import json
        print(json.dumps(logging_.summarize(), indent=2, ensure_ascii=False))
        return 0

    session_id = str(uuid.uuid4())[:8]
    tool_count = len(filter_tools(await mcp.list_tools()))
    print(
        f"provider={args.provider}  session={session_id}  "
        f"surface={current_profile()}  tools={tool_count}"
    )
    print(f"logging to {logging_.LOG_PATH}\n")

    anthropic_messages: list[dict] = []
    gemini_previous_id: str | None = None

    async def ask(question: str) -> None:
        nonlocal anthropic_messages, gemini_previous_id
        if args.provider == "anthropic":
            anthropic_messages = await run_anthropic(question, session_id, anthropic_messages)
        else:
            gemini_previous_id = await run_gemini(question, session_id, gemini_previous_id)

    if args.question:
        await ask(" ".join(args.question))
        return 0

    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            return 0
        if user_input == "!summary":
            import json
            print(json.dumps(logging_.summarize(session_id), indent=2, ensure_ascii=False))
            continue
        await ask(user_input)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

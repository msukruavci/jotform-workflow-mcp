"""
Agentic loop on Gemini Flash, driving this project's tools directly.

Same idea as agent/run.py (Anthropic), different provider. Both call into
mcp.call_tool, so neither holds a second copy of any tool logic — a change
to a tool is picked up by both automatically.

Two things about Gemini that are not just "a different model name":

1. **Interactions API, not generate_content.** Conversation state lives
   server-side; you pass `previous_interaction_id` instead of resending
   history. Tools and generation_config are interaction-scoped, so they
   must be re-declared on every call even though the history isn't.

2. **Gemini only accepts a subset of OpenAPI schema.** Our `add_step` and
   `update_step` take a free-form `config` object whose valid keys depend
   on which step_type was chosen — that's not expressible in Gemini's
   subset (an object with no declared properties). See ADAPTER NOTE below
   for how that's handled and what it costs.

Setup:
    GEMINI_API_KEY=...   in .env, alongside JOTFORM_API_KEY
    pip install google-genai

Usage:
    python -m agent.gemini_run "list my workflows"
    python -m agent.gemini_run              # interactive
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

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
MAX_TURNS = 20  # a runaway loop burns real API credit and real Jotform writes

# The free tier is 5 requests/minute for gemini-3.6-flash — a single
# multi-step build (schema lookups + create + several add_step/connect_steps
# calls) burns through that in well under a minute. Retry with backoff
# rather than crash; the API's own error message tells us how long to wait,
# so we use that instead of a fixed guess when it's present.
RATE_LIMIT_MAX_RETRIES = 5
RATE_LIMIT_DEFAULT_WAIT_S = 20.0

SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text()

# ADAPTER NOTE — free-form object parameters
#
# `add_step(config)` and `update_step(config)` accept an arbitrary dict whose
# valid keys depend on the step_type: an email step takes `subject`/`to`, a
# pause step takes something else entirely. The MCP schema says
# `{"type": "object", "additionalProperties": true}`, which is honest but
# outside the OpenAPI subset Gemini's function calling accepts.
#
# So for Gemini these are declared as JSON *strings* and parsed back before
# the tool runs. The cost: the model can emit malformed JSON, which the
# schema can no longer prevent — so _parse_json_params returns a readable
# error to the model instead of raising, and the model gets a chance to fix
# it. That's strictly worse than a typed parameter, and it's a Gemini
# limitation rather than a choice; the Anthropic loop passes the object
# through unchanged.
JSON_STRING_PARAMS = {"config"}


def _to_gemini_schema(schema: dict) -> dict:
    """
    MCP's JSON Schema -> the OpenAPI subset Gemini accepts.

    Drops what Gemini doesn't use (`title`, `additionalProperties`) and
    rewrites free-form objects into JSON strings. `default` is dropped too
    — Gemini's subset doesn't carry it, and a parameter that's simply
    absent from `required` already reads as optional.
    """
    properties = {}
    for name, prop in (schema.get("properties") or {}).items():
        if name in JSON_STRING_PARAMS:
            properties[name] = {
                "type": "string",
                "description": (
                    "A JSON object, encoded as a string. Example: "
                    '{"subject": "Approved", "to": []}. '
                    "Call get_step_schema first to see which keys are valid "
                    "for this step type."
                ),
            }
            continue

        clean = {k: v for k, v in prop.items()
                 if k not in ("title", "default", "additionalProperties")}
        clean.setdefault("type", "string")
        properties[name] = clean

    out: dict = {"type": "object", "properties": properties}
    if schema.get("required"):
        out["required"] = list(schema["required"])
    return out


def _parse_json_params(name: str, args: dict) -> tuple[dict, str | None]:
    """Turn the JSON-string params back into real dicts. Returns (args, error)."""
    parsed = dict(args)
    for key in JSON_STRING_PARAMS:
        value = parsed.get(key)
        if not isinstance(value, str):
            continue
        if not value.strip():
            parsed[key] = {}
            continue
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as e:
            return parsed, (
                f"`{key}` was not valid JSON ({e}). Send it as a JSON object "
                f'encoded in a string, e.g. {{"subject": "Approved"}}.'
            )
        if not isinstance(decoded, dict):
            return parsed, f"`{key}` must be a JSON object, got {type(decoded).__name__}."
        parsed[key] = decoded
    return parsed, None


async def build_tool_declarations() -> list[dict]:
    return [
        {
            "type": "function",
            "name": t.name,
            "description": (t.description or "").strip(),
            "parameters": _to_gemini_schema(t.input_schema),
        }
        for t in await mcp.list_tools()
    ]


async def run_tool(name: str, args: dict) -> str:
    args, error = _parse_json_params(name, args)
    if error:
        # Returned as a tool result, not raised — the model reads it and
        # retries with corrected arguments, same as any other tool error.
        return json.dumps({"error": error})

    try:
        result = await mcp.call_tool(name, args)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"{type(e).__name__}: {e}"})

    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)

    parts = [getattr(b, "text", None) for b in (getattr(result, "content", None) or [])]
    parts = [p for p in parts if p]
    return "\n".join(parts) if parts else "{}"


def _extract_usage(interaction) -> tuple[int | None, int | None]:
    """
    Best-effort token usage from an Interactions API response.

    Not confirmed against a live call at the time this was written — the
    public docs describe the request/response shape for tools and steps
    in detail but don't show the usage field name. Tries the plausible
    candidates (`usage`, `usage_metadata`) and known sub-field spellings;
    returns (None, None) rather than guessing if none match, so a wrong
    field name shows up as "cost unknown" in the log, not a silently
    wrong number. First real run should confirm which candidate actually
    hit and this comment should be updated to say which one.
    """
    usage = getattr(interaction, "usage", None) or getattr(interaction, "usage_metadata", None)
    if usage is None:
        return None, None
    input_tokens = (getattr(usage, "input_tokens", None)
                    or getattr(usage, "prompt_token_count", None))
    output_tokens = (getattr(usage, "output_tokens", None)
                     or getattr(usage, "candidates_token_count", None)
                     or getattr(usage, "completion_tokens", None))
    return input_tokens, output_tokens


def _is_rate_limit(e: Exception) -> bool:
    """
    Deliberately not matching on a specific exception class: the traceback
    that motivated this (2026-08-11) showed the real class
    (google.genai._gaos.lib.compat_errors.RateLimitError) living in a
    private module, not the public `google.genai.errors` we import from —
    a name that can move under us without notice. Matching on the message
    content and any status-code-ish attribute is uglier but survives an
    SDK internals reshuffle; a public, stable exception class to catch
    would be better if one shows up in a future SDK version.
    """
    status = getattr(e, "status_code", None) or getattr(e, "code", None)
    if status == 429:
        return True
    text = str(e).lower()
    return "429" in text or "rate limit" in text or "quota" in text or "too_many_requests" in text


def _extract_retry_after(e: Exception) -> float:
    """Google's own message says e.g. "Please retry in 18.33s" — use that
    over a fixed guess when we can parse it."""
    import re
    match = re.search(r"retry in (\d+(?:\.\d+)?)s", str(e), re.IGNORECASE)
    if match:
        return float(match.group(1)) + 1.0  # small buffer
    return RATE_LIMIT_DEFAULT_WAIT_S


async def _create_with_retry(client, **kwargs):
    """client.interactions.create, retrying on rate limits with backoff."""
    for attempt in range(RATE_LIMIT_MAX_RETRIES):
        try:
            return client.interactions.create(**kwargs)
        except Exception as e:  # noqa: BLE001 — see _is_rate_limit's docstring
            if not _is_rate_limit(e) or attempt == RATE_LIMIT_MAX_RETRIES - 1:
                raise
            wait_s = _extract_retry_after(e)
            print(f"\n  [rate limited — waiting {wait_s:.0f}s "
                  f"(attempt {attempt + 1}/{RATE_LIMIT_MAX_RETRIES})]")
            await asyncio.sleep(wait_s)
    raise RuntimeError("unreachable")  # loop always returns or raises


async def converse(client, tools: list[dict], user_text: str,
                   previous_id: str | None, trace: dict | None = None) -> str | None:
    """
    One user turn. Returns the interaction id to continue from.

    Same `trace` contract as agent/run.py's converse(): fill in
    tool_calls/answer/token counts on a dict the caller owns, so
    qa_terminal.py can log either provider identically.
    """
    if trace is not None:
        trace.setdefault("tool_calls", [])
        trace.setdefault("answer_parts", [])
        trace.setdefault("input_tokens", 0)
        trace.setdefault("output_tokens", 0)

    pending_input: object = user_text

    for _ in range(MAX_TURNS):
        try:
            interaction = await _create_with_retry(
                client,
                model=MODEL,
                system_instruction=SYSTEM_PROMPT,
                input=pending_input,
                tools=tools,
                previous_interaction_id=previous_id,
            )
        except Exception as e:  # noqa: BLE001
            print(f"\n[Gemini API error] {e}")
            if trace is not None:
                trace["answer"] = "\n".join(trace.pop("answer_parts"))
                trace["error"] = str(e)
            return previous_id

        previous_id = interaction.id

        if trace is not None:
            in_tok, out_tok = _extract_usage(interaction)
            if in_tok is not None:
                trace["input_tokens"] += in_tok
            if out_tok is not None:
                trace["output_tokens"] += out_tok

        calls = [s for s in interaction.steps if s.type == "function_call"]

        text = getattr(interaction, "output_text", None)
        if text and text.strip():
            print(f"\n{text}")
            if trace is not None:
                trace["answer_parts"].append(text)

        if not calls:
            if trace is not None:
                trace["answer"] = "\n".join(trace.pop("answer_parts"))
                if trace["input_tokens"] == 0 and trace["output_tokens"] == 0:
                    trace["input_tokens"] = trace["output_tokens"] = None
            return previous_id

        results = []
        for call in calls:
            print(f"  [tool] {call.name}({json.dumps(call.arguments, ensure_ascii=False)[:120]})")
            t0 = time.perf_counter()
            output = await run_tool(call.name, call.arguments or {})
            duration_ms = (time.perf_counter() - t0) * 1000
            preview = output[:150].replace("\n", " ")
            print(f"  [result] {preview}{'...' if len(output) > 150 else ''}")
            if trace is not None:
                trace["tool_calls"].append({
                    "name": call.name, "arguments": call.arguments or {},
                    "result": output, "duration_ms": round(duration_ms, 1),
                })
            results.append({
                "type": "function_result",
                "name": call.name,
                "call_id": call.id,
                "result": [{"type": "text", "text": output}],
            })

        pending_input = results

    print(f"\n[stopped: hit the {MAX_TURNS}-turn limit]")
    if trace is not None:
        trace["answer"] = "\n".join(trace.pop("answer_parts"))
        trace["error"] = f"hit {MAX_TURNS}-turn limit"
    return previous_id


async def main() -> int:
    try:
        from google import genai
    except ImportError:
        print("pip install google-genai")
        return 1

    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is not set — add it to .env")
        return 1

    client = genai.Client()
    tools = await build_tool_declarations()
    print(f"{len(tools)} tools loaded, model={MODEL}\n")

    previous_id: str | None = None

    if len(sys.argv) > 1:
        await converse(client, tools, " ".join(sys.argv[1:]), previous_id)
        return 0

    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if user_input.lower() in ("exit", "quit", ""):
            return 0
        previous_id = await converse(client, tools, user_input, previous_id)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
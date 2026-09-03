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
from agent.tool_profiles import current_profile, filter_tools  # noqa: E402

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


def _clean_schema_node(node: Any, defs: dict[str, Any]) -> Any:
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        ref_name = node["$ref"].split("/")[-1]
        target = defs.get(ref_name, {})
        return _clean_schema_node(target, defs)

    clean: dict[str, Any] = {}
    for k, v in node.items():
        if k in ("title", "default", "additionalProperties", "$defs"):
            continue
        if k == "properties" and isinstance(v, dict):
            props = {}
            for pk, pv in v.items():
                if pk in JSON_STRING_PARAMS:
                    props[pk] = {
                        "type": "string",
                        "description": (
                            "A JSON object, encoded as a string. Example: "
                            '{"subject": "Approved", "to": []}. '
                            "Call get_step_schema first to see which keys are valid "
                            "for this step type."
                        ),
                    }
                else:
                    props[pk] = _clean_schema_node(pv, defs)
            clean["properties"] = props
        elif k == "items" and isinstance(v, dict):
            clean["items"] = _clean_schema_node(v, defs)
        elif k == "required" and isinstance(v, (list, tuple)):
            clean["required"] = list(v)
        else:
            clean[k] = v

    if clean.get("type") == "object" and not clean.get("properties"):
        clean["type"] = "string"
        clean["description"] = (clean.get("description", "") + " (JSON object encoded as string or dict)").strip()
    return clean


def _to_gemini_schema(schema: dict) -> dict:
    """
    MCP's JSON Schema -> the OpenAPI subset Gemini accepts.

    Resolves $defs/$ref recursively, drops unsupported fields (title, default, additionalProperties),
    and converts free-form/empty object parameters to valid typed declarations.
    """
    defs = schema.get("$defs") or {}
    out = _clean_schema_node(schema, defs)
    if not isinstance(out, dict):
        out = {"type": "object", "properties": {}}
    out.setdefault("type", "object")
    return out


def _parse_json_params(name: str, args: dict) -> tuple[dict, str | None]:
    """Turn JSON-string params back into real dicts. Returns (args, error)."""
    parsed = dict(args)
    for key in JSON_STRING_PARAMS:
        value = parsed.get(key)
        if isinstance(value, str):
            if not value.strip():
                parsed[key] = {}
            else:
                try:
                    parsed[key] = json.loads(value)
                except json.JSONDecodeError as e:
                    return parsed, f"`{key}` was not valid JSON ({e})."

    # Also handle steps[].config if emitted as stringified JSON
    if "steps" in parsed and isinstance(parsed["steps"], list):
        cleaned_steps = []
        for s in parsed["steps"]:
            if isinstance(s, dict):
                s_dict = dict(s)
                if "config" in s_dict and isinstance(s_dict["config"], str):
                    val = s_dict["config"].strip()
                    if not val:
                        s_dict["config"] = {}
                    else:
                        try:
                            s_dict["config"] = json.loads(val)
                        except json.JSONDecodeError as e:
                            return parsed, f"Step '{s_dict.get('ref')}' config was not valid JSON ({e})."
                cleaned_steps.append(s_dict)
            else:
                cleaned_steps.append(s)
        parsed["steps"] = cleaned_steps
    if "step_updates" in parsed and isinstance(parsed["step_updates"], list):
        for update in parsed["step_updates"]:
            if isinstance(update, dict) and isinstance(update.get("config"), str):
                try:
                    update["config"] = json.loads(update["config"])
                except json.JSONDecodeError as error:
                    return parsed, f"Step update config was not valid JSON ({error})."

    return parsed, None


async def build_tool_declarations() -> list[dict]:
    return [
        {
            "type": "function",
            "name": t.name,
            "description": (t.description or "").strip(),
            "parameters": _to_gemini_schema(t.input_schema),
        }
        for t in filter_tools(await mcp.list_tools())
    ]


async def run_tool(name: str, args: dict, session_id: str | None = None) -> str:
    args, error = _parse_json_params(name, args)
    if error:
        # Returned as a tool result, not raised — the model reads it and
        # retries with corrected arguments, same as any other tool error.
        return json.dumps({"error": error})

    try:
        context = {"session_id": session_id} if session_id else None
        result = await mcp.call_tool(name, args, context)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"{type(e).__name__}: {e}"})

    structured = getattr(result, "structured_content", None)
    if structured is not None:
        if name == "show_workflow":
            from agent.run import _model_result
            structured = _model_result(name, structured)
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
            output = await run_tool(
                call.name, call.arguments or {}, (trace or {}).get("session_id")
            )
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

    from agent import cost, logging_
    import uuid

    client = genai.Client()
    tools = await build_tool_declarations()
    session_id = str(uuid.uuid4())[:8]
    print(f"{len(tools)} tools loaded, surface={current_profile()}, model={MODEL}, session={session_id}\n")

    previous_id: str | None = None

    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        trace: dict = {"session_id": session_id}
        t0 = time.perf_counter()
        await converse(client, tools, question, previous_id, trace=trace)
        duration_ms = (time.perf_counter() - t0) * 1000
        logging_.log_turn(
            session_id=session_id, provider="gemini", model=MODEL,
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
        trace = {"session_id": session_id}
        t0 = time.perf_counter()
        previous_id = await converse(client, tools, user_input, previous_id, trace=trace)
        duration_ms = (time.perf_counter() - t0) * 1000
        logging_.log_turn(
            session_id=session_id, provider="gemini", model=MODEL,
            question=user_input, answer=trace.get("answer", ""),
            tool_calls=trace.get("tool_calls", []),
            input_tokens=trace.get("input_tokens"), output_tokens=trace.get("output_tokens"),
            cost_usd=cost.estimate_cost(MODEL, trace.get("input_tokens"), trace.get("output_tokens")),
            duration_ms=duration_ms, error=trace.get("error"),
        )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

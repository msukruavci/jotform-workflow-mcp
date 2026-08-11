"""
Token pricing for cost estimation in the Q&A terminal logs.

Verified via web search 2026-08-11. Prices change — re-check before
trusting these for anything beyond a rough order of magnitude, and
never trust a number this table can't account for: unknown models
return None rather than a guessed price.

(input_usd_per_million, output_usd_per_million)
"""
from __future__ import annotations

PRICING: dict[str, tuple[float, float]] = {
    # Anthropic — claude-sonnet-5 is introductory pricing through
    # 2026-08-31, standard $3/$15 after. claude-sonnet-4-5 is the
    # previous generation, kept here only for logs from before the
    # agent/run.py default was corrected (see decision-log.md).
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-fable-5": (10.00, 50.00),

    # Google
    "gemini-3.6-flash": (1.50, 7.50),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-pro": (2.00, 12.00),  # <=200K input tokens; doubles above
}


def estimate_cost(model: str, input_tokens: int | None,
                  output_tokens: int | None) -> float | None:
    """
    Returns USD, or None if the model isn't in the table or token counts
    are missing. None is a deliberately visible "unknown", not a 0 or a
    guess — a silent wrong number in a cost log is worse than a gap.
    """
    if model not in PRICING or input_tokens is None or output_tokens is None:
        return None
    in_rate, out_rate = PRICING[model]
    return (input_tokens / 1_000_000 * in_rate) + (output_tokens / 1_000_000 * out_rate)

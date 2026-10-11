#!/usr/bin/env python3
"""model_config.py — which Claude model each call site uses, and what the model needs.

One flag per call site, each defaulting to claude-sonnet-4-6 (today's model). Switching a site to Sonnet 5.5 is one
line here; switching back is the same line. Nothing else in the code names a model for these calls.

    GENERATE_MODEL          adwriter.generate_ad without the search tool (purpose generate)
    RESEARCH_SEARCH_MODEL   adwriter.generate_ad with the search tool (purpose research_search)
    REPRICE_MODEL           the paragraph rewrites: reprice and recon update (adwriter._capped_completion)
    TOW_LOOKUP_MODEL        towing.lookup (tow_refresh --lookup; builds make no tow lookup)

Sonnet 5.5 requests carry thinking={"type": "between_tools"} and an effort level (output_config.effort, default
medium). Never thinking.disabled, budget_tokens or tool_choice.

Token budgets: Sonnet 5.5 counts about 49% more input tokens for the same text and writes about 20% more output
tokens, so its output caps are 1.2x (OUTPUT_FACTOR). Its prices ($2 / $10 per MTok, api_cost.PRICES) are two thirds
of 4.6's, so the same work costs about the same in dollars (input 1.49 x 2/3 = 0.99x, output 1.2 x 2/3 = 0.80x):
the generate guard's dollar limits (GUARD_LIMITS) stay where they are for both models.
"""
from __future__ import annotations

import math
from typing import Any

SONNET_46 = "claude-sonnet-4-6"
SONNET_55 = "claude-sonnet-5-5"

# ---- the flags (all off: today's model) ------------------------------------------------------------------------- #
GENERATE_MODEL = SONNET_46
RESEARCH_SEARCH_MODEL = SONNET_46
REPRICE_MODEL = SONNET_46
TOW_LOOKUP_MODEL = SONNET_46

SONNET_55_EFFORT = "medium"          # low | medium | high
EFFORT_LEVELS = ("low", "medium", "high")

# ---- what changes with the model --------------------------------------------------------------------------------- #
INPUT_FACTOR = {SONNET_46: 1.00, SONNET_55: 1.49}     # input tokens for the same text, relative to 4.6
OUTPUT_FACTOR = {SONNET_46: 1.00, SONNET_55: 1.20}    # output tokens for the same copy, relative to 4.6
# The generate guard (api_cost): dollars per call / per ad and searches. Same for both (see the docstring).
GUARD_LIMITS = {
    SONNET_46: {"call_usd": 0.75, "ad_usd": 1.50, "searches": 6},
    SONNET_55: {"call_usd": 0.75, "ad_usd": 1.50, "searches": 6},
}


def request_kwargs(model: str, *, effort: str | None = None) -> dict[str, Any]:
    """The model and its model-specific request fields. 4.6: just the model. 5.5: thinking between tools and an
    effort level."""
    out: dict[str, Any] = {"model": model}
    if model == SONNET_55:
        level = effort or SONNET_55_EFFORT
        if level not in EFFORT_LEVELS:
            raise ValueError(f"effort {level!r} is not one of {EFFORT_LEVELS}")
        out["thinking"] = {"type": "between_tools"}
        out["output_config"] = {"effort": level}
    return out


def output_cap(model: str, base_tokens: int) -> int:
    """A max_tokens cap set for 4.6, scaled for `model`."""
    return int(math.ceil(base_tokens * OUTPUT_FACTOR.get(model, 1.0)))


def guard_limits(model: str) -> dict[str, float]:
    return dict(GUARD_LIMITS.get(model, GUARD_LIMITS[SONNET_46]))

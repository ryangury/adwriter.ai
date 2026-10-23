#!/usr/bin/env python3
"""model_config.py — which Claude model each call site uses, and what the model needs.

One flag per call site, and for the ad-writing calls one per inventory side: the Mercedes-Benz prompts (status 10 MB
CPO and 16 Z stock) and the non-MB prompts (11 Hendrick Certified, 12 Hendrick Affordable, 13 As-Is). Switching a
flag is one line here, and so is switching it back. Nothing else in the code names a model for these calls; the
orchestrator and the website's manual generate / reprice read the same flags.

    GENERATE_MODEL_MB / _NON_MB   adwriter.generate_ad without the search tool (purpose generate)
    REPRICE_MODEL_MB / _NON_MB    the paragraph-two rewrite of a reprice (adwriter._capped_completion)
    RESEARCH_SEARCH_MODEL_MB / _NON_MB   adwriter.generate_ad with the search tool (purpose research_search)
    TOW_LOOKUP_MODEL              towing.lookup (tow_refresh --lookup; builds make no tow lookup)

A recon update makes no model call. A status outside 11 / 12 / 13 (None included) uses the MB flag.

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

# ---- the flags -------------------------------------------------------------------------------------------------- #
MB_STATUSES = (10, 16)
NON_MB_STATUSES = (11, 12, 13)

GENERATE_MODEL_MB = SONNET_46
GENERATE_MODEL_NON_MB = SONNET_55
REPRICE_MODEL_MB = SONNET_46
REPRICE_MODEL_NON_MB = SONNET_55
RESEARCH_SEARCH_MODEL_MB = SONNET_46
RESEARCH_SEARCH_MODEL_NON_MB = SONNET_55
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


def is_non_mb(status_code: int | None) -> bool:
    return status_code in NON_MB_STATUSES


def generate_model(status_code: int | None) -> str:
    """The model for an ad written without the search tool."""
    return GENERATE_MODEL_NON_MB if is_non_mb(status_code) else GENERATE_MODEL_MB


def research_search_model(status_code: int | None) -> str:
    """The model for an ad written with the search tool (the research search)."""
    return RESEARCH_SEARCH_MODEL_NON_MB if is_non_mb(status_code) else RESEARCH_SEARCH_MODEL_MB


def reprice_model(status_code: int | None) -> str:
    return REPRICE_MODEL_NON_MB if is_non_mb(status_code) else REPRICE_MODEL_MB


def tag(model: str) -> dict[str, Any]:
    """ad_history's generated_with: the model and, for Sonnet 5.5, its effort level."""
    return {"model": model, "effort": SONNET_55_EFFORT if model == SONNET_55 else None}


def describe(tag_or_model: dict[str, Any] | str | None) -> str:
    t = tag(tag_or_model) if isinstance(tag_or_model, str) else (tag_or_model or {})
    if not t.get("model"):
        return "unknown"
    return t["model"] + (f" (effort {t['effort']})" if t.get("effort") else "")


# The morning run's "research refresh" step (orchestrator, after the builds): cars with a Research-page entry, a VIN
# override or "No figure needed" get the no-model text edit (research.auto_refresh). False: the step is skipped.
RESEARCH_AUTO_APPLY = True

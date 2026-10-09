"""bounded_search.py — one web-search conversation with hard limits.

The Anthropic web-search tool can leave a request open for many minutes (the
10/8 build waited 28 minutes on one electric-range search; tow lookups sat
for 4 minutes per attempt before timing out). Every search conversation goes
through run_search():

  * the request is STREAMED and given timeout=silence_s, which the HTTP client
    applies to each read: the request is aborted after `silence_s` (120)
    seconds without receiving any data, however long it has been running;
  * the whole conversation (every pause_turn round, every retry) has a time
    budget (`budget_s`), checked between stream events;
  * at most `max_requests` (4) billed requests are made, a silence-abort retry
    included; the SDK's own retries are turned off so every request is counted;
  * one retry after a silence abort, if budget and requests remain;
  * a conversation over `slow_s` is logged.

Anything that stops the search raises SearchUnavailable (reason: "silent",
"budget", "limit" or "error"); the caller decides what to do (the tow and
range lookups cache nothing; the ad-writing call carries on without search).
"""
from __future__ import annotations

import sys
import time
from typing import Any, Callable

import anthropic

import api_cost

SILENCE_S = 120
BUDGET_S = 5 * 60
MAX_REQUESTS = 4
SLOW_S = 60


def _log_degrade(guard, reason, label, purpose, stock, model, retry, log) -> None:
    api_cost.record(None, purpose=purpose, stock=stock, model=model, retry=retry, label=label,
                    stop_reason=f"degraded: {reason}")
    log(f"[search] DEGRADE {label}: {reason} (searches {guard.searches}, call ${guard.cost:.2f}, "
        f"ad ${guard.ad_before + guard.cost:.2f}); the search tool is dropped, the ad is written from the data package")


class SearchUnavailable(RuntimeError):
    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


def run_search(
    client: Any,
    *,
    kwargs: dict[str, Any],
    messages: list[dict[str, Any]],
    label: str,
    silence_s: float = SILENCE_S,
    budget_s: float = BUDGET_S,
    max_requests: int = MAX_REQUESTS,
    slow_s: float = SLOW_S,
    purpose: str = "other",
    stock: str | None = None,
    guard: "api_cost.Guard | None" = None,
    max_cost_usd: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = lambda m: print(m, file=sys.stderr, flush=True),
) -> Any:
    """The final message of a search conversation (pause_turn rounds followed).
    `kwargs`: model, max_tokens, tools, system... for messages.stream();
    `messages` is extended in place when the server pauses the turn.

    Every billed request is written to api_cost_log (api_cost.record), an
    aborted one as a zero-token row.

    `guard` (api_cost.Guard, ad-writing calls): each request's web-search tool is
    limited to the searches left (max_uses); when the searches are used up or a
    budget is near and the server paused the turn, the conversation stops with
    api_cost.SearchDegrade (logged) and the caller writes without the tool.
    `max_cost_usd` (tow / range lookups): when the conversation's ACTUAL logged
    cost reaches it and more is still needed, it stops with
    SearchUnavailable("cost"); nothing is cached by the callers."""
    c = client.with_options(max_retries=0) if hasattr(client, "with_options") else client
    started = clock()
    used = 0
    silent_aborts = 0
    spent = 0.0
    response = None
    while True:
        if used >= max_requests:
            raise SearchUnavailable("limit", f"{label}: still paused after {max_requests} billed requests; stopped")
        if clock() - started >= budget_s:
            raise SearchUnavailable("budget", f"{label}: over its {int(budget_s // 60)}-minute budget; stopped")
        if guard is not None and (reason := guard.degrade_reason()):
            _log_degrade(guard, reason, label, purpose, stock, kwargs.get("model"), silent_aborts > 0, log)
            raise api_cost.SearchDegrade(reason, f"{label}: {reason}; writing without the search tool")
        used += 1
        live_searches = 0
        workspace = None
        model = kwargs.get("model")
        req_kwargs = kwargs
        if guard is not None:
            req_kwargs = {**kwargs, "tools": [
                {**t, "max_uses": guard.tool_max_uses(t.get("max_uses"))} if str(t.get("name")) == "web_search" else t
                for t in kwargs.get("tools") or []]}
        try:
            with c.messages.stream(messages=messages, timeout=float(silence_s), **req_kwargs) as stream:
                for _event in stream:
                    if getattr(_event, "type", None) == "content_block_start" and                             getattr(getattr(_event, "content_block", None), "type", None) == "server_tool_use":
                        live_searches += 1
                    if clock() - started >= budget_s:
                        api_cost.record(None, purpose=purpose, stock=stock, model=model, retry=silent_aborts > 0,
                                        label=label, stop_reason="aborted: budget", searches=live_searches)
                        raise SearchUnavailable(
                            "budget", f"{label}: over its {int(budget_s // 60)}-minute budget; stopped")
                response = stream.get_final_message()
                workspace = api_cost.workspace_of(stream)
        except (SearchUnavailable, api_cost.SearchDegrade):
            raise
        except anthropic.APITimeoutError as exc:
            api_cost.record(None, purpose=purpose, stock=stock, model=model, retry=silent_aborts > 0, label=label,
                            stop_reason="aborted: no data", searches=live_searches)
            silent_aborts += 1
            log(f"[search] {label}: no data for {int(silence_s)} s (billed request {used} of {max_requests})")
            if silent_aborts > 1:
                raise SearchUnavailable("silent", f"{label}: silent for {int(silence_s)} s twice; stopped") from exc
            continue
        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.APIStatusError) as exc:
            api_cost.record(None, purpose=purpose, stock=stock, model=model, retry=silent_aborts > 0, label=label,
                            stop_reason=f"error: {type(exc).__name__}", searches=live_searches)
            raise SearchUnavailable("error", f"{label}: search request failed: {exc}") from exc
        cost = api_cost.record(response, purpose=purpose, stock=stock, model=model, retry=silent_aborts > 0, label=label,
                               workspace_id=workspace)
        spent += cost or 0.0
        if guard is not None:
            guard.add(cost, api_cost.usage_of(response)["web_search_requests"])
        if max_cost_usd is not None and spent >= max_cost_usd and getattr(response, "stop_reason", None) == "pause_turn":
            api_cost.record(None, purpose=purpose, stock=stock, model=model, retry=silent_aborts > 0, label=label,
                            stop_reason=f"capped: ${spent:.2f} >= ${max_cost_usd:.2f}")
            raise SearchUnavailable("cost", f"{label}: ${spent:.2f} spent (cap ${max_cost_usd:.2f}); stopped, nothing cached")
        if getattr(response, "stop_reason", None) != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})
    elapsed = clock() - started
    if elapsed > slow_s:
        log(f"[search] SLOW {label}: {elapsed:.0f} s over {used} billed request(s)")
    return response

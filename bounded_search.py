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

SILENCE_S = 120
BUDGET_S = 5 * 60
MAX_REQUESTS = 4
SLOW_S = 60


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
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = lambda m: print(m, file=sys.stderr, flush=True),
) -> Any:
    """The final message of a search conversation (pause_turn rounds followed).
    `kwargs`: model, max_tokens, tools, system... for messages.stream();
    `messages` is extended in place when the server pauses the turn."""
    c = client.with_options(max_retries=0) if hasattr(client, "with_options") else client
    started = clock()
    used = 0
    silent_aborts = 0
    response = None
    while True:
        if used >= max_requests:
            raise SearchUnavailable("limit", f"{label}: still paused after {max_requests} billed requests; stopped")
        if clock() - started >= budget_s:
            raise SearchUnavailable("budget", f"{label}: over its {int(budget_s // 60)}-minute budget; stopped")
        used += 1
        try:
            with c.messages.stream(messages=messages, timeout=float(silence_s), **kwargs) as stream:
                for _event in stream:
                    if clock() - started >= budget_s:
                        raise SearchUnavailable(
                            "budget", f"{label}: over its {int(budget_s // 60)}-minute budget; stopped")
                response = stream.get_final_message()
        except SearchUnavailable:
            raise
        except anthropic.APITimeoutError as exc:
            silent_aborts += 1
            log(f"[search] {label}: no data for {int(silence_s)} s (billed request {used} of {max_requests})")
            if silent_aborts > 1:
                raise SearchUnavailable("silent", f"{label}: silent for {int(silence_s)} s twice; stopped") from exc
            continue
        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.APIStatusError) as exc:
            raise SearchUnavailable("error", f"{label}: search request failed: {exc}") from exc
        if getattr(response, "stop_reason", None) != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})
    elapsed = clock() - started
    if elapsed > slow_s:
        log(f"[search] SLOW {label}: {elapsed:.0f} s over {used} billed request(s)")
    return response

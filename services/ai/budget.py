"""
Session budget guard for AI API calls.

HOW IT WORKS:
  Every call to the Gemini API returns a `usage_metadata` object in the response.
  That object contains the actual token counts for that call:
    - prompt_token_count     → tokens we sent (system prompt + task context + user message)
    - candidates_token_count → tokens the model generated back

  gemini_provider.py reads those counts from the response and passes them here
  via record_usage(). We multiply by the per-token dollar rates to get the cost
  of that call, add it to the running session total, and raise BudgetExceededError
  if we've crossed the limit.

  The blown flag is module-level and intentionally never resets within a process.
  Once the budget is hit, every subsequent AI call raises immediately without
  touching the API. A server restart is required to re-enable AI — that's the
  "human in the loop" checkpoint.

PRICING (Gemini 2.5 Flash Lite — verify at https://ai.google.dev/pricing, rates below may be wrong):
  Input:  $0.10 / 1M tokens  ← unconfirmed, check before relying on cost estimates
  Output: $0.40 / 1M tokens  ← unconfirmed, check before relying on cost estimates

SESSION_LIMIT defaults to $0.05 but can be overridden via AI_SESSION_BUDGET in .env.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

# Configurable via .env so you can raise/lower the cap without touching code
SESSION_LIMIT: float = float(os.environ.get("AI_SESSION_BUDGET", "0.05"))

# Dollar cost per token (input and output are priced differently)
_INPUT_RATE  = 0.10 / 1_000_000   # per input token  — update after verifying at ai.google.dev/pricing
_OUTPUT_RATE = 0.40 / 1_000_000   # per output token — update after verifying at ai.google.dev/pricing

# Running total and kill-switch — module-level so they survive across requests
# but reset when the server process restarts
_cumulative_cost: float = 0.0
_budget_blown: bool = False

# Separate log file just for cost tracking — keeps errors.log clean
# and gives you an easy audit trail of every API call with its token counts
_handler = RotatingFileHandler("costs.log", maxBytes=1_000_000, backupCount=3)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
_cost_log = logging.getLogger("ai.cost")
_cost_log.setLevel(logging.INFO)
_cost_log.addHandler(_handler)


class BudgetExceededError(RuntimeError):
    pass


def check() -> None:
    """
    Pre-flight check — call this before hitting the API to fail fast
    if the budget was already blown by a previous request this session.
    """
    if _budget_blown:
        raise BudgetExceededError(
            f"Session AI budget of ${SESSION_LIMIT:.2f} exceeded. "
            "AI is disabled until the server is restarted."
        )


def record_usage(input_tokens: int, output_tokens: int, model: str) -> float:
    """
    Called by the provider after every successful API response.

    input_tokens / output_tokens come directly from response.usage_metadata,
    which Gemini populates with the actual token counts for that call.

    Returns the dollar cost of this specific call.
    Raises BudgetExceededError if this call pushed the running total over the limit.
    Once blown, every future call also raises — no further API calls are made.
    """
    global _cumulative_cost, _budget_blown

    # If a previous call already blew the budget, stop immediately
    if _budget_blown:
        raise BudgetExceededError(
            f"Session AI budget of ${SESSION_LIMIT:.2f} already exceeded. "
            "AI is disabled until the server is restarted."
        )

    call_cost = input_tokens * _INPUT_RATE + output_tokens * _OUTPUT_RATE
    _cumulative_cost += call_cost

    # Every call is logged so you can audit exactly what ran up the bill
    _cost_log.info(
        "model=%-30s  in=%6d  out=%6d  call=$%.5f  total=$%.5f  limit=$%.2f",
        model, input_tokens, output_tokens, call_cost, _cumulative_cost, SESSION_LIMIT,
    )

    if _cumulative_cost >= SESSION_LIMIT:
        _budget_blown = True
        raise BudgetExceededError(
            f"Session AI budget of ${SESSION_LIMIT:.2f} reached "
            f"(spent ${_cumulative_cost:.4f}). "
            "AI is disabled until the server is restarted."
        )

    return call_cost


def get_stats() -> dict:
    """Current spend stats — useful for a status endpoint or debug logging."""
    return {
        "total_cost": round(_cumulative_cost, 6),
        "limit":      SESSION_LIMIT,
        "remaining":  round(max(0.0, SESSION_LIMIT - _cumulative_cost), 6),
        "blown":      _budget_blown,
    }

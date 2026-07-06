"""
Hourly rolling budget guard for AI API calls.

HOW IT WORKS:
  Every call to the Gemini API returns a `usage_metadata` object in the response.
  That object contains the actual token counts for that call, split across the
  input -> process -> output pipeline:
    - prompt_token_count          → input tokens (system prompt + task context + user message)
    - tool_use_prompt_token_count → input-side overhead for function-calling tool declarations
    - thoughts_token_count        → "process" tokens spent on internal reasoning, billed as output
    - candidates_token_count      → output tokens the model generated back

  gemini_provider.py sums prompt_token_count + tool_use_prompt_token_count into
  input_tokens, and candidates_token_count + thoughts_token_count into output_tokens,
  then passes both here via record_usage(). We multiply by the per-token dollar rates
  to get the cost of that call, insert a row into the ai_usage_log Postgres table, and
  raise BudgetExceededError if the sum of costs in the last 60 minutes exceeds the limit.

  Once the rolling window slides past old entries, the budget automatically recovers —
  no server restart needed. The limit applies to all API calls combined (generative
  AND embedding).

  The rolling window is backed by the ai_usage_log Postgres table (not an in-memory
  deque) specifically so the cap holds across multiple serverless instances — an
  in-memory version would give every cold-started instance its own independent
  budget, silently multiplying the effective limit. costs.log remains a write-only
  local audit trail alongside it, never read back.

PRICING (verify at https://ai.google.dev/pricing, rates below may be wrong):
  Gemini 2.5 Flash Lite — Input:  $0.10 / 1M tokens  ← unconfirmed
  Gemini 2.5 Flash Lite — Output: $0.40 / 1M tokens  ← unconfirmed
  gemini-embedding-001  — Input:  $0.025 / 1M tokens  ← unconfirmed (estimated from text-embedding-004)
  Embedding responses carry no usage_metadata, so token count is estimated at 1 token per 4 chars.

HOURLY_LIMIT defaults to $0.15 but can be overridden via AI_HOURLY_BUDGET in .env.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

import psycopg2

HOURLY_LIMIT: float = float(os.environ.get("AI_HOURLY_BUDGET", "0.15"))

_INPUT_RATE  = 0.10  / 1_000_000
_OUTPUT_RATE = 0.40  / 1_000_000
_EMBED_RATE  = 0.025 / 1_000_000

_handler = RotatingFileHandler("costs.log", maxBytes=1_000_000, backupCount=3)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
_cost_log = logging.getLogger("ai.cost")
_cost_log.setLevel(logging.INFO)
_cost_log.addHandler(_handler)

# Own lazily-created connection, independent of Flask's g-scoped one — embedder.py
# and gemini_provider.py call check()/record_usage() from contexts that may not
# have a Flask app/request context (e.g. standalone scripts, indexer.index_all()).
_conn = None


def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(os.environ["DATABASE_URL"])
        _conn.autocommit = True
    return _conn


class BudgetExceededError(RuntimeError):
    pass


def _hourly_cost() -> float:
    """Sum of all call costs within the last 60 minutes."""
    with _get_conn().cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(cost), 0) FROM ai_usage_log WHERE ts >= now() - interval '1 hour'")
        return float(cur.fetchone()[0])


def _trim() -> None:
    """Drop entries older than 24h — a housekeeping pass, not the rolling-window
    boundary itself (that's the `ts >= now() - interval '1 hour'` filter in
    _hourly_cost()). Keeping a day of history costs nothing at this scale and
    leaves room for a future stats/debug view."""
    with _get_conn().cursor() as cur:
        cur.execute("DELETE FROM ai_usage_log WHERE ts < now() - interval '24 hours'")


def check() -> None:
    """
    Pre-flight check — call this before hitting the API to fail fast
    if the hourly budget is already exhausted.
    """
    _trim()
    spent = _hourly_cost()
    if spent >= HOURLY_LIMIT:
        raise BudgetExceededError(
            f"Hourly AI budget of ${HOURLY_LIMIT:.2f} exceeded "
            f"(${spent:.4f} spent in the last hour). Try again later."
        )


def record_usage(input_tokens: int, output_tokens: int, model: str) -> float:
    """
    Called by the provider after every successful API response.

    Returns the dollar cost of this specific call.
    Raises BudgetExceededError if this call pushed the rolling hourly total over the limit.
    """
    _trim()

    call_cost = input_tokens * _INPUT_RATE + output_tokens * _OUTPUT_RATE
    with _get_conn().cursor() as cur:
        cur.execute(
            "INSERT INTO ai_usage_log (cost, kind, model) VALUES (%s, 'generation', %s)",
            (call_cost, model),
        )
    hourly = _hourly_cost()

    _cost_log.info(
        "model=%-30s  in=%6d  out=%6d  call=$%.5f  hour=$%.5f  limit=$%.2f",
        model, input_tokens, output_tokens, call_cost, hourly, HOURLY_LIMIT,
    )

    if hourly >= HOURLY_LIMIT:
        raise BudgetExceededError(
            f"Hourly AI budget of ${HOURLY_LIMIT:.2f} reached "
            f"(${hourly:.4f} spent in the last hour). Try again later."
        )

    return call_cost


def record_embedding_usage(char_count: int, model: str) -> float:
    """
    Called by the embedder after every embed_content API call.

    Token count is estimated at 1 token per 4 characters.
    """
    _trim()

    estimated_tokens = max(1, char_count // 4)
    call_cost = estimated_tokens * _EMBED_RATE
    with _get_conn().cursor() as cur:
        cur.execute(
            "INSERT INTO ai_usage_log (cost, kind, model) VALUES (%s, 'embedding', %s)",
            (call_cost, model),
        )
    hourly = _hourly_cost()

    _cost_log.info(
        "model=%-30s  chars=%6d  ~tokens=%5d  call=$%.6f  hour=$%.5f  limit=$%.2f  [embed]",
        model, char_count, estimated_tokens, call_cost, hourly, HOURLY_LIMIT,
    )

    if hourly >= HOURLY_LIMIT:
        raise BudgetExceededError(
            f"Hourly AI budget of ${HOURLY_LIMIT:.2f} reached "
            f"(${hourly:.4f} spent in the last hour). Try again later."
        )

    return call_cost


def get_stats() -> dict:
    """Current hourly spend stats — useful for a status endpoint or debug logging."""
    _trim()
    hourly = _hourly_cost()
    return {
        "hourly_cost":  round(hourly, 6),
        "limit":        HOURLY_LIMIT,
        "remaining":    round(max(0.0, HOURLY_LIMIT - hourly), 6),
        "window_hours": 1,
    }

import logging
import os
import random
import time

from services.ai import budget

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BASE_DELAY = 1.0


def _is_retryable(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code in _RETRYABLE_STATUS:
        return True
    try:
        return int(str(exc).split()[0]) in _RETRYABLE_STATUS
    except (ValueError, IndexError):
        return False


def _with_retry(fn):
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:
            if not _is_retryable(exc) or attempt == _MAX_RETRIES:
                raise
            delay = _BASE_DELAY * (2 ** attempt) + random.uniform(0, 1)
            logger.warning(
                "Transient embed error (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1, _MAX_RETRIES, delay, exc,
            )
            time.sleep(delay)

_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


_EMBED_MODEL = "gemini-embedding-001"  # successor to text-embedding-004 in the v2 SDK


def embed(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> list[float]:
    budget.check()
    from google.genai import types
    cfg = types.EmbedContentConfig(task_type=task_type)
    response = _with_retry(lambda: _get_client().models.embed_content(
        model=_EMBED_MODEL, contents=text, config=cfg,
    ))
    budget.record_embedding_usage(len(text), _EMBED_MODEL)
    return list(response.embeddings[0].values)


def embed_query(text: str) -> list[float]:
    return embed(text, task_type="RETRIEVAL_QUERY")


def embed_batch(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    if not texts:
        return []
    budget.check()
    from google.genai import types
    total_chars = sum(len(t) for t in texts)
    cfg = types.EmbedContentConfig(task_type=task_type)
    try:
        response = _with_retry(lambda: _get_client().models.embed_content(
            model=_EMBED_MODEL, contents=texts, config=cfg,
        ))
        budget.record_embedding_usage(total_chars, _EMBED_MODEL)
        return [list(e.values) for e in response.embeddings]
    except Exception:
        logger.warning("Batch embed failed after retries, falling back to sequential")
        return [embed(t, task_type=task_type) for t in texts]

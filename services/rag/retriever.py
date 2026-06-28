import logging
import re

from .embedder import embed_query
from .store import StoredChunk, list_collections, query_collection

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "and", "but", "or", "nor",
    "not", "so", "yet", "to", "of", "in", "on", "at", "by", "for", "with",
    "about", "from", "that", "this", "these", "those", "i", "me", "my",
    "we", "our", "you", "your", "he", "she", "it", "they", "them", "their",
    "what", "which", "who", "how", "when", "where", "why",
}


def _keywords(query: str) -> set[str]:
    words = re.sub(r"[^\w\s]", " ", query.lower()).split()
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def retrieve(
    query: str,
    k: int = 5,
    collections: list[str] | None = None,
) -> list[StoredChunk]:
    try:
        query_emb = embed_query(query)
    except Exception:
        logger.exception("Failed to embed query for RAG retrieval")
        return []

    kw = _keywords(query)
    target = collections or list_collections()

    raw: list[StoredChunk] = []
    for col in target:
        raw.extend(query_collection(col, query_emb, k=k * 2))

    if not raw:
        return []

    # Hybrid re-rank: keyword presence in chunk reduces cosine distance
    for chunk in raw:
        if kw:
            text_lower = chunk.text.lower()
            hits = sum(1 for w in kw if w in text_lower)
            if hits:
                chunk.distance *= max(0.5, 1.0 - 0.1 * min(hits, 5))

    seen: set[str] = set()
    ranked: list[StoredChunk] = []
    for chunk in sorted(raw, key=lambda c: c.distance):
        key = chunk.text[:120]
        if key not in seen:
            seen.add(key)
            ranked.append(chunk)

    MAX_DISTANCE = 0.3
    return [c for c in ranked[:k] if c.distance <= MAX_DISTANCE]

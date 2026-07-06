"""
Connection engine — surfaces non-obvious, cross-folder semantic links between vault
notes. Explicitly parallel to services/rag/ (see CONNECTION_ENGINE_DESIGN.md for the
full reasoning): reads the existing vector store read-only via store.py/embedder.py's
public functions only, never imports retriever.py or injector.py, and never modifies
any RAG pipeline file. Results are cached in their own SQLite table, not a new
ChromaDB collection (a new collection would silently get pulled into the standard
retriever's default "search every collection" behavior — see the design doc).
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from services.rag.embedder import embed_query
from services.rag.store import list_collections, query_collection
from services.vault import storage

logger = logging.getLogger(__name__)

# The standard retriever caps confident matches at distance <= 0.3. This engine
# deliberately looks in a different, looser band — too close is "obviously the same
# topic" (not a discovery), too far is noise. See CONNECTION_ENGINE_DESIGN.md.
# MAX_DISTANCE was 0.45 originally; lowered after testing against real vault content
# showed genuinely unrelated notes landing at ~0.41-0.42 (margin of ~0.03 from the old
# cutoff, sometimes less than the noise introduced by a since-fixed indexing race) —
# real cross-folder matches in this vault cluster at <=0.39, so 0.40 keeps those while
# excluding both the unrelated case and several near-duplicate generic reference pages.
MIN_DISTANCE = 0.15
MAX_DISTANCE = 0.40
MAX_CHARS_FOR_EMBEDDING = 2000

# Chat transcripts are conversation history, not vault notes — never a source or
# target of a "note connection."
_EXCLUDED_COLLECTIONS = {"chats"}

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "and", "but", "or", "not", "to", "of", "in", "on", "at", "by",
    "for", "with", "about", "from", "that", "this", "these", "those", "you",
    "your", "they", "them", "their", "what", "which", "who", "how", "when",
}


@dataclass
class Connection:
    source_path: str
    target_path: str
    summary: str
    score: float  # cosine distance — lower is closer


def _collection_for(note_path: str) -> str:
    """First path segment of a vault-relative path is its folder/collection name —
    same convention services/rag/indexer.py uses, kept as a tiny local copy so this
    module has no import dependency on the RAG indexing pipeline."""
    parts = Path(note_path).parts
    return parts[0] if parts else ""


def _note_text(note_path: str) -> str | None:
    """Reads a markdown vault note's body (frontmatter stripped), truncated to a
    representative excerpt for embedding. v1 only supports .md notes — the dominant
    vault content type — rather than adding more format-parsing dependencies here;
    other formats are skipped gracefully (empty result, not an error)."""
    if not note_path.endswith(".md"):
        return None
    try:
        raw = storage.download(note_path).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return None
    try:
        import frontmatter as fm
        text = fm.loads(raw).content.strip()
    except Exception:
        text = raw.strip()
    return text[:MAX_CHARS_FOR_EMBEDDING] if text else None


def _keywords(text: str) -> set[str]:
    words = re.sub(r"[^\w\s]", " ", text.lower()).split()
    return {w for w in words if w not in _STOPWORDS and len(w) > 3}


def _passes_filter(distance: float, source_collection: str, target_collection: str) -> bool:
    """The 'non-obvious' heuristic as a standalone, independently-testable pure
    function: moderate cross-folder semantic overlap — not near-duplicate (too
    close), not noise (too far), and never within the source note's own folder.
    See CONNECTION_ENGINE_DESIGN.md for the reasoning behind the specific band."""
    if target_collection == source_collection:
        return False
    return MIN_DISTANCE <= distance <= MAX_DISTANCE


def _summarize(source_path: str, source_collection: str, target_collection: str,
               source_text: str, target_text: str) -> str:
    shared = sorted(_keywords(source_text) & _keywords(target_text))[:5]
    base = (
        f"Semantically related to {source_path}, despite living in a different "
        f"vault section ({source_collection} vs {target_collection})"
    )
    return f"{base} — shared terms: {', '.join(shared)}." if shared else f"{base}."


def discover_connections(note_path: str, k: int = 5, db=None) -> list["Connection"]:
    """Find non-obvious connections from one vault note to others: moderate,
    cross-folder semantic overlap the standard retriever wouldn't surface. Live query
    — always recomputes, no cache read here (see get_saved_connections for that). If
    `db` is given, results are also upserted into note_connections as a best-effort
    cache/log, but that's not required for this function's own correctness."""
    note_path = note_path.lstrip("/")

    source_text = _note_text(note_path)
    if not source_text:
        return []

    source_collection = _collection_for(note_path)

    try:
        query_emb = embed_query(source_text)
    except Exception:
        logger.exception("Failed to embed %s for connection discovery", note_path)
        return []

    candidates = []
    for col in list_collections():
        if col == source_collection or col in _EXCLUDED_COLLECTIONS:
            continue
        candidates.extend(query_collection(col, query_emb, k=max(k * 3, 10)))

    best_per_target = {}
    for c in candidates:
        if c.source_path == note_path:
            continue
        if not _passes_filter(c.distance, source_collection, c.collection):
            continue
        existing = best_per_target.get(c.source_path)
        if existing is None or c.distance < existing.distance:
            best_per_target[c.source_path] = c

    ranked = sorted(best_per_target.values(), key=lambda c: c.distance)[:k]

    connections = []
    for c in ranked:
        summary = _summarize(note_path, source_collection, c.collection, source_text, c.text)
        connections.append(Connection(
            source_path=note_path,
            target_path=c.source_path,
            summary=summary,
            score=c.distance,
        ))

    if db is not None:
        try:
            save_connections(db, connections, source_collection)
        except Exception:
            logger.exception("Failed to persist connections for %s", note_path)

    return connections


def _connection_id(source_path: str, target_path: str) -> str:
    return hashlib.md5(f"{source_path}::{target_path}".encode()).hexdigest()


def save_connections(db, connections: list["Connection"], source_collection: str) -> None:
    """Upserts discovered connections into note_connections, keyed by
    (source_path, target_path) so repeat discovery runs overwrite rather than
    accumulate duplicates."""
    now = datetime.now(timezone.utc).isoformat()
    for c in connections:
        db.execute(
            """INSERT INTO note_connections
               (id, source_path, target_path, source_collection, target_collection,
                distance, summary, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
                   source_path = EXCLUDED.source_path,
                   target_path = EXCLUDED.target_path,
                   source_collection = EXCLUDED.source_collection,
                   target_collection = EXCLUDED.target_collection,
                   distance = EXCLUDED.distance,
                   summary = EXCLUDED.summary,
                   created_at = EXCLUDED.created_at""",
            (
                _connection_id(c.source_path, c.target_path),
                c.source_path, c.target_path,
                source_collection, _collection_for(c.target_path),
                c.score, c.summary, now,
            ),
        )
    db.commit()


def get_saved_connections(db, note_path: str) -> list["Connection"]:
    """Reads back previously discovered connections for a note without recomputing."""
    note_path = note_path.lstrip("/")
    rows = db.execute(
        "SELECT target_path, summary, distance FROM note_connections "
        "WHERE source_path = ? ORDER BY distance ASC",
        (note_path,),
    ).fetchall()
    return [
        Connection(source_path=note_path, target_path=r["target_path"],
                   summary=r["summary"], score=r["distance"])
        for r in rows
    ]


def delete_connections_for(db, note_path: str) -> None:
    """Best-effort cleanup when a vault file is deleted or moved — mirrors how the
    RAG index is already cleaned up on vault delete in app.py."""
    note_path = note_path.lstrip("/")
    db.execute(
        "DELETE FROM note_connections WHERE source_path = ? OR target_path = ?",
        (note_path, note_path),
    )
    db.commit()

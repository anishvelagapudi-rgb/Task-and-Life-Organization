import hashlib
import logging
import os
from dataclasses import dataclass

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
from pgvector import Vector
from pgvector.psycopg2 import register_vector

logger = logging.getLogger(__name__)

# Own lazily-created connection, independent of Flask's g-scoped one in db.py —
# rag_test.py and other standalone scripts call index_file()/query_collection()
# directly with no Flask app/request context. Autocommit because every call here
# was previously a fire-and-forget Chroma operation (upsert/delete/query), never
# paired with an explicit commit().
_conn = None


def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(os.environ["DATABASE_URL"])
        _conn.autocommit = True
        register_vector(_conn)
    return _conn


def _chunk_id(source_path: str, index: int) -> str:
    h = hashlib.md5(source_path.encode()).hexdigest()[:8]
    return f"{h}_{index}"


@dataclass
class StoredChunk:
    text: str
    source_path: str
    collection: str
    heading: str
    distance: float
    ai_generated: bool = False
    reviewed: bool = True


def list_collections() -> list[str]:
    """Return names of all collections currently in the vector store.

    Only collections with >=1 chunk are returned (a collection whose last chunk
    was deleted "disappears") — unlike Chroma, which kept empty collection objects
    alive until an explicit delete_collection() call. Functionally a no-op for
    retriever.py (searching one fewer, empty collection changes nothing), and
    arguably a nice self-cleaning property."""
    try:
        with _get_conn().cursor() as cur:
            cur.execute("SELECT DISTINCT collection FROM vault_chunks ORDER BY collection")
            return [row[0] for row in cur.fetchall()]
    except Exception:
        return []


def upsert_chunks(collection_name: str, chunks, embeddings: list[list[float]]) -> None:
    if not chunks:
        return
    rows = [
        (
            _chunk_id(c.source_path, i),
            collection_name,
            c.source_path,
            c.heading,
            bool(c.ai_generated),
            bool(c.reviewed),
            c.text,
            Vector(embeddings[i]),
        )
        for i, c in enumerate(chunks)
    ]
    with _get_conn().cursor() as cur:
        execute_values(cur, """
            INSERT INTO vault_chunks (id, collection, source_path, heading,
                                       ai_generated, reviewed, text, embedding)
            VALUES %s
            ON CONFLICT (collection, id) DO UPDATE SET
                source_path = EXCLUDED.source_path,
                heading = EXCLUDED.heading,
                ai_generated = EXCLUDED.ai_generated,
                reviewed = EXCLUDED.reviewed,
                text = EXCLUDED.text,
                embedding = EXCLUDED.embedding
        """, rows)


def delete_by_source(collection_name: str, source_path: str) -> None:
    try:
        with _get_conn().cursor() as cur:
            cur.execute(
                "DELETE FROM vault_chunks WHERE collection = %s AND source_path = %s",
                (collection_name, source_path),
            )
    except Exception:
        pass


def delete_collection(collection_name: str) -> None:
    """Deletes every chunk in a collection. Public function replacing the old
    private-API reach-around (`_get_client().delete_collection(name)`) that
    app.py's vault_delete_folder route used against the Chroma client directly."""
    try:
        with _get_conn().cursor() as cur:
            cur.execute("DELETE FROM vault_chunks WHERE collection = %s", (collection_name,))
    except Exception:
        logger.exception("Failed to delete collection %s", collection_name)


def count_by_source(collection_name: str, source_path: str) -> int:
    """Public function replacing rag_test.py's private-API reach-around
    (`_get_collection(...).get(where=...)`) used for idempotency assertions."""
    with _get_conn().cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM vault_chunks WHERE collection = %s AND source_path = %s",
            (collection_name, source_path),
        )
        return cur.fetchone()[0]


def query_collection(
    collection_name: str, query_embedding: list[float], k: int = 10
) -> list[StoredChunk]:
    try:
        q = Vector(query_embedding)
        with _get_conn().cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT source_path, heading, ai_generated, reviewed, text,
                       embedding <=> %s AS distance
                FROM vault_chunks
                WHERE collection = %s
                ORDER BY embedding <=> %s
                LIMIT %s
            """, (q, collection_name, q, k))
            rows = cur.fetchall()
        chunks = []
        for row in rows:
            source_path = row["source_path"]
            if source_path is None:
                # Defense in depth — source_path is NOT NULL on vault_chunks, so this
                # can't trigger on fresh data, but mirrors the deliberate Chroma-era
                # behavior (documented in CLAUDE.md) of skipping just the bad chunk
                # rather than dropping the whole collection's results.
                logger.warning(
                    "Skipping chunk with missing/None metadata in collection %s",
                    collection_name,
                )
                continue
            chunks.append(StoredChunk(
                text=row["text"],
                source_path=source_path,
                collection=collection_name,
                heading=row["heading"] or "",
                distance=row["distance"],
                ai_generated=bool(row["ai_generated"]),
                reviewed=bool(row["reviewed"]),
            ))
        return chunks
    except Exception:
        logger.exception("Query failed for collection %s", collection_name)
        return []

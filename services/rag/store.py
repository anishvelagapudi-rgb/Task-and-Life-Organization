import hashlib
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(__file__)
CHROMA_PATH = os.path.abspath(os.path.join(_HERE, "..", "..", "data", "chroma"))

def list_collections() -> list[str]:
    """Return names of all collections currently in the vector store."""
    try:
        return [c.name for c in _get_client().list_collections()]
    except Exception:
        return []

_client = None


def _get_client():
    global _client
    if _client is None:
        import chromadb
        os.makedirs(CHROMA_PATH, exist_ok=True)
        _client = chromadb.PersistentClient(path=CHROMA_PATH)
    return _client


def _get_collection(name: str):
    return _get_client().get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )


def _chunk_id(source_path: str, index: int) -> str:
    h = hashlib.md5(source_path.encode()).hexdigest()[:8]
    return f"{h}_{index}"


@dataclass
class StoredChunk:
    text: str
    source_path: str
    collection: str
    heading: str
    ai_generated: bool
    reviewed: bool
    distance: float


def upsert_chunks(collection_name: str, chunks, embeddings: list[list[float]]) -> None:
    if not chunks:
        return
    col = _get_collection(collection_name)
    col.upsert(
        ids=[_chunk_id(c.source_path, i) for i, c in enumerate(chunks)],
        embeddings=embeddings,
        documents=[c.text for c in chunks],
        metadatas=[
            {
                "source_path": c.source_path,
                "heading": c.heading,
                "ai_generated": int(c.ai_generated),
                "reviewed": int(c.reviewed),
            }
            for c in chunks
        ],
    )


def delete_by_source(collection_name: str, source_path: str) -> None:
    try:
        col = _get_collection(collection_name)
        col.delete(where={"source_path": source_path})
    except Exception:
        pass


def query_collection(
    collection_name: str, query_embedding: list[float], k: int = 10
) -> list[StoredChunk]:
    try:
        col = _get_collection(collection_name)
        n = col.count()
        if n == 0:
            return []
        results = col.query(
            query_embeddings=[query_embedding],
            n_results=min(k, n),
            include=["documents", "metadatas", "distances"],
        )
        return [
            StoredChunk(
                text=doc,
                source_path=meta["source_path"],
                collection=collection_name,
                heading=meta.get("heading", ""),
                ai_generated=bool(meta.get("ai_generated", 0)),
                reviewed=bool(meta.get("reviewed", 1)),
                distance=dist,
            )
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            )
        ]
    except Exception:
        logger.exception("Query failed for collection %s", collection_name)
        return []

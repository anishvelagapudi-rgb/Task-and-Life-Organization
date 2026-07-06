import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _collection_for(key: str) -> str | None:
    """Return the vector-store collection name for a vault storage key.

    Any top-level "folder" in the key (e.g. "journal/note.md" -> "journal") is a
    valid collection — no static allowlist needed, so user-added sections (inbox,
    school, etc.) work automatically.
    """
    parts = key.strip("/").split("/")
    if not parts or not parts[0] or parts[0].startswith("."):
        return None
    return parts[0]


def index_file(key: str) -> None:
    from .chunker import SUPPORTED_EXTENSIONS
    if Path(key).suffix.lower() not in SUPPORTED_EXTENSIONS:
        return
    collection = _collection_for(key)
    if not collection:
        return
    try:
        from services.vault import storage
        from .chunker import chunk_bytes
        from .embedder import embed_batch
        from .store import delete_by_source, upsert_chunks

        delete_by_source(collection, key)
        data = storage.download(key)
        chunks = chunk_bytes(data, key, collection)
        if not chunks:
            return
        embeddings = embed_batch([c.text for c in chunks])
        upsert_chunks(collection, chunks, embeddings)
        logger.info("Indexed %d chunks from %s", len(chunks), key)
    except FileNotFoundError:
        logger.info("Skipping index for missing vault key %s", key)
    except Exception:
        logger.exception("Failed to index %s", key)


def delete_file(key: str) -> None:
    collection = _collection_for(key)
    if not collection:
        return
    try:
        from .store import delete_by_source
        delete_by_source(collection, key)
        logger.info("Removed index for %s", key)
    except Exception:
        logger.exception("Failed to remove index for %s", key)


def index_all() -> None:
    from services.vault import storage
    from .chunker import SUPPORTED_EXTENSIONS
    count = 0
    for folder in storage.list_top_level_folders():
        for key in storage.list_keys(folder):
            if Path(key).suffix.lower() in SUPPORTED_EXTENSIONS:
                index_file(key)
                count += 1
    logger.info("Initial vault index complete: %d files processed", count)

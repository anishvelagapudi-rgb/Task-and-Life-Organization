import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(__file__)
VAULT_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", "data", "vault"))

def _collection_for(path: str) -> str | None:
    """Return the ChromaDB collection name for a vault file.

    Any direct subfolder of the vault root is a valid collection — no static
    allowlist needed, so user-added sections (inbox, school, etc.) work automatically.
    """
    try:
        rel = os.path.relpath(path, VAULT_ROOT)
        parts = Path(rel).parts
        if not parts or parts[0].startswith("."):
            return None
        return parts[0]
    except Exception:
        return None


def index_file(path: str) -> None:
    from .chunker import SUPPORTED_EXTENSIONS
    if Path(path).suffix.lower() not in SUPPORTED_EXTENSIONS:
        return
    collection = _collection_for(path)
    if not collection:
        return
    try:
        from .chunker import chunk_file
        from .embedder import embed_batch
        from .store import delete_by_source, upsert_chunks

        delete_by_source(collection, path)
        chunks = chunk_file(path, collection)
        if not chunks:
            return
        embeddings = embed_batch([c.text for c in chunks])
        upsert_chunks(collection, chunks, embeddings)
        logger.info("Indexed %d chunks from %s", len(chunks), path)
    except Exception:
        logger.exception("Failed to index %s", path)


def delete_file(path: str) -> None:
    collection = _collection_for(path)
    if not collection:
        return
    try:
        from .store import delete_by_source
        delete_by_source(collection, path)
        logger.info("Removed index for %s", path)
    except Exception:
        logger.exception("Failed to remove index for %s", path)


def index_all() -> None:
    if not os.path.exists(VAULT_ROOT):
        logger.info("Vault root %s not found, skipping initial index", VAULT_ROOT)
        return
    from .chunker import SUPPORTED_EXTENSIONS
    count = 0
    for root, _, files in os.walk(VAULT_ROOT):
        for fname in files:
            if Path(os.path.join(root, fname)).suffix.lower() in SUPPORTED_EXTENSIONS:
                index_file(os.path.join(root, fname))
                count += 1
    logger.info("Initial vault index complete: %d files processed", count)

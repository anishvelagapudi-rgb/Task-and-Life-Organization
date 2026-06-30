import logging

from .chunker import Chunk, CHUNK_MAX_CHARS
from .embedder import embed_batch
from .store import delete_by_source, upsert_chunks

logger = logging.getLogger(__name__)
CHAT_COLLECTION = "chats"


def _source_path(chat_id: str) -> str:
    return f"chats/{chat_id}"


def index_chat(chat_id: str, title: str, messages: list[dict]) -> None:
    source = _source_path(chat_id)
    delete_by_source(CHAT_COLLECTION, source)

    lines = [f"PAST CONVERSATION: {title}\n"]
    for m in messages:
        role = "User" if m["role"] == "user" else "Assistant"
        lines.append(f"{role}: {m['content']}")
    full_text = "\n".join(lines)

    chunks = []
    pos = 0
    idx = 0
    while pos < len(full_text):
        end = min(pos + CHUNK_MAX_CHARS, len(full_text))
        if end < len(full_text):
            nl = full_text.rfind("\n", pos, end)
            if nl > pos:
                end = nl
        piece = full_text[pos:end].strip()
        if piece:
            chunks.append(Chunk(
                text=piece,
                source_path=source,
                collection=CHAT_COLLECTION,
                heading=title if idx == 0 else f"{title} (cont.)",
            ))
            idx += 1
        pos = end

    if not chunks:
        return

    embeddings = embed_batch([c.text for c in chunks])
    if len(embeddings) != len(chunks):
        logger.error("Embedding count mismatch for chat %s (%d chunks, %d embeddings) — skipping index", chat_id, len(chunks), len(embeddings))
        return
    upsert_chunks(CHAT_COLLECTION, chunks, embeddings)
    logger.info("Indexed chat %s (%d chunks)", chat_id, len(chunks))


def deindex_chat(chat_id: str) -> None:
    delete_by_source(CHAT_COLLECTION, _source_path(chat_id))
    logger.info("Deindexed chat %s", chat_id)

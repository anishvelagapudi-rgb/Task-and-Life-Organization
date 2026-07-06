from .store import StoredChunk


def build_context(chunks: list[StoredChunk]) -> str:
    if not chunks:
        return ""
    lines = [
        "VAULT CONTEXT (your personal notes — prefer over general knowledge when relevant):",
        "Note: chunks may not always be perfectly relevant; use your judgment.",
    ]
    for i, c in enumerate(chunks, 1):
        src = c.source_path
        if src.startswith("chats/"):
            src = "[past conversation]"
        heading_label = f" § {c.heading}" if c.heading else ""
        lines.append(f"\n[{i}] {src}{heading_label}")
        lines.append(c.text)
        lines.append("---")
    return "\n".join(lines)

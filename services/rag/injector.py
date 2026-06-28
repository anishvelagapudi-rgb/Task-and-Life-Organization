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
        if "/data/vault/" in src:
            src = src.split("/data/vault/", 1)[1]
        unreviewed = (
            " [AI-GENERATED, UNREVIEWED — treat with skepticism]"
            if c.ai_generated and not c.reviewed
            else ""
        )
        heading_label = f" § {c.heading}" if c.heading else ""
        lines.append(f"\n[{i}] {src}{heading_label}{unreviewed}")
        lines.append(c.text)
        lines.append("---")
    return "\n".join(lines)

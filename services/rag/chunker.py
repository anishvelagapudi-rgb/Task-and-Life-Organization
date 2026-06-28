import re
from dataclasses import dataclass, field
from pathlib import Path


CHUNK_MAX_CHARS = 2000
OVERLAP_CHARS = 200
SUPPORTED_EXTENSIONS = {".md", ".pdf", ".html", ".htm", ".docx", ".txt"}


@dataclass
class Chunk:
    text: str
    source_path: str
    collection: str
    heading: str = ""
    tags: list = field(default_factory=list)
    ai_generated: bool = False
    reviewed: bool = True


def _parse_file(path: str) -> tuple[dict, str]:
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        import frontmatter
        post = frontmatter.loads(raw)
        return dict(post.metadata), post.content
    except Exception:
        return {}, raw


def _extract_pdf(path: str) -> tuple[dict, str]:
    from pypdf import PdfReader
    reader = PdfReader(path)
    pages = [page.extract_text() or "" for page in reader.pages]
    return {}, "\n\n".join(p.strip() for p in pages if p.strip())


def _extract_html(path: str) -> tuple[dict, str]:
    from bs4 import BeautifulSoup
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup.find_all(["h1", "h2", "h3"]):
        level = int(tag.name[1])
        tag.replace_with(f"\n{'#' * level} {tag.get_text()}\n")
    return {}, soup.get_text(separator="\n", strip=True)


def _extract_docx(path: str) -> tuple[dict, str]:
    from docx import Document
    doc = Document(path)
    parts = []
    for para in doc.paragraphs:
        if not para.text.strip():
            continue
        if para.style.name.startswith("Heading"):
            try:
                level = min(int(para.style.name.split()[-1]), 3)
            except (ValueError, IndexError):
                level = 1
            parts.append(f"{'#' * level} {para.text}")
        else:
            parts.append(para.text)
    return {}, "\n\n".join(parts)


def _extract_text(path: str) -> tuple[dict, str]:
    ext = Path(path).suffix.lower()
    if ext == ".md":
        return _parse_file(path)
    if ext == ".pdf":
        return _extract_pdf(path)
    if ext in (".html", ".htm"):
        return _extract_html(path)
    if ext == ".docx":
        return _extract_docx(path)
    return {}, Path(path).read_text(encoding="utf-8", errors="replace")


def _split_by_headings(text: str) -> list[tuple[str, str]]:
    pattern = re.compile(r"^(#{1,3} .+)$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return [("", text.strip())]
    parts = []
    if matches[0].start() > 0:
        preamble = text[: matches[0].start()].strip()
        if preamble:
            parts.append(("", preamble))
    for i, m in enumerate(matches):
        heading = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        parts.append((heading, body))
    return parts


def _split_paragraphs(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    paras = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]
    chunks, current, current_len = [], [], 0
    for para in paras:
        if current_len + len(para) > max_chars and current:
            chunks.append("\n\n".join(current))
            current, current_len = [para], len(para)
        else:
            current.append(para)
            current_len += len(para)
    if current:
        chunks.append("\n\n".join(current))
    return chunks or [text[:max_chars]]


def chunk_file(path: str, collection: str) -> list[Chunk]:
    meta, content = _extract_text(path)
    tags = meta.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    ai_generated = bool(meta.get("ai_generated", False))
    reviewed = bool(meta.get("reviewed", True))

    chunks = []
    prev_tail = ""
    for heading, body in _split_by_headings(content):
        overlap = f"…{prev_tail}\n\n" if prev_tail else ""
        combined_body = f"{overlap}{body}".strip()
        combined = f"{heading}\n\n{combined_body}".strip() if heading else combined_body
        for piece in _split_paragraphs(combined, CHUNK_MAX_CHARS):
            if piece.strip():
                chunks.append(Chunk(
                    text=piece,
                    source_path=path,
                    collection=collection,
                    heading=heading,
                    tags=tags,
                    ai_generated=ai_generated,
                    reviewed=reviewed,
                ))
        prev_tail = body[-OVERLAP_CHARS:].strip() if body else ""
    return chunks

"""
Convert uploaded files to markdown and save them to the vault.

Each converter reads from a bytes payload and returns a markdown string.
The markdown always includes frontmatter with upload provenance metadata.
"""

import csv
import io
import re
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
from werkzeug.utils import secure_filename

from services.vault import storage

ALLOWED_EXTENSIONS = {".md", ".txt", ".pdf", ".html", ".htm", ".csv", ".docx"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(name: str) -> str:
    stem = Path(name).stem
    slug = re.sub(r"[^\w\-]", "-", stem.lower()).strip("-")
    return slug or "upload"


def _wrap(content: str, meta: dict) -> str:
    post = frontmatter.Post(content, **meta)
    return frontmatter.dumps(post)


def _base_meta(original_filename: str, filetype: str) -> dict:
    return {
        "type": "upload",
        "title": Path(original_filename).stem,
        "source": original_filename,
        "filetype": filetype,
        "ai_generated": False,
        "reviewed": True,
        "uploaded_at": _now(),
    }


# ── per-format converters ──────────────────────────────────────────────────────

def _convert_txt(data: bytes, filename: str) -> str:
    content = data.decode("utf-8", errors="replace")
    return _wrap(content, _base_meta(filename, "txt"))


def _convert_md(data: bytes, filename: str) -> str:
    text = data.decode("utf-8", errors="replace")
    # Preserve existing frontmatter; add upload_at if missing
    try:
        post = frontmatter.loads(text)
        if "uploaded_at" not in post.metadata:
            post["uploaded_at"] = _now()
        return frontmatter.dumps(post)
    except Exception:
        return text


def _convert_csv(data: bytes, filename: str) -> str:
    text = data.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return _wrap("(empty CSV)", _base_meta(filename, "csv"))
    headers = rows[0]
    sep = "| " + " | ".join(["---"] * len(headers)) + " |"
    lines = [
        "| " + " | ".join(headers) + " |",
        sep,
    ]
    for row in rows[1:]:
        # Pad or trim to match header count
        padded = list(row) + [""] * (len(headers) - len(row))
        lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in padded[: len(headers)]) + " |")
    return _wrap("\n".join(lines), _base_meta(filename, "csv"))


def _convert_html(data: bytes, filename: str) -> str:
    from bs4 import BeautifulSoup

    html = data.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    title_tag = soup.find("title")
    title = title_tag.get_text().strip() if title_tag else Path(filename).stem
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    raw = soup.get_text(separator="\n")
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()
    meta = _base_meta(filename, "html")
    meta["title"] = title
    return _wrap(raw, meta)


def _convert_pdf(data: bytes, filename: str) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    sections = []
    for i, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if text:
            sections.append(f"## Page {i}\n\n{text}")
    content = "\n\n".join(sections) if sections else "(No extractable text in this PDF)"
    return _wrap(content, _base_meta(filename, "pdf"))


def _convert_docx(data: bytes, filename: str) -> str:
    import docx

    doc = docx.Document(io.BytesIO(data))
    paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    content = "\n\n".join(paras) or "(Empty document)"
    return _wrap(content, _base_meta(filename, "docx"))


_CONVERTERS = {
    ".md":   _convert_md,
    ".txt":  _convert_txt,
    ".csv":  _convert_csv,
    ".html": _convert_html,
    ".htm":  _convert_html,
    ".pdf":  _convert_pdf,
    ".docx": _convert_docx,
}


# ── public API ─────────────────────────────────────────────────────────────────

def save_upload(file_storage, target_folder: str) -> str:
    """
    Convert an uploaded FileStorage object to markdown and save it in the vault
    Storage bucket under target_folder/. Returns the storage key of the saved file.

    Raises ValueError for unsupported file types.
    """
    original = secure_filename(file_storage.filename or "upload")
    ext = Path(original).suffix.lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type '{ext}'. "
            f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    data = file_storage.read()
    converter = _CONVERTERS[ext]
    md_content = converter(data, original)

    slug = _slugify(original)
    key = f"{target_folder}/{slug}.md"
    if storage.exists(key):
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        key = f"{target_folder}/{slug}-{ts}.md"

    storage.upload(key, md_content.encode("utf-8"), content_type="text/markdown")

    return key

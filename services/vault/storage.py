import os

from supabase import create_client, Client
from storage3.exceptions import StorageApiError

_BUCKET = "vault"
_client: Client | None = None


def _get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
    return _client


def _bucket():
    return _get_client().storage.from_(_BUCKET)


def upload(key: str, content: bytes, content_type: str = "text/markdown") -> None:
    _bucket().upload(key, content, file_options={"content-type": content_type, "upsert": "true"})


def download(key: str) -> bytes:
    try:
        return _bucket().download(key)
    except StorageApiError as e:
        raise FileNotFoundError(key) from e


def exists(key: str) -> bool:
    return _bucket().exists(key)


def list_keys(prefix: str = "") -> list[str]:
    """One-level listing of file keys under `prefix` (a top-level vault folder name,
    no leading/trailing slash needed). Excludes folder pseudo-entries and the
    zero-byte `.keep` placeholders vault_new_folder() writes for empty folders —
    Supabase Storage has no real "empty folder" concept, only implicit prefixes."""
    prefix = prefix.rstrip("/")
    entries = _bucket().list(prefix) if prefix else _bucket().list()
    keys = []
    for e in entries:
        if e.get("id") is None:
            continue  # folder pseudo-entry, not a real file
        name = e["name"]
        if name == ".keep":
            continue
        keys.append(f"{prefix}/{name}" if prefix else name)
    return keys


def list_top_level_folders() -> list[str]:
    entries = _bucket().list()
    return [e["name"] for e in entries if e.get("id") is None]


def list_files(prefix: str) -> list[dict]:
    """One-level listing of real files (not folder pseudo-entries or `.keep`
    placeholders) under `prefix`, as {name, updated_at, size} dicts — the subset
    of Storage's list() metadata the vault browser needs for display."""
    prefix = prefix.rstrip("/")
    entries = _bucket().list(prefix)
    return [
        {
            "name": e["name"],
            "updated_at": e["updated_at"],
            "size": (e.get("metadata") or {}).get("size", 0),
        }
        for e in entries
        if e.get("id") is not None and e["name"] != ".keep"
    ]


def delete(key: str) -> None:
    _bucket().remove([key])


def delete_prefix(prefix: str) -> None:
    """Replaces shutil.rmtree() for a vault folder — lists everything one level
    under the prefix and bulk-removes it, including the `.keep` placeholder if
    the folder is otherwise empty."""
    prefix = prefix.rstrip("/")
    entries = _bucket().list(prefix)
    keys = [f"{prefix}/{e['name']}" for e in entries if e.get("id") is not None]
    if keys:
        _bucket().remove(keys)


def move(src_key: str, dest_key: str) -> None:
    _bucket().move(src_key, dest_key)

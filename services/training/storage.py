import os

from supabase import create_client, Client
from storage3.exceptions import StorageApiError

_BUCKET = "training-journal"
_client: Client | None = None


def _get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
    return _client


def _bucket():
    return _get_client().storage.from_(_BUCKET)


def upload(key: str, content: bytes, content_type: str) -> None:
    _bucket().upload(key, content, file_options={"content-type": content_type, "upsert": "true"})


def download(key: str) -> bytes:
    try:
        return _bucket().download(key)
    except StorageApiError as e:
        raise FileNotFoundError(key) from e


def delete(key: str) -> None:
    _bucket().remove([key])


def signed_url(key: str, expires_in: int = 3600) -> str:
    """Attachments live in a private bucket, so the browser can't hit storage_key
    directly — this hands templates a short-lived signed URL to render an <img>/
    link with, same private-bucket-plus-signed-link shape Supabase recommends
    instead of making the bucket public."""
    return _bucket().create_signed_url(key, expires_in)["signedURL"]

import logging
import os

from .indexer import VAULT_ROOT, delete_file, index_file

logger = logging.getLogger(__name__)

_observer = None


def start(vault_path: str = VAULT_ROOT) -> None:
    global _observer
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        os.makedirs(vault_path, exist_ok=True)

        from .chunker import SUPPORTED_EXTENSIONS

        def _supported(path: str) -> bool:
            from pathlib import Path as _Path
            return _Path(path).suffix.lower() in SUPPORTED_EXTENSIONS

        class _Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory and _supported(event.src_path):
                    index_file(event.src_path)

            def on_modified(self, event):
                if not event.is_directory and _supported(event.src_path):
                    index_file(event.src_path)

            def on_deleted(self, event):
                if not event.is_directory and _supported(event.src_path):
                    delete_file(event.src_path)

            def on_moved(self, event):
                if not event.is_directory:
                    if _supported(event.src_path):
                        delete_file(event.src_path)
                    if _supported(event.dest_path):
                        index_file(event.dest_path)

        _observer = Observer()
        _observer.daemon = True
        _observer.schedule(_Handler(), vault_path, recursive=True)
        _observer.start()
        logger.info("Vault watcher started at %s", vault_path)
    except Exception:
        logger.exception("Vault watcher failed to start — file-based indexing disabled")


def stop() -> None:
    global _observer
    if _observer and _observer.is_alive():
        try:
            _observer.stop()
            _observer.join(timeout=5)
        except Exception:
            pass

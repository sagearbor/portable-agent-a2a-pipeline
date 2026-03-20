"""
Persistent store for recurring-meeting Jira defaults.

Implements the "Recurring Meeting Memory" feature described in PRP §4.8.

BACKEND TOGGLE
--------------
Set SERIES_STORE_BACKEND in .env:

  SERIES_STORE_BACKEND=file    # default — stores bot/data/series_defaults.json
  SERIES_STORE_BACKEND=blob    # Azure Blob Storage (production)

Blob mode requires:
  AZURE_STORAGE_ACCOUNT_URL=https://<account>.blob.core.windows.net
  BLOB_CONTAINER_NAME=sagejirabot          # optional, defaults to "sagejirabot"
  BLOB_SERIES_DEFAULTS_NAME=series-defaults.json  # optional

Auth uses DefaultAzureCredential (managed identity in production, az login locally).
No storage key or connection string needed.

Usage (same regardless of backend):
    from bot.data.series_defaults_store import series_defaults_store

    defaults = series_defaults_store.get(series_master_id)
    if defaults:
        jira_base_url = defaults["jira_base_url"]
        project_key   = defaults["project_key"]

    series_defaults_store.set(series_master_id, {
        "jira_base_url": "https://acme.atlassian.net",
        "project_key":   "ST",
    })

    series_defaults_store.delete(series_master_id)
"""

import json
import os
import pathlib
import threading
from datetime import datetime, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

EXPIRY_DAYS = 90
_BACKEND = os.getenv("SERIES_STORE_BACKEND", "file").lower()

# File backend config
_DATA_DIR   = pathlib.Path(__file__).parent
_STORE_FILE = _DATA_DIR / "series_defaults.json"

# Blob backend config (only used when SERIES_STORE_BACKEND=blob)
_STORAGE_ACCOUNT_URL = os.getenv("AZURE_STORAGE_ACCOUNT_URL", "")
_CONTAINER_NAME      = os.getenv("BLOB_CONTAINER_NAME", "sagejirabot")
_BLOB_NAME           = os.getenv("BLOB_SERIES_DEFAULTS_NAME", "series-defaults.json")


# ---------------------------------------------------------------------------
# SeriesDefaultsStore
# ---------------------------------------------------------------------------

class SeriesDefaultsStore:
    """
    Stores recurring-meeting Jira defaults keyed by Teams seriesMasterId.

    Backend is controlled by SERIES_STORE_BACKEND env var:
    - "file": JSON file on local disk (default, good for dev and VM)
    - "blob": Azure Blob Storage (production / Container Apps)

    Public API is identical regardless of backend. Swap by changing .env only.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # Validate blob config at startup if blob backend selected
        if _BACKEND == "blob" and not _STORAGE_ACCOUNT_URL:
            raise EnvironmentError(
                "SERIES_STORE_BACKEND=blob requires AZURE_STORAGE_ACCOUNT_URL to be set"
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, series_master_id: str) -> Optional[dict]:
        """Return saved Jira defaults for this series, or None if not found / expired."""
        with self._lock:
            store = self._load()
        entry = store.get(series_master_id)
        if entry is None:
            return None
        # Check expiry
        saved_at_str = entry.get("saved_at")
        if saved_at_str:
            try:
                saved_at = datetime.fromisoformat(saved_at_str)
                if datetime.utcnow() - saved_at > timedelta(days=EXPIRY_DAYS):
                    with self._lock:
                        store = self._load()
                        store.pop(series_master_id, None)
                        self._save(store)
                    return None
            except ValueError:
                pass
        return {
            "jira_base_url": entry.get("jira_base_url", ""),
            "project_key":   entry.get("project_key",   ""),
        }

    def set(self, series_master_id: str, defaults: dict) -> None:
        """Save Jira defaults for this series."""
        with self._lock:
            store = self._load()
            store[series_master_id] = {
                "jira_base_url": defaults.get("jira_base_url", ""),
                "project_key":   defaults.get("project_key", ""),
                "saved_at":      datetime.utcnow().isoformat(),
            }
            self._save(store)

    def delete(self, series_master_id: str) -> bool:
        """Remove saved defaults for this series. Returns True if deleted."""
        with self._lock:
            store = self._load()
            if series_master_id not in store:
                return False
            del store[series_master_id]
            self._save(store)
        return True

    @property
    def backend(self) -> str:
        return _BACKEND

    # ------------------------------------------------------------------
    # Private — File backend
    # ------------------------------------------------------------------

    def _load_file(self) -> dict:
        if not _STORE_FILE.exists():
            return {}
        try:
            return json.loads(_STORE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_file(self, store: dict) -> None:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _STORE_FILE.write_text(
            json.dumps(store, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Private — Blob backend
    # ------------------------------------------------------------------

    def _load_blob(self) -> dict:
        from azure.storage.blob import BlobServiceClient
        from azure.identity import DefaultAzureCredential
        try:
            client = BlobServiceClient(
                account_url=_STORAGE_ACCOUNT_URL,
                credential=DefaultAzureCredential(),
            )
            blob = client.get_blob_client(container=_CONTAINER_NAME, blob=_BLOB_NAME)
            data = blob.download_blob().readall()
            return json.loads(data)
        except Exception:
            # Blob missing (first run) or unreadable — start fresh
            return {}

    def _save_blob(self, store: dict) -> None:
        from azure.storage.blob import BlobServiceClient, ContentSettings
        from azure.identity import DefaultAzureCredential
        client = BlobServiceClient(
            account_url=_STORAGE_ACCOUNT_URL,
            credential=DefaultAzureCredential(),
        )
        # Ensure container exists (no-op if already exists)
        container = client.get_container_client(_CONTAINER_NAME)
        try:
            container.create_container()
        except Exception:
            pass  # Already exists
        blob = client.get_blob_client(container=_CONTAINER_NAME, blob=_BLOB_NAME)
        blob.upload_blob(
            json.dumps(store, indent=2, ensure_ascii=False).encode("utf-8"),
            overwrite=True,
            content_settings=ContentSettings(content_type="application/json"),
        )

    # ------------------------------------------------------------------
    # Backend router
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if _BACKEND == "blob":
            return self._load_blob()
        return self._load_file()

    def _save(self, store: dict) -> None:
        if _BACKEND == "blob":
            self._save_blob(store)
        else:
            self._save_file(store)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

series_defaults_store = SeriesDefaultsStore()

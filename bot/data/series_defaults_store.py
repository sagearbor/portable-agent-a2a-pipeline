"""
Persistent store for recurring-meeting Jira defaults.

Implements the "Recurring Meeting Memory" feature described in PRP §4.8.

When a user provides a Jira URL in a Teams meeting that belongs to a recurring
series (detected via channel_data.meeting.seriesMasterId), the bot can save
the Jira settings so future sessions of the same series auto-apply them.

Storage: a local JSON file (bot/data/series_defaults.json) keyed by
seriesMasterId. Entries expire after EXPIRY_DAYS days of inactivity.

In Phase 2 (Azure deployment) replace the JSON backend with Azure Table Storage
while keeping the same get() / set() / delete() interface.

Usage:
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
import pathlib
from datetime import datetime, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DATA_DIR = pathlib.Path(__file__).parent          # bot/data/
_STORE_FILE = _DATA_DIR / "series_defaults.json"

EXPIRY_DAYS = 90  # Entries older than this are pruned on next write


# ---------------------------------------------------------------------------
# SeriesDefaultsStore
# ---------------------------------------------------------------------------

class SeriesDefaultsStore:
    """
    JSON-file-backed store for recurring-meeting Jira defaults.

    Each entry is a dict with:
        jira_base_url: str
        project_key:   str
        saved_at:      ISO-8601 timestamp (set automatically by set())

    The store is loaded from disk lazily on first access and written back
    on every mutating operation.  No caching across calls — simple and safe
    for single-process deployments.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, series_master_id: str) -> Optional[dict]:
        """
        Return saved Jira defaults for this series, or None if not found
        or if the entry has expired.

        Args:
            series_master_id: Teams Graph API seriesMasterId string

        Returns:
            dict with keys jira_base_url and project_key, or None
        """
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
                    # Expired — delete and return None
                    del store[series_master_id]
                    self._save(store)
                    return None
            except ValueError:
                pass  # Bad timestamp — treat as non-expired, log nothing

        return {
            "jira_base_url": entry.get("jira_base_url", ""),
            "project_key":   entry.get("project_key",   ""),
        }

    def set(self, series_master_id: str, defaults: dict) -> None:
        """
        Save Jira defaults for this series.

        Args:
            series_master_id: Teams Graph API seriesMasterId string
            defaults: dict with keys jira_base_url (str) and project_key (str)
        """
        store = self._load()
        store[series_master_id] = {
            "jira_base_url": defaults.get("jira_base_url", ""),
            "project_key":   defaults.get("project_key", ""),
            "saved_at":      datetime.utcnow().isoformat(),
        }
        self._save(store)

    def delete(self, series_master_id: str) -> bool:
        """
        Remove saved defaults for this series.

        Args:
            series_master_id: Teams Graph API seriesMasterId string

        Returns:
            True if an entry was deleted, False if no entry existed.
        """
        store = self._load()
        if series_master_id not in store:
            return False
        del store[series_master_id]
        self._save(store)
        return True

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        """Load the JSON store from disk. Returns empty dict if file missing."""
        if not _STORE_FILE.exists():
            return {}
        try:
            return json.loads(_STORE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable file — start fresh
            return {}

    def _save(self, store: dict) -> None:
        """Write the store dict to disk as JSON."""
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _STORE_FILE.write_text(
            json.dumps(store, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Module-level singleton — import and use directly:
#   from bot.data.series_defaults_store import series_defaults_store
# ---------------------------------------------------------------------------

series_defaults_store = SeriesDefaultsStore()

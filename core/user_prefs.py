"""
Per-user preferences (Jira project, base URL) keyed by Atlassian account id.

Stored in a local JSON file so preferences survive container restarts.
The file lives at USER_PREFS_PATH (default: data/user_prefs.json) and
is mounted as a volume in docker-compose.yml.

TODO (production / Azure Container Apps): ACA has ephemeral local storage,
so this file-backed store must be replaced with Azure Table Storage or
Cosmos DB keyed by account_id. The get/set functions here are the only
surface area that needs swapping — keep their signatures stable.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "user_prefs.json"
_PATH = Path(os.environ.get("USER_PREFS_PATH", _DEFAULT_PATH))
_LOCK = threading.Lock()


def _load_all() -> dict[str, dict[str, Any]]:
    if not _PATH.exists():
        return {}
    try:
        return json.loads(_PATH.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_all(data: dict[str, dict[str, Any]]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(_PATH)


def get_prefs(account_id: str) -> dict[str, Any]:
    """Return stored prefs for this user, or an empty dict if none."""
    if not account_id:
        return {}
    with _LOCK:
        return dict(_load_all().get(account_id, {}))


def set_prefs(account_id: str, prefs: dict[str, Any]) -> dict[str, Any]:
    """
    Merge ``prefs`` into the stored prefs for this user and persist.
    Returns the merged dict. Silently no-ops if account_id is empty.
    """
    if not account_id:
        return {}
    with _LOCK:
        all_prefs = _load_all()
        current = all_prefs.get(account_id, {})
        current.update({k: v for k, v in prefs.items() if v is not None})
        all_prefs[account_id] = current
        _write_all(all_prefs)
        return dict(current)

"""
Per-user preferences API — stores the last Jira project/base a signed-in
user interacted with so we can default to it on next page load instead
of hard-coding "ST".
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from bot.api.routes.auth_jira import get_oauth_session
from core.user_prefs import get_prefs, set_prefs

router = APIRouter()


class UserPrefs(BaseModel):
    last_project_key: str | None = None
    last_base_url:    str | None = None


@router.get("/user/prefs")
async def read_user_prefs(request: Request) -> dict:
    """Return current user's stored prefs. Empty dict when not signed in."""
    sess = get_oauth_session(request)
    if not sess or not sess.get("account_id"):
        return {}
    return get_prefs(sess["account_id"])


@router.post("/user/prefs")
async def write_user_prefs(request: Request, body: UserPrefs) -> dict:
    """Merge-update current user's prefs. 401 when not signed in."""
    sess = get_oauth_session(request)
    if not sess or not sess.get("account_id"):
        raise HTTPException(status_code=401, detail="Sign in required")
    return set_prefs(sess["account_id"], body.model_dump(exclude_none=True))

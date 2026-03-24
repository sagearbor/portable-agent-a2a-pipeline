"""
POST /api/v1/jira/check-duplicates

Checks Jira for potential duplicate issues matching a list of ticket summaries.
Used by the web UI to warn users before creating tickets that may already exist.

For each summary, extracts keywords and searches via JQL. Returns up to 3
matching issues per summary.

SSRF mitigation: same base_url validation as jira_projects.py — only
https://*.atlassian.net addresses are accepted.
"""

import re
import requests

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from bot.api.routes._jira_helpers import validate_base_url, get_jira_auth

router = APIRouter()

# Backward compatibility
_validate_base_url = validate_base_url


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

# Common words to strip when building search keywords
_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "is", "are", "was", "were",
    "be", "been", "being", "in", "on", "at", "to", "for", "of", "with",
    "by", "from", "as", "into", "through", "during", "before", "after",
    "it", "its", "this", "that", "these", "those", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "can", "shall",
    "not", "no", "so", "if", "then", "than", "too", "very", "just",
    "about", "up", "out", "all", "each", "every", "both", "few", "more",
    "most", "other", "some", "such", "only", "own", "same", "also",
    "how", "what", "which", "who", "whom", "when", "where", "why",
    "new", "need", "create", "add", "update", "implement", "set",
})


def _extract_keywords(summary: str, max_keywords: int = 3) -> list[str]:
    """
    Extract 2-3 meaningful keywords from a ticket summary.

    Strips common/stop words, keeps the most likely distinguishing terms
    for JQL text search.
    """
    # Remove punctuation and split
    words = re.findall(r"[a-zA-Z0-9]+", summary)
    # Filter stop words and very short tokens
    meaningful = [
        w for w in words
        if w.lower() not in _STOP_WORDS and len(w) > 2
    ]
    return meaningful[:max_keywords]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class DuplicateMatch(BaseModel):
    key: str
    summary: str
    url: str


class DuplicateResult(BaseModel):
    summary: str
    duplicates: list[DuplicateMatch]


class CheckDuplicatesRequest(BaseModel):
    summaries: list[str]
    project_key: str
    base_url: str


class CheckDuplicatesResponse(BaseModel):
    results: list[DuplicateResult]


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------

@router.post("/jira/check-duplicates", response_model=CheckDuplicatesResponse)
async def check_duplicates(
    req: CheckDuplicatesRequest,
) -> CheckDuplicatesResponse:
    """
    Check Jira for potential duplicate issues matching the given summaries.

    For each summary:
    1. Extracts 2-3 keywords (strips common words)
    2. Builds JQL: project = "KEY" AND summary ~ "kw1 kw2" AND statusCategory != Done
    3. Calls Jira REST API (POST /rest/api/3/search/jql)
    4. Returns up to 3 matching issues per summary
    """
    # Validate base_url (SSRF guard)
    base = validate_base_url(req.base_url)

    # Resolve credentials via shared helper
    auth = get_jira_auth()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    jira_search_url = f"{base}/rest/api/3/search/jql"

    results: list[DuplicateResult] = []

    for summary in req.summaries:
        keywords = _extract_keywords(summary)

        if not keywords:
            # No meaningful keywords — skip search, return empty
            results.append(DuplicateResult(summary=summary, duplicates=[]))
            continue

        # Build JQL text search
        keyword_text = " ".join(keywords)
        jql = (
            f'project = "{req.project_key}" '
            f'AND summary ~ "{keyword_text}" '
            f'AND statusCategory != Done'
        )

        payload = {
            "jql": jql,
            "fields": ["key", "summary"],
            "maxResults": 3,
        }

        try:
            resp = requests.post(
                jira_search_url,
                json=payload,
                auth=auth,
                headers=headers,
                timeout=15,
            )

            if resp.status_code in (401, 403):
                raise HTTPException(
                    status_code=401,
                    detail="Invalid Jira credentials — check your email and API token.",
                )

            if not resp.ok:
                # Non-fatal per-summary — log and return empty for this one
                # (JQL syntax errors from unusual summaries should not crash
                # the entire batch)
                results.append(DuplicateResult(summary=summary, duplicates=[]))
                continue

            data = resp.json()
            duplicates = []
            for issue in data.get("issues", []):
                duplicates.append(DuplicateMatch(
                    key=issue["key"],
                    summary=issue["fields"].get("summary", ""),
                    url=f"{base}/browse/{issue['key']}",
                ))

            results.append(DuplicateResult(
                summary=summary,
                duplicates=duplicates,
            ))

        except HTTPException:
            raise
        except requests.exceptions.ConnectionError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Could not reach Jira at {base}: {exc}",
            )
        except requests.exceptions.Timeout:
            raise HTTPException(
                status_code=504,
                detail=f"Timed out connecting to Jira at {base}",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Unexpected error checking duplicates: {exc}",
            )

    return CheckDuplicatesResponse(results=results)

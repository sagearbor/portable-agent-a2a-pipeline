"""Backward-compat shim — imports from core/jira_context.py"""
from core.jira_context import *  # noqa: F401,F403
from core.jira_context import (  # noqa: F401
    query_epics,
    query_recent_stories,
    query_sprints,
    query_fix_versions,
    enrich_draft_tickets,
    match_tickets_to_epics,
)

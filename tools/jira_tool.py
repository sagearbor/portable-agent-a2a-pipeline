"""Backward-compat shim — imports from core/tools/jira_tool.py"""
from core.tools.jira_tool import *  # noqa: F401,F403
from core.tools.jira_tool import (  # noqa: F401
    create_ticket,
    create_issue_link,
    check_project_permission,
    JiraCredentials,
    _client,
    _PRIORITY_MAP,
)

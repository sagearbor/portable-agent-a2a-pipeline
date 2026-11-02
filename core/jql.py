"""
JQL quoting helpers.

Every value that reaches a JQL string from a request (project keys, free
text from ticket summaries or transcripts) must go through one of these.
Plain f-string interpolation such as ``f'project = "{key}"'`` lets a caller
close the quote and append their own clauses, e.g.
``ST" OR project = "OTHER`` -> reads another project with whatever
credential runs the query (the shared bot token, for some routes).

* ``jql_quote(value)``      -> a safe double-quoted JQL string literal.
* ``jql_text_quote(value)`` -> the same, for the right-hand side of ``~``
  (text search).  Lucene operator characters are replaced with spaces so
  free text cannot change the meaning of the search.
* ``validate_project_key(key)`` -> the key upper-cased, or ValueError.
"""

import re

# Jira project keys: a letter, then letters/digits/underscore (Jira's own rule).
_PROJECT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,49}$")

# Issue keys: PROJECT-123
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,49}-[1-9][0-9]{0,9}$")

# Lucene special characters that are meaningful inside a JQL text search.
_LUCENE_SPECIAL = re.compile(r'[+\-&|!(){}\[\]^~*?:\\/"]')

# Control characters (incl. newlines) have no business inside a JQL literal.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def validate_project_key(key: str) -> str:
    """Return *key* upper-cased, or raise ``ValueError`` if it is not a Jira project key."""
    k = (key or "").strip()
    if not _PROJECT_KEY_RE.match(k):
        raise ValueError(f"Invalid Jira project key: {key!r}")
    return k.upper()


def is_issue_key(key: str) -> bool:
    return bool(ISSUE_KEY_RE.match((key or "").strip()))


def jql_quote(value) -> str:
    """Return *value* as a double-quoted JQL string literal (backslash and quote escaped)."""
    s = _CONTROL.sub(" ", str(value))
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def jql_text_quote(value) -> str:
    """Quote free text for a ``~`` (contains) clause: Lucene operators removed, then quoted."""
    s = _LUCENE_SPECIAL.sub(" ", _CONTROL.sub(" ", str(value)))
    s = re.sub(r"\s+", " ", s).strip()
    return jql_quote(s)

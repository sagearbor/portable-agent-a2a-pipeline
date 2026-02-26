"""
Outlook tool - stub version for Phase 1.

In Phase 1 this returns fake emails so we can build and test
the full A2A pipeline without needing real Microsoft Graph credentials.

In Phase 2 this will be replaced with real Microsoft Graph API calls.
The function signature stays the same so agent code does not change.
"""


def read_emails(folder: str = "Inbox", max_results: int = 5) -> list[dict]:
    """
    Read emails from a folder.

    Phase 1: returns stub data.
    Phase 2: will call Microsoft Graph API with managed identity auth.

    Returns a list of email dicts with keys:
        id, subject, sender, body, received_at
    """

    # ------------------------------------------------------------------
    # STUB - replace this block in Phase 2 with real Graph API call
    # ------------------------------------------------------------------
    stub_emails = [
        {
            "id": "email-001",
            "subject": "Production bug: login fails for SSO users",
            "sender": "alice@duke.edu",
            "body": (
                "Hi team, we have reports that SSO users cannot log in since this morning. "
                "Affects roughly 30 users in the cardiology department. "
                "Error in logs: SAML assertion expired. Priority: high."
            ),
            "received_at": "2026-02-26T09:15:00Z",
        },
        {
            "id": "email-002",
            "subject": "Lunch order for Friday",
            "sender": "bob@duke.edu",
            "body": "Hey, who wants pizza on Friday? Reply with your order by Thursday noon.",
            "received_at": "2026-02-26T10:02:00Z",
        },
        {
            "id": "email-003",
            "subject": "Data pipeline missing records for Feb 24",
            "sender": "carol@duke.edu",
            "body": (
                "The nightly ETL job on Feb 24 completed with 0 records loaded. "
                "Source system was up. No error email was sent. "
                "We need this investigated before end of week reporting."
            ),
            "received_at": "2026-02-26T11:30:00Z",
        },
    ]
    # ------------------------------------------------------------------

    print(f"[outlook_tool] Reading folder='{folder}', returning {len(stub_emails)} emails (stub)")
    return stub_emails[:max_results]

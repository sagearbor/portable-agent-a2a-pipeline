"""
Microsoft Graph API client for fetching Teams meeting transcripts.

STUB: Real implementation requires IT to provision:
  - Azure AD app registration with the bot's managed identity OR a service principal
  - Microsoft Graph application permissions (admin consent required):
      OnlineMeetings.Read.All            - read meeting metadata + transcript IDs
      OnlineMeetingTranscript.Read.All   - read transcript content (VTT format)
  - See docs/it-request-teams-bot.md for the complete IT request

The function signatures here are the STABLE CONTRACT.
Phase 2 replaces the stub bodies with real Graph API calls without
changing any caller code.

Graph API reference:
  GET /v1.0/users/{userId}/onlineMeetings/{meetingId}/transcripts
  GET /v1.0/users/{userId}/onlineMeetings/{meetingId}/transcripts/{transcriptId}/content

Authentication:
  - Locally: az login session via AzureCliCredential
             scope: https://graph.microsoft.com/.default
  - In Azure: system-assigned managed identity on the bot container
              (no secrets required once IT grants the permissions)
"""

import os
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Stub transcript for local testing when Graph permissions aren't available
# ---------------------------------------------------------------------------
_SAMPLE_STUB_TRANSCRIPT = """\
Alice Chen: Good morning everyone. Let's get started with sprint planning.
Bob Martinez: Before we begin, I wanted to flag that the login timeout issue
  is affecting all users in production. It needs to be fixed before we ship next week.
Alice Chen: Agreed, that should be Critical priority. Who can take it?
Bob Martinez: I can handle it. I'll need about 3 days.
Carol Kim: I also noticed the data export button is broken on Firefox.
  It's been like this for two weeks and users keep reporting it.
Alice Chen: That's a High priority bug, Carol can you file that?
Carol Kim: Sure, I'll do it right after this meeting.
Bob Martinez: We should also update the on-call runbook. The current one
  doesn't cover the new authentication flow we deployed last month.
Alice Chen: Good point. Let's make that a Medium priority task.
  Anything else before we look at the backlog?
Carol Kim: That's all from me.
Alice Chen: Great. Let's look at the backlog items for this sprint.
"""


def get_meeting_transcript(meeting_id: str, organizer_id: str) -> str:
    """
    Fetch the transcript for a Teams meeting.
    Returns plain text transcript with speaker labels.

    Args:
        meeting_id:    The Teams online meeting ID
                       (from onlineMeetings API or meeting URL)
        organizer_id:  The Azure AD object ID (userId) of the meeting organizer.
                       Graph API requires the organizer's user ID to scope
                       the transcript request.

    Returns:
        Plain text transcript string with speaker labels, e.g.:
        "Alice: We need to fix the login issue.\nBob: I'll handle it."

    Raises:
        NotImplementedError: Always, until IT provisions Graph permissions.
        RuntimeError:        If Graph API call fails (real implementation).

    STUB: raises NotImplementedError with instructions for IT.
    Real implementation (Phase 2):
        1. Get token: AzureCliCredential().get_token("https://graph.microsoft.com/.default")
        2. GET /v1.0/users/{organizer_id}/onlineMeetings/{meeting_id}/transcripts
           -> response.json()["value"][0]["id"]  # get transcript ID
        3. GET /v1.0/users/{organizer_id}/onlineMeetings/{meeting_id}/transcripts/{transcript_id}/content
           Header: Accept: text/vtt
           -> VTT formatted transcript text
        4. Pass to bot.adapters.transcript_adapter.parse_vtt() to clean the text
    """
    raise NotImplementedError(
        "Real transcript fetching requires IT to provision Graph API permissions.\n"
        "See docs/it-request-teams-bot.md for the exact IT request.\n"
        "Use the `paste` command or POST /api/v1/process-transcript directly "
        "until permissions are granted.\n\n"
        f"Required permissions:\n"
        f"  - OnlineMeetings.Read.All\n"
        f"  - OnlineMeetingTranscript.Read.All\n"
        f"Meeting ID received: {meeting_id}\n"
        f"Organizer ID received: {organizer_id}"
    )


def get_meeting_transcript_stub(meeting_title: str = "Sample Meeting") -> str:
    """
    Return a sample transcript for local testing without Graph permissions.

    Use this to test the full pipeline end-to-end while waiting for IT:
        from bot.graph.transcript_fetcher import get_meeting_transcript_stub
        transcript = get_meeting_transcript_stub("Sprint Planning")
        # then POST to /api/v1/process-transcript with dry_run=True
    """
    return f"# {meeting_title}\n{_SAMPLE_STUB_TRANSCRIPT}"


def list_meeting_transcripts(organizer_id: str, meeting_id: str) -> list[dict]:
    """
    List available transcripts for a meeting.

    Returns list of dicts with keys:
        id:            transcript ID (use with get_meeting_transcript)
        createdDateTime: ISO8601 timestamp of when transcript was created
        meetingId:     the meeting ID

    STUB: raises NotImplementedError.
    Real implementation: GET /v1.0/users/{organizer_id}/onlineMeetings/{meeting_id}/transcripts
    """
    raise NotImplementedError(
        "Requires IT to provision OnlineMeetings.Read.All Graph permission. "
        "See docs/it-request-teams-bot.md."
    )

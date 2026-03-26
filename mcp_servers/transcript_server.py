"""
MCP server wrapping transcript tools for DCRI.

Thin wrapper around core/ functions — no business logic here.
Exposes tools for transcript parsing, format detection, and
mock transcript generation.

Run standalone:
    python mcp_servers/transcript_server.py

Configure in Claude Code (.claude.json or settings):
    See .claude/mcp-config-example.json
"""

from fastmcp import FastMCP

mcp = FastMCP(
    "dcri-transcript-tools",
    instructions="Meeting transcript parsing and mock generation for DCRI",
)


# ---------------------------------------------------------------------------
# Tool: parse_transcript
# ---------------------------------------------------------------------------

@mcp.tool()
def parse_transcript(
    text: str,
    meeting_title: str = "Meeting",
) -> list[dict]:
    """Parse a meeting transcript (WebVTT, Webex, or plain text) into structured segments.

    Auto-detects the transcript format and splits it into chunks suitable
    for pipeline processing. Each chunk becomes an email-shaped dict.

    Args:
        text: Raw transcript text in any supported format.
        meeting_title: Human-readable meeting name (used in segment subjects).

    Returns:
        List of email-shaped dicts with keys: id, sender, subject, body.
    """
    from core.adapters.transcript_adapter import transcript_to_pipeline_input

    return transcript_to_pipeline_input(text, meeting_title)


# ---------------------------------------------------------------------------
# Tool: detect_transcript_format
# ---------------------------------------------------------------------------

@mcp.tool()
def detect_transcript_format(text: str) -> str:
    """Detect the format of a transcript string.

    Args:
        text: Raw transcript text to analyze.

    Returns:
        One of: "vtt" (WebVTT/Teams/Zoom), "webex" (Webex export), or "plain".
    """
    from core.adapters.transcript_adapter import detect_format

    return detect_format(text)


# ---------------------------------------------------------------------------
# Tool: generate_mock_transcript
# ---------------------------------------------------------------------------

@mcp.tool()
def generate_mock_transcript(
    idea: str,
    detail_level: str = "medium",
) -> dict:
    """Generate a realistic meeting transcript from an idea description.

    Uses the LLM to create a synthetic meeting transcript with fictional
    speakers, timestamps, action items, and natural dialogue.

    Args:
        idea: Description of the meeting topic (e.g. "Sprint planning for auth module").
        detail_level: One of "low", "medium", "high", "hardest".
            Controls transcript length from ~300 to ~6000 words.

    Returns:
        Dict with keys:
          - transcript: the generated transcript text
          - speakers: list of speaker names used
    """
    import json
    import pathlib
    import random

    # Validate detail_level
    allowed = {"low", "medium", "high", "hardest"}
    detail_level = detail_level.lower()
    if detail_level not in allowed:
        raise ValueError(f"detail_level must be one of {sorted(allowed)}, got '{detail_level}'")

    if not idea or not idea.strip():
        raise ValueError("idea must not be empty")

    # Load speakers from bot/data/mock_speakers.json
    # Walk up from mcp_servers/ to find the project root
    project_root = pathlib.Path(__file__).parent.parent
    speakers_path = project_root / "bot" / "data" / "mock_speakers.json"
    if not speakers_path.exists():
        raise RuntimeError(f"mock_speakers.json not found at {speakers_path}")

    with open(speakers_path, "r") as f:
        data = json.load(f)
    all_speakers = data.get("male", []) + data.get("female", [])

    # Pick random speakers (more for longer meetings)
    speaker_range = {"low": (3, 4), "medium": (3, 5), "high": (4, 6), "hardest": (6, 8)}
    lo, hi = speaker_range.get(detail_level, (3, 5))
    num_speakers = random.randint(lo, hi)
    chosen = random.sample(all_speakers, min(num_speakers, len(all_speakers)))
    speaker_names = [s["name"] for s in chosen]

    # Word count targets by detail level
    detail_word_counts = {
        "low":     (300, 500),
        "medium":  (800, 1200),
        "high":    (1500, 2500),
        "hardest": (4000, 6000),
    }
    min_words, max_words = detail_word_counts[detail_level]

    # Token limits per detail level
    detail_token_limits = {
        "low":     4096,
        "medium":  4096,
        "high":    8192,
        "hardest": 16384,
    }

    # Build prompt
    system_prompt = (
        "You are a meeting transcript generator. Generate a realistic meeting "
        "transcript that looks like it came from an automated transcription service.\n"
        "\n"
        "Rules:\n"
        "- Use ONLY these speaker names: " + ", ".join(speaker_names) + "\n"
        "- Format each line as: [HH:MM:SS] Speaker Name: dialogue\n"
        "- Start timestamps at [00:00:00] and increment realistically\n"
        "- Include natural dialogue: interruptions, agreements, questions, tangents\n"
        "- Include 3-5 clear action items organically in the conversation\n"
        "- Include decisions that were made during the meeting\n"
        f"- Target length: {min_words}-{max_words} words\n"
        "- Make it feel like a real meeting, not a script\n"
        "\n"
        "Return ONLY the transcript text, no preamble or explanation."
    )

    user_content = f"Generate a meeting transcript about: {idea.strip()}"

    # Call LLM via core client
    from core.clients.client import get_client, token_limit_kwarg
    from core.config.settings import PROVIDER, TEMPERATURE

    client, model = get_client()

    if PROVIDER in ("openai_responses", "azure_responses"):
        response = client.responses.create(
            model=model,
            instructions=system_prompt,
            input=user_content,
            temperature=TEMPERATURE,
        )
        raw = response.output_text
    else:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_content},
            ],
            temperature=TEMPERATURE,
            **token_limit_kwarg(model, detail_token_limits.get(detail_level, 4096)),
        )
        raw = response.choices[0].message.content

    transcript = (raw or "").strip()

    return {
        "transcript": transcript,
        "speakers": speaker_names,
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()

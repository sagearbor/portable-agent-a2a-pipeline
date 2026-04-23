"""
Transcript adapter — converts various meeting transcript formats into
the email-shaped dict list that the existing 3-agent pipeline expects.

Supported formats:
  - WebVTT (.vtt) — Teams and Zoom exports
  - Webex plain text (.txt) — Webex meeting exports
  - Plain text — any other format (paste from Teams chat, etc.)

Usage:
    from bot.adapters.transcript_adapter import transcript_to_pipeline_input
    items = transcript_to_pipeline_input(raw_text, meeting_title="Sprint Planning")
    # items is a list of email-shaped dicts suitable for agent1_email.run_on_items()
"""

import re
import textwrap
from typing import Optional


def parse_vtt(text: str) -> str:
    """
    Strip WebVTT headers, timestamp lines, and metadata blocks.
    Preserves speaker: dialogue lines.

    Input format (Teams/Zoom .vtt):
        WEBVTT

        00:00:05.000 --> 00:00:10.000
        Alice: We need to fix the login timeout.

        00:00:11.000 --> 00:00:15.000
        Bob: I'll take that task.

    Output:
        Alice: We need to fix the login timeout.
        Bob: I'll take that task.
    """
    lines = text.splitlines()
    result_lines = []
    for line in lines:
        stripped = line.strip()
        # Skip WEBVTT header
        if stripped == "WEBVTT":
            continue
        # Skip blank lines (we'll re-add them smartly)
        if not stripped:
            continue
        # Skip VTT timestamp lines: "00:00:05.000 --> 00:00:10.000"
        if re.match(r'^\d{1,2}:\d{2}[:.]\d{2,3}.*-->', stripped):
            continue
        # Skip NOTE blocks
        if stripped.startswith("NOTE"):
            continue
        # Skip cue identifiers (lines that are just numbers or UUIDs before a timestamp)
        if re.match(r'^[\da-f-]+$', stripped, re.IGNORECASE) and len(stripped) < 50:
            continue
        result_lines.append(stripped)

    return "\n".join(result_lines)


def parse_webex_txt(text: str) -> str:
    """
    Parse Webex meeting transcript export format.

    Webex format:
        0:00  Alice Smith
              We need to fix the login issue.

        0:05  Bob Jones
              Agreed, I will handle it.

    Output:
        Alice Smith: We need to fix the login issue.
        Bob Jones: Agreed, I will handle it.
    """
    lines = text.splitlines()
    result_lines = []
    current_speaker = None
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Webex timestamp + speaker line: "0:05  Bob Jones" or "00:05:12  Alice Smith"
        timestamp_speaker_match = re.match(r'^(\d{1,2}:\d{2}(?::\d{2})?)\s{2,}(.+)$', stripped)
        if timestamp_speaker_match:
            current_speaker = timestamp_speaker_match.group(2).strip()
            i += 1
            continue

        # Continuation of dialogue (indented or following a speaker line)
        if current_speaker and stripped:
            # Check if this line looks like a new timestamp (next speaker)
            if re.match(r'^\d{1,2}:\d{2}', stripped):
                # This is a new timestamp line, handle in next iteration
                i += 1
                continue
            result_lines.append(f"{current_speaker}: {stripped}")
            current_speaker = None  # Reset after first dialogue line
        elif stripped:
            result_lines.append(stripped)

        i += 1

    return "\n".join(result_lines)


def detect_format(text: str) -> str:
    """
    Detect the transcript format.

    Returns:
        "vtt"    — WebVTT format (Teams/Zoom export)
        "webex"  — Webex plain text format
        "plain"  — Plain text (speaker: dialogue or free form)
    """
    stripped = text.strip()

    # VTT starts with "WEBVTT" header
    if stripped.startswith("WEBVTT"):
        return "vtt"

    # Webex format: lines starting with "0:00  Name" or "00:00:00  Name"
    # Match at least 2 such lines to be confident
    webex_pattern = re.compile(r'^\d{1,2}:\d{2}(?::\d{2})?\s{2,}\w', re.MULTILINE)
    webex_matches = webex_pattern.findall(text)
    if len(webex_matches) >= 2:
        return "webex"

    return "plain"


def clean_transcript(text: str) -> str:
    """
    Auto-detect the format and clean the transcript to plain text
    with speaker: dialogue format.

    Returns cleaned plain text.
    """
    fmt = detect_format(text)
    if fmt == "vtt":
        return parse_vtt(text)
    elif fmt == "webex":
        return parse_webex_txt(text)
    else:
        # Plain text: just normalize whitespace
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return len(text) // 4


def _get_max_input_tokens() -> int:
    """
    Return the max input tokens for the current model, based on settings.

    Uses MODEL_CONTEXT_WINDOWS and MAX_INPUT_FRACTION from settings.
    Falls back to a conservative 100k if the model isn't listed.
    """
    from core.config.settings import MODELS, PROVIDER, MODEL_CONTEXT_WINDOWS, MAX_INPUT_FRACTION
    model = MODELS.get(PROVIDER, "gpt-4o")
    context_window = MODEL_CONTEXT_WINDOWS.get(model, 100_000)
    return int(context_window * MAX_INPUT_FRACTION)


def transcript_to_pipeline_input(
    transcript: str,
    meeting_title: str = "Meeting",
    max_chunk_chars: int = 6000,
) -> list[dict]:
    """
    Convert a meeting transcript into a list of email-shaped dicts
    that agent1_email.run_on_items() can process.

    Strategy:
      1. Clean/parse the transcript to normalized speaker: dialogue text
      2. Check if the whole transcript fits within the model's context window
         - If yes: return a single item (no chunking) for best LLM comprehension
         - If no:  split into chunks capped at max_chunk_chars
      3. Each item becomes one "email" dict with:
           id:      "transcript_seg_N"
           sender:  primary speaker (or "Meeting Participants")
           subject: "[meeting_title] Transcript Segment N"
           body:    the text

    Args:
        transcript:      Raw transcript text (any supported format)
        meeting_title:   Human-readable meeting name (used in email subject)
        max_chunk_chars: Maximum characters per chunk when chunking is needed

    Returns:
        List of email-shaped dicts for the pipeline.
    """
    # Step 1: clean to plain text
    clean = clean_transcript(transcript)
    if not clean.strip():
        return []

    # Step 2: split into speaker-turn lines
    lines = [line for line in clean.splitlines() if line.strip()]
    if not lines:
        return []

    full_text = "\n".join(lines)
    estimated_tokens = _estimate_tokens(full_text)
    max_input_tokens = _get_max_input_tokens()

    # Step 3: decide whether to chunk
    if estimated_tokens <= max_input_tokens:
        # Whole transcript fits — send as one item for best comprehension
        print(f"[adapter] Transcript fits in context ({estimated_tokens:,} est. tokens "
              f"<= {max_input_tokens:,} max). Sending as single segment.")
        return [{
            "id":      "transcript_full",
            "sender":  "Meeting Participants",
            "subject": f"[{meeting_title}] Full Transcript",
            "body":    full_text,
        }]

    # Transcript too large — chunk it
    print(f"[adapter] Transcript exceeds context ({estimated_tokens:,} est. tokens "
          f"> {max_input_tokens:,} max). Chunking into segments.")

    chunks: list[list[str]] = []
    current_chunk: list[str] = []
    current_chars = 0
    TURNS_PER_CHUNK = 30

    for line in lines:
        line_chars = len(line)
        if current_chunk and (
            len(current_chunk) >= TURNS_PER_CHUNK or
            current_chars + line_chars > max_chunk_chars
        ):
            chunks.append(current_chunk)
            current_chunk = []
            current_chars = 0
        current_chunk.append(line)
        current_chars += line_chars

    if current_chunk:
        chunks.append(current_chunk)

    # Step 4: build email-shaped dicts
    items = []
    for i, chunk_lines in enumerate(chunks):
        chunk_text = "\n".join(chunk_lines)

        speaker_counts: dict[str, int] = {}
        for line in chunk_lines:
            match = re.match(r'^([^:]+):', line)
            if match:
                speaker = match.group(1).strip()
                speaker_counts[speaker] = speaker_counts.get(speaker, 0) + 1
        primary_speaker = (
            max(speaker_counts, key=speaker_counts.get)  # type: ignore[arg-type]
            if speaker_counts else "Meeting Participant"
        )

        items.append({
            "id":      f"transcript_seg_{i}",
            "sender":  primary_speaker,
            "subject": f"[{meeting_title}] Transcript Segment {i + 1}",
            "body":    chunk_text,
        })

    return items

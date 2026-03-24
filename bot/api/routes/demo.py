"""
Demo Mode endpoints for generating synthetic meeting transcripts.

POST /api/v1/generate-transcript  — LLM-generated meeting transcript from an idea
GET  /api/v1/sample-transcript    — Download a pre-written sample transcript file

The generate-transcript endpoint uses the same provider-branching pattern as
the agent code (agents/agent1_email.py), calling get_client() and switching
between Responses API and Chat Completions based on PROVIDER.
"""

import asyncio
import json
import pathlib
import random
import logging
from functools import partial

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

from config.settings import PROVIDER, TEMPERATURE
from clients.client import get_client, token_limit_kwarg

# Transcript generation needs more tokens than the default MAX_TOKENS (2048)
# A "low" detail transcript is ~500 words (~700 tokens), but "high" can be 2500+ words
_TRANSCRIPT_MAX_TOKENS = 4096

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Mock speakers cache — loaded once from bot/data/mock_speakers.json
# ---------------------------------------------------------------------------

_speakers_cache: list[dict] | None = None
_DATA_DIR = pathlib.Path(__file__).parent.parent.parent / "data"


def _load_speakers() -> list[dict]:
    """
    Load and cache speakers from bot/data/mock_speakers.json.

    Returns a flat list combining both male and female speaker dicts.
    Each dict has keys: name, tagline.
    """
    global _speakers_cache
    if _speakers_cache is not None:
        return _speakers_cache

    speakers_path = _DATA_DIR / "mock_speakers.json"
    if not speakers_path.exists():
        raise RuntimeError(
            f"mock_speakers.json not found at {speakers_path}. "
            "Ensure bot/data/mock_speakers.json exists."
        )

    with open(speakers_path, "r") as f:
        data = json.load(f)

    # Combine both lists into one flat list
    _speakers_cache = data.get("male", []) + data.get("female", [])
    return _speakers_cache


# ---------------------------------------------------------------------------
# Detail level -> word count mapping
# ---------------------------------------------------------------------------

_DETAIL_WORD_COUNTS = {
    "low":    (300, 500),
    "medium": (800, 1200),
    "high":   (1500, 2500),
}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class GenerateTranscriptRequest(BaseModel):
    idea: str
    detail_level: str = "medium"

    @field_validator("detail_level")
    @classmethod
    def validate_detail_level(cls, v: str) -> str:
        allowed = {"low", "medium", "high"}
        if v.lower() not in allowed:
            raise ValueError(
                f"detail_level must be one of {sorted(allowed)}, got '{v}'"
            )
        return v.lower()


class GenerateTranscriptResponse(BaseModel):
    transcript: str
    speakers: list[str]


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

@router.post("/generate-transcript", response_model=GenerateTranscriptResponse)
async def generate_transcript(
    req: GenerateTranscriptRequest,
) -> GenerateTranscriptResponse:
    """
    Generate a synthetic meeting transcript using the LLM.

    1. Picks 3-5 random speakers from mock_speakers.json
    2. Maps detail_level to a target word count range
    3. Calls the LLM (provider-branched) to generate a realistic transcript
    4. Returns the transcript text and speaker names used
    """
    logger.info("[demo] Request received — idea: %r, detail_level: %s", req.idea[:100], req.detail_level)

    if not req.idea or not req.idea.strip():
        raise HTTPException(
            status_code=422,
            detail="idea must not be empty",
        )

    # Step 1: Pick random speakers
    all_speakers = _load_speakers()
    num_speakers = random.randint(3, 5)
    chosen = random.sample(all_speakers, min(num_speakers, len(all_speakers)))
    speaker_names = [s["name"] for s in chosen]

    # Step 2: Map detail_level to word count
    min_words, max_words = _DETAIL_WORD_COUNTS[req.detail_level]

    # Step 3: Build prompt
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

    user_content = f"Generate a meeting transcript about: {req.idea.strip()}"

    # Log the fully assembled prompts
    logger.info("[demo] === SYSTEM PROMPT ===\n%s", system_prompt)
    logger.info("[demo] === USER CONTENT ===\n%s", user_content)

    # Step 4: Call LLM — run in thread pool to avoid blocking the event loop
    # (client.chat.completions.create is synchronous)
    def _call_llm():
        client, model = get_client()
        logger.info(
            "[demo] Generating transcript with %s (detail=%s, speakers=%d)",
            model, req.detail_level, len(speaker_names),
        )

        if PROVIDER in ("openai_responses", "azure_responses"):
            response = client.responses.create(
                model=model,
                instructions=system_prompt,
                input=user_content,
                temperature=TEMPERATURE,
            )
            return response.output_text
        else:
            # Retry up to 3 times — Azure content filter can intermittently
            # return None content even for safe prompts
            for attempt in range(3):
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_content},
                    ],
                    temperature=TEMPERATURE,
                    **token_limit_kwarg(model, _TRANSCRIPT_MAX_TOKENS),
                )
                choice = response.choices[0]
                content = choice.message.content
                finish = choice.finish_reason
                # Log full message structure for debugging
                logger.info(
                    "[demo] Attempt %d: content_type=%s, content_len=%d, finish=%s, "
                    "role=%s, refusal=%s, tool_calls=%s",
                    attempt + 1,
                    type(content).__name__,
                    len(content) if content else 0,
                    finish,
                    choice.message.role,
                    getattr(choice.message, 'refusal', None),
                    getattr(choice.message, 'tool_calls', None),
                )
                if content and len(content) > 10:
                    return content
                logger.warning(
                    "[demo] Attempt %d returned empty/None (finish_reason=%s), retrying...",
                    attempt + 1, finish,
                )
            # All retries exhausted
            return content or ""

    try:
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, _call_llm)
        logger.info("[demo] LLM final result: %d chars", len(raw) if raw else 0)
        if not raw:
            logger.error("[demo] LLM returned None/empty after retries! Idea: %r", req.idea[:100])

    except Exception as exc:
        logger.error("[demo] LLM call failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "error_code": "LLM_FAILURE",
                "message": f"Failed to generate transcript: {type(exc).__name__}: {exc}",
            },
        )

    transcript = raw.strip()

    return GenerateTranscriptResponse(
        transcript=transcript,
        speakers=speaker_names,
    )


@router.get("/sample-transcript")
async def sample_transcript():
    """
    Return the pre-written sample transcript as a file download.

    Serves bot/data/sample_transcript.txt with Content-Disposition: attachment
    so the browser downloads it rather than rendering inline.
    """
    sample_path = _DATA_DIR / "sample_transcript.txt"

    if not sample_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Sample transcript file not found. Ensure bot/data/sample_transcript.txt exists.",
        )

    return FileResponse(
        path=str(sample_path),
        media_type="text/plain",
        filename="sample_transcript.txt",
    )

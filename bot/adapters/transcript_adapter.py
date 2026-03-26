"""Backward-compat shim — imports from core/adapters/transcript_adapter.py"""
from core.adapters.transcript_adapter import *  # noqa: F401,F403
from core.adapters.transcript_adapter import (  # noqa: F401
    transcript_to_pipeline_input,
    clean_transcript,
    detect_format,
    parse_vtt,
    parse_webex_txt,
)

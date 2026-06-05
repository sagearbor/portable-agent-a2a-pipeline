"""
Tolerant JSON parsing for LLM responses.

LLMs routinely emit *almost*-valid JSON: wrapped in ```json fences, with a
sentence of prose before or after, a trailing comma, or an empty value like
``"due_date": ,``.  Strict ``json.loads`` rejects all of these and the pipeline
silently returns zero results.

``parse_llm_json`` strips the common wrappers, tries strict parsing first (fast
path, no surprises), and only falls back to the ``json_repair`` library when
that fails — logging the offending region so the malformation is visible.
"""

import json
import logging

logger = logging.getLogger(__name__)


def _strip_to_json(raw: str) -> str:
    """Remove ```json fences and any prose before the first ``{``/``[``."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if starts:
        cut = min(starts)
        if cut > 0:
            text = text[cut:]
    return text


def parse_llm_json(raw: str, *, context: str = "llm"):
    """
    Parse a JSON object/array from an LLM response, tolerant of fences, prose,
    trailing commas and empty values.

    Returns the parsed object.  Raises ``ValueError`` (with a snippet) if the
    text cannot be parsed even after repair, so callers can surface a clear
    error instead of a bare ``JSONDecodeError``.
    """
    if not raw or not raw.strip():
        raise ValueError(f"[{context}] empty LLM response")

    text = _strip_to_json(raw)

    # Fast path: strict parse. The vast majority of responses land here.
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        lines = text.splitlines()
        ln = max(0, exc.lineno - 1)
        region = "\n".join(lines[max(0, ln - 1): ln + 2])
        logger.warning(
            "[%s] strict JSON parse failed (%s); attempting json_repair. "
            "Region near line %d:\n%s",
            context, exc, exc.lineno, region,
        )

        try:
            from json_repair import repair_json
        except ImportError as imp:  # pragma: no cover - depends on env
            raise ValueError(
                f"[{context}] invalid LLM JSON ({exc}) and json_repair is not "
                f"installed. Snippet: {text[max(0, exc.pos - 60): exc.pos + 60]!r}"
            ) from imp

        # return_objects=True hands back the parsed Python object directly.
        repaired = repair_json(text, return_objects=True)
        if repaired in (None, "", [], {}):
            raise ValueError(
                f"[{context}] could not parse LLM JSON even after repair: {exc}. "
                f"Snippet: {text[max(0, exc.pos - 60): exc.pos + 60]!r}"
            ) from exc

        logger.info("[%s] JSON recovered via json_repair", context)
        return repaired

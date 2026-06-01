"""Tiny LLM-backed helpers for parsing the patient's free-text location and
normalizing their target language. Run once per board (not per agent), and
the results are passed forward via the case prefix + translator inputs."""
import json
import logging

from app import llm, prompts

log = logging.getLogger(__name__)


def extract_country_region(location_text: str) -> dict:
    """Return {country, region, city, confidence}. Empty strings on failure."""
    default = {"country": "", "region": "", "city": "", "confidence": "low"}
    text = (location_text or "").strip()
    if not text:
        return default

    messages = [
        {"role": "system", "content": prompts.LOCATION_EXTRACTOR},
        {"role": "user", "content": text},
    ]
    try:
        resp = llm.chat(messages, response_format={"type": "json_object"})
        raw = resp.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        return {
            "country": str(parsed.get("country", "") or "").strip(),
            "region": str(parsed.get("region", "") or "").strip(),
            "city": str(parsed.get("city", "") or "").strip(),
            "confidence": str(parsed.get("confidence", "low") or "low").lower(),
        }
    except llm.QuotaExceeded as e:
        log.warning("Location extractor hit LLM quota: %s", e)
        return default
    except Exception:
        log.exception("Location extractor failed; defaulting to empty.")
        return default


def normalize_language(target_language: str) -> str:
    """Trim and default. We let the translator handle ambiguity (e.g., 'Chinese')
    via its own prompt; this just provides a clean string."""
    cleaned = (target_language or "").strip()
    if not cleaned:
        return "English"
    return cleaned

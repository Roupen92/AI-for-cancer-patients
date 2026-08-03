"""Keep patient-derived text out of the logs.

The privacy page tells patients "we do not log the content of what you shared".
That has to be literally true, and it nearly wasn't: every search tool logged its
query on a failure path, and search queries are built from the patient's own
words. "metastatic pancreatic cancer diet Manchester" is not a verbatim question,
but it describes a person's health and where they live, and it was landing in
Railway's log stream.

Nothing here is anonymisation — it is refusal. By default the text is replaced by
its length, which is enough to tell a truncation bug from an empty string while
telling you nothing about the person. Set CANCERPATIENT_LOG_CONTENT=1 to see real
text while debugging locally; never set it on a deployment serving patients.
"""
import os


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Opt IN. A privacy control that defaults to "leak" is not a privacy control.
LOG_CONTENT = _flag("CANCERPATIENT_LOG_CONTENT", False)

# Per-turn timing lines. These carry no patient text — only ids, durations and
# counts — but the switch exists so "log nothing about turns" is one variable.
LOG_TIMING = _flag("CANCERPATIENT_LOG_TIMING", True)


def scrub(text, limit: int = 120) -> str:
    """Render text for a log line: redacted by default, real only when opted in."""
    s = "" if text is None else str(text)
    if not LOG_CONTENT:
        return f"<redacted:{len(s)}c>"
    return s[:limit]

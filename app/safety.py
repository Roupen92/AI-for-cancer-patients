"""Deterministic red-flag screen for incoming patient messages.

Runs BEFORE any LLM call, on every turn. The router (an LLM) also screens for
red flags, but an LLM screen alone is not a safety control: it can be talked
out of it, it can be distracted by a long message, and it can simply have a bad
day. This module is the floor — regex, no model, no network, no failure mode.

The two are ORed together in chat.py: either one firing shows the patient the
emergency block. False positives here cost one short paragraph the patient can
scroll past. A false negative could cost a life, so the patterns are deliberately
broad and the tie-break is always "warn".

`screen()` never raises.
"""
import re
from dataclasses import dataclass, field


@dataclass
class RedFlag:
    present: bool = False
    kind: str = ""            # "emergency" | "crisis" | ""
    why: str = ""             # plain-English name of what matched
    matched: list[str] = field(default_factory=list)   # diagnostics only

    @property
    def is_crisis(self) -> bool:
        return self.kind == "crisis"


# --------------------------------------------------------------------------- #
# Self-harm / suicidal ideation, including passive ideation. Checked FIRST so a
# message containing both a physical and a psychological red flag is routed to
# the crisis block, which carries the helpline numbers.
# --------------------------------------------------------------------------- #
_CRISIS_PATTERNS: list[tuple[str, str]] = [
    (r"\b(kill|killing)\s+(myself|my\s?self)\b", "thoughts of suicide"),
    (r"\bsuicid(e|al)\b", "thoughts of suicide"),
    (r"\btake\s+my\s+own\s+life\b", "thoughts of suicide"),
    (r"\bend\s+(my\s+life|it\s+all|things)\b", "thoughts of ending your life"),
    (r"\b(hurt|harm|cut)(ting)?\s+(myself|my\s?self)\b", "thoughts of self-harm"),
    (r"\boverdose\b", "mention of overdose"),
    (r"\bdon'?t\s+want\s+to\s+(be\s+here|live|wake\s+up|go\s+on)\b", "not wanting to be here"),
    (r"\b(want|wish)\w*\s+(i\s+(was|were)\s+dead|to\s+die|to\s+be\s+dead)\b", "wanting to die"),
    (r"\bbetter\s+off\s+(dead|gone|without\s+me)\b", "feeling others would be better off"),
    (r"\bno\s+(point|reason)\s+(in\s+)?(going\s+on|living|carrying\s+on)\b", "hopelessness"),
    (r"\bcan'?t\s+(go\s+on|do\s+this\s+any\s?more|keep\s+going)\b", "feeling unable to go on"),
    (r"\bjust\s+want\s+it\s+to\s+(stop|end|be\s+over)\b", "wanting it to stop"),
    (r"\bgive\s+up\s+on\s+(treatment|everything|life)\b", "wanting to give up"),
]

# --------------------------------------------------------------------------- #
# Physical emergencies. Each entry is (pattern, plain-English description).
# Patterns are intentionally symptom-shaped rather than diagnosis-shaped: a
# patient types "my chest feels tight", not "I have angina".
# --------------------------------------------------------------------------- #
_EMERGENCY_PATTERNS: list[tuple[str, str]] = [
    # Cardiac
    (r"\bchest\s+(pain|pressure|tight(ness)?|heaviness|crushing)\b", "chest pain or pressure"),
    (r"\b(pain|pressure)\s+in\s+my\s+chest\b", "chest pain or pressure"),
    (r"\bheart\s+(attack|racing\s+and\s+i\s+feel\s+faint)\b", "possible heart attack"),
    (r"\bpassed\s+out\b|\bfaint(ed|ing)\b|\blost\s+consciousness\b|\bblack(ed)?\s+out\b",
     "fainting or losing consciousness"),
    # Stroke — FAST symptoms
    (r"\bface\s+(is\s+)?droop(ing|ed)?\b|\bdroop(ing)?\s+(on\s+one\s+side|face)\b", "face drooping"),
    (r"\bslurr(ed|ing)\s+(my\s+)?(speech|words)\b|\bcan'?t\s+(speak|get\s+my\s+words\s+out)\b",
     "sudden trouble speaking"),
    (r"\b(sudden|suddenly)\b[^.]{0,40}\b(weak(ness)?|numb(ness)?|paralysis|can'?t\s+move)\b",
     "sudden weakness or numbness"),
    (r"\b(weak(ness)?|numb(ness)?)\b[^.]{0,30}\bone\s+side\b", "weakness on one side"),
    (r"\bstroke\s+symptoms?\b|\bthink\s+i'?m\s+having\s+a\s+stroke\b", "possible stroke"),
    (r"\b(sudden|suddenly)\b[^.]{0,40}\b(vision\s+loss|lost\s+my\s+(vision|sight)|can'?t\s+see)\b",
     "sudden vision loss"),
    (r"\b(sudden|suddenly)\b[^.]{0,30}\bconfus(ed|ion)\b", "sudden confusion"),
    # Breathing
    (r"\b(can'?t|cannot|struggling\s+to|unable\s+to)\s+breathe?\b", "trouble breathing"),
    (r"\b(severe|sudden|suddenly)\b[^.]{0,30}\b(short(ness)?\s+of\s+breath|breathless)\b",
     "sudden severe breathlessness"),
    (r"\bgasping\s+for\s+(air|breath)\b", "gasping for air"),
    (r"\blips?\s+(are\s+)?(turning\s+)?blue\b", "lips turning blue"),
    # Bleeding
    (r"\b(coughing|throwing|vomit(ing)?)\s+up\s+blood\b", "coughing or vomiting blood"),
    (r"\bvomit(ing)?\s+(looks\s+like\s+)?coffee\s+grounds\b", "vomit that looks like coffee grounds"),
    (r"\b(heavy|uncontrolled|won'?t\s+stop)\s+bleeding\b", "bleeding that will not stop"),
    (r"\bbleeding\s+(that\s+)?(won'?t|will\s+not)\s+stop\b", "bleeding that will not stop"),
    (r"\bblack\s+(tarry\s+)?stool(s)?\b|\btarry\s+stool", "black or tarry stool"),
    (r"\bblood\s+in\s+my\s+(stool|poo|vomit)\b", "blood in stool or vomit"),
    # Infection / sepsis, including the neutropenic-fever case.
    # `[^\n]` rather than `[^.]`: a decimal temperature ("fever of 38.9") contains
    # a period, and excluding periods made this miss the exact case it exists for.
    (r"\bfever\b[^\n]{0,60}\b(chemo(therapy)?|immunosuppress|immune[- ]suppress|transplant|neutropeni)",
     "a fever while on treatment that lowers your immune system"),
    (r"\b(chemo(therapy)?|immunosuppress|immune[- ]suppress|transplant|neutropeni)[^\n]{0,60}\bfever\b",
     "a fever while on treatment that lowers your immune system"),
    (r"\btemperature\s+(of\s+)?(3[89]|4\d)(\.\d)?\s*(c|celsius)?\b", "a high temperature"),
    # Fahrenheit (101+) and Celsius (38+) — patients report either.
    (r"\bfever\s+of\s+(10[1-9]|1[1-9]\d)\b", "a high fever"),
    (r"\bfever\s+of\s+(3[89]|4\d)(\.\d)?\b", "a high fever"),
    (r"\bsepsis\b|\bsepti[ck]\b", "possible sepsis"),
    (r"\bstiff\s+neck\b[^.]{0,40}\b(fever|rash|light)\b", "stiff neck with fever"),
    (r"\bnon[- ]?blanching\s+rash\b|\brash\s+that\s+doesn'?t\s+fade\b", "a rash that does not fade"),
    # Allergy
    (r"\b(anaphyla(xis|ctic)|throat\s+(is\s+)?(closing|swelling)|tongue\s+swelling)\b",
     "signs of a severe allergic reaction"),
    (r"\bface\s+(and\s+)?(lips?\s+)?swelling\s+up\b", "sudden facial swelling"),
    # Neuro
    (r"\bworst\s+headache\s+(of\s+my\s+life|ever)\b", "the worst headache of your life"),
    (r"\b(sudden|thunderclap)\b[^.]{0,20}\bheadache\b", "a sudden severe headache"),
    (r"\bseizure\b[^.]{0,40}\b(first|never\s+had|won'?t\s+stop|back\s+to\s+back)\b", "a new or repeated seizure"),
    (r"\bcan'?t\s+(feel|move)\s+my\s+(legs?|arms?|hands?|feet)\b", "loss of movement or feeling in a limb"),
    (r"\b(new\s+)?(loss\s+of\s+)?bladder\s+(and\s+bowel\s+)?control\b", "new loss of bladder or bowel control"),
    (r"\bsaddle\s+(numbness|an(a)?esthesia)\b", "numbness between the legs"),
    # Obstetric / paediatric — this app is not the right place for either
    (r"\b(my\s+)?(baby|newborn|infant|toddler|child)\b[^.]{0,50}\b(not\s+breathing|limp|unresponsive|blue|won'?t\s+wake|high\s+fever|seizure)\b",
     "a seriously unwell baby or child"),
    (r"\bbleeding\s+heavily\b[^.]{0,30}\bpregnan", "heavy bleeding in pregnancy"),
    # Explicit self-report of an emergency
    (r"\b(should\s+i\s+)?(go\s+to|call)\s+(the\s+)?(er|a\s?&\s?e|emergency\s+room|ambulance|911|999|112)\b",
     "you are asking whether this is an emergency"),
]

_CRISIS_RE = [(re.compile(p, re.IGNORECASE), why) for p, why in _CRISIS_PATTERNS]
_EMERGENCY_RE = [(re.compile(p, re.IGNORECASE), why) for p, why in _EMERGENCY_PATTERNS]


# Phrasings that mean the patient is asking ABOUT a symptom in general, or
# reporting it in the past, rather than having it right now. These downgrade an
# emergency match — but never a crisis match, where we always warn.
_HYPOTHETICAL_RE = re.compile(
    r"\b("
    r"what\s+(does|do|is|are|should)\b|"
    r"why\s+(does|do|is|are)\b|"
    r"what\s+causes\b|"
    r"is\s+it\s+normal\b|"
    r"i\s+(had|used\s+to\s+have|was\s+told)\b|"
    r"last\s+(year|month|week)\b|"
    r"my\s+(mother|father|mum|mom|dad|husband|wife|partner)\s+(died|passed)\b|"
    r"in\s+the\s+past\b|"
    r"after\s+my\s+(surgery|operation)\s+(last|in)\b|"
    r"the\s+leaflet\s+says\b|"
    r"the\s+doctor\s+said\b"
    r")",
    re.IGNORECASE,
)

# Present-tense markers that override the hypothetical downgrade. "What does
# chest pain mean" is a question; "what does this chest pain mean, it started an
# hour ago" is an emergency.
_PRESENT_TENSE_RE = re.compile(
    r"\b("
    r"right\s+now|currently|at\s+the\s+moment|as\s+i\s+write|"
    r"(started|began|came\s+on)\s+(an?|\d+|a\s+few|half\s+an)?\s*(minute|hour)s?\s+ago|"
    r"for\s+the\s+last\s+(few\s+|\d+\s+)?(minutes?|hours?)|"
    r"since\s+(this\s+)?(morning|afternoon|tonight)|"
    r"i\s+am\s+having|i'?m\s+having|it'?s\s+happening|happening\s+now|"
    r"today|tonight|just\s+started|getting\s+worse"
    r")",
    re.IGNORECASE,
)


def screen(text: str) -> RedFlag:
    """Screen one patient message for red flags. Never raises."""
    try:
        msg = (text or "").strip()
        if not msg:
            return RedFlag()

        # Crisis first — it outranks everything and is never downgraded.
        crisis_hits = [why for rx, why in _CRISIS_RE if rx.search(msg)]
        if crisis_hits:
            return RedFlag(
                present=True,
                kind="crisis",
                why=crisis_hits[0],
                matched=sorted(set(crisis_hits)),
            )

        emergency_hits = [why for rx, why in _EMERGENCY_RE if rx.search(msg)]
        if not emergency_hits:
            return RedFlag()

        # A general question ("what does chest pain feel like?") is not an
        # emergency — unless something in the message says it is happening now.
        if _HYPOTHETICAL_RE.search(msg) and not _PRESENT_TENSE_RE.search(msg):
            return RedFlag()

        return RedFlag(
            present=True,
            kind="emergency",
            why=emergency_hits[0],
            matched=sorted(set(emergency_hits)),
        )
    except Exception:  # pragma: no cover — a screen that raises is worse than useless
        return RedFlag()


# Phrases that mean a string was written ABOUT the patient for the system,
# rather than TO the patient. The router is an LLM and drifts into clinical
# note-taking voice ("The patient is asking for an exact insulin dose…",
# "Advise the patient to contact their care team"), which reads as nonsense
# when spliced into "You mentioned **…**".
_THIRD_PERSON = re.compile(
    r"\b(the\s+patient|patient\s+(is|may|should|appears|reports|seems)|"
    r"advise\s+(the\s+)?patient|do\s+not\s+provide|the\s+user|"
    r"safety\s+guardrails?|guardrails?)\b",
    re.IGNORECASE,
)


def clean_patient_phrase(text: str, *, max_chars: int) -> str:
    """Return `text` only if it is safe to show a patient verbatim, else "".

    Rejects clinician/system voice and anything long enough to be a rationale
    rather than a phrase. The caller always has a safe default to fall back on,
    so dropping a borderline string costs nothing and keeps a leaked internal
    note off a frightened person's screen.
    """
    s = " ".join((text or "").split())
    if not s:
        return ""
    if len(s) > max_chars:
        return ""
    if _THIRD_PERSON.search(s):
        return ""
    return s


def emergency_markdown(flag: RedFlag, action: str = "") -> str:
    """The block shown to the patient, above everything else in the answer.

    Deterministic text — not model output — so it cannot be reworded, softened,
    or dropped by an LLM pass. `action` is the router's one-line suggestion when
    available; there is always a safe default.
    """
    from app import prompts

    if not flag.present:
        return ""

    if flag.is_crisis:
        return f"## {prompts.EMERGENCY_BANNER_HEADING}\n\n{prompts.CRISIS_LINES_BLOCK}\n"

    # `why` is spliced into a sentence, so it must be a short noun phrase; `action`
    # stands alone but must still be addressed to the patient.
    why = clean_patient_phrase(flag.why, max_chars=140) or "something you described"
    safe_action = clean_patient_phrase(action, max_chars=320)
    default_action = (
        "Please call your local emergency number (911 in the US, 999 in the UK, "
        "112 in much of Europe, 000 in Australia), or go to your nearest emergency "
        "room now. If you have an on-call number for your clinic or treatment team, "
        "call that first only if you cannot get emergency help."
    )
    return (
        f"## {prompts.EMERGENCY_BANNER_HEADING}\n\n"
        f"You mentioned **{why}**. That needs someone to examine you in person, "
        f"and it needs to happen now — not after you finish reading this.\n\n"
        f"{safe_action or default_action}\n\n"
        f"Do not wait to see whether it settles, and do not drive yourself if you "
        f"feel faint or short of breath.\n\n"
        f"---\n\n"
        f"Here is what you asked about as well, for when you are safe:\n"
    )

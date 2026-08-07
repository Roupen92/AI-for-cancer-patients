"""Deterministic screen for genomic content in a patient's message.

Same contract as app/safety.py, and for the same reason: regex only, no model, no
network, never raises. What this produces is prepended to the answer in Python
AFTER every LLM pass, so no model can reword or drop it.

Three gates, chosen because each prevents a harm that a prompt rule alone has
been observed to leak:

  1. ORIGIN — germline or somatic. `BRCA2 c.5946delT pathogenic` is the identical
     string whether it came from a blood draw or a tumour block, and the two
     readings have opposite consequences for the patient's children. Even the lab
     often cannot tell: tumour-only sequencing does not distinguish constitutional
     from acquired origin by design, and in a 49,264-patient series only ~27% of
     tumour-detected variants above the usual germline-referral thresholds turned
     out to be germline. So a model inferring origin from the gene name or the
     allele fraction is wrong most of the time. The rule is ASK, NEVER INFER.

     The asymmetry decides the tie-break. Germline read as somatic is SILENT — the
     family is simply never told, and nobody discovers the error. Somatic read as
     germline is loud, distressing, and correctable by one blood test. Both are
     harms; only one is invisible. Hence: refuse to resolve, and say so.

  2. NUMBERS WITHOUT AN ASSAY — a threshold is a property of a test and a tumour
     type, not of a number. The widely-quoted TMB cutoff was set for one specific
     FDA-approved assay; HRD scores run on lab-specific scales; PD-L1 comes as CPS
     or TPS with different antibodies and different cutoffs per cancer. "My TMB is
     9" is not a number anyone can rule on.

  3. CONSUMER TESTS — a 23andMe-style BRCA report checks a few dozen of more than
     four thousand known variants. "Nothing found" is not "nothing there", and the
     documented real-world harm is a patient skipping screening they need.
"""
import re
from dataclasses import dataclass, field


@dataclass
class ReportSignal:
    present: bool = False
    # "germline" | "somatic" | "dtc" | "mixed" | "ambiguous" | ""
    origin: str = ""
    markers_without_assay: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)


# The preamble talks about "your test results", so it must only fire when the
# patient actually HAS a result — a named gene or marker, variant notation, a
# classification word, or an explicit mention of a report or test. "Is diabetes
# genetic?" is a genuine genetics question but not a report to interpret, and
# opening it with a block about which sample type was sequenced would be noise.
_RESULT_SIGNAL = re.compile(
    # `brca[12]?` catches the bare "the BRCA gene", which is how patients actually
    # write it — and the message that phrase most often appears in is "23andMe says
    # I don't have the BRCA gene, so I can skip screening", the exact case this
    # whole screen exists for.
    r"\b(brca[12]?|egfr|kras|nras|braf|alk|ros1|ntrk|ret|met|her2|erbb2|tp53|atm|"
    r"palb2|chek2|mlh1|msh2|msh6|pms2|epcam|jak2|flt3|idh1|idh2|pik3ca|pten|kit|"
    r"cftr|hfe|cyp2c19|cyp2d6|dpyd|tpmt|hla-b|bcr-?abl|"
    r"msi|msi-h|dmmr|pmmr|tmb|hrd|pd-?l1|cps|tps|vaf)\b"
    r"|\b(pathogenic|benign|variant of (uncertain|unknown) significance|\bvus\b|"
    r"likely pathogenic|likely benign|germline|somatic|carrier)\b"
    r"|\b(my|the|this) (report|results?|panel|sequencing|biopsy report)\b"
    r"|\b(genetic|genomic|molecular|biomarker|gene) (test|testing|panel|report|"
    r"results?|sequencing|screening)\b"
    r"|\b[A-Z]\d{1,4}[A-Z*]\b"
    r"|\b[cgmnp]\.\S+",
    re.IGNORECASE,
)

# Cheap prefilter. Broader than _RESULT_SIGNAL, so the expensive work only runs on
# messages that are plausibly about genetics at all.
_GENOMIC_CONTEXT = re.compile(
    r"\b(gene|genes|genetic|genomic|genome|mutation|variant|biomarker|molecular|"
    r"sequenc\w*|panel|ngs|exome|allele|chromosom\w*|hereditary|inherited|carrier|"
    r"germline|somatic|pathogenic|benign|vus|msi|dmmr|pmmr|tmb|hrd|pd-?l1|"
    r"brca1|brca2|egfr|kras|nras|braf|alk|ros1|ntrk|ret|met|her2|erbb2|tp53|atm|"
    r"palb2|chek2|mlh1|msh2|msh6|pms2|epcam|jak2|flt3|idh1|idh2|pik3ca|pten|"
    r"cftr|hfe|cyp2c19|cyp2d6|dpyd|tpmt|hla-b|bcr-?abl)\b"
    r"|\b[A-Z]\d{1,4}[A-Z*]\b"
    r"|\b[cgmnp]\.\S+",
    re.IGNORECASE,
)

_GERMLINE_MARKERS = re.compile(
    r"\b(germline|hereditary|inherited|carrier|blood (draw|sample|test)|saliva|"
    r"buccal|spit|autosomal (dominant|recessive)|lynch syndrome|"
    r"hereditary cancer panel|myrisk|heterozygous|homozygous|de novo|"
    r"family history of|my (mother|father|sister|brother|aunt|uncle|grandmother|"
    r"grandfather) had)\b",
    re.IGNORECASE,
)
_SOMATIC_MARKERS = re.compile(
    r"\b(somatic|tumou?r (sequencing|profiling|panel|ngs|test|testing|biopsy)|"
    r"biopsy (was )?sequenc\w*|liquid biopsy|ct?dna|vaf|"
    r"allele (frequency|fraction)|tmb|msi|dmmr|pmmr|pd-?l1|hrd|"
    r"fusion|amplification|foundation ?one|f1cdx|caris|tempus|guardant|"
    r"msk-?impact|tso ?500|oncomine|next-generation sequencing of my tumou?r)\b",
    re.IGNORECASE,
)
# Consumer tests outrank germline markers: a 23andMe report IS germline, but it is
# not clinical-grade germline, and conflating the two is the specific harm here.
_DTC_MARKERS = re.compile(
    r"\b(23 ?and ?me|ancestry ?dna|ancestry\.com|myheritage|nebula genomics|"
    r"tellmegen|living ?dna|spit kit|dna kit|raw data|promethease|"
    r"genetic health risk|carrier status report)\b|\brs\d{3,}\b",
    re.IGNORECASE,
)

# Markers whose number is meaningless without the assay that produced it.
_ASSAY_BOUND = (
    ("TMB", re.compile(r"\bTMB\b[^\n]{0,20}\d|\d+(?:\.\d+)?\s*mut\w*\s*/?\s*mb", re.IGNORECASE)),
    ("MSI", re.compile(r"\bMSI\b[^\n]{0,20}\d", re.IGNORECASE)),
    ("PD-L1", re.compile(r"\bPD-?L1\b[^\n]{0,20}\d|\b(CPS|TPS)\b\s*(of|=|:)?\s*\d", re.IGNORECASE)),
    ("HRD", re.compile(r"\b(HRD|GIS|genomic instability score)\b[^\n]{0,20}\d", re.IGNORECASE)),
    ("allele fraction", re.compile(r"\b(VAF|allele (frequency|fraction))\b[^\n]{0,15}\d", re.IGNORECASE)),
)
_ASSAY_NAMED = re.compile(
    r"\b(foundation ?one|f1cdx|foundationone liquid|caris|tempus ?x[tfe]?|"
    r"guardant ?360|msk-?impact|oncomine|tso ?500|mychoice|myriad|"
    r"22c3|28-?8|sp142|sp263|73-?10|dako|ventana|mantis|msisensor|"
    r"pcr|immunohistochemistry|ihc)\b",
    re.IGNORECASE,
)

# Report-header identifiers. A variant list already identifies someone even with
# the name removed; the header adds the parts that identify them to anyone.
_IDENTIFIERS = (
    ("date of birth", re.compile(r"\b(DOB|date of birth)\b", re.IGNORECASE)),
    ("a date that looks like a birth date", re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-](19|20)\d{2}\b")),
    ("medical record number", re.compile(r"\b(MRN|medical record (number|no))\b", re.IGNORECASE)),
    ("a specimen or accession number",
     re.compile(r"\b(accession|specimen|report)\s*(#|no\.?|number|id)\s*:?\s*[A-Z0-9-]{4,}", re.IGNORECASE)),
    ("your doctor's name",
     re.compile(r"\b(ordering|referring)\s+(physician|provider|doctor|clinician)\b", re.IGNORECASE)),
)


def classify(text: str) -> ReportSignal:
    """Screen one patient message for genomic content. Never raises."""
    try:
        return _classify(text or "")
    except Exception:  # pragma: no cover — a screen must never break a turn
        return ReportSignal()


def _classify(text: str) -> ReportSignal:
    sig = ReportSignal()
    if not _GENOMIC_CONTEXT.search(text):
        return sig

    dtc = bool(_DTC_MARKERS.search(text))
    # Naming a consumer DNA test IS having a result, whether or not any gene or
    # classification word appears with it.
    if not (_RESULT_SIGNAL.search(text) or dtc):
        return sig
    sig.present = True

    germline = bool(_GERMLINE_MARKERS.search(text))
    somatic = bool(_SOMATIC_MARKERS.search(text))

    if dtc:
        sig.origin = "dtc"
        sig.matched.append("consumer-test marker")
    elif germline and somatic:
        sig.origin = "mixed"
    elif germline:
        sig.origin = "germline"
    elif somatic:
        sig.origin = "somatic"
    else:
        sig.origin = "ambiguous"

    assay_named = bool(_ASSAY_NAMED.search(text))
    for label, pattern in _ASSAY_BOUND:
        if pattern.search(text) and not assay_named:
            sig.markers_without_assay.append(label)

    for label, pattern in _IDENTIFIERS:
        if pattern.search(text):
            sig.identifiers.append(label)

    return sig


# --------------------------------------------------------------------------- #
# The preamble
# --------------------------------------------------------------------------- #
#
# Assembled here and prepended in chat.run_turn AFTER translation, exactly like
# safety.emergency_markdown() and for the same reason: nothing may reword it.
#
# Known limitation, stated rather than papered over: this is English, so a
# non-English patient receives it untranslated. The right fix is a translated
# string table, NOT routing it through the translator — that would hand an LLM the
# one block that exists because no LLM may touch it.

_WHICH_TEST = """**First, one question: which test was this?**

There are two very different kinds, and they can look identical on the page.

- A **germline** test reads the DNA you were born with, usually from blood or saliva. It can have implications for blood relatives.
- A **tumour** (somatic) test reads changes that happened inside the cancer itself. Those changes are not passed on to children.

The same gene name — BRCA2, TP53, ATM — appears on both, so I will not guess which one you have: guessing wrong in either direction matters. If you can tell me which it was, or the line near the top of the report naming the sample type, I can be much more useful."""

_DTC_NOTE = """**A consumer DNA test is not the same evidence as a clinical one.**

Tests like 23andMe check a selected list of specific spots rather than reading a gene from end to end. Two things follow: a result of "nothing found" does not mean nothing is there, and a result that does turn up needs confirming at a clinical laboratory before anyone acts on it. Please don't let either the reassurance or the worry from a consumer report change your care until a clinician has confirmed it."""

_COUNSELLOR = """**Who can actually interpret this for you:** a genetic counsellor is a health professional whose whole job is genetic test results — what a result means, what it does not mean, what it means for your family, and what your options are. They are not there to tell you what to do. Most cancer centres and many hospitals have one, and you can ask your care team for a referral. You do not need to already understand your report to see one; that is the point of the appointment."""

_PRIVACY = """**One privacy note:** a list of your genetic changes identifies you even with your name removed, and it says something about your blood relatives too. If you paste a report here, delete the header first — the name, date of birth, and report number."""

_HEADER = """**About your test results**

I can explain what the words and numbers on a genetic or genomic report mean. I can't tell you what your report means for you — that needs someone with your full record in front of them."""


def preamble(sig: ReportSignal) -> str:
    """Deterministic block prepended above a genomics answer. "" when not needed."""
    if not sig.present:
        return ""

    parts = [_HEADER]

    if sig.origin in ("ambiguous", "mixed"):
        parts.append(_WHICH_TEST)
    elif sig.origin == "dtc":
        parts.append(_DTC_NOTE)

    if sig.markers_without_assay:
        listed = ", ".join(sig.markers_without_assay)
        parts.append(
            "**A number on its own can't be read.**\n\n"
            f"You mentioned: {listed}. For each of these, the line between \"high\" "
            "and \"low\" is set by the specific laboratory test used and the specific "
            "type of cancer — different tests give different numbers from the same "
            "sample, and the same number means different things in different cancers. "
            "The two things to find are the **name of the test** (usually on the first "
            "or last page) and **which cancer it was run on**."
        )

    if sig.identifiers:
        parts.append(
            "**Careful with the top of the report.** What you sent looks like it "
            f"includes {', '.join(sig.identifiers)}. You don't need to share any of "
            "that with me for this — and it is the part worth removing before you "
            "paste a report anywhere."
        )

    parts.append(_COUNSELLOR)
    parts.append(_PRIVACY)

    return "\n\n".join(parts) + "\n\n---\n"

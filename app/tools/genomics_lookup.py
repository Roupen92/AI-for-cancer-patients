"""Plain-language lookup for the words and numbers on a genomic report.

Three keyless sources, each answering a different question, tried in this order:

    vocabulary  "what does MSI-high / pathogenic / germline mean"
                -> NCI Dictionary of Cancer Terms + Dictionary of Genetics Terms
    gene        "what does BRCA2 / BRAF / EGFR actually do"
                -> MedlinePlus Genetics, falling back to NCBI Gene
    variant     "what is known about this exact change"
                -> CIViC (somatic clinical interpretations, CC0)

Everything here is public-domain or CC0 and needs no API key, which matters: this
tool exists because an agent that has to explain a patient's own report must never
be answering from training data. OncoKB and COSMIC are deliberately absent — both
forbid this use in their licence terms (OncoKB's registration explicitly bars API
access without a paid licence and bars using the data to train models).

Two hazards drove the design, and both are handled here rather than in the prompt:

  * Searching the NCI dictionary for variant notation returns a DRUG. `V600E`
    resolves to "BRAF (V600E) kinase inhibitor RO5185426" — vemurafenib. A naive
    lookup would tell a patient their mutation IS a medication. So variant-shaped
    tokens are never sent to the dictionary.
  * A zero-hit response is HTTP 200 with an empty body. Rendered carelessly that
    reads as "nothing concerning was found", which is the silent-failure mode
    app/health.py exists to prevent. Every miss here is reported as a miss.
"""
import asyncio
import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

_NCI_GLOSSARY = "https://webapis.cancer.gov/glossary/v1"
_NCI_PAGE = "https://www.cancer.gov/publications/dictionaries"
_MEDLINEPLUS_GENE = "https://medlineplus.gov/download/genetics/gene"
_HGNC = "https://rest.genenames.org"
_CIVIC = "https://civicdb.org/api/graphql"

_TIMEOUT = 20.0

SCHEMA = {
    "name": "genomics_lookup",
    "description": (
        "Look up the plain-English meaning of terms, genes, and molecular markers "
        "from a patient's genetic or genomic report. Returns patient-audience "
        "definitions from the National Cancer Institute's dictionaries, what a gene "
        "normally does from MedlinePlus Genetics, and known clinical associations "
        "for a specific variant from CIViC. Every result is registered in the "
        "evidence ledger so you can cite it as [N]. This tool describes what terms "
        "MEAN IN GENERAL — it cannot interpret this patient's result for them, and "
        "it cannot tell you whether a finding is inherited or acquired."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "terms": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Report vocabulary to define, one concept per item, spelled out "
                    "where possible: 'microsatellite instability', 'tumor mutational "
                    "burden', 'variant of uncertain significance', 'germline "
                    "variant', 'pathogenic variant'. Hyphenated shorthand works "
                    "('MSI-H'), but a phrase with a space instead of the hyphen does "
                    "not — this is an exact term lookup, not a search engine."
                ),
            },
            "genes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Gene symbols to describe, e.g. ['BRCA2', 'EGFR']. Common "
                    "aliases are resolved to the official symbol (HER2 -> ERBB2)."
                ),
            },
            "variant": {
                "type": "string",
                "description": (
                    "Optional. One specific alteration as the patient's report "
                    "writes it, e.g. 'V600E', 'G12C', 'Exon 19 Deletion'. Looks up "
                    "known clinical associations, each tied to a disease and a "
                    "published source. NOT eligibility or a treatment plan."
                ),
            },
        },
    },
}

# Variant notation must never reach the dictionary endpoints (see module
# docstring). Verified live: `V600E` returns "BRAF (V600E) kinase inhibitor
# RO5185426" — vemurafenib, a drug.
#
# The gene-prefixed form ("BRAF V600E") is included too. It does NOT hit the drug
# (that search returns 0), but it returns 0 for everything, so leaving it here
# would report "no dictionary entry for BRAF V600E" — a miss that reads like an
# absence of knowledge rather than a misrouted query. Observed in a live run: the
# agent asked for exactly that.
_VARIANT_BARE = r"[A-Za-z]\d{1,4}[A-Za-z*]"
_VARIANT_SHAPED = re.compile(
    r"^\s*(?:"
    rf"{_VARIANT_BARE}"
    rf"|[A-Za-z][A-Za-z0-9-]{{1,9}}\s+{_VARIANT_BARE}"    # BRAF V600E, KRAS G12C
    r"|[cgmnp]\.\S+"
    r"|(?:[A-Za-z][A-Za-z0-9-]{1,9}\s+)?(?:exon|intron)\s*\d+.*"
    r"|\S*(?:del|ins|dup|fs)\S*"
    r")\s*$",
    re.IGNORECASE,
)


def _looks_like_a_variant(term: str) -> bool:
    return bool(_VARIANT_SHAPED.match(term or ""))


def _slug(term: str) -> str:
    """The NCI dictionary's prettyUrlName form of a term."""
    out = re.sub(r"[^a-z0-9]+", "-", (term or "").strip().lower())
    return out.strip("-")


async def _get_json(client: httpx.AsyncClient, url: str, **kwargs) -> Any:
    try:
        r = await client.get(url, **kwargs)
        if r.status_code != 200:
            return None
        return r.json()
    except (httpx.RequestError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# NCI dictionaries — the patient-language layer
# --------------------------------------------------------------------------- #
#
# Only two dictionary/audience combinations exist: Cancer.gov/Patient (~9,400
# terms) and Genetics/HealthProfessional (~240). Genetics/Patient 404s, so
# useFallback=true is how a genetics term is reached at all — and it reports which
# audience it actually returned, which is the signal the caller needs: a
# HealthProfessional definition is clinician-register prose that must be flagged
# as such rather than handed to a patient as-is.
_NCI_LOOKUPS = (
    ("Cancer.gov", "Patient", "cancer-terms"),
    ("Genetics", "HealthProfessional", "genetics-dictionary"),
)


async def _nci_term(client: httpx.AsyncClient, term: str) -> dict | None:
    slug = _slug(term)
    if not slug:
        return None

    for dictionary, audience, page in _NCI_LOOKUPS:
        data = await _get_json(
            client,
            f"{_NCI_GLOSSARY}/Terms/{dictionary}/{audience}/en/{slug}",
            params={"useFallback": "true"},
        )
        if not isinstance(data, dict) or not data.get("termName"):
            continue
        definition = ((data.get("definition") or {}).get("text") or "").strip()
        if not definition:
            continue
        returned_audience = (data.get("audience") or audience).strip()
        return {
            "term": (data.get("termName") or term).strip(),
            "definition": definition,
            "audience": returned_audience,
            "url": f"{_NCI_PAGE}/{page}/def/{data.get('prettyUrlName') or slug}",
            "source_id": f"nci:{dictionary}:{data.get('prettyUrlName') or slug}",
        }

    # Fall back to a search, which catches a term whose slug we guessed wrong.
    for dictionary, audience, page in _NCI_LOOKUPS:
        data = await _get_json(
            client,
            f"{_NCI_GLOSSARY}/Terms/search/{dictionary}/{audience}/en/{term}",
            params={"matchType": "Contains", "size": 1},
        )
        results = (data or {}).get("results") or []
        if not results:
            continue
        hit = results[0]
        definition = ((hit.get("definition") or {}).get("text") or "").strip()
        if not definition:
            continue
        return {
            "term": (hit.get("termName") or term).strip(),
            "definition": definition,
            "audience": (hit.get("audience") or audience).strip(),
            "url": f"{_NCI_PAGE}/{page}/def/{hit.get('prettyUrlName') or _slug(term)}",
            "source_id": f"nci:{dictionary}:{hit.get('prettyUrlName') or _slug(term)}",
        }
    return None


# --------------------------------------------------------------------------- #
# Gene layer
# --------------------------------------------------------------------------- #

async def _official_symbol(client: httpx.AsyncClient, symbol: str) -> str:
    """Resolve an alias to the official HGNC symbol (HER2 -> ERBB2).

    Field-scoped search only. The bare all-fields form ranks badly enough to be
    dangerous — `/search/p53` puts PURPL, JMY and KLLN above TP53.
    """
    clean = re.sub(r"[^A-Za-z0-9._-]", "", symbol or "").strip()
    if not clean:
        return ""
    for field in ("symbol", "alias_symbol", "prev_symbol"):
        data = await _get_json(
            client,
            f"{_HGNC}/search/{field}/{clean}",
            headers={"Accept": "application/json"},
        )
        docs = ((data or {}).get("response") or {}).get("docs") or []
        if docs:
            return (docs[0].get("symbol") or clean).strip()
    return clean


async def _gene_description(client: httpx.AsyncClient, symbol: str) -> dict | None:
    """What the gene normally does, from MedlinePlus Genetics.

    Gene pages carry only a 'function' section — nothing variant-level, and the
    framing is inherited-disease, so a somatic finding must not be explained from
    here alone. About a quarter of the most-requested biomarker genes (ERBB2/HER2,
    MET, ROS1, ESR1, PALB2, CHEK2 …) have no page at all, which is why the miss is
    reported explicitly rather than silently returning nothing.
    """
    slug = re.sub(r"[^a-z0-9-]", "", (symbol or "").lower())
    if not slug:
        return None
    data = await _get_json(client, f"{_MEDLINEPLUS_GENE}/{slug}.json")
    if not isinstance(data, dict):
        return None

    texts = []
    for item in data.get("text-list") or []:
        block = (item or {}).get("text") or {}
        html = block.get("html") or ""
        plain = re.sub(r"<[^>]+>", " ", html)
        plain = re.sub(r"\s+", " ", plain).strip()
        if plain:
            texts.append(plain)
    if not texts:
        return None

    conditions = []
    for item in (data.get("related-health-condition-list") or [])[:6]:
        name = ((item or {}).get("related-health-condition") or {}).get("name")
        if name:
            conditions.append(str(name).strip())

    return {
        "symbol": (data.get("gene-symbol") or symbol).strip(),
        "name": (data.get("name") or "").strip(),
        "function": " ".join(texts)[:1200],
        "conditions": conditions,
        # Surfaced deliberately: several of these pages were last reviewed years
        # ago (BRCA2 in 2015, EGFR in 2017) and a patient deserves to know the age
        # of what they are being told.
        "reviewed": (data.get("reviewed") or "").strip(),
        "url": (data.get("ghr-page") or f"https://medlineplus.gov/genetics/gene/{slug}").strip(),
        "source_id": f"medlineplus:gene:{slug}",
    }


# --------------------------------------------------------------------------- #
# Variant layer — CIViC
# --------------------------------------------------------------------------- #

_CIVIC_VARIANT_QUERY = """
query($name: String!) {
  variants(name: $name, first: 5) {
    nodes { id name singleVariantMolecularProfileId feature { name } }
  }
}
"""
_CIVIC_PROFILE_QUERY = """
query($id: Int!) {
  molecularProfile(id: $id) {
    name
    description
    # No `status` argument exists on this field — `includeRejected` (default
    # false) is the real control, so omitting it already excludes rejected items.
    evidenceItems(first: 6) {
      nodes {
        evidenceLevel
        evidenceType
        evidenceDirection
        significance
        disease { name }
        therapies { name }
        source { citationId sourceType }
      }
    }
  }
}
"""


# CIViC curates prognostic evidence, and it arrives in exactly the register the
# genomics prompt forbids: "correlated with poor prognosis", "prognostic / poor
# outcome". Handing that to the agent and asking it not to repeat it is the weak
# version of the rule — an agent given a "poor outcome" line will eventually pass
# it on, and the patient reads a survival forecast off their own report.
#
# So it is removed here instead. Prognostic evidence items are dropped whole, and
# prognosis-bearing sentences are cut from the description. What survives is what
# the patient can actually use: what the alteration is, and which treatments have
# been studied against it in which disease. Deterministic, per this codebase's
# preference for enforcement over instruction.
_PROGNOSIS_RE = re.compile(
    r"\b(prognos\w*|survival|poor outcome|worse outcome|better outcome|mortality|"
    r"aggressive|indolent|life expectancy)\b",
    re.IGNORECASE,
)


def _strip_prognosis(text: str) -> str:
    kept = [
        sentence
        for sentence in re.split(r"(?<=[.!?])\s+", text or "")
        if sentence.strip() and not _PROGNOSIS_RE.search(sentence)
    ]
    return " ".join(kept).strip()


async def _civic_variant(client: httpx.AsyncClient, variant: str, gene_hint: str = "") -> dict | None:
    """Known clinical associations for one alteration, from CIViC (CC0).

    Everything returned is disease-scoped on purpose. The same BRAF V600E behaves
    differently in melanoma and in colorectal cancer, so an association rendered
    without its disease name is worse than none. `evidenceLevel: A` means the
    association is well studied — a statement about the literature, never about
    this patient.
    """
    payload = {"query": _CIVIC_VARIANT_QUERY, "variables": {"name": variant}}
    try:
        r = await client.post(_CIVIC, json=payload)
        if r.status_code != 200:
            return None
        nodes = (((r.json() or {}).get("data") or {}).get("variants") or {}).get("nodes") or []
    except (httpx.RequestError, ValueError):
        return None
    if not nodes:
        return None

    # Prefer the gene the patient actually named — "V600E" alone matches variants
    # in several genes.
    chosen = nodes[0]
    if gene_hint:
        for node in nodes:
            if ((node.get("feature") or {}).get("name") or "").upper() == gene_hint.upper():
                chosen = node
                break

    profile_id = chosen.get("singleVariantMolecularProfileId")
    if not isinstance(profile_id, int):
        return None

    try:
        r = await client.post(
            _CIVIC, json={"query": _CIVIC_PROFILE_QUERY, "variables": {"id": profile_id}}
        )
        if r.status_code != 200:
            return None
        profile = ((r.json() or {}).get("data") or {}).get("molecularProfile") or {}
    except (httpx.RequestError, ValueError):
        return None
    if not profile:
        return None

    associations = []
    n_prognostic_dropped = 0
    for item in ((profile.get("evidenceItems") or {}).get("nodes") or []):
        if (item.get("evidenceType") or "").upper() == "PROGNOSTIC":
            n_prognostic_dropped += 1
            continue
        disease = ((item.get("disease") or {}) or {}).get("name") or "(disease not stated)"
        therapies = ", ".join(
            (t or {}).get("name", "") for t in (item.get("therapies") or []) if t
        )
        pmid = ((item.get("source") or {}) or {}).get("citationId") or ""
        associations.append(
            {
                "disease": disease,
                "type": (item.get("evidenceType") or "").replace("_", " ").lower(),
                "direction": (item.get("evidenceDirection") or "").replace("_", " ").lower(),
                "significance": (item.get("significance") or "")
                .replace("SENSITIVITYRESPONSE", "sensitivity/response")
                .replace("_", " ")
                .lower(),
                "therapies": therapies,
                "level": item.get("evidenceLevel") or "",
                "pmid": str(pmid) if pmid else "",
            }
        )

    description = re.sub(r"\s+", " ", (profile.get("description") or "")).strip()
    description = _strip_prognosis(description)
    name = (profile.get("name") or variant).strip()
    return {
        "name": name,
        "gene": ((chosen.get("feature") or {}).get("name") or "").strip(),
        "description": description[:1200],
        "associations": associations,
        "n_prognostic_dropped": n_prognostic_dropped,
        "url": f"https://civicdb.org/molecular-profiles/{profile_id}/summary",
        "source_id": f"civic:mp:{profile_id}",
    }


# --------------------------------------------------------------------------- #
# Tool entry point
# --------------------------------------------------------------------------- #

_MAX_TERMS = 6
_MAX_GENES = 4


async def run(args: dict, ctx) -> str:
    terms = [str(t).strip() for t in (args.get("terms") or []) if str(t).strip()]
    genes = [str(g).strip() for g in (args.get("genes") or []) if str(g).strip()]
    variant = str(args.get("variant") or "").strip()

    if not (terms or genes or variant):
        return (
            "Error: give at least one of `terms`, `genes`, or `variant`. Pull them "
            "out of what the patient actually wrote — do not invent a gene or a "
            "marker they did not mention."
        )

    # A variant sent to the dictionary comes back as a DRUG (see module docstring).
    misrouted = [t for t in terms if _looks_like_a_variant(t)]
    terms = [t for t in terms if not _looks_like_a_variant(t)]
    if misrouted and not variant:
        variant = misrouted[0]

    terms, genes = terms[:_MAX_TERMS], genes[:_MAX_GENES]

    memo_key = f"{sorted(terms)}|{sorted(genes)}|{variant}"
    if getattr(ctx, "already_asked", None) and ctx.already_asked("genomics_lookup", memo_key):
        return (
            "You already looked these up this turn and these sources are "
            "deterministic — the answer is identical. Write your answer from what "
            "you have. If something was missing, say so plainly to the patient "
            "rather than searching again."
        )

    blocks: list[str] = []
    misses: list[str] = []

    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers={"User-Agent": "PatientGuide/1.0 (patient education)"}
    ) as client:
        term_results, gene_symbols, variant_result = await asyncio.gather(
            asyncio.gather(*(_nci_term(client, t) for t in terms)) if terms else _empty(),
            asyncio.gather(*(_official_symbol(client, g) for g in genes)) if genes else _empty(),
            _civic_variant(client, variant, genes[0] if genes else "") if variant else _none(),
        )

        gene_results = (
            await asyncio.gather(*(_gene_description(client, s) for s in gene_symbols))
            if gene_symbols
            else []
        )

    for term, result in zip(terms, term_results):
        if result is None:
            misses.append(f"no dictionary entry for '{term}'")
            continue
        entry = ctx.ledger.add(
            source_kind="patient_source",
            source_id=result["source_id"],
            title=f"{result['term']} — NCI Dictionary",
            journal="National Cancer Institute",
            url=result["url"],
            summary=result["definition"],
            article_type="Reference definition",
            retrieved_by=ctx.specialist_id,
        )
        clinician_note = (
            "  NOTE: this definition is written for health professionals — put it "
            "into plain words before the patient reads it.\n"
            if result["audience"].lower().startswith("health")
            else ""
        )
        blocks.append(
            f"[{entry.label}] DEFINITION — {result['term']}\n"
            f"  {result['definition']}\n"
            f"{clinician_note}"
            f"  Source: National Cancer Institute dictionary   {result['url']}\n"
        )

    for symbol, result in zip(gene_symbols, gene_results):
        if result is None:
            misses.append(
                f"MedlinePlus Genetics has no page for '{symbol}' (it does not cover "
                "every gene — try pubmed_search_and_fetch or patient_source_search "
                "for this one, and do not fill the gap from memory)"
            )
            continue
        entry = ctx.ledger.add(
            source_kind="patient_source",
            source_id=result["source_id"],
            title=f"{result['symbol']} gene — MedlinePlus Genetics",
            journal="MedlinePlus Genetics (U.S. National Library of Medicine)",
            year=(result["reviewed"] or "")[:4],
            url=result["url"],
            summary=result["function"],
            article_type="Gene reference",
            retrieved_by=ctx.specialist_id,
        )
        blocks.append(
            f"[{entry.label}] GENE — {result['symbol']}"
            + (f" ({result['name']})" if result["name"] else "")
            + "\n"
            + f"  What this gene normally does: {result['function']}\n"
            + (
                f"  MedlinePlus links this gene to: {', '.join(result['conditions'])}. "
                "CAUTION: that list is about INHERITED conditions. If the patient's "
                "result came from tumour testing, these conditions are probably NOT "
                "what their report is about — do not present them as their diagnosis.\n"
                if result["conditions"]
                else ""
            )
            + (f"  Page last reviewed: {result['reviewed']}\n" if result["reviewed"] else "")
            + f"  Source: MedlinePlus Genetics   {result['url']}\n"
        )

    if variant:
        if variant_result is None:
            misses.append(
                f"CIViC has no curated entry for '{variant}' (it does not cover "
                "everything — HRD and many rare variants are absent). Say plainly "
                "that you could not find variant-specific information rather than "
                "describing the variant from memory"
            )
        else:
            entry = ctx.ledger.add(
                source_kind="patient_source",
                source_id=variant_result["source_id"],
                title=f"{variant_result['name']} — CIViC clinical interpretations",
                journal="CIViC (Clinical Interpretation of Variants in Cancer)",
                url=variant_result["url"],
                summary=variant_result["description"],
                article_type="Variant interpretation",
                retrieved_by=ctx.specialist_id,
            )
            lines = [
                f"[{entry.label}] VARIANT — {variant_result['name']}"
                + (f"  (gene: {variant_result['gene']})" if variant_result["gene"] else ""),
            ]
            if variant_result["description"]:
                lines.append(f"  What is known about it: {variant_result['description']}")
            for assoc in variant_result["associations"]:
                bits = [f"  · In {assoc['disease']}"]
                if assoc["type"]:
                    bits.append(f"{assoc['type']} evidence")
                if assoc["significance"]:
                    bits.append(assoc["significance"])
                if assoc["direction"]:
                    bits.append(f"({assoc['direction']})")
                if assoc["therapies"]:
                    bits.append(f"— therapies studied: {assoc['therapies']}")
                if assoc["level"]:
                    bits.append(f"[CIViC evidence level {assoc['level']}]")
                if assoc["pmid"]:
                    bits.append(f"PMID {assoc['pmid']}")
                lines.append(" ".join(bits))
            if variant_result.get("n_prognostic_dropped"):
                lines.append(
                    f"  ({variant_result['n_prognostic_dropped']} prognostic findings "
                    "about this alteration were withheld from you deliberately — "
                    "outlook and survival are not yours to relay. You MAY tell the "
                    "patient that research on outlook exists and that their "
                    "oncologist is the person to discuss it with, which is true and "
                    "more useful than silence. You may NOT characterise what it says.)"
                )
            lines.append(
                "  HOW TO READ THE ABOVE: each line is tied to a SPECIFIC DISEASE — "
                "the same change behaves differently in different cancers, so never "
                "quote one without its disease. 'Evidence level A' means the "
                "association is well studied in the literature; it says nothing "
                "about this patient. A direction of 'does not support' or a "
                "significance of 'resistance' means the treatment FAILED. None of "
                "this is a treatment recommendation or a statement of eligibility.\n"
            )
            blocks.append("\n".join(lines))

    if not blocks:
        return (
            "genomics_lookup found nothing for: "
            + "; ".join(misses)
            + ".\nThis is a real miss, not a reassurance — these sources do not cover "
            "everything. Do NOT explain the term or gene from memory. Either try "
            "`pubmed_search_and_fetch` / `patient_source_search`, or tell the patient "
            "honestly that you could not find a trustworthy plain-language source and "
            "point them to their care team or a genetic counsellor."
        )

    header = [
        "Plain-language reference for report vocabulary. These entries define what "
        "terms MEAN IN GENERAL.",
        "They do NOT tell you whether this patient's finding is inherited (germline) "
        "or acquired (somatic) — nothing here can establish that, only the patient's "
        "own test can. They do not give a prognosis and they are not eligibility.",
        "",
    ]
    if misses:
        header.insert(2, "NOT FOUND (say so plainly; do not fill from memory): " + "; ".join(misses))

    return "\n".join(header + blocks)


async def _empty() -> list:
    return []


async def _none() -> None:
    return None

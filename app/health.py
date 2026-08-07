"""Backend health: which LLM and search providers are configured, and do they work.

Why this exists: the app's worst failure mode is silent. If the search backend's
key lapses, every specialist retrieves nothing, the citation gate does its job,
and every agent honestly abstains — so a patient gets "I couldn't find anything"
for every question and nothing anywhere says the credential is dead. That looks
like a bad product rather than an expired token.

`snapshot()` is free and reports what is *configured*. `probe()` actually calls
each backend and reports what *works*; it costs a few tokens and one search
query, so it is opt-in rather than something an uptime pinger triggers by
accident.
"""
import asyncio
import logging
import os

import httpx

from app.config import MODEL_NAME, PROVIDER

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 20.0


def _llm_key_for(provider: str) -> str | None:
    if provider == "openrouter":
        return os.getenv("OPENROUTER_API_KEY") or os.getenv("OPEN_ROUTER")
    if provider == "openai":
        return os.getenv("OPENAI_API_KEY")
    return os.getenv("GEMINI_API_KEY")


def snapshot() -> dict:
    """Configuration-only view. No network, no cost."""
    search = {
        "perplexity": bool(os.getenv("PERPLEXITY_API_KEY") or os.getenv("PPLX_API_KEY")),
        "brave": bool(
            os.getenv("Brave_API")
            or os.getenv("BRAVE_API_KEY")
            or os.getenv("BRAVE_SEARCH_API_KEY")
        ),
    }
    return {
        "llm": {
            "provider": PROVIDER,
            "model": MODEL_NAME,
            "key_configured": bool(_llm_key_for(PROVIDER)),
        },
        "search": {
            **search,
            # Keyless retrieval still works without either: PubMed, Europe PMC,
            # Semantic Scholar and ClinicalTrials.gov need no credentials. The
            # patient-facing plain-language sources are what go dark.
            "any_configured": any(search.values()),
        },
        "keyless_sources": [
            "pubmed",
            "europe_pmc",
            "semantic_scholar",
            "clinicaltrials_gov",
            "nci_dictionary",
            "medlineplus_genetics",
            "civic",
        ],
    }


async def _probe_llm() -> dict:
    from app import llm

    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                llm.chat,
                [{"role": "user", "content": "Reply with the single word: ok"}],
            ),
            timeout=PROBE_TIMEOUT,
        )
        text = (resp.choices[0].message.content or "").strip()
        return {"ok": bool(text), "detail": text[:60] or "empty response"}
    except asyncio.TimeoutError:
        return {"ok": False, "detail": f"timed out after {PROBE_TIMEOUT:.0f}s"}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:200]}"}


async def _probe_perplexity() -> dict:
    key = os.getenv("PERPLEXITY_API_KEY") or os.getenv("PPLX_API_KEY")
    if not key:
        return {"ok": False, "detail": "no key configured"}
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as c:
            r = await c.post(
                "https://api.perplexity.ai/search",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"query": "heart failure diet"},
            )
        if r.status_code == 200:
            return {"ok": True, "detail": f"{len(r.json().get('results') or [])} results"}
        return {"ok": False, "detail": f"HTTP {r.status_code}: {r.text[:120]}"}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:200]}"}


async def _probe_brave() -> dict:
    key = (
        os.getenv("Brave_API")
        or os.getenv("BRAVE_API_KEY")
        or os.getenv("BRAVE_SEARCH_API_KEY")
    )
    if not key:
        return {"ok": False, "detail": "no key configured"}
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as c:
            r = await c.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": "heart failure diet", "count": 1},
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
            )
        if r.status_code == 200:
            return {"ok": True, "detail": "ok"}
        # Brave signals a bad token with 422, not 401.
        return {"ok": False, "detail": f"HTTP {r.status_code}: {r.text[:120]}"}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:200]}"}


async def _probe_genomic_sources() -> dict:
    """Liveness for the three keyless genomics sources.

    These need no key, so they cannot expire — but they can move, and the failure
    mode is the one this module exists for: every genomics answer degrades to
    "I couldn't find anything" with nothing saying why. Each check asserts
    SEMANTICS, not just a 200, because all three return 200 with an empty body for
    a query they don't understand.
    """
    import httpx

    async def one(name: str, check) -> tuple[str, dict]:
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
                return name, await check(client)
        except Exception as e:
            return name, {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:120]}"}

    async def nci(client):
        r = await client.get(f"{_NCI_GLOSSARY}/HealthCheck/status")
        body = r.text.strip().strip('"')
        return {"ok": r.status_code == 200 and "alive" in body.lower(), "detail": body[:60]}

    async def medlineplus(client):
        r = await client.get("https://medlineplus.gov/download/genetics/gene/braf.json")
        symbol = (r.json() or {}).get("gene-symbol") if r.status_code == 200 else None
        return {"ok": symbol == "BRAF", "detail": f"gene-symbol={symbol!r}"}

    async def civic(client):
        r = await client.post(
            "https://civicdb.org/api/graphql",
            json={"query": '{ variants(name:"V600E", first:1){ nodes{ id name } } }'},
        )
        nodes = (((r.json() or {}).get("data") or {}).get("variants") or {}).get("nodes") or []
        return {"ok": bool(nodes), "detail": f"{len(nodes)} variant(s) for V600E"}

    results = dict(
        await asyncio.gather(
            one("nci_dictionary", nci),
            one("medlineplus_genetics", medlineplus),
            one("civic", civic),
        )
    )
    # The dictionary is the load-bearing one: it carries the patient-audience
    # definitions. MedlinePlus and CIViC each cover only part of the ground.
    results["ok"] = results["nci_dictionary"]["ok"]
    return results


_NCI_GLOSSARY = "https://webapis.cancer.gov/glossary/v1"


async def probe() -> dict:
    """Live-check every backend. Costs a few tokens and two search queries."""
    llm_res, pplx, brave, genomic = await asyncio.gather(
        _probe_llm(), _probe_perplexity(), _probe_brave(), _probe_genomic_sources()
    )
    snap = snapshot()
    search_ok = pplx["ok"] or brave["ok"]
    return {
        "ok": llm_res["ok"] and search_ok,
        "llm": {**snap["llm"], **llm_res},
        "search": {
            "perplexity": pplx,
            "brave": brave,
            "ok": search_ok,
            # One working backend means no redundancy: if it lapses, every
            # specialist abstains and the patient sees "I couldn't find anything".
            "redundant": pplx["ok"] and brave["ok"],
        },
        # Keyless, so not part of top-level `ok` — a genomics outage degrades one
        # specialist rather than the app.
        "genomic_sources": genomic,
        "keyless_sources": snap["keyless_sources"],
    }


def log_startup_state() -> None:
    """One line at boot so a dead credential is visible without a probe."""
    snap = snapshot()
    log.info(
        "LLM provider=%s model=%s key=%s | search: perplexity=%s brave=%s",
        snap["llm"]["provider"],
        snap["llm"]["model"],
        "set" if snap["llm"]["key_configured"] else "MISSING",
        "set" if snap["search"]["perplexity"] else "missing",
        "set" if snap["search"]["brave"] else "missing",
    )
    if not snap["llm"]["key_configured"]:
        log.warning("No API key for provider %s — every request will fail.", snap["llm"]["provider"])
    if not snap["search"]["any_configured"]:
        log.warning(
            "No search backend configured (PERPLEXITY_API_KEY / Brave_API). The helpers "
            "can still reach PubMed and ClinicalTrials.gov, but the patient-facing "
            "plain-language sources are unavailable and most everyday questions will abstain."
        )
    elif not (snap["search"]["perplexity"] and snap["search"]["brave"]):
        log.info(
            "Only one search backend is configured — no fallback if it fails. "
            "Check GET /api/health?probe=1 to confirm it actually works."
        )

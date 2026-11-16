"""Europe PMC client — id resolution + JATS full text for OA papers.

Search:   https://www.ebi.ac.uk/europepmc/webservices/rest/search
Fulltext: https://www.ebi.ac.uk/europepmc/webservices/rest/{PMCID}/fullTextXML

The JATS path is preferred over PDF: sections arrive structured —
no GROBID needed on this branch (PLAN A1 source priority).
"""

from __future__ import annotations

from typing import Any

import httpx

from ..evidence import Evidence
from . import ClientError, get_bytes, get_json, get_text

API = "https://www.ebi.ac.uk/europepmc/webservices/rest"


def resolve_ids(
    *,
    doi: str | None = None,
    pmid: str | None = None,
    pmcid: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[dict[str, Any], Evidence]:
    """One of doi/pmid/pmcid -> the others + OA/in-EPMC flags."""
    if pmcid:
        query = f"PMCID:{pmcid.lstrip('PMC')}"
    elif pmid:
        query = f"EXT_ID:{pmid} AND SRC:MED"
    elif doi:
        query = f'DOI:"{doi}"'
    else:
        raise ClientError("resolve_ids needs doi, pmid, or pmcid")
    data, ev = get_json(
        f"{API}/search",
        params={
            "query": query, "format": "json", "resultType": "core",
        },
        source="europepmc", transport=transport,
    )
    results = (data.get("resultList") or {}).get("result") or []
    hit = results[0] if results else {}
    out = {
        "pmid": hit.get("pmid"),
        "pmcid": hit.get("pmcid"),
        "doi": hit.get("doi"),
        "in_epmc": hit.get("inEPMC") == "y",
        "is_open_access": hit.get("isOpenAccess") == "y",
        "full_text_in_pmc": (
            hit.get("hasFullText") == "y"
            or bool(hit.get("pmcid"))
        ),
    }
    ev.summary = f"europepmc resolve {query} -> pmcid={out['pmcid']}"
    ev.payload = {"hit_count": len(results)}
    return out, ev


def fetch_jats(
    pmcid: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, Evidence]:
    """PMCID -> JATS XML full text + Evidence (OA subset only)."""
    pmc = pmcid if pmcid.startswith("PMC") else f"PMC{pmcid}"
    xml, ev = get_text(
        f"{API}/{pmc}/fullTextXML",
        source="europepmc", transport=transport,
    )
    if "DOCTYPE" in xml and "<article" not in xml[:2000]:
        raise ClientError(
            f"{pmc}: fullTextXML returned non-article (not in OA subset?)"
        )
    ev.summary = f"europepmc JATS {pmc}"
    ev.payload = {"bytes": len(xml)}
    return xml, ev


def fetch_supplementary(
    pmcid: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[bytes, Evidence]:
    """PMCID -> ZIP of all supplementary files (OA subset only)."""
    pmc = pmcid if pmcid.startswith("PMC") else f"PMC{pmcid}"
    data, ev = get_bytes(
        f"{API}/{pmc}/supplementaryFiles",
        # europepmc streams these archives slowly — multi-MB ZIPs
        # take minutes; the default 120s read timeout truncates them.
        timeout=300.0,
        source="europepmc", transport=transport,
    )
    ev.summary = f"europepmc supplements {pmc}"
    ev.payload = {"bytes": len(data)}
    return data, ev

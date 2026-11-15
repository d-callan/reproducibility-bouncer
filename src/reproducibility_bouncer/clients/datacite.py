"""DataCite probe — DOI-level existence/type routing for repos that
don't have a dedicated client (figshare, dataverse, generic DOIs).
Crossref mints DOIs too but DataCite covers the data-DOI set."""

from __future__ import annotations

from typing import Any

import httpx

from ..evidence import Evidence
from . import ClientError, get_json

API = "https://api.datacite.org"


def probe(
    doi: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[dict[str, Any] | None, Evidence]:
    try:
        rec, ev = get_json(
            f"{API}/dois/{doi.lower()}",
            source="datacite", transport=transport,
        )
    except ClientError as e:
        if "404" in str(e):
            ev = Evidence(source="datacite", endpoint=f"{API}/dois/{doi}",
                          summary=f"datacite {doi}: not found")
            return None, ev
        raise
    a = (rec.get("data") or {}).get("attributes") or {}
    ev.summary = f"datacite {doi}: {a.get('publisher', '')}"
    return {
        "repo": "datacite",
        "object_id": doi,
        "title": ((a.get("titles") or [{}])[0].get("title") or ""),
        "publisher": a.get("publisher", ""),
        "types": (a.get("types") or {}).get("resourceTypeGeneral", ""),
        "version": a.get("version", ""),
        "publication_year": a.get("publicationYear"),
        "public": a.get("state") == "findable",
        "url": a.get("url", f"https://doi.org/{doi}"),
    }, ev

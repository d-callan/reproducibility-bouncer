"""Dryad probe — dataset DOIs of form 10.5061/dryad.*."""

from __future__ import annotations

from typing import Any

import httpx

from ..evidence import Evidence
from . import ClientError, get_json

API = "https://datadryad.org/api/v2"


def probe(
    doi: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[dict[str, Any] | None, Evidence]:
    import urllib.parse
    enc = urllib.parse.quote(f"doi:{doi}", safe="")
    try:
        rec, ev = get_json(
            f"{API}/datasets/{enc}",
            source="dryad", transport=transport,
        )
    except ClientError as e:
        if "404" in str(e):
            ev = Evidence(source="dryad", endpoint=f"{API}/datasets/{enc}",
                          summary=f"dryad {doi}: not found")
            return None, ev
        raise
    ev.summary = f"dryad {doi}: {rec.get('title', '')[:60]}"
    return {
        "repo": "dryad",
        "object_id": doi,
        "title": rec.get("title", ""),
        "version": str(rec.get("versionNumber") or ""),
        "license": rec.get("license"),
        "publication_status": rec.get("publicationStatus"),
        "public": rec.get("publicationStatus") == "published",
        "url": f"https://doi.org/{doi}",
    }, ev

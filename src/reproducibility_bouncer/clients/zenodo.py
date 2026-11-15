"""Zenodo probe — records are DOI-minted; versioned vs concept DOI
matters for binding (A4): a concept DOI resolves to *latest* and is a
mutability flag, a version DOI is a pinned instance.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from ..evidence import Evidence
from . import ClientError, get_json

API = "https://zenodo.org/api"


def _recid(doi_or_id: str) -> str:
    if m := re.search(r"zenodo\.(\d+)", doi_or_id):
        return m.group(1)
    return doi_or_id.lstrip("zenodo:")


def probe(
    doi: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[dict[str, Any] | None, Evidence]:
    recid = _recid(doi)
    try:
        rec, ev = get_json(
            f"{API}/records/{recid}",
            source="zenodo", transport=transport,
        )
    except ClientError as e:
        if "404" in str(e):
            ev = Evidence(source="zenodo",
                          endpoint=f"{API}/records/{recid}",
                          summary=f"zenodo {doi}: not found")
            return None, ev
        raise
    meta = rec.get("metadata") or {}
    files = rec.get("files") or []
    ev.summary = f"zenodo {recid}: {meta.get('title', '')[:60]}"
    return {
        "repo": "zenodo",
        "object_id": f"zenodo.{recid}",
        "title": meta.get("title", ""),
        "doi": rec.get("doi"),
        "concept_doi": (rec.get("conceptdoi")
                        or meta.get("conceptdoi")),
        "is_concept": ".org/record" in doi or False,
        "version": str(meta.get("version") or rec.get("revision") or ""),
        "publication_date": meta.get("publication_date"),
        "license": (meta.get("license") or {}).get("id"),
        "n_files": len(files),
        "total_bytes": sum(
            f.get("size", 0) for f in files if isinstance(f, dict)
        ),
        "public": rec.get("metadata", {}).get("access_right", "open")
        == "open",
        "url": f"https://doi.org/10.5281/zenodo.{recid}",
    }, ev

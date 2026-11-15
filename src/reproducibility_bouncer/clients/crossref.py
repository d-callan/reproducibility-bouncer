"""Crossref REST client — work metadata by DOI.

https://api.crossref.org/works/{doi}
"""

from __future__ import annotations

from typing import Any

import httpx

from ..evidence import Evidence
from . import ClientError, get_json

API = "https://api.crossref.org"


def get_work(
    doi: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[dict[str, Any], Evidence]:
    """DOI -> normalized work metadata + Evidence."""
    data, ev = get_json(
        f"{API}/works/{doi}", source="crossref", transport=transport,
    )
    msg = data.get("message") or {}
    title = (msg.get("title") or [""])[0]
    authors = [
        " ".join(p for p in (a.get("given"), a.get("family")) if p)
        for a in msg.get("author", [])
    ]
    year = None
    for key in ("published-print", "published-online", "issued"):
        parts = (msg.get(key) or {}).get("date-parts") or []
        if parts and parts[0]:
            year = parts[0][0]
            break
    out = {
        "doi": msg.get("DOI", doi),
        "title": title,
        "authors": authors,
        "journal": (msg.get("container-title") or [""])[0],
        "year": year,
        "type": msg.get("type", ""),
        "publisher": msg.get("publisher", ""),
        "resource_url": (msg.get("resource") or {}).get(
            "primary", {}
        ).get("URL"),
        "relations": msg.get("relation") or {},
        "links": msg.get("link") or [],
    }
    ev.summary = f"crossref work {doi}"
    ev.payload = {"type": out["type"], "year": year}
    return out, ev

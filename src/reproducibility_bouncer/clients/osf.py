"""OSF probe — osf.io/{5-char id} nodes."""

from __future__ import annotations

from typing import Any

import httpx

from ..evidence import Evidence
from . import ClientError, get_json

API = "https://api.osf.io/v2"


def probe(
    node_id: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[dict[str, Any] | None, Evidence]:
    try:
        rec, ev = get_json(
            f"{API}/nodes/{node_id}/",
            source="osf", transport=transport,
        )
    except ClientError as e:
        if "404" in str(e) or "410" in str(e):
            ev = Evidence(source="osf",
                          endpoint=f"{API}/nodes/{node_id}/",
                          summary=f"osf {node_id}: not found")
            return None, ev
        raise
    attrs = (rec.get("data") or {}).get("attributes") or {}
    ev.summary = f"osf {node_id}: {attrs.get('title', '')[:60]}"
    return {
        "repo": "osf",
        "object_id": node_id,
        "title": attrs.get("title", ""),
        "public": bool(attrs.get("public")),
        "registration": bool(attrs.get("registration")),
        "modified": attrs.get("date_modified"),
        "url": f"https://osf.io/{node_id}/",
    }, ev

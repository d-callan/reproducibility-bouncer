"""Unpaywall client — OA status + best location by DOI.

https://api.unpaywall.org/v2/{doi}?email=...
Requires an email — UNPAYWALL_EMAIL env or the `email` arg.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ..evidence import Evidence
from . import ClientError, get_json

API = "https://api.unpaywall.org/v2"


def get_oa(
    doi: str,
    *,
    email: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[dict[str, Any], Evidence]:
    """DOI -> OA status record + Evidence."""
    email = email or os.environ.get("UNPAYWALL_EMAIL")
    if not email:
        raise ClientError(
            "Unpaywall requires an email: set UNPAYWALL_EMAIL."
        )
    data, ev = get_json(
        f"{API}/{doi}", params={"email": email}, source="unpaywall",
        transport=transport,
    )
    best = data.get("best_oa_location") or {}
    out = {
        "doi": data.get("DOI", doi),
        "is_oa": bool(data.get("is_oa")),
        "oa_status": data.get("oa_status", ""),
        "genre": data.get("genre", ""),
        "best_oa_location": {
            "host_type": best.get("host_type"),
            "url_for_pdf": best.get("url_for_pdf"),
            "url_for_landing_page": best.get("url_for_landing_page"),
            "version": best.get("version"),
            "license": best.get("license"),
            "pmh_id": best.get("pmh_id"),
        },
        "oa_locations": [
            {
                "host_type": loc.get("host_type"),
                "url_for_pdf": loc.get("url_for_pdf"),
                "url_for_landing_page": loc.get("url_for_landing_page"),
                "version": loc.get("version"),
                "pmh_id": loc.get("pmh_id"),
            }
            for loc in data.get("oa_locations") or []
        ],
    }
    ev.summary = f"unpaywall oa {doi} ({out['oa_status'] or 'closed'})"
    ev.payload = {
        "is_oa": out["is_oa"], "oa_status": out["oa_status"],
    }
    return out, ev

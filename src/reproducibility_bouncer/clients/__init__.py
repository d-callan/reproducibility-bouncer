"""Identifier → repo API clients (A3 dispatch registry lives here later).

Every fetch returns ``(payload, Evidence)`` — endpoint provenance is
recorded for each external call (specs/data-model.md §Evidence).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ..evidence import Evidence

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
UA = "reproducibility-bouncer/0.1 (+https://github.com/d-callan/reproducibility-bouncer)"


class ClientError(RuntimeError):
    pass


def _get(
    url: str,
    *,
    params: dict[str, Any] | None,
    source: str,
    timeout: float,
    transport: httpx.BaseTransport | None,
    headers: dict[str, str] | None = None,
) -> tuple[httpx.Response, Evidence]:
    try:
        with httpx.Client(transport=transport) as c:
            resp = c.get(
                url, params=params, timeout=timeout,
                headers={"User-Agent": UA, **(headers or {})},
                follow_redirects=True,
            )
    except httpx.HTTPError as e:
        raise ClientError(f"{source} request failed: {e}") from e
    if resp.status_code != 200:
        raise ClientError(
            f"{source} {resp.status_code} for {url}: {resp.text[:300]}"
        )
    ev = Evidence(
        source=source, endpoint=str(resp.request.url),
        summary=f"GET {url}",
    )
    return resp, ev


def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    source: str,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[dict[str, Any], Evidence]:
    """GET url -> (parsed json, Evidence). transport injectable for tests."""
    resp, ev = _get(
        url, params=params, source=source, timeout=timeout,
        transport=transport, headers=headers,
    )
    return resp.json(), ev


def get_text(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    source: str,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, Evidence]:
    """GET url -> (raw text, Evidence)."""
    resp, ev = _get(
        url, params=params, source=source, timeout=timeout,
        transport=transport,
    )
    return resp.text, ev


def get_bytes(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    source: str,
    timeout: float = 120.0,
    transport: httpx.BaseTransport | None = None,
) -> tuple[bytes, Evidence]:
    """GET url -> (raw bytes, Evidence) — PDFs and other artifacts."""
    resp, ev = _get(
        url, params=params, source=source, timeout=timeout,
        transport=transport,
    )
    ev.summary += f" ({resp.headers.get('content-type', '?')})"
    return resp.content, ev

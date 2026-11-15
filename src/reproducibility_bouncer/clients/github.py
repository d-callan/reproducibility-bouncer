"""GitHub probe — repo existence, license, archive state, activity.

GITHUB_TOKEN raises the rate limit. `archived` and a personal-vs-org
distinction feed the mutability flags; floating-branch references
(`HEAD`/`main` with no tag) are A4's problem, existence is ours.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ..evidence import Evidence
from . import ClientError, get_json

API = "https://api.github.com"


def probe(
    repo: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[dict[str, Any] | None, Evidence]:
    """'owner/name' -> record or None."""
    repo = repo.removesuffix(".git")
    headers: dict[str, str] = {}
    if tok := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {tok}"
    try:
        rec, ev = get_json(
            f"{API}/repos/{repo}", params=None, source="github",
            headers=headers or None, transport=transport,
        )
    except ClientError as e:
        if "404" in str(e):
            ev = Evidence(source="github",
                          endpoint=f"{API}/repos/{repo}",
                          summary=f"github {repo}: not found")
            return None, ev
        raise
    ev.summary = f"github {repo}: stars={rec.get('stargazers_count')}"
    return {
        "repo": "github",
        "object_id": repo,
        "full_name": rec.get("full_name", repo),
        "default_branch": rec.get("default_branch"),
        "license": (rec.get("license") or {}).get("spdx_id"),
        "archived": bool(rec.get("archived")),
        "is_org": (rec.get("owner") or {}).get("type") == "Organization",
        "pushed_at": rec.get("pushed_at"),
        "has_readme": None,  # filled by readme probe when needed
        "public": not rec.get("private", False),
        "url": rec.get("html_url", f"https://github.com/{repo}"),
    }, ev

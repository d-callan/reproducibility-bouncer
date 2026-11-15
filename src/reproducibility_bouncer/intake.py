"""A0 — intake classification.

Deterministic regex hints first (s0_classify discipline); JEV `choice`
is reserved for genuinely ambiguous inputs and isn't used at M0.
Refusals: non-paper URLs, unsupported formats, encrypted PDFs — better
to refuse than audit garbage.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path

from pydantic import BaseModel


class InputKind(str, Enum):
    doi = "doi"
    pmid = "pmid"
    pmcid = "pmcid"
    arxiv = "arxiv"
    url = "url"
    local_file = "local_file"
    unknown = "unknown"


class Intake(BaseModel):
    raw: str
    kind: InputKind
    normalized: str = ""
    draft: bool = False
    notes: list[str] = []


_DOI = re.compile(r"^10\.\d{4,9}/\S+$", re.I)
_PMID = re.compile(r"^\d{6,9}$")
_PMCID = re.compile(r"^PMC\d+$", re.I)
_ARXIV = re.compile(r"^(\d{4}\.\d{4,5}|[a-z\-]+/\d{7})(v\d+)?$", re.I)
_DOI_IN_URL = re.compile(r"(10\.\d{4,9}/[^\s\"'<>]+)")
_PMC_IN_URL = re.compile(r"(PMC\d{6,})", re.I)
_PMID_IN_URL = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)")
_ARXIV_IN_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/([^\s?#]+)")

_PREFIXES = (
    "doi:", "pmid:", "pmcid:", "arxiv:", "biorxiv:", "medrxiv:",
)


def classify(raw: str, *, draft: bool = False) -> Intake:
    """Classify a CLI input string. Never raises — unknown is a value."""
    s = raw.strip()
    out = Intake(raw=raw, kind=InputKind.unknown)

    # explicit prefixes first
    low = s.lower()
    for p in _PREFIXES:
        if low.startswith(p):
            val = s[len(p):].strip()
            kind = {
                "doi:": InputKind.doi,
                "biorxiv:": InputKind.doi,
                "medrxiv:": InputKind.doi,
                "pmid:": InputKind.pmid,
                "pmcid:": InputKind.pmcid,
                "arxiv:": InputKind.arxiv,
            }[p]
            out.kind, out.normalized = kind, val
            return out

    if draft or Path(s).is_file():
        out.kind = InputKind.local_file
        out.normalized = str(Path(s).expanduser().resolve())
        out.draft = True
        return out

    if _DOI.match(s):
        out.kind, out.normalized = InputKind.doi, s.rstrip(".,;")
        return out
    if _PMCID.match(s):
        out.kind, out.normalized = InputKind.pmcid, s.upper()
        return out
    if _PMID.match(s):
        out.kind, out.normalized = InputKind.pmid, s
        return out
    if _ARXIV.match(s):
        out.kind, out.normalized = InputKind.arxiv, s
        return out

    if s.startswith(("http://", "https://")):
        out.kind = InputKind.url
        if m := _DOI_IN_URL.search(s):
            # doi.org/x, publisher URLs, biorxiv content DOIs
            out.kind = InputKind.doi
            out.normalized = m.group(1).rstrip(".,;/)")
            out.notes.append(f"doi extracted from url {s}")
            return out
        if m := _PMID_IN_URL.search(s):
            out.kind = InputKind.pmid
            out.normalized = m.group(1)
            return out
        if m := _PMC_IN_URL.search(s):
            out.kind = InputKind.pmcid
            out.normalized = m.group(1).upper()
            return out
        if m := _ARXIV_IN_URL.search(s):
            out.kind = InputKind.arxiv
            out.normalized = m.group(1).removesuffix(".pdf")
            return out
        out.normalized = s
        out.notes.append(
            "url not recognized as a paper id — likely a refusal"
        )
        return out

    return out

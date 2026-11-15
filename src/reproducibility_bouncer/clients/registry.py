"""A3 identifier registry — map an identifier string -> repo client.

The dispatch table is the one A4's registry-mediated probes reuse:
each family gets a probe function and (where meaningful) a version
pattern for uniqueness checks. Repo coverage is incremental —
unregistered families route to DataCite for DOI-level probing or
stay unresolved with a note, never crash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from ..evidence import Evidence
from . import ClientError, datacite, dryad, ena, geo, github, osf, zenodo

Probe = Callable[..., tuple[dict[str, Any] | None, Evidence | None]]


@dataclass
class Family:
    """One identifier family -> probe dispatch + metadata."""
    name: str                      # e.g. "geo", "sra", "github"
    patterns: list[re.Pattern[str]]
    probe: Probe | None            # None = recognized but unprobed
    kind: str = "dataset"          # dataset|code|container|reference
    # version tail the family can carry ("GCA_….15" style)
    version_pattern: str = r""
    notes: str = ""


_VERSIONED = r"\d+(?:\.\d+)+"

FAMILIES: list[Family] = [
    Family(
        "geo", [re.compile(r"^GS[EM]\d+$")], geo.probe,
        version_pattern=r"\d+$",
    ),
    Family(
        "sra", [re.compile(
            r"^(?:[SED]R[RSXPD]|PRJ[NED][ABD]?)\d+$")],
        ena.probe,
    ),
    Family(
        "zenodo", [re.compile(r"^10\.5281/zenodo\.\d+$"),
                   re.compile(r"^zenodo\.\d+$")],
        zenodo.probe, kind="dataset",
        version_pattern=_VERSIONED,
    ),
    Family(
        "dryad", [re.compile(r"^10\.5061/dryad\.[\w-]+$")],
        dryad.probe,
    ),
    Family(
        "osf", [re.compile(r"^osf\.io/(\w{5})$"),
                re.compile(r"^[a-z0-9]{5}$")],
        osf.probe,
    ),
    Family(
        "github", [re.compile(
            r"^(?:github\.com/)?[\w.-]+/[\w.-]+$")],
        github.probe, kind="code",
    ),
    # recognized, not yet probed — record, don't guess
    Family("pride", [re.compile(r"^PXD\d+$")], None,
           notes="ProteomeXchange client not implemented"),
    Family("arrayexpress", [re.compile(r"^E-MTAB-\d+$")], None,
           notes="BioStudies client not implemented"),
    Family("assembly", [re.compile(r"^GC[AF]_\d{9}(?:\.\d+)?$")], None,
           kind="reference",
           version_pattern=r"\.\d+$",
           notes="NCBI datasets client not implemented"),
]


def route(identifier: str) -> tuple[Family, str]:
    """identifier -> (family, normalized). First match wins;
    unregistered -> datacite if it looks like a DOI, else a
    'generic' family with no probe."""
    s = identifier.strip().rstrip(".,;)")
    for fam in FAMILIES:
        for pat in fam.patterns:
            if m := pat.fullmatch(s) or pat.search(s):
                norm = s
                if fam.name == "github":
                    norm = s.removeprefix("github.com/")
                elif fam.name == "osf" and s.startswith("osf.io/"):
                    norm = s.split("/")[-1]
                elif fam.name == "zenodo" and s.startswith("zenodo."):
                    norm = f"10.5281/{s}"
                return fam, norm
    if re.match(r"^10\.\d{4,9}/\S+$", s):
        return Family("datacite", [], datacite.probe), s
    return Family("generic", [], None,
                  notes="unregistered identifier family"), s


# registry family -> Identifier.family (spec literal: accession|doi|url|path)
_IDENT_FAMILY = {
    "geo": "accession", "sra": "accession", "pride": "accession",
    "arrayexpress": "accession", "assembly": "accession",
    "zenodo": "doi", "dryad": "doi", "datacite": "doi",
    "github": "url", "osf": "url",
}


def ident_family(fam_name: str) -> str:
    return _IDENT_FAMILY.get(fam_name, "accession")


def probe(
    identifier: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[dict[str, Any] | None, Evidence | None, Family]:
    """identifier -> (record|None, Evidence|None, family)."""
    fam, norm = route(identifier)
    if fam.probe is None:
        return None, None, fam
    try:
        rec, ev = fam.probe(norm, transport=transport)
    except ClientError:
        raise
    return rec, ev, fam

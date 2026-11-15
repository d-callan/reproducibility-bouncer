"""A3 — artifact resolution.

Every declared/implied artifact -> a probed Artifact record.
Resolution outcomes are deterministic -> confidence 1.0. Existence
only — whether the reference picks out the *right* instance is A4.

Availability is a class: public | controlled-access | on-request |
absent (specs/data-model.md). Unresolved families are recorded with
a note, never guessed.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .auditrecord import (
    Artifact,
    ClarificationRequest,
    Document,
    Finding,
    Identifier,
    Resolved,
    UnitRef,
    _new_id,
)
from .clients import ClientError, registry
from .evidence import Evidence

log = logging.getLogger(__name__)

# (regex, ) — extraction over unit text, ordered: most specific first.
_ID_RES: list[re.Pattern[str]] = [
    re.compile(r"\b10\.5281/zenodo\.\d+\b"),
    re.compile(r"\b10\.5061/dryad\.[\w-]+\b", re.I),
    re.compile(r"(?:https?://)?github\.com/[\w.-]+/[\w.-]+", re.I),
    re.compile(r"(?:https?://)?osf\.io/\w{5}\b", re.I),
    re.compile(r"\bGC[AF]_\d{9}(?:\.\d+)?\b"),
    re.compile(r"\bE-MTAB-\d+\b"),
    re.compile(r"\bPXD\d+\b"),
    re.compile(r"\bGS[EM]\d+\b"),
    re.compile(r"\bPRJ[A-Z]{2}\d+\b"),
    re.compile(r"\b[SED]R[RSXP]\d+\b"),
]

_ON_REQUEST = re.compile(
    r"available\s+(?:from\s+(?:the\s+)?corresponding\s+author|"
    r"upon\s+(?:reasonable\s+)?request|on\s+request|"
    r"by\s+request)|reasonable\s+request",
    re.I,
)
_GH_STRIP = re.compile(r"/(?:tree|blob|releases?|commit)/.*$")


def extract_identifiers(
    doc: Document,
) -> dict[str, list[UnitRef]]:
    """normalized identifier -> all citing unit_refs."""
    found: dict[str, list[UnitRef]] = {}
    for si, sec in enumerate(doc.sections):
        for u in sec.units:
            ref = UnitRef(section=si, unit_index=u.index)
            for pat in _ID_RES:
                for m in pat.finditer(u.text):
                    raw = _GH_STRIP.sub(
                        "", m.group(0).rstrip(".,;/")
                    ).removeprefix("https://").removeprefix("http://")
                    norm = registry.route(raw)[1]
                    if norm not in found:
                        found[norm] = []
                    if ref not in found[norm]:
                        found[norm].append(ref)
    return found


def _availability(rec: dict[str, Any] | None, fam_name: str) -> str:
    if rec is None:
        return "absent"
    if not rec.get("public", True):
        return "controlled-access"
    return "public"


def resolve_artifacts(
    doc: Document,
    *,
    evidence: list[Evidence] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[list[Artifact], list[Finding], list[Evidence]]:
    """doc -> (artifacts, findings, evidence). `evidence` may be an
    existing list to append to (refs index into it)."""
    ev: list[Evidence] = evidence if evidence is not None else []
    artifacts: list[Artifact] = []
    findings: list[Finding] = []

    for ident, refs in extract_identifiers(doc).items():
        art = Artifact(
            id=_new_id("artifact", ident),
            identifier=Identifier(
                family=registry.ident_family(
                    registry.route(ident)[0].name
                ),
                raw=ident, normalized=ident,
            ),
            source_units=refs,
        )
        try:
            rec, e, fam = registry.probe(ident, transport=transport)
        except ClientError as err:
            findings.append(Finding(
                id=_new_id("finding", ident),
                axis="artifact_access", severity="warning",
                subject=[art.ref()], mechanism="deterministic",
                confidence=1.0,
                description=f"probe failed for {ident}: {err}",
            ))
            artifacts.append(art)
            continue
        if e is not None:
            art_ev_idx = len(ev)
            ev.append(e)
        if rec is not None:
            art.resolved = Resolved(
                repo=rec["repo"],
                object_id=rec.get("object_id", ident),
                version=str(rec.get("version") or ""),
                url=rec.get("url", ""),
                evidence=[f"evidence:{art_ev_idx}"],
            )
            art.kind = fam.kind
            art.license = rec.get("license")
            art.observed = {
                k: rec[k] for k in
                ("title", "n_runs", "n_files", "total_bytes",
                 "organism", "publication_status")
                if rec.get(k) is not None
            }
        art.availability = _availability(rec, fam.name)
        if fam.probe is None:
            findings.append(Finding(
                id=_new_id("finding", ident + ":family"),
                axis="artifact_access", severity="informational",
                subject=[art.ref()], mechanism="deterministic",
                confidence=1.0,
                description=f"{ident}: {fam.notes or 'unregistered family'}",
                coverage_caveat=True,
            ))
        elif rec is None:
            findings.append(Finding(
                id=_new_id("finding", ident + ":absent"),
                axis="artifact_access", severity="blocking",
                subject=[art.ref()], mechanism="deterministic",
                confidence=1.0,
                description=f"{ident} does not resolve at {fam.name} "
                            "— declared artifact absent",
                suggestion="deposit the artifact or fix the accession",
            ))
        else:
            # mutability: unarchived personal repo, concept DOI,
            # floating release links
            if (fam.name == "github" and not rec.get("archived")
                    and not rec.get("is_org")):
                art.mutability_flags.append(
                    "personal unarchived repo — versionless URL may change")
            if rec.get("concept_doi") and rec["concept_doi"] != rec.get("doi"):
                art.mutability_flags.append(
                    "concept DOI resolves to latest version")
            if not rec.get("license") and fam.kind == "code":
                art.mutability_flags.append("no license detected")
        artifacts.append(art)

    # "available on request" — a declared-but-unresolvable artifact class
    for si, sec in enumerate(doc.sections):
        for u in sec.units:
            if _ON_REQUEST.search(u.text):
                art = Artifact(
                    id=_new_id("artifact", f"onrequest:{si}:{u.index}"),
                    kind="dataset",
                    identifier=None,
                    availability="on-request",
                    source_units=[UnitRef(section=si, unit_index=u.index)],
                )
                artifacts.append(art)
                findings.append(Finding(
                    id=_new_id("finding", f"onreq:{si}:{u.index}"),
                    axis="artifact_access", severity="blocking",
                    subject=[art.ref()], mechanism="deterministic",
                    confidence=1.0,
                    description="'available on request' — no fetchable "
                                "identifier; A6 cannot retrieve inputs",
                    suggestion="deposit at a repository with a usable "
                               "access mechanism",
                ))
    return artifacts, findings, ev

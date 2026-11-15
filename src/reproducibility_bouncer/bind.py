"""A4 — specification binding.

Existence != identification. For each slot: does the declared value
pick out ONE concrete instance? Three layers per spec:

  generic_invariant — shape checks on the declared value
  registry_probe    — reuse the A3 identifier->client table
  judged            — JEV fallback for unregistered families

check_path is recorded per slot; the deterministic fraction lands in
audit.meta['binding_check_coverage'].
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .auditrecord import (
    AnalysisUnit,
    Artifact,
    BoundInstance,
    ClarificationRequest,
    Finding,
    Slot,
    _new_id,
)
from .clients import ClientError, registry
from .evidence import Evidence

log = logging.getLogger(__name__)

# version-ish tail: 1.34 / v2.7 / 2023-01 / .15 / commit-ish hex
# (must contain a letter — pure-digit runs are accession ids, not shas)
_VERSIONISH = re.compile(
    r"(?:v?\d+\.\d+|\.\d+|[-_ ]v?\d{4}[-_]\d{2}|"
    r"(?=[0-9a-f]*[a-f])[0-9a-f]{7,40})\b", re.I
)
_INPUT_SLOT_TYPES = {
    "dataset", "reference_genome", "versioned_identifier",
}


def _has_version(value: str) -> bool:
    return bool(_VERSIONISH.search(value))


def _is_unique(value: str) -> bool:
    """obvious ambiguity markers: 'or', 'x/y', ranges"""
    return not re.search(r"\b(or|vs?\.?)\b|[/…-]\s*\w", value, re.I)


def bind_slots(
    units: list[AnalysisUnit],
    slots: list[Slot],
    artifacts: list[Artifact],
    *,
    jev,
    doc_sections=None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[list[Finding], list[ClarificationRequest], dict[str, Any]]:
    """Bind each slot to a unique instance. Returns findings,
    clarification requests, and coverage stats."""
    findings: list[Finding] = []
    questions: list[ClarificationRequest] = []
    by_norm = {
        (a.identifier.normalized if a.identifier else ""): a
        for a in artifacts
    }
    judged: list[tuple[Slot, dict[str, Any]]] = []
    bound: set[str] = set()  # slot ids bound w/ det or judged evidence

    for slot in slots:
        if slot.declared_value is None:
            if slot.unknown is None:
                slot.unknown = "not_yet_checked"
            findings.append(Finding(
                id=_new_id("finding", f"unbound:{slot.id}"),
                axis="input_binding",
                severity="blocking"
                if slot.slot_type in _INPUT_SLOT_TYPES else "warning",
                subject=[f"slot:{slot.id}", slot.unit_id],
                mechanism="deterministic", confidence=1.0,
                description=f"slot '{slot.name}' has no declared "
                            "value — methods omit it",
                suggestion=f"state the value of '{slot.name}' "
                           "explicitly",
            ))
            questions.append(ClarificationRequest(
                id=_new_id("clarification_request", slot.id),
                blocking=slot.slot_type in _INPUT_SLOT_TYPES,
                question=f"What is the value of '{slot.name}' "
                         "for this analysis?",
                context_refs=[f"slot:{slot.id}", slot.unit_id],
                resolves_unknown=slot.unknown,
            ))
            continue

        # layer 1: generic invariants -------------------------------
        if slot.slot_type in ("versioned_identifier",
                              "reference_genome", "software"):
            slot.check_path = "generic_invariant"
            if not _has_version(slot.declared_value):
                sev = ("blocking" if slot.slot_type
                       == "versioned_identifier" else "warning")
                findings.append(Finding(
                    id=_new_id("finding", f"unver:{slot.id}"),
                    axis="input_binding", severity=sev,
                    subject=[f"slot:{slot.id}"],
                    mechanism="deterministic", confidence=1.0,
                    description=f"'{slot.declared_value}' carries no "
                                "version/release — instance not pinned",
                    suggestion="pin the exact version "
                               "(e.g. GCA_….15, tool=x.y)",
                ))

        if not _is_unique(slot.declared_value):
            slot.check_path = "generic_invariant"
            findings.append(Finding(
                id=_new_id("finding", f"ambig:{slot.id}"),
                axis="input_binding", severity="warning",
                subject=[f"slot:{slot.id}"],
                mechanism="deterministic", confidence=1.0,
                description=f"'{slot.declared_value}' names multiple "
                            "alternatives — ambiguous",
            ))
            continue

        # layer 2: registry-mediated probe --------------------------
        fam, norm = registry.route(slot.declared_value)
        if fam.name != "generic":
            slot.check_path = "registry_probe"
            if art := by_norm.get(norm):
                slot.bound_instance = BoundInstance(
                    artifact_id=f"artifact:{art.id}",
                    instance=(art.resolved.version
                              if art.resolved else ""),
                )
                bound.add(slot.id)
                if art.availability != "public":
                    findings.append(Finding(
                        id=_new_id("finding", f"access:{slot.id}"),
                        axis="artifact_access", severity="blocking",
                        subject=[f"slot:{slot.id}", art.ref()],
                        mechanism="deterministic", confidence=1.0,
                        description=f"slot '{slot.name}' binds to "
                                    f"{norm} but access is "
                                    f"{art.availability}",
                    ))
            elif fam.version_pattern and not re.search(
                fam.version_pattern, norm
            ):
                findings.append(Finding(
                    id=_new_id("finding", f"unpin:{slot.id}"),
                    axis="input_binding", severity="warning",
                    subject=[f"slot:{slot.id}"],
                    mechanism="deterministic", confidence=1.0,
                    description=f"{norm} lacks its family's version "
                                "part — could float",
                ))
            continue

        # layer 3: judged fallback -----------------------------------
        slot.check_path = "judged"
        judged.append((slot, {}))

    if judged:
        q = {}
        for slot, _ in judged:
            q[f"a4_binding_unique:{slot.id}"] = {
                "type": "noul",
                "instructions": (
                    f"Slot '{slot.name}' = "
                    f"'{slot.declared_value}'. Does this identify "
                    "exactly one versioned instance?"
                ),
            }
        answers = jev.ask(
            {"slots": [s.declared_value for s, _ in judged]},
            q, stage="a4",
        )
        for slot, _ in judged:
            a = answers.get(f"a4_binding_unique:{slot.id}", {})
            conf = float(a.get("confidence", 0.5))
            if a.get("noul", 1.0) < 0.4:
                findings.append(Finding(
                    id=_new_id("finding", f"judged:{slot.id}"),
                    axis="input_binding", severity="warning",
                    subject=[f"slot:{slot.id}"],
                    mechanism="jev",
                    jev_question_id="a4_binding_unique",
                    confidence=conf,
                    description=f"'{slot.declared_value}' judged "
                                "non-unique/unbound",
                ))
            elif "noul" in a:
                bound.add(slot.id)

    det = sum(1 for s in slots
              if s.check_path in ("generic_invariant", "registry_probe"))
    coverage = {
        # spec: fraction of declared inputs bound w/ deterministic or
        # judged evidence (registry-bound + judged-unique)
        "binding_check_coverage": len(bound) / len(slots)
        if slots else 1.0,
        "n_slots": len(slots), "n_deterministic": det,
        "n_judged": len(judged), "n_bound": len(bound),
        "n_unchecked": sum(1 for s in slots
                           if s.check_path == "unchecked"),
    }
    return findings, questions, coverage

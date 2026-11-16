"""A5 — static audit checks.

Deterministic: unfetched supplements, missing licenses, absent input
artifacts (the rest of artifact_access is A3's). Judged (per
specs/jev-questions.md): a5_methods_complete, a5_methods_placement,
a5_robustness, a5_test_design.

Negative findings carry coverage_caveat when A2's extraction coverage
was low — "we found nothing" means less when we read less.
"""

from __future__ import annotations

import logging
from typing import Any

from .auditrecord import (
    AnalysisUnit,
    Document,
    Finding,
    Slot,
    _new_id,
)

log = logging.getLogger(__name__)

_COV_LOW = 0.7     # below this a2 coverage, negative findings caveat
_SCORE_LOW = 0.5   # judged rubric threshold -> warning


def _unit_methods_text(doc, unit, slots) -> str:
    return " | ".join(
        s.declared_value or s.name
        for s in slots if s.unit_id == f"analysis_unit:{unit.id}"
    )[:1500]


def static_checks(
    doc: Document,
    units: list[AnalysisUnit],
    slots: list[Slot],
    *,
    jev,
    coverage: dict[str, float],
) -> list[Finding]:
    findings: list[Finding] = []

    # ---- deterministic -------------------------------------------
    for i, sup in enumerate(doc.supplements):
        unfetched = not sup.get("fetched")
        unparsed = sup.get("fetched") and sup.get("units") is None
        if unfetched or unparsed:
            desc = (f"supplement '{sup.get('filename')}' declared "
                    "but not fetched" if unfetched else
                    f"supplement '{sup.get('filename')}' fetched "
                    "but could not be parsed")
            findings.append(Finding(
                id=_new_id("finding", f"supp:{i}"),
                axis="artifact_access", severity="warning",
                subject=[f"document:{doc.id}"],
                mechanism="deterministic", confidence=1.0,
                description=f"{desc} — its content is unaudited",
                coverage_caveat=True,
            ))

    # ---- judged ----------------------------------------------------
    q: dict[str, dict[str, Any]] = {}
    for unit in units:
        manifest = _unit_methods_text(doc, unit, slots)
        q[f"a5_methods_complete:{unit.id}"] = {
            "type": "score",
            "instructions": "Are all pipeline parameters recoverable "
                        "from text + supplement for this analysis?",
            "criteria": ["0 none", "1 few", "2 half", "3 most",
                         "4 all"],
        }
        q[f"a5_methods_placement:{unit.id}"] = {
            "type": "choice",
            "instructions": "Where do the analysis-critical details live?",
            "criteria": {
                "proper": "in the Methods section",
                "buried": "in main text/supplement, not Methods",
                "absent": "nowhere",
            },
        }
        q[f"a5_robustness:{unit.id}"] = {
            "type": "score",
            "instructions": "Sensitivity analyses / seeds / pre-specified "
                        "exclusions reported?",
            "criteria": ["0 none", "1 few", "2 some", "3 most",
                         "4 all"],
        }
    for i, st in enumerate(doc.inline_stats):
        q[f"a5_test_design:{i}"] = {
            "type": "choice",
            "instructions": (
                f"Does test '{st.get('test')}' match the described "
                "design (paired vs independent, n, distribution)?"
            ),
            "criteria": {
                "matched": "test matches the design",
                "mismatch": "test does not match",
                "unclear": "cannot determine",
            },
        }
    if q:
        state = {
            "units": {u.id: manifest_per_unit(doc, u, slots)
                      for u in units},
            "methods": " | ".join(
                s.declared_value or s.name for s in slots
            )[:3000],
        }
        answers = jev.ask(state, q, stage="a5")
        for unit in units:
            caveat = coverage.get(unit.id, 1.0) < _COV_LOW
            for qid, axis, what in (
                (f"a5_methods_complete:{unit.id}",
                 "executability", "methods completeness"),
                (f"a5_robustness:{unit.id}",
                 "result_verifiability", "robustness reporting"),
            ):
                a = answers.get(qid, {})
                if "score" in a and a["score"] / 4.0 < _SCORE_LOW:
                    findings.append(Finding(
                        id=_new_id("finding", qid),
                        axis=axis, severity="warning",
                        subject=[f"analysis_unit:{unit.id}"],
                        mechanism="jev", jev_question_id=qid.split(":")[0],
                        confidence=float(a.get("confidence", 0.5)),
                        coverage_caveat=caveat,
                        description=f"low {what} "
                                    f"(score {a['score']}/4)",
                    ))
            pl = answers.get(f"a5_methods_placement:{unit.id}", {})
            if pl.get("choice") in ("buried", "absent"):
                findings.append(Finding(
                    id=_new_id("finding", f"place:{unit.id}"),
                    axis="methods_placement", severity="warning",
                    subject=[f"analysis_unit:{unit.id}"],
                    mechanism="jev",
                    jev_question_id="a5_methods_placement",
                    confidence=float(pl.get("confidence", 0.5)),
                    description="analysis-critical details are "
                                f"{pl['choice']} — not in Methods",
                ))
        for i, st in enumerate(doc.inline_stats):
            a = answers.get(f"a5_test_design:{i}", {})
            if a.get("choice") == "mismatch":
                findings.append(Finding(
                    id=_new_id("finding", f"stat:{i}"),
                    axis="result_verifiability", severity="warning",
                    subject=[f"document:{doc.id}"],
                    mechanism="jev",
                    jev_question_id="a5_test_design",
                    confidence=float(a.get("confidence", 0.5)),
                    description=f"{st.get('test')} test at "
                                f"{st.get('unit_ref')} judged "
                                "mismatched to design",
                ))
    return findings


def manifest_per_unit(doc, unit, slots) -> str:
    return " | ".join(
        f"{s.name}={s.declared_value}"
        for s in slots if s.unit_id == f"analysis_unit:{unit.id}"
    )

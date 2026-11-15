"""Pipeline orchestration — A0..A5 -> A7 report (static mode).

`run_static` returns (Audit, report). `run_full` adds A6 — the
gate + K Orbit reproduction runs — then rebuilds the report with a
PASS/FAIL/INDETERMINATE verdict.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import orbit, reproduce
from .auditrecord import Audit, _new_id
from .bind import bind_slots
from .materialize import materialize_slots
from .document import acquire
from .extract import extract_units
from .intake import Intake, classify
from .report import build_report
from .resolve import resolve_artifacts
from .static_checks import static_checks

log = logging.getLogger(__name__)


def run_static(
    intake: Intake,
    *,
    draft: bool = False,
    jev=None,
    llm=None,
    transport: httpx.BaseTransport | None = None,
    input_str: str = "",
) -> tuple[Audit, dict[str, Any]]:
    doc, doc_evidence = acquire(intake, draft=draft, transport=transport)
    audit = Audit(
        id=_new_id("audit", input_str or intake.raw),
        mode="draft" if draft else "published",
        document=doc,
        evidence=doc_evidence,
        meta={"stages_run": ["a0", "a1"]},
    )

    units, slots, a2_findings, coverage = extract_units(
        doc, jev=jev, llm=llm,
    )
    audit.units = units
    audit.slots = slots
    audit.findings.extend(a2_findings)
    audit.meta["a2_coverage"] = coverage
    audit.meta["stages_run"].append("a2")

    arts, a3_findings, audit.evidence = resolve_artifacts(
        doc, evidence=audit.evidence, transport=transport,
    )
    audit.artifacts = arts
    audit.findings.extend(a3_findings)
    audit.meta["stages_run"].append("a3")

    a4_findings, questions, cov = bind_slots(
        units, slots, arts, jev=jev, transport=transport,
    )
    audit.findings.extend(a4_findings)
    audit.questions.extend(questions)
    audit.meta.update(cov)
    m_findings, m_questions, m_stats, m_evidence = materialize_slots(
        units, slots, arts, jev=jev, llm=llm, document=doc,
        transport=transport,
    )
    audit.findings.extend(m_findings)
    audit.questions.extend(m_questions)
    audit.evidence.extend(m_evidence)
    audit.meta.update(m_stats)
    audit.meta["stages_run"].append("a4")

    audit.findings.extend(static_checks(
        doc, units, slots, jev=jev, coverage=coverage,
    ))
    audit.meta["stages_run"].append("a5")

    report = build_report(
        audit, jev=jev, llm=llm,
        input_str=input_str or intake.raw,
        mode=audit.mode,
    )
    return audit, report


def run_full(
    intake: Intake,
    *,
    draft: bool = False,
    k: int = 5,
    tau: int = 3,
    force: bool = False,
    run_timeout_s: int | None = None,
    jev=None,
    llm=None,
    transport: httpx.BaseTransport | None = None,
    input_str: str = "",
    invoke=None,
) -> tuple[Audit, dict[str, Any]]:
    """Static audit -> A6 gate + K Orbit runs -> A7 verdict.

    `invoke` is injectable for tests (defaults to orbit.run_orbit).
    """
    audit, _report = run_static(
        intake, draft=draft, jev=jev, llm=llm,
        transport=transport, input_str=input_str,
    )
    argv, version, pi_version = orbit.find_orbit()
    audit.runs = reproduce.run_full(
        audit, k=k, jev=jev, force=force,
        timeout_s=run_timeout_s or orbit.RUN_TIMEOUT_S,
        invoke=invoke or orbit.run_orbit,
        argv=argv, orbit_version=version, pi_version=pi_version,
    )
    audit.meta["stages_run"].append("a6")
    report = build_report(
        audit, jev=jev, llm=llm,
        input_str=input_str or intake.raw,
        mode=audit.mode, full=True, tau=tau, k=k,
    )
    return audit, report

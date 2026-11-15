"""A7 — report assembly.

Static mode emits READINESS; blocking findings flavor it
`gate-blocked:<criterion>` per the A6 gate spec. Axes are severity
gates, not scores (Decision 12): each axis reports its worst
finding level + finding ids, not a number that pretends to sum.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .auditrecord import Audit, SCHEMA_VERSION

AXES = [
    "artifact_access", "input_binding", "sample_metadata",
    "methods_placement", "executability", "result_verifiability",
]
_SEV_ORDER = {"blocking": 2, "warning": 1, "informational": 0}

# blocking finding axis -> gate criterion (PLAN §A6 gate)
_AXIS_GATE = {
    "artifact_access": "public-inputs",
    "input_binding": "public-inputs",
    "sample_metadata": "public-inputs",
}


def _axis_domain(axis: str, audit: Audit) -> tuple[str, bool]:
    """axis -> (stage that checks it, domain non-empty?). Level rules:
    stage not run -> 'unchecked'; domain empty -> 'na'; else worst
    finding severity ('ok' when findings exist only at informational
    level or none — a run check that found nothing)."""
    if axis == "artifact_access":
        return "a3", bool(audit.artifacts)
    if axis == "input_binding":
        return "a4", bool(audit.slots)
    if axis == "sample_metadata":
        return "a4", any(
            s.slot_type == "sample_grouping" for s in audit.slots)
    if axis in ("methods_placement", "executability"):
        return "a5", True
    if axis == "result_verifiability":
        return "a2", bool(audit.units)
    return "", True


def _costs(jev, llm) -> dict[str, Any]:
    """Usage rollup. total_usd is the sum of upstream-priced calls;
    None when no call reported a price (unpriced != free)."""
    blocks: dict[str, Any] = {}
    total = 0.0
    priced_any = False
    for key, client in (("jev", jev), ("llm", llm)):
        usage: dict[str, Any] = (
            client.total_usage()
            if client is not None and hasattr(client, "total_usage")
            else {}
        )
        if model := getattr(client, "model", None):
            usage["model"] = model
        if (c := usage.get("cost_usd")) is not None:
            total += c
            priced_any = True
        blocks[key] = usage
    blocks["total_usd"] = round(total, 6) if priced_any else None
    return blocks


def build_report(
    audit: Audit,
    *,
    jev=None,
    llm=None,
    input_str: str = "",
    mode: str = "published",
    full: bool = False,
    tau: int = 3,
    k: int = 5,
) -> dict[str, Any]:
    axes: dict[str, Any] = {}
    stages = set(audit.meta.get("stages_run", []))
    for axis in AXES:
        stage, applicable = _axis_domain(axis, audit)
        fids = [f for f in audit.findings if f.axis == axis]
        if stage and stage not in stages:
            level = "unchecked"
        elif not applicable:
            level = "na"
        else:
            worst = max(
                (_SEV_ORDER[f.severity] for f in fids), default=0)
            level = {2: "blocking", 1: "warning", 0: "ok"}[worst]
        axes[axis] = {
            "level": level,
            "confidence": min(
                (f.confidence for f in fids), default=1.0,
            ),
            "finding_ids": [f.id for f in fids],
        }

    blocking = [f for f in audit.findings if f.severity == "blocking"]
    open_q = [
        q for q in audit.questions
        if q.blocking and q.answer is None
    ]
    gate = sorted({
        _AXIS_GATE.get(f.axis, f.axis) for f in blocking
    } or (["open-questions"] if open_q else []))
    verdict = {
        "label": "READINESS",
        "flavor": f"gate-blocked:{','.join(gate)}" if gate else None,
        "tau": None, "k": None,
        "passing_runs": None, "checked_runs": None,
    }
    if full:
        from .reproduce import verdict_stats
        stats = verdict_stats(audit.runs)
        passing, checked = stats["passing_runs"], stats["checked_runs"]
        label = (
            "INDETERMINATE" if checked == 0 and not stats["flavor"]
            else "PASS" if passing >= tau else "FAIL"
        )
        verdict = {
            "label": label, "flavor": stats["flavor"],
            "tau": tau, "k": k,
            "passing_runs": passing, "checked_runs": checked,
        }

    return {
        "verdict": verdict,
        "axes": axes,
        "document": audit.document.id if audit.document else None,
        "counts": {
            "analysis_units": len(audit.units),
            "slots": len(audit.slots),
            "artifacts": len(audit.artifacts),
            "findings": len(audit.findings),
            "questions": len(audit.questions),
            "headline_units": sum(u.headline for u in audit.units),
        },
        "findings": [f.model_dump(mode="json") for f in audit.findings],
        "questions": [q.model_dump(mode="json")
                      for q in audit.questions],
        "runs": ([r.model_dump(mode="json") for r in audit.runs]
                 if audit.runs else None),
        "costs": _costs(jev, llm),
        "meta": {
            **audit.meta,
            "schema_version": SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "input": input_str, "mode": mode, "full": full,
            "ensemble": list({json.dumps(r.model, sort_keys=True)
                              for r in audit.runs})
            if audit.runs else [],
        },
    }

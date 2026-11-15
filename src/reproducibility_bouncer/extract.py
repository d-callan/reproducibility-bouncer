"""A2 — analysis-unit extraction.

The generator proposes analysis units + slots against *numbered
citable units*; every emitted element carries citation numbers that
are re-materialized verbatim (units.py discipline) and re-judged by
JEV. Section-wise map+merge when the paper exceeds a single prompt.

JEV questions (specs/jev-questions.md): a2_unit_real, a2_headline,
a2_slot_value, a2_provenance, a2_extract_coverage.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .auditrecord import (
    AnalysisUnit,
    Audit,
    Claim,
    ClarificationRequest,
    Document,
    Finding,
    OutputSpec,
    Slot,
    UnitRef,
    _new_id,
)

log = logging.getLogger(__name__)

_EXTRACT_SECTIONS = {
    "methods", "results", "captions", "das", "cas", "supplement",
}
_CONTEXT_SECTIONS = {"abstract", "discussion"}
_PROMPT_BUDGET = 60_000        # chars of numbered units per LLM call
_HEADLINE_CAP = 3
_NOUL_SUPPORT, _NOUL_REJECT = 0.6, 0.4  # P(supported) thresholds

_SYSTEM = """You extract analysis units from a scientific paper.
An analysis unit is a distinct computation producing an output
(figure, table, scalar result). For each unit report:

- title: short name
- headline: is this a key claim (named in abstract/discussion)?
- cites: source unit ids like "2.3" that establish the analysis exists
- outputs: [{result_key (fig2b.hr style), kind, label, expected value
  if numeric and stated, cites}]
- inputs: [{name, slot_type, declared_value exactly as written, cites}]
  slot_type: dataset|software|parameter|reference_genome|
  sample_grouping|versioned_identifier|other
- methods: same shape — software/protocol/parameter slots
- claims: [{text, cites}]

Rules: cite ONLY unit ids shown. Never invent values — if the paper
doesn't state a value use declared_value null. Quote nothing; cite
numbers. Reply JSON {"units": [...]}."""


def _numbered(doc: Document, names: set[str]) -> str:
    lines = []
    for si, sec in enumerate(doc.sections):
        if sec.name not in names:
            continue
        lines.append(f"## section {si} ({sec.name})")
        lines.extend(f"[{si}.{u.index}] {u.text}" for u in sec.units)
    return "\n".join(lines)


def _cite(v: Any, doc: Document) -> UnitRef | None:
    """'sec.unit' or {section,unit_index} -> valid UnitRef, else None."""
    if isinstance(v, str):
        m = re.fullmatch(r"(\d+)\.(\d+)", v.strip())
        if not m:
            return None
        si, ui = int(m.group(1)), int(m.group(2))
    elif isinstance(v, dict):
        try:
            si, ui = int(v["section"]), int(v["unit_index"])
        except (KeyError, TypeError, ValueError):
            return None
    else:
        return None
    if 0 <= si < len(doc.sections):
        if any(u.index == ui for u in doc.sections[si].units):
            return UnitRef(section=si, unit_index=ui)
    return None


def _cites(raw: Any, doc: Document) -> list[UnitRef]:
    if not isinstance(raw, list):
        return []
    seen, out = set(), []
    for v in raw:
        if (r := _cite(v, doc)) is not None:
            key = (r.section, r.unit_index)
            if key not in seen:
                seen.add(key)
                out.append(r)
    return out


def _unit_texts(doc: Document, refs: list[UnitRef]) -> list[str]:
    out = []
    for r in refs:
        if r.section < len(doc.sections):
            for u in doc.sections[r.section].units:
                if u.index == r.unit_index:
                    out.append(u.text)
                    break
    return out


def _prompt_blocks(doc: Document) -> list[str]:
    """One or more numbered-unit blocks — split when over budget."""
    full = _numbered(doc, _EXTRACT_SECTIONS)
    if len(full) <= _PROMPT_BUDGET:
        return [full]
    blocks, cur = [], f"## section -1 (context)\n{_numbered(doc, _CONTEXT_SECTIONS)}"
    for si, sec in enumerate(doc.sections):
        if sec.name not in _EXTRACT_SECTIONS:
            continue
        part = f"## section {si} ({sec.name})\n" + "\n".join(
            f"[{si}.{u.index}] {u.text}" for u in sec.units
        )
        if len(cur) + len(part) > _PROMPT_BUDGET and cur.strip():
            blocks.append(cur)
            cur = ""
        cur += part + "\n"
    if cur.strip():
        blocks.append(cur)
    return blocks


def _propose(doc: Document, llm) -> list[dict[str, Any]]:
    """LLM proposals per block -> one merged raw list."""
    ctx = _numbered(doc, _CONTEXT_SECTIONS)
    raw: list[dict[str, Any]] = []
    for block in _prompt_blocks(doc):
        user = (
            "SOURCE UNITS (id = section.unit):\n\n"
            + ("## context (abstract/discussion)\n" + ctx + "\n\n"
               if "## context" not in block else "")
            + block
        )
        try:
            data = json.loads(llm.complete(_SYSTEM, user))
        except (json.JSONDecodeError, TypeError) as e:
            log.warning("a2: bad generator JSON: %s", e)
            continue
        units = data.get("units")
        if isinstance(units, list):
            raw.extend(u for u in units if isinstance(u, dict))
    return _merge_proposals(raw)


def _merge_proposals(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedup cross-block proposals sharing a result_key or ≥0.5 of
    their cited units (Jaccard)."""
    merged: list[dict[str, Any]] = []
    for u in raw:
        keys = {
            o.get("result_key") for o in u.get("outputs") or []
            if o.get("result_key")
        }
        cites = set(map(str, u.get("cites") or []))
        target = None
        for m in merged:
            mkeys = {
                o.get("result_key") for o in m.get("outputs") or []
                if o.get("result_key")
            }
            mcites = set(map(str, m.get("cites") or []))
            same = (keys & mkeys) or (
                cites and mcites
                and len(cites & mcites) / len(cites | mcites) >= 0.5
            )
            if same:
                target = m
                break
        if target is None:
            merged.append(dict(u))
        else:
            for field in ("cites", "outputs", "inputs", "methods",
                          "claims"):
                tgt = target.setdefault(field, [])
                tgt.extend(
                    x for x in u.get(field) or [] if x not in tgt
                )
            target["headline"] = bool(
                target.get("headline") or u.get("headline")
            )
    return merged


def _to_records(
    proposals: list[dict[str, Any]], doc: Document,
) -> tuple[list[AnalysisUnit], dict[str, list[Slot]]]:
    units: list[AnalysisUnit] = []
    slots_by_unit: dict[str, list[Slot]] = {}
    for rank, p in enumerate(proposals):
        uid = _new_id("analysis_unit", json.dumps(p, sort_keys=True))
        unit = AnalysisUnit(
            id=uid,
            headline=bool(p.get("headline")),
            rank=rank,
            source_units=_cites(p.get("cites"), doc),
        )
        for o in p.get("outputs") or []:
            kind = str(o.get("kind", "other"))
            unit.outputs.append(OutputSpec(
                result_key=str(o.get("result_key") or ""),
                kind=kind if kind in
                ("figure", "table", "scalar", "other") else "other",
                label=str(o.get("label") or ""),
                expected=o.get("expected")
                if isinstance(o.get("expected"), dict) else None,
                evidence=_cites(o.get("cites"), doc),
            ))
        unit.claims = [
            Claim(text=str(c.get("text") or ""),
                  evidence=_cites(c.get("cites"), doc))
            for c in p.get("claims") or [] if c.get("text")
        ]
        slots: list[Slot] = []
        for group, raw_slots in (
            ("inputs", p.get("inputs")), ("methods", p.get("methods")),
        ):
            for s in raw_slots or []:
                if not isinstance(s, dict):
                    continue
                st = str(s.get("slot_type") or "other")
                slot = Slot(
                    id=_new_id(
                        "slot",
                        f"{uid}:{s.get('name')}:{s.get('declared_value')}",
                    ),
                    unit_id=f"analysis_unit:{uid}",
                    name=str(s.get("name") or "unnamed"),
                    slot_type=st if st in Slot.model_fields[
                        "slot_type"
                    ].annotation.__args__ else "other",
                    declared_value=(
                        str(s["declared_value"])
                        if s.get("declared_value") is not None else None
                    ),
                    evidence=_cites(s.get("cites"), doc),
                )
                slots.append(slot)
                getattr(unit, group).append(f"slot:{slot.id}")
        units.append(unit)
        slots_by_unit[uid] = slots
    return units, slots_by_unit


def extract_units(
    doc: Document,
    *,
    jev,
    llm,
) -> tuple[list[AnalysisUnit], list[Slot], list[Finding], dict[str, float]]:
    """A2: propose -> verify -> records. Returns units, slots,
    findings, and per-unit extraction-coverage scores (feeds
    coverage_caveat on later negative findings)."""
    proposals = _propose(doc, llm)
    units, slots_by_unit = _to_records(proposals, doc)
    all_slots = [s for ss in slots_by_unit.values() for s in ss]

    # ---- one JEV batch: all questions, all units -------------------
    state = {"sections": _numbered(doc, _EXTRACT_SECTIONS | _CONTEXT_SECTIONS)[:100_000]}
    q: dict[str, dict[str, Any]] = {}
    for unit in units:
        texts = _unit_texts(doc, unit.source_units)
        q[f"a2_unit_real:{unit.id}"] = {
            "type": "noul",
            "instructions": (
                "Cited units: " + " | ".join(texts[:8])
                + ". Is this a distinct analysis producing an output?"
            ),
        }
        q[f"a2_headline:{unit.id}"] = {
            "type": "choice",
            "instructions": "Is this analysis one of the paper's key "
                        "results (named in abstract/discussion)?",
            "criteria": {"headline": "key result",
                         "secondary": "supporting/peripheral"},
        }
        q[f"a2_extract_coverage:{unit.id}"] = {
            "type": "score",
            "instructions": "Fraction of slots a reproducer needs that "
                        "are stated for this analysis?",
            "criteria": [
                "0 = nothing stated", "1 = most missing",
                "2 = about half", "3 = most stated",
                "4 = everything needed is stated",
            ],
        }
        for slot in slots_by_unit[unit.id]:
            stexts = _unit_texts(doc, slot.evidence)
            if slot.declared_value is not None:
                q[f"a2_slot_value:{slot.id}"] = {
                    "type": "noul",
                    "instructions": (
                        "Cited units: " + " | ".join(stexts[:6])
                        + f". Do they support {slot.name}"
                        + f" = '{slot.declared_value}'?"
                    ),
                }
            q[f"a2_provenance:{slot.id}"] = {
                "type": "choice",
                "instructions": f"How was the value for slot "
                            f"'{slot.name}' obtained?",
                "criteria": {
                    "reported": "stated in text/supplement",
                    "observed": "read from an artifact",
                    "inferred": "derived (e.g. from filenames)",
                },
            }
    answers = jev.ask(state, q, stage="a2") if q else {}

    # ---- apply verdicts -------------------------------------------
    findings: list[Finding] = []
    coverage: dict[str, float] = {}
    kept: list[AnalysisUnit] = []
    for unit in units:
        a = answers.get(f"a2_unit_real:{unit.id}", {})
        if a.get("noul", 1.0) < _NOUL_REJECT:
            findings.append(Finding(
                id=_new_id("finding", unit.id),
                axis="result_verifiability", severity="warning",
                subject=[f"analysis_unit:{unit.id}"],
                mechanism="jev", jev_question_id="a2_unit_real",
                confidence=float(a.get("confidence", 0.5)),
                description="generator proposed an analysis unit the "
                            "judge could not verify in cited units — "
                            "dropped",
            ))
            continue
        hl = answers.get(f"a2_headline:{unit.id}", {})
        if hl.get("choice"):
            unit.headline = hl["choice"] == "headline"
        cov = answers.get(f"a2_extract_coverage:{unit.id}", {})
        if "score" in cov:
            coverage[unit.id] = cov["score"] / 4.0  # 5-level rubric
        for slot in slots_by_unit[unit.id]:
            sa = answers.get(f"a2_slot_value:{slot.id}", {})
            if sa and sa.get("noul", 1.0) < _NOUL_REJECT:
                slot.declared_value = None
                slot.unknown = "not_yet_checked"
            prov = answers.get(f"a2_provenance:{slot.id}", {})
            if prov.get("choice") in ("reported", "observed",
                                      "inferred"):
                slot.provenance = prov["choice"]
        kept.append(unit)
    for i, u in enumerate(kept):
        u.rank = i
    # cap headline set (A6 budget: only headline units attempted)
    n_hl = 0
    for u in kept:
        if u.headline and n_hl >= _HEADLINE_CAP:
            u.headline = False
        n_hl += u.headline
    kept_slots = [
        s for u in kept for s in slots_by_unit[u.id]
    ]
    return kept, kept_slots, findings, coverage

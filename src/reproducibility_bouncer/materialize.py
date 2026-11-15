"""A4b — spec materialization.

A bound slot answers "which artifact?"; a spec answers "which concrete
instance, completely, and on what evidence?". Every slot is evaluated
against a contract keyed on slot_type:

  sample_grouping  -> the declared group resolved to concrete samples
                      (GEO SOFT sheet), or an explicit failure
  reference_genome -> {assembly accession+version, annotation
                      {source, release}}; the annotation field is only
                      required when the unit's other inputs reference
                      one (gtf/gff/annotation language)
  <generic>        -> bound instance or declared value; repository
                      verification decides support

spec_score = completeness x support:

  completeness — fraction of contract-required fields populated
                 (0.0 when no spec can be produced at all)
  support      — 1.0 when verified against repository evidence;
                 JEV confidence when only judged; 0.5 when no evidence
                 exists either way. This is the hallucination-vs-
                 evidenced axis: a paper-declared value nobody checked
                 cannot score 1.0.

NA semantics: a slot type whose domain doesn't apply to this audit
simply doesn't occur; report axes map an empty domain to "na". Slots
the pass never reached keep spec_score None (reported "unchecked").
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

import httpx

from .auditrecord import (
    AnalysisUnit,
    Artifact,
    ClarificationRequest,
    Finding,
    Slot,
    _new_id,
)
from .clients import ClientError, geo
from .clients.registry import route

log = logging.getLogger(__name__)

_ASSEMBLY = re.compile(r"GC[AF]_\d{9}(?:\.\d+)?")
_ANNOTATION_LANG = re.compile(
    r"\b(gtf|gff\d?|annotation|gencode|ensembl|refseq\s+genes?)\b", re.I,
)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _tokens(s: str) -> list[str]:
    return [t for t in _norm(s).split() if len(t) >= 3]


def _sample_blob(sample: dict[str, Any]) -> str:
    parts = [sample.get("title", ""), sample.get("source", "")]
    for vals in sample.get("characteristics", {}).values():
        parts.extend(vals)
    return _norm(" ".join(parts))


def _distinct_values(
    samples: list[dict[str, Any]],
) -> dict[str, dict[str, list[str]]]:
    """field -> normalized value -> accessions holding it. Built over
    the parsed sheet so a declared group can be checked against the
    repository's own vocabulary rather than free text."""
    idx: dict[str, dict[str, list[str]]] = {}
    for s in samples:
        for key, vals in s.get("characteristics", {}).items():
            for v in vals:
                idx.setdefault(key, {}).setdefault(
                    _norm(v), []).append(s["accession"])
    return idx


def _match_groups(
    declared: str, samples: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, str]]]:
    """Declared group -> (matched accessions, matched_via).

    Primary rule — field-value subset: a distinct characteristic
    value matches when its token set is a subset of the declared
    group's (repository value is a shorter form, e.g. 'PA14' inside
    'UCBPP-PA14') or vice versa. This is deliberately bidirectional:
    papers declare strain-grade names, deposits carry short codes.

    Fallback — blob all-token: every declared token appears somewhere
    in the sample's title/source/characteristics (catches groups
    encoded only in titles)."""
    dtoks = set(_tokens(declared))
    matched: list[str] = []
    via: list[dict[str, str]] = []
    if not dtoks:
        return matched, via
    idx = _distinct_values(samples)
    for field, values in idx.items():
        for vnorm, accs in values.items():
            vtoks = set(vnorm.split())
            if vtoks and (vtoks <= dtoks or dtoks <= vtoks):
                via.append({"field": field, "value": vnorm,
                            "mechanism": "deterministic"})
                matched.extend(a for a in accs if a not in matched)
    blob_hits = [s["accession"] for s in samples
                 if all(t in _sample_blob(s) for t in dtoks)]
    extra = [a for a in blob_hits if a not in matched]
    if extra:
        matched.extend(extra)
        via.append({"field": "(free text)", "value": declared,
                    "mechanism": "deterministic"})
    return matched, via


# cap on candidate sets offered to the judge — arbitrary, but
# judged mapping is a last resort; beyond ~3 plausible sample-sets
# the declaration is too vague to score anyway
_MAX_GROUP_CANDIDATES = 3


def _group_candidates(
    declared: str, samples: list[dict[str, Any]],
    top: int = _MAX_GROUP_CANDIDATES,
) -> list[dict[str, Any]]:
    """Deterministic no-match fallback: rank the sheet's distinct
    characteristic values by token overlap with the declared group.
    Returns [{field, value, accessions}] for the judge to confirm —
    the judged band exists because depositors encode groups under
    arbitrary vocabularies (PA14 vs UCBPP-PA14, DMSO vs vehicle)."""
    dtoks = set(_tokens(declared))
    scored = []
    for field, values in _distinct_values(samples).items():
        for vnorm, accs in values.items():
            overlap = len(dtoks & set(vnorm.split()))
            if overlap:
                scored.append({
                    "field": field, "value": vnorm,
                    "accessions": accs, "overlap": overlap,
                    "n": len(accs)})
    scored.sort(key=lambda c: (c["overlap"], c["n"]), reverse=True)
    return scored[:top]


# ------------------------------------------------------------------
# contract handlers — each returns (spec, missing_fields, support)
# and may append findings / questions / evidence via ctx dicts.

def _dataset_artifacts(unit: AnalysisUnit,
                     slots_by_id: dict[str, Slot],
                     arts_by_id: dict[str, Artifact],
                     ) -> list[Artifact]:
    """Dataset artifacts bound to this unit's OTHER input slots."""
    out = []
    for ref in unit.inputs:
        s = slots_by_id.get(ref.replace("slot:", ""))
        if (s is not None and s.bound_instance
                and s.slot_type in ("dataset", "versioned_identifier")):
            art = arts_by_id.get(
                s.bound_instance.artifact_id.replace("artifact:", ""))
            if art is not None:
                out.append(art)
    return out


_GSE = re.compile(r"^GSE\d+$")


def _geo_datasets(arts) -> dict[str, Artifact]:
    """normalized GSE accession -> artifact, for every GEO dataset
    resolved anywhere in the audit."""
    return {
        a.identifier.normalized: a
        for a in arts
        if a.identifier and _GSE.match(a.identifier.normalized or "")
    }


def _get_sheet(
    gse: str, slot: Slot, unit: AnalysisUnit, ctx: dict[str, Any],
) -> dict[str, Any] | None:
    """Fetch + cache one SOFT sheet; None on failure (already
    reported)."""
    if gse not in ctx["sheets"]:
        try:
            sheet, ev = geo.sample_sheet(
                gse, transport=ctx["transport"])
            ctx["sheets"][gse] = sheet
            ctx["evidence"].append(ev)
        except ClientError as e:
            ctx["sheets"][gse] = None
            ctx["findings"].append(Finding(
                id=_new_id("finding", f"sheet:{slot.id}:{gse}"),
                axis="sample_metadata", severity="warning",
                subject=[f"slot:{slot.id}", unit.id],
                mechanism="deterministic", confidence=1.0,
                description=f"GEO sample sheet for {gse} "
                            f"unfetchable ({e}) — group membership "
                            "unverifiable",
            ))
    return ctx["sheets"][gse]


def _sample_grouping(
    slot: Slot, unit: AnalysisUnit, ctx: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[str], float]:
    required = ["matched_samples"]
    declared = slot.declared_value or ""
    geo_arts = _geo_datasets(ctx["arts_by_id"].values())
    bound = _dataset_artifacts(
        unit, ctx["slots_by_id"], ctx["arts_by_id"])
    # bound dataset slots win; when the unit declares groups but
    # bound no GEO dataset the group is still checkable against every
    # GEO series the paper resolved — otherwise a purely descriptive
    # dataset declaration would leave group specs unproducible even
    # when the mapping is public
    candidates = (
        [a.identifier.normalized for a in bound
         if a.identifier and a.identifier.normalized in geo_arts]
        or list(geo_arts)
    )
    spec: dict[str, Any] = {
        "declared": declared, "dataset": None,
        "datasets_checked": candidates, "matched_samples": [],
        "matched_via": []}
    if not candidates:
        return spec, required, required, 0.5
    matched_in: list[str] = []
    any_sheet = False
    for gse in candidates:
        sheet = _get_sheet(gse, slot, unit, ctx)
        if sheet is None:
            continue
        any_sheet = True
        hits, via = _match_groups(declared, sheet["samples"])
        hits = [h for h in hits if h not in spec["matched_samples"]]
        if hits:
            matched_in.append(gse)
            spec["matched_samples"].extend(hits)
            spec["matched_via"].extend(
                v for v in via if v not in spec["matched_via"])
            ctx["gse_assignments"].setdefault(gse, set()).update(hits)
            ctx["gse_sheets"][gse] = sheet
            ctx["gse_arts"][gse] = geo_arts[gse].id
    spec["n_series_samples"] = {
        g: ctx["sheets"][g]["n_series"] for g in candidates
        if ctx["sheets"].get(g)}
    spec["n_parsed_samples"] = {
        g: len(ctx["sheets"][g]["samples"]) for g in candidates
        if ctx["sheets"].get(g)}
    if matched_in:
        spec["dataset"] = (matched_in[0] if len(matched_in) == 1
                           else matched_in)
        return spec, required, [], 1.0
    if not any_sheet:
        return spec, required, required, 0.5
    # deterministic matching failed — the mapping may still exist
    # under a depositor vocabulary we can't enumerate; rank
    # candidate characteristic values and hand them to the judge
    cands: list[dict[str, Any]] = []
    for gse in candidates:
        sheet = ctx["sheets"].get(gse)
        if not sheet:
            continue
        for c in _group_candidates(declared, sheet["samples"]):
            cands.append({"gse": gse, **c})
    cands.sort(key=lambda c: (c["overlap"], c["n"]), reverse=True)
    cands = cands[:_MAX_GROUP_CANDIDATES]
    spec["candidates"] = [
        {k: c[k] for k in ("gse", "field", "value", "n")}
        for c in cands]
    if cands and ctx.get("jev") is not None:
        ctx["to_judge_groups"].append(
            {"slot": slot, "unit": unit, "cands": cands})
        return spec, required, required, 1.0
    # zero token overlap — depositor vocabulary may still name the
    # group semantically ('untreated' vs 'control'); let the
    # generator propose candidates the judge can then dispose of
    if (not cands and ctx.get("llm") is not None
            and ctx.get("document") is not None):
        ctx["to_propose_groups"].append(
            {"slot": slot, "unit": unit, "gses": candidates,
             "cands": []})
        return spec, required, required, 1.0
    _absent_group(slot, unit, spec, ctx)
    return spec, required, ["matched_samples"], 1.0


def _reference_genome(
    slot: Slot, unit: AnalysisUnit, ctx: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[str], float]:
    declared = slot.declared_value or ""
    spec: dict[str, Any] = {"assembly": None, "annotation": None}
    required = ["assembly"]
    # annotation required only when the unit consumes one: any sibling
    # slot mentioning gtf/gff/annotation language makes it mandatory.
    if any(
        _ANNOTATION_LANG.search(s.declared_value or "")
        for s in ctx["slots_by_id"].values()
        if s.unit_id == slot.unit_id and s.id != slot.id
    ):
        required.append("annotation")
    missing = list(required)
    if m := _ASSEMBLY.search(declared):
        spec["assembly"] = m.group(0)
        missing.remove("assembly")
        acc = m.group(0)
        support = 1.0 if "." in acc else 0.5
    else:
        fam, norm = route(declared)
        if fam.name == "assembly":
            spec["assembly"] = norm
            missing.remove("assembly")
            support = 1.0 if re.search(r"\.\d+$", norm) else 0.5
        else:
            support = 0.0  # placeholder — judged below
    if support == 0.0:
        ctx["to_judge"].append(slot)
    return spec, required, missing, support


def _generic(
    slot: Slot, unit: AnalysisUnit, ctx: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[str], float]:
    required = ["instance"]
    if slot.bound_instance:
        spec = {"instance": slot.bound_instance.instance or
                slot.bound_instance.artifact_id,
                "artifact": slot.bound_instance.artifact_id}
        return spec, required, [], 1.0
    if slot.declared_value:
        return {"instance": slot.declared_value}, required, [], 0.5
    return {"instance": None}, required, ["instance"], 0.5


CONTRACTS: dict[
    str,
    Callable[[Slot, AnalysisUnit, dict[str, Any]],
             tuple[dict[str, Any], list[str], list[str], float]],
] = {
    "sample_grouping": _sample_grouping,
    "reference_genome": _reference_genome,
}


def materialize_slots(
    units: list[AnalysisUnit],
    slots: list[Slot],
    artifacts: list[Artifact],
    *,
    jev=None,
    llm=None,
    document=None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[list[Finding], list[ClarificationRequest],
           dict[str, Any], list]:
    """Materialize every slot's spec. Returns (findings, questions,
    stats, new_evidence)."""
    findings: list[Finding] = []
    questions: list[ClarificationRequest] = []
    evidence: list = []
    units_by_id = {u.id: u for u in units}
    ctx: dict[str, Any] = {
        "slots_by_id": {s.id: s for s in slots},
        "arts_by_id": {a.id: a for a in artifacts},
        "sheets": {}, "findings": findings,
        "questions": questions, "evidence": evidence,
        "transport": transport, "to_judge": [], "jev": jev,
        "llm": llm, "document": document,
        "to_judge_groups": [], "to_propose_groups": [],
        "gse_assignments": {}, "gse_sheets": {}, "gse_arts": {},
    }

    for slot in slots:
        unit = units_by_id.get(slot.unit_id.replace(
            "analysis_unit:", ""))
        if unit is None:
            continue
        handler = CONTRACTS.get(slot.slot_type, _generic)
        spec, required, missing, support = handler(slot, unit, ctx)
        completeness = 1.0 - len(missing) / len(required)
        slot.spec = spec
        slot.spec["missing"] = missing
        slot.spec_score = round(completeness * support, 3)
        slot.spec_basis = _basis(slot, support)

    _judge_refs(ctx, jev)
    _propose_groups(ctx, llm, document)
    _judge_groups(ctx, jev)
    _unassigned_samples(ctx)

    # findings emit after judging so a JEV-confirmed ref doesn't
    # keep a premature 'nospec' warning
    for slot in slots:
        if slot.spec_score is None:
            continue
        missing = (slot.spec or {}).get("missing", [])
        if (slot.spec_score == 0.0 and slot.declared_value
                is not None and slot.slot_type in CONTRACTS
                and not (slot.spec or {}).get("absent")):
            findings.append(Finding(
                id=_new_id("finding", f"nospec:{slot.id}"),
                axis=_spec_axis(slot), severity="warning",
                subject=[f"slot:{slot.id}", slot.unit_id],
                mechanism="deterministic", confidence=1.0,
                description=f"'{slot.name}' declares "
                            f"'{slot.declared_value}' but no instance "
                            "spec is producible — scored 0",
            ))
        elif missing and slot.slot_type in CONTRACTS:
            findings.append(Finding(
                id=_new_id("finding", f"part:{slot.id}"),
                axis=_spec_axis(slot), severity="informational",
                subject=[f"slot:{slot.id}", slot.unit_id],
                mechanism="deterministic", confidence=1.0,
                description=f"spec for '{slot.name}' incomplete: "
                            f"missing {', '.join(missing)}",
            ))

    scores = [s.spec_score for s in slots if s.spec_score is not None]
    stats = {
        "n_spec_evaluated": len(scores),
        "spec_mean_score": (round(sum(scores) / len(scores), 3)
                            if scores else None),
        "n_spec_zero": sum(1 for x in scores if x == 0.0),
    }
    return findings, questions, stats, evidence


def _absent_group(
    slot: Slot, unit: AnalysisUnit, spec: dict[str, Any],
    ctx: dict[str, Any],
) -> None:
    """Sheets fetched, nothing matched (deterministically or under
    judgment) — the absence claim is only as strong as parse
    coverage, so truncated sheets are named."""
    spec["absent"] = True
    coverage = ", ".join(
        f"{g} ({spec['n_parsed_samples'].get(g, 0)}/"
        f"{spec['n_series_samples'].get(g, '?')} parsed)"
        for g in spec.get("datasets_checked", [])
        if g in spec.get("n_parsed_samples", {}))
    ctx["findings"].append(Finding(
        id=_new_id("finding", f"nogrp:{slot.id}"),
        axis="sample_metadata", severity="warning",
        subject=[f"slot:{slot.id}", unit.id],
        mechanism="deterministic", confidence=1.0,
        description=f"declared group '{slot.declared_value}' "
                    f"matches no sample in {coverage} — the "
                    "sample->group mapping does not exist in "
                    "repository metadata",
        suggestion="deposit the sample->group table with the series "
                   "or name the characteristic column that encodes it",
    ))
    ctx["questions"].append(ClarificationRequest(
        id=_new_id("clarification_request", f"grp:{slot.id}"),
        blocking=False,
        question=f"Which samples in "
                 f"{', '.join(spec.get('datasets_checked', []))} "
                 f"constitute '{slot.declared_value}'?",
        context_refs=[f"slot:{slot.id}", unit.id],
        resolves_unknown="cannot_determine",
    ))


# vocabulary shown to the proposer is capped per field — arbitrary
# bound, but 'which characteristic column encodes the group' rarely
# needs >40 values to see the answer
_MAX_VOCAB_VALUES = 40

_PROPOSE_SYSTEM = """\
You map declared sample groups from a paper to the characteristic
fields of a GEO series sample sheet. The input gives the sheet's
ACTUAL vocabulary (field -> distinct normalized values) plus each
declared group and the paper sentence it came from. Propose only
mappings you can argue from the vocabulary itself — copy field and
value verbatim. If no value plausibly identifies a declared group,
propose nothing for it. Respond with JSON only:
{"mappings": [{"slot": <index>, "gse": "<GSE>", "field": "<field>",
"value": "<value>"}]}"""


def _slot_quote(slot: Slot, document, max_chars: int = 500) -> str:
    """The paper span the slot was declared in — grounding for the
    proposer so it argues from what the paper said, not the slot
    name alone."""
    texts = []
    for r in (slot.evidence or [])[:2]:
        try:
            texts.append(
                document.sections[r.section]
                .units[r.unit_index - 1].text)
        except (IndexError, TypeError, AttributeError):
            continue
    return " \u2026 ".join(texts)[:max_chars]


def _propose_groups(ctx: dict[str, Any], llm, document) -> None:
    """Zero-overlap fallback: the generator sees the sheet's real
    field->value vocabulary and proposes mappings; each proposal is
    verified deterministically against that vocabulary before the
    judge ever sees it — hallucinated fields/values drop silently
    and the GSM lists remain repository-real."""
    pending = ctx["to_propose_groups"]
    if not pending:
        return
    if llm is None or document is None:
        for p in pending:
            _absent_group(p["slot"], p["unit"],
                          p["slot"].spec or {}, ctx)
        return
    idxs: dict[str, dict] = {}
    for p in pending:
        for gse in p["gses"]:
            sheet = ctx["sheets"].get(gse)
            if sheet and gse not in idxs:
                idxs[gse] = _distinct_values(sheet["samples"])
    vocab = {
        gse: {f: [v for v, _ in sorted(
            vals.items(), key=lambda kv: -len(kv[1]))
            [:_MAX_VOCAB_VALUES]]
            for f, vals in idx.items()}
        for gse, idx in idxs.items()}
    payload = json.dumps({
        "vocabulary": vocab,
        "declared_groups": [
            {"slot": i, "declared": p["slot"].declared_value,
             "paper_context": _slot_quote(p["slot"], document)}
            for i, p in enumerate(pending)]}, ensure_ascii=False)
    try:
        data = json.loads(llm.complete(_PROPOSE_SYSTEM, payload))
    except Exception:
        data = {}
    for m in data.get("mappings") or []:
        try:
            i = int(m["slot"])
            gse, field = str(m["gse"]), str(m["field"])
            vnorm = _norm(str(m["value"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= i < len(pending):
            continue
        accs = (idxs.get(gse, {}).get(field, {}) or {}).get(vnorm)
        if not accs:
            continue  # hallucinated field/value — drop
        pending[i]["cands"].append({
            "gse": gse, "field": field, "value": vnorm,
            "accessions": accs, "overlap": 0, "n": len(accs),
            "proposed": True})
    for p in pending:
        p["cands"] = p["cands"][:_MAX_GROUP_CANDIDATES]
        if p["cands"]:
            spec = p["slot"].spec or {}
            spec["candidates"] = [
                {k: c[k] for k in ("gse", "field", "value", "n")}
                for c in p["cands"]]
            ctx["to_judge_groups"].append(p)
        else:
            _absent_group(p["slot"], p["unit"],
                          p["slot"].spec or {}, ctx)


def _judge_groups(ctx: dict[str, Any], jev) -> None:
    """Resolve queued group mappings: one noul per ranked candidate
    sample-set. Depositor vocabulary is arbitrary — the judge sees
    the repository's actual field/value vocabulary and decides
    whether the declared group corresponds. Affirmed sets merge;
    support = judge confidence (never 1.0 — judged != verified)."""
    pending = ctx["to_judge_groups"]
    if not pending or jev is None:
        for p in pending:
            _absent_group(p["slot"], p["unit"],
                          p["slot"].spec or {}, ctx)
        return
    questions: dict[str, dict[str, Any]] = {}
    key_of: dict[str, tuple[dict, int]] = {}
    for p in pending:
        for i, c in enumerate(p["cands"]):
            qid = f"a4_grp_map:{p['slot'].id}:{i}"
            key_of[qid] = (p, i)
            questions[qid] = {
                "type": "noul",
                "instructions": (
                    f"A paper declares sample group "
                    f"'{p['slot'].declared_value}'. In {c['gse']} "
                    "the samples have characteristic "
                    f"{c['field']} = '{c['value']}' on {c['n']} "
                    "rows. Does the declared group correspond to "
                    "those samples?"),
            }
    state = {
        "groups": [
            {"declared": p["slot"].declared_value,
             "candidates": [{
                 "gse": c["gse"], "field": c["field"],
                 "value": c["value"], "n": c["n"],
                 "example_accessions": c["accessions"][:5],
             } for c in p["cands"]]}
            for p in pending],
    }
    answers = jev.ask(state, questions, stage="a4")
    affirmed: dict[str, list[tuple[float, dict]]] = {}
    for p in pending:
        affirmed[p["slot"].id] = []
    for qid, (p, i) in key_of.items():
        a = answers.get(qid, {})
        if a.get("noul", 0) >= 0.5:
            affirmed[p["slot"].id].append(
                (float(a.get("confidence", 0.5)), p["cands"][i]))
    for p in pending:
        slot, spec = p["slot"], p["slot"].spec or {}
        hits = affirmed[slot.id]
        if not hits:
            slot.spec_basis = "judged:absent"
            _absent_group(slot, p["unit"], spec, ctx)
            continue
        hits.sort(key=lambda h: h[0], reverse=True)
        conf = hits[0][0]
        matched: list[str] = []
        for _, c in hits:
            for a in c["accessions"]:
                if a not in matched:
                    matched.append(a)
            gse = c["gse"]
            ctx["gse_assignments"].setdefault(
                gse, set()).update(c["accessions"])
        spec["matched_samples"] = matched
        spec["dataset"] = hits[0][1]["gse"] if len(
            {c["gse"] for _, c in hits}) == 1 else [
            c["gse"] for _, c in hits]
        spec["matched_via"] = [
            {"field": c["field"], "value": c["value"],
             "mechanism": ("proposed+judged" if c.get("proposed")
                           else "judged")} for _, c in hits]
        spec["missing"] = [
            m for m in spec.get("missing", [])
            if m != "matched_samples"]
        slot.spec_score = round(conf, 3)  # completeness now 1.0
        slot.spec_basis = f"judged:{conf:.2f}"


def _spec_axis(slot: Slot) -> str:
    return ("sample_metadata" if slot.slot_type == "sample_grouping"
            else "input_binding")


def _basis(slot: Slot, support: float) -> str:
    if support == 1.0:
        return ("registry-verified" if slot.check_path
                == "registry_probe" else "repository-verified")
    if support == 0.0:
        return "unverifiable"
    return "declared-only"


def _judge_refs(ctx: dict[str, Any], jev) -> None:
    """JEV support for refgenomes that declared a name, not an
    accession — one batch question."""
    pending = ctx["to_judge"]
    if not pending or jev is None:
        for slot in pending:
            slot.spec_basis = "unverifiable"
        return
    q = {
        f"a4_ref_pin:{s.id}": {
            "type": "noul",
            "instructions": (
                f"Reference genome '{s.declared_value}'. Does this "
                "name exactly one genome assembly release usable "
                "without further choices?"
            ),
        }
        for s in pending
    }
    answers = jev.ask(
        {"refs": [s.declared_value for s in pending]},
        q, stage="a4",
    )
    for s in pending:
        a = answers.get(f"a4_ref_pin:{s.id}", {})
        conf = float(a.get("confidence", 0.5))
        missing = list((s.spec or {}).get("missing", []))
        required = ["assembly"] + (
            ["annotation"] if "annotation" in missing else [])
        if a.get("noul", 0) >= 0.5:
            # a verifiably-unique name IS a usable assembly pin —
            # it just isn't accession-grade, hence support < 1
            s.spec["assembly"] = s.declared_value
            s.spec["assembly_kind"] = "name"
            if "assembly" in missing:
                missing.remove("assembly")
            s.spec["missing"] = missing
            support = conf
        else:
            support = 0.0
        completeness = 1.0 - len(missing) / len(required)
        s.spec_score = round(completeness * support, 3)
        s.spec_basis = f"judged:{conf:.2f}"


def _unassigned_samples(ctx: dict[str, Any]) -> None:
    """Series-level consistency: GEO rows that no declared group
    accounts for. One informational finding per GSE."""
    for gse, assigned in ctx["gse_assignments"].items():
        sheet = ctx["gse_sheets"].get(gse) or {}
        rows = [s["accession"] for s in sheet.get("samples", [])]
        orphan = [r for r in rows if r not in assigned]
        if orphan:
            ctx["findings"].append(Finding(
                id=_new_id("finding", f"orphan:{gse}"),
                axis="sample_metadata", severity="informational",
                subject=[f"artifact:{ctx['gse_arts'].get(gse, gse)}"],
                mechanism="deterministic", confidence=1.0,
                description=f"{len(orphan)}/{len(rows)} samples in "
                            f"{gse} map to no declared group — "
                            "paper may subset the series "
                            f"({', '.join(orphan[:5])}…)",
            ))

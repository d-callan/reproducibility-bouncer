"""Thin audit-record container — Decision 9.

Graph-shaped *schema* (typed records + typed refs + evidence on every
assertion + confidence), flat Pydantic lists as the *container* — not a
vendored KnowledgeGraph. Audits are evidence trees with all-one-hop
access; merge is explicit and small (~30 lines), and conflicting
extractions surface `conflicting` rather than silently taking a max.

Refs are `{record_type}:{id}` strings resolved via `Audit.find()`.
Record field specs: specs/data-model.md.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from .evidence import Evidence

SCHEMA_VERSION = "0.1"

Ref = str  # "{record_type}:{id}"


def _new_id(record_type: str, seed: str = "") -> str:
    digest = hashlib.sha256(
        f"{record_type}|{seed}|{datetime.now(timezone.utc).isoformat()}"
        .encode()
    ).hexdigest()[:12]
    return f"{record_type[:3]}-{digest}"


def make_ref(record: "Record") -> Ref:
    return f"{record.record_type}:{record.id}"


# ---------------------------------------------------------------- units


class UnitRef(BaseModel):
    """Pointer into Document citable units — {section, unit_index}."""
    section: int
    unit_index: int


class Unit(BaseModel):
    """One citable unit: a sentence (prose) or line (table/serialized)."""
    index: int
    text: str
    span: tuple[int, int] | None = None  # char offsets in source when known


class SectionName(str, Enum):
    abstract = "abstract"
    intro = "intro"
    methods = "methods"
    results = "results"
    discussion = "discussion"
    captions = "captions"
    das = "das"
    cas = "cas"
    refs = "refs"
    supplement = "supplement"
    other = "other"


class Section(BaseModel):
    name: SectionName
    units: list[Unit] = Field(default_factory=list)


# ---------------------------------------------------------------- records


class Record(BaseModel):
    """Every audit record: id + type + schema_version."""
    id: str = ""
    record_type: str = ""
    schema_version: str = SCHEMA_VERSION

    def ref(self) -> Ref:
        return make_ref(self)


class Document(Record):
    record_type: str = "document"
    source: dict[str, str | None] = Field(default_factory=dict)
    # doi / pmid / pmcid / arxiv_id / url / local_path
    source_used: Literal["vor", "pmc_oa", "preprint", "draft"] | None = None
    retrieval_url: str | None = None
    retrieved_at: datetime | None = None
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    journal: str = ""
    year: int | None = None
    sections: list[Section] = Field(default_factory=list)
    inline_stats: list[dict[str, Any]] = Field(default_factory=list)
    supplements: list[dict[str, Any]] = Field(default_factory=list)
    parse_path: Literal["grobid", "europepmc_jats", "fallback",
                        "pandoc"] = "fallback"
    coverage_notes: list[str] = Field(default_factory=list)


class OutputSpec(BaseModel):
    result_key: str  # e.g. "fig2b.hr", "table1.n"
    kind: Literal["figure", "table", "scalar", "other"]
    label: str = ""
    expected: dict[str, Any] | None = None
    # {value, ci, tolerance: {rel, abs}} — null tolerance → defaults
    evidence: list[UnitRef] = Field(default_factory=list)


class Claim(BaseModel):
    text: str
    evidence: list[UnitRef] = Field(default_factory=list)


class AnalysisUnit(Record):
    record_type: str = "analysis_unit"
    headline: bool = False
    rank: int = 0
    outputs: list[OutputSpec] = Field(default_factory=list)
    inputs: list[Ref] = Field(default_factory=list)   # → Slot
    methods: list[Ref] = Field(default_factory=list)  # → Slot
    claims: list[Claim] = Field(default_factory=list)
    source_units: list[UnitRef] = Field(default_factory=list)


SlotType = Literal[
    "versioned_identifier", "dataset", "software", "parameter",
    "sample_grouping", "reference_genome", "other",
]
Provenance = Literal[
    "reported", "observed", "inferred", "confirmed", "conflicting",
    "confirmed_by_user",
]
UnknownKind = Literal[
    "not_yet_checked", "cannot_determine", "needs_human_judgement",
]


class BoundInstance(BaseModel):
    artifact_id: Ref
    instance: str = ""  # release / version / rowset


class Slot(Record):
    record_type: str = "slot"
    unit_id: Ref = ""
    name: str = ""
    slot_type: SlotType = "other"
    declared_value: str | None = None
    bound_instance: BoundInstance | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    provenance: Provenance = "reported"
    unknown: UnknownKind | None = None
    check_path: Literal[
        "generic_invariant", "registry_probe", "judged", "unchecked",
    ] = "unchecked"
    evidence: list[UnitRef] = Field(default_factory=list)
    # A4 materialization: the concrete instance spec this slot pins,
    # its completeness x support score, and the evidence basis. A slot
    # whose spec SHOULD exist but can't be produced scores 0; slots
    # never evaluated keep spec_score None (distinct from NA — see
    # report axes).
    spec: dict[str, Any] | None = None
    spec_score: float | None = None
    spec_basis: str = ""


class Identifier(BaseModel):
    family: Literal["accession", "doi", "url", "path"]
    raw: str
    normalized: str


class Resolved(BaseModel):
    repo: str
    object_id: str = ""
    version: str = ""
    url: str = ""
    evidence: list[Ref] = Field(default_factory=list)  # → Evidence idx


class Artifact(Record):
    record_type: str = "artifact"
    kind: Literal["dataset", "code", "container", "reference",
                  "other"] = "other"
    identifier: Identifier | None = None
    resolved: Resolved | None = None
    availability: Literal[
        "public", "controlled-access", "on-request", "absent",
    ] | None = None
    declared: dict[str, Any] = Field(default_factory=dict)
    observed: dict[str, Any] = Field(default_factory=dict)
    license: str | None = None
    mutability_flags: list[str] = Field(default_factory=list)
    source_units: list[UnitRef] = Field(default_factory=list)


class Finding(Record):
    record_type: str = "finding"
    axis: Literal[
        "artifact_access", "input_binding", "sample_metadata",
        "methods_placement", "executability", "result_verifiability",
        "self_sufficiency", "reporting_compliance", "other",
    ] = "other"
    severity: Literal["blocking", "warning", "informational"] = (
        "informational"
    )
    subject: list[Ref] = Field(default_factory=list)
    mechanism: Literal["deterministic", "jev", "generator"] = (
        "deterministic"
    )
    jev_question_id: str | None = None
    confidence: float = 1.0
    coverage_caveat: bool = False
    derived_from: list[Ref] = Field(default_factory=list)
    description: str = ""
    suggestion: str = ""
    question_id: Ref | None = None


class ClarificationRequest(Record):
    record_type: str = "clarification_request"
    blocking: bool = False
    question: str = ""
    context_refs: list[Ref] = Field(default_factory=list)
    resolves_unknown: UnknownKind = "not_yet_checked"
    answer: dict[str, Any] | None = None
    # {text, answered_by: "user", at}
    mid_run: bool = False


class Milestone(BaseModel):
    m: Literal["M1", "M2", "M3", "M4"]
    status: Literal[
        "reached", "failed", "M1-blocked:access", "unchecked",
    ]
    ts: datetime | None = None


class Stall(BaseModel):
    milestone: str
    proximate_cause: str = ""
    mapped_finding_id: Ref | None = None


class Concordance(BaseModel):
    result_key: str
    expected: Any = None
    produced: Any = None
    tolerance_used: dict[str, float] = Field(default_factory=dict)
    verdict: Literal["match", "mismatch", "unchecked"] = "unchecked"


class ReproRun(Record):
    record_type: str = "repro_run"
    k_index: int = 0
    model: dict[str, str] = Field(default_factory=dict)
    orbit_version: str = ""
    pi_version: str = ""
    workdir: str = ""
    session_dir: str = ""
    gate: dict[str, Any] = Field(default_factory=dict)
    milestones: list[Milestone] = Field(default_factory=list)
    stall: Stall | None = None
    clarifications: list[Ref] = Field(default_factory=list)
    concordance: list[Concordance] = Field(default_factory=list)
    cost: dict[str, float] = Field(default_factory=dict)
    wall_s: float = 0.0
    status: Literal[
        "pass", "fail", "gate_blocked", "indeterminate", "error",
    ] = "error"


# ---------------------------------------------------------------- audit


class Audit(BaseModel):
    """The flat container: lists of records + typed refs (Decision 9)."""

    id: str = Field(default_factory=lambda: _new_id("audit"))
    mode: Literal["published", "draft"] = "published"
    created: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    document: Document | None = None
    units: list[AnalysisUnit] = Field(default_factory=list)
    slots: list[Slot] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    questions: list[ClarificationRequest] = Field(default_factory=list)
    runs: list[ReproRun] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    _LISTS = (
        "units", "slots", "artifacts", "evidence", "findings",
        "questions", "runs",
    )

    def all_records(self) -> list[Record]:
        out: list[Record] = []
        if self.document:
            out.append(self.document)
        for name in self._LISTS:
            out.extend(
                r for r in getattr(self, name) if isinstance(r, Record)
            )
        return out

    def find(self, ref: Ref) -> Record | None:
        """One-hop ref resolution: 'slot:s-abc' -> the record."""
        if not ref or ":" not in ref:
            return None
        _, rid = ref.split(":", 1)
        for r in self.all_records():
            if r.id == rid:
                return r
        return None

    # -- merge -----------------------------------------------------------

    def _natural_key(self, rec: Record) -> str:
        """Dedup key per record type — cheap, stable, content-based."""
        if isinstance(rec, Slot):
            return f"{rec.unit_id}|{rec.name}"
        if isinstance(rec, Artifact):
            ident = rec.identifier.normalized if rec.identifier else ""
            return ident or rec.id
        if isinstance(rec, Finding):
            subj = ",".join(sorted(rec.subject))
            return f"{rec.axis}|{subj}|{rec.description[:80]}"
        if isinstance(rec, ClarificationRequest):
            return rec.question[:120]
        if isinstance(rec, ReproRun):
            return f"{rec.k_index}|{rec.model.get('model', '')}"
        if isinstance(rec, AnalysisUnit):
            keys = ",".join(sorted(o.result_key for o in rec.outputs))
            return keys or f"rank{rec.rank}"
        return rec.id

    def merge(self, other: "Audit") -> list[Finding]:
        """Merge `other` into self. Dedup by (type, natural key); on
        conflicting same-key records keep BOTH and emit a `conflicting`
        finding — never silent max (Decision 9). Returns the conflict
        findings appended."""
        conflicts: list[Finding] = []
        ours = {
            (r.record_type, self._natural_key(r)): r
            for r in self.all_records()
        }
        for rec in other.all_records():
            key = (rec.record_type, other._natural_key(rec))
            existing = ours.get(key)
            if existing is None:
                self._append(rec)
                continue
            if existing.model_dump() == rec.model_dump():
                continue
            f = Finding(
                id=_new_id("finding", rec.id),
                severity="warning",
                mechanism="deterministic",
                subject=[existing.ref(), rec.ref()],
                description=(
                    f"conflicting {rec.record_type} records for "
                    f"{key[1]!r} — both kept"
                ),
            )
            self._append(rec)  # keep both, per spec
            self.findings.append(f)
            conflicts.append(f)
        self.evidence.extend(
            e for e in other.evidence if e not in self.evidence
        )
        return conflicts

    def _append(self, rec: Record) -> None:
        name = rec.record_type + "s"
        if rec.record_type == "clarification_request":
            name = "questions"
        elif rec.record_type == "repro_run":
            name = "runs"
        elif rec.record_type == "analysis_unit":
            name = "units"
        elif rec.record_type == "document":
            self.document = rec  # type: ignore[assignment]
            return
        lst = getattr(self, name, None)
        if isinstance(lst, list):
            lst.append(rec)

    # -- io ----------------------------------------------------------------

    def to_json(self, **kw: Any) -> str:
        return self.model_dump_json(**kw)

    @classmethod
    def from_json(cls, data: str) -> "Audit":
        return cls.model_validate(json.loads(data))

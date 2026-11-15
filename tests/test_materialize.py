"""A4b materialization: spec contracts, scoring, axis semantics."""

import gzip

import httpx

from reproducibility_bouncer.auditrecord import (
    AnalysisUnit,
    Artifact,
    Audit,
    BoundInstance,
    Document,
    Identifier,
    Section,
    SectionName,
    Slot,
    Unit,
    UnitRef,
)
from reproducibility_bouncer.materialize import (
    _match_groups,
    materialize_slots,
)
from reproducibility_bouncer.report import build_report

MATRIX = """\
!Sample_geo_accession\t"GSM1"\t"GSM2"\t"GSM3"
!Sample_title\t"PA14 wild-type rep1"\t"PA14 mutant rep1"\t"PA14 wild-type rep2"
!Sample_source_name_ch1\t"Pseudomonas aeruginosa UCBPP-PA14"\t"Pseudomonas aeruginosa mutant"\t"Pseudomonas aeruginosa UCBPP-PA14"
!Sample_characteristics_ch1\t"strain: UCBPP-PA14"\t"strain: mutant-x"\t"strain: UCBPP-PA14"
!Sample_characteristics_ch1\t"treatment: control"\t"treatment: none"\t"treatment: drugged"
"""


def _soft_transport() -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "ftp.ncbi.nlm.nih.gov":
            return httpx.Response(
                200, content=gzip.compress(MATRIX.encode()))
        return httpx.Response(404, text="unmocked")
    return httpx.MockTransport(handler)


class _FakeJev:
    def __init__(self, answers=None):
        self.answers = answers or {}

    def ask(self, state, questions, stage=""):
        return {
            qid: self.answers.get(
                qid.split(":")[0],
                {"type": "noul", "noul": 0.5, "confidence": 0.5},
            )
            for qid in questions
        }


def _unit(uid="u1", inputs=()):
    return AnalysisUnit(id=uid, inputs=list(inputs))


def _slot(name, stype, value, uid="u1"):
    return Slot(id=f"s-{name}", unit_id=f"analysis_unit:{uid}",
                name=name, slot_type=stype, declared_value=value)


def _gse_artifact():
    return Artifact(
        id="art-gse", kind="dataset",
        identifier=Identifier(
            family="accession", raw="GSE999999",
            normalized="GSE999999"),
        availability="public",
    )


def test_group_resolves_against_soft_sheet():
    ds = _slot("reads", "dataset", "GSE999999")
    ds.bound_instance = BoundInstance(artifact_id="artifact:art-gse")
    grp = _slot("wt", "sample_grouping", "UCBPP-PA14")
    unit = _unit(inputs=["slot:s-reads", "slot:s-wt"])
    findings, questions, stats, ev = materialize_slots(
        [unit], [ds, grp], [_gse_artifact()],
        jev=_FakeJev(), transport=_soft_transport())
    assert grp.spec_score == 1.0
    assert sorted(grp.spec["matched_samples"]) == ["GSM1", "GSM3"]
    assert grp.spec_basis == "repository-verified"
    # one series row (GSM2) maps to no declared group -> orphan note
    assert any("no declared group" in f.description
               and f.axis == "sample_metadata"
               and f.severity == "informational" for f in findings)
    assert stats["n_spec_evaluated"] == 2


def test_group_not_in_sheet_scores_zero_with_question():
    ds = _slot("reads", "dataset", "GSE999999")
    ds.bound_instance = BoundInstance(artifact_id="artifact:art-gse")
    grp = _slot("ghost", "sample_grouping", "strain-omega")
    unit = _unit(inputs=["slot:s-reads", "slot:s-ghost"])
    findings, questions, stats, ev = materialize_slots(
        [unit], [ds, grp], [_gse_artifact()],
        jev=_FakeJev(), transport=_soft_transport())
    assert grp.spec_score == 0.0
    assert grp.spec["matched_samples"] == []
    assert any("matches no sample" in f.description
               for f in findings)
    assert any(not q.blocking and "strain-omega" in q.question
               for q in questions)


def test_group_falls_back_to_document_level_gse():
    # dataset slot declared descriptively (no accession) -> unbound;
    # group still resolvable via the paper's only GEO artifact
    ds = _slot("reads", "dataset", "RNA-seq data")
    grp = _slot("wt", "sample_grouping", "UCBPP-PA14")
    unit = _unit(inputs=["slot:s-reads", "slot:s-wt"])
    materialize_slots(
        [unit], [ds, grp], [_gse_artifact()],
        jev=_FakeJev(), transport=_soft_transport())
    assert grp.spec_score == 1.0
    assert grp.spec["dataset"] == "GSE999999"
    assert grp.spec["datasets_checked"] == ["GSE999999"]


def test_group_judged_match_affirmed():
    # 'mutant strain' vs sheet value 'mutant-x': shared token but
    # neither subset -> no deterministic match -> JEV candidate
    ds = _slot("reads", "dataset", "GSE999999")
    ds.bound_instance = BoundInstance(artifact_id="artifact:art-gse")
    grp = _slot("mut", "sample_grouping", "mutant strain")
    unit = _unit(inputs=["slot:s-reads", "slot:s-mut"])
    jev = _FakeJev({"a4_grp_map": {"type": "noul", "noul": 0.9,
                                   "confidence": 0.8}})
    materialize_slots(
        [unit], [ds, grp], [_gse_artifact()],
        jev=jev, transport=_soft_transport())
    assert grp.spec["matched_samples"] == ["GSM2"]
    assert grp.spec["matched_via"] == [
        {"field": "strain", "value": "mutant x",
         "mechanism": "judged"}]
    assert grp.spec_score == 0.8
    assert grp.spec_basis == "judged:0.80"


class _FakeLlm:
    def __init__(self, resp: str):
        self.resp = resp

    def complete(self, system: str, user: str) -> str:
        return self.resp


def _doc(text: str = "Cells were mock treated before assay."):
    return Document(id="d", sections=[Section(
        name=SectionName.methods,
        units=[Unit(index=1, text=text)])])


def test_group_llm_proposed_affirmed():
    # 'mock treated' shares no token with any sheet value -> LLM
    # proposes treatment=control from the paper context -> JEV affirms
    ds = _slot("reads", "dataset", "GSE999999")
    ds.bound_instance = BoundInstance(artifact_id="artifact:art-gse")
    grp = _slot("vc", "sample_grouping", "mock treated")
    grp.evidence = [UnitRef(section=0, unit_index=1)]
    unit = _unit(inputs=["slot:s-reads", "slot:s-vc"])
    llm = _FakeLlm(
        '{"mappings": [{"slot": 0, "gse": "GSE999999",'
        ' "field": "treatment", "value": "control"}]}')
    jev = _FakeJev({"a4_grp_map": {"type": "noul", "noul": 0.9,
                                   "confidence": 0.8}})
    materialize_slots(
        [unit], [ds, grp], [_gse_artifact()], jev=jev, llm=llm,
        document=_doc(), transport=_soft_transport())
    assert grp.spec["matched_samples"] == ["GSM1"]
    assert grp.spec["matched_via"] == [
        {"field": "treatment", "value": "control",
         "mechanism": "proposed+judged"}]
    assert grp.spec_score == 0.8
    assert grp.spec_basis == "judged:0.80"


def test_group_llm_proposal_hallucinated_dropped():
    # proposer invents a field/value not in the sheet vocabulary ->
    # deterministic guard drops it -> absence, no judge involvement
    ds = _slot("reads", "dataset", "GSE999999")
    ds.bound_instance = BoundInstance(artifact_id="artifact:art-gse")
    grp = _slot("vc", "sample_grouping", "mock treated")
    unit = _unit(inputs=["slot:s-reads", "slot:s-vc"])
    llm = _FakeLlm(
        '{"mappings": [{"slot": 0, "gse": "GSE999999",'
        ' "field": "genotype", "value": "dox"}]}')
    findings, questions, _, _ = materialize_slots(
        [unit], [ds, grp], [_gse_artifact()], jev=_FakeJev(),
        llm=llm, document=_doc(), transport=_soft_transport())
    assert grp.spec_score == 0.0
    assert any("matches no sample" in f.description
               for f in findings)
    assert any("mock treated" in q.question for q in questions)


def test_group_judged_denied_absent():
    ds = _slot("reads", "dataset", "GSE999999")
    ds.bound_instance = BoundInstance(artifact_id="artifact:art-gse")
    grp = _slot("mut", "sample_grouping", "mutant strain")
    unit = _unit(inputs=["slot:s-reads", "slot:s-mut"])
    jev = _FakeJev({"a4_grp_map": {"type": "noul", "noul": 0.1,
                                   "confidence": 0.9}})
    findings, questions, _, _ = materialize_slots(
        [unit], [ds, grp], [_gse_artifact()],
        jev=jev, transport=_soft_transport())
    assert grp.spec_score == 0.0
    assert grp.spec_basis == "judged:absent"
    assert any("matches no sample" in f.description
               for f in findings)
    assert any("mutant strain" in q.question for q in questions)


def test_group_without_dataset_scores_zero():
    grp = _slot("wt", "sample_grouping", "UCBPP-PA14")
    unit = _unit(inputs=["slot:s-wt"])
    findings, _, stats, _ = materialize_slots(
        [unit], [grp], [], jev=_FakeJev())
    assert grp.spec_score == 0.0
    assert grp.spec_basis == "declared-only"
    assert stats["n_spec_zero"] == 1


def test_refgenome_accession_pins_deterministically():
    ref = _slot("asm", "reference_genome", "GCF_000006765.1")
    unit = _unit(inputs=["slot:s-asm"])
    materialize_slots([unit], [ref], [], jev=_FakeJev())
    assert ref.spec["assembly"] == "GCF_000006765.1"
    assert ref.spec_score == 1.0


def test_refgenome_name_judged_unique():
    ref = _slot("asm", "reference_genome", "UCBPP-PA14")
    unit = _unit(inputs=["slot:s-asm"])
    jev = _FakeJev({"a4_ref_pin": {"type": "noul", "noul": 0.9,
                                   "confidence": 0.8}})
    materialize_slots([unit], [ref], [], jev=jev)
    assert ref.spec["assembly"] == "UCBPP-PA14"
    assert ref.spec["assembly_kind"] == "name"
    assert ref.spec_score == 0.8


def test_refgenome_name_judged_nonunique_scores_zero():
    ref = _slot("asm", "reference_genome", "E. coli")
    unit = _unit(inputs=["slot:s-asm"])
    jev = _FakeJev({"a4_ref_pin": {"type": "noul", "noul": 0.1,
                                   "confidence": 0.9}})
    findings, *_ = materialize_slots([unit], [ref], [], jev=jev)
    assert ref.spec_score == 0.0
    assert any("no instance spec is producible" in f.description
               for f in findings)


def test_refgenome_annotation_required_by_sibling():
    ref = _slot("asm", "reference_genome", "GCF_000006765.1")
    ann = _slot("genes", "dataset", "GENCODE v44 gtf")
    unit = _unit(inputs=["slot:s-asm", "slot:s-genes"])
    materialize_slots([unit], [ref, ann], [], jev=_FakeJev())
    # assembly pinned but annotation unconsumed -> completeness 0.5
    assert ref.spec_score == 0.5
    assert ref.spec["missing"] == ["annotation"]


def test_generic_bound_vs_declared_vs_none():
    bound = _slot("d", "dataset", "GSE999999")
    bound.bound_instance = BoundInstance(
        artifact_id="artifact:art-gse", instance="v1")
    declared = _slot("t", "software", "DESeq2 1.34")
    nothing = _slot("p", "parameter", None)
    unit = _unit(inputs=["slot:s-d", "slot:s-t", "slot:s-p"])
    materialize_slots([unit], [bound, declared, nothing], [],
                      jev=_FakeJev())
    assert bound.spec_score == 1.0
    assert declared.spec_score == 0.5
    assert nothing.spec_score == 0.0


def test_match_groups_subset_semantics():
    samples = [
        {"accession": "G1", "title": "x",
         "source": "", "characteristics":
         {"strain": ["UCBPP-PA14 WT"], "tx": ["drug"]}},
        {"accession": "G2", "title": "PA14 but no strain key",
         "source": "", "characteristics": {}},
        {"accession": "G3", "title": "y",
         "source": "", "characteristics":
         {"strain": ["mutant-x"]}},
    ]
    # 'UCBPP-PA14' declares strain-grade name; repository short form
    # 'ucbpp pa14 wt' is a superset -> field match on G1
    hits, via = _match_groups("UCBPP-PA14", samples)
    assert hits == ["G1"]
    assert via == [{"field": "strain", "value": "ucbpp pa14 wt",
                    "mechanism": "deterministic"}]
    # 'PA14' field-matches G1 (subset) and blob-matches title-only G2
    hits, via = _match_groups("PA14", samples)
    assert hits == ["G1", "G2"]
    assert {v["field"] for v in via} == {"strain", "(free text)"}
    # a declared value naming a different strain matches nothing
    assert _match_groups("mutant-omega", samples) == ([], [])
    assert _match_groups("zzz", samples) == ([], [])
    # multi-value declarations still require field-level identity
    hits, _ = _match_groups("mutant-x", samples)
    assert hits == ["G3"]


def _audit(slots, stages, findings=()):
    return Audit(
        id="a", slots=slots, findings=list(findings),
        meta={"stages_run": stages},
    )


def test_axis_na_when_domain_empty():
    rep = build_report(
        _audit([], ["a0", "a1", "a2", "a3", "a4", "a5"]))
    assert rep["axes"]["sample_metadata"]["level"] == "na"
    assert rep["axes"]["artifact_access"]["level"] == "na"
    assert rep["axes"]["input_binding"]["level"] == "na"


def test_axis_unchecked_when_stage_missing():
    rep = build_report(_audit([], ["a0", "a1"]))
    for axis in ("input_binding", "sample_metadata",
                 "methods_placement", "executability"):
        assert rep["axes"][axis]["level"] == "unchecked"


def test_axis_ok_requires_ran_stage_and_domain():
    slots = [_slot("wt", "sample_grouping", "x")]
    rep = build_report(
        _audit(slots, ["a0", "a1", "a2", "a3", "a4", "a5"]))
    assert rep["axes"]["sample_metadata"]["level"] == "ok"

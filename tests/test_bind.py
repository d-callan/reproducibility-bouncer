from reproducibility_bouncer.auditrecord import (
    AnalysisUnit,
    Artifact,
    Identifier,
    Resolved,
    Slot,
)
from reproducibility_bouncer.bind import bind_slots
from reproducibility_bouncer.jev import JevClient


def _slot(name, stype, value, uid="u1"):
    return Slot(
        id=f"s-{name}", unit_id=f"analysis_unit:{uid}",
        name=name, slot_type=stype, declared_value=value,
    )


def test_null_slot_blocks_with_question():
    slots = [_slot("reads", "dataset", None)]
    findings, questions, cov = bind_slots(
        [], slots, [], jev=JevClient(mock=True),
    )
    assert any(f.severity == "blocking" for f in findings)
    assert len(questions) == 1 and questions[0].blocking


def test_unversioned_identifier_findings():
    slots = [
        _slot("asm", "reference_genome", "GCA_000001405"),
        _slot("tool", "software", "STAR v2.7.10a"),
    ]
    findings, questions, cov = bind_slots(
        [], slots, [], jev=JevClient(mock=True),
    )
    kinds = {(f.description[:20]) for f in findings}
    assert any("carries no" in f.description for f in findings)
    # STAR v2.7.10a has a version -> no generic-invariant finding
    assert not any(f.subject == ["slot:s-tool"] for f in findings)


def test_ambiguous_value_flagged():
    slots = [_slot("asm", "reference_genome",
                   "GRCh38 or GRCh37")]
    findings, _, _ = bind_slots([], slots, [], jev=JevClient(mock=True))
    assert any("ambiguous" in f.description for f in findings)


def test_registry_probe_binds_artifact():
    art = Artifact(
        id="a1", kind="dataset",
        identifier=Identifier(family="accession", raw="GSE999999",
                              normalized="GSE999999"),
        resolved=Resolved(repo="geo", object_id="GSE999999",
                          version="", url="u"),
        availability="public",
    )
    slots = [_slot("reads", "dataset", "GSE999999")]
    findings, _, cov = bind_slots(
        [], slots, [art], jev=JevClient(mock=True),
    )
    assert slots[0].bound_instance is not None
    assert slots[0].bound_instance.artifact_id == "artifact:a1"
    assert slots[0].check_path == "registry_probe"
    assert not findings


def test_generic_family_goes_judged():
    slots = [_slot("mystery", "other", "the foo corpus")]
    findings, _, cov = bind_slots(
        [], slots, [], jev=JevClient(mock=True),
    )
    assert slots[0].check_path == "judged"
    assert cov["n_judged"] == 1
    # mock jev noul=0.5 -> no non-unique finding
    assert not findings


def test_coverage_stats():
    slots = [
        _slot("a", "dataset", None),
        _slot("b", "software", "x v1.0"),
        _slot("c", "other", "weird thing"),
    ]
    _, _, cov = bind_slots([], slots, [], jev=JevClient(mock=True))
    assert cov["n_slots"] == 3
    assert 0 <= cov["binding_check_coverage"] <= 1

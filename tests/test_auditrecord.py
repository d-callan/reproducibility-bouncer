import json

from reproducibility_bouncer.auditrecord import (
    AnalysisUnit,
    Artifact,
    Audit,
    Document,
    Finding,
    Identifier,
    Slot,
    _new_id,
)


def _slot(unit: str, name: str, value: str | None = None):
    return Slot(
        id=_new_id("slot", name), unit_id=unit, name=name,
        slot_type="dataset", declared_value=value,
    )


def test_find_one_hop():
    a = Audit()
    s = _slot("u1", "reads")
    a.slots.append(s)
    assert a.find(s.ref()) is s
    assert a.find("slot:nope") is None


def test_merge_dedups_identical():
    a, b = Audit(), Audit()
    s1, s2 = _slot("u1", "reads", "GSE1"), _slot("u1", "reads", "GSE1")
    s2.id = s1.id  # identical record under a copied id
    a.slots.append(s1)
    b.slots.append(s2)
    conflicts = a.merge(b)
    assert conflicts == []
    assert len(a.slots) == 1


def test_merge_keeps_conflict_both_sides():
    a, b = Audit(), Audit()
    a.slots.append(_slot("u1", "reads", "GSE1"))
    b.slots.append(_slot("u1", "reads", "GSE2"))
    conflicts = a.merge(b)
    assert len(conflicts) == 1
    assert len(a.slots) == 2  # both kept, never silent max
    assert conflicts[0].severity == "warning"
    assert "conflicting" in conflicts[0].description


def test_merge_document():
    a, b = Audit(), Audit()
    b.document = Document(id=_new_id("document"), title="t")
    a.merge(b)
    assert a.document is not None and a.document.title == "t"


def test_roundtrip_json():
    a = Audit(mode="draft")
    a.units.append(AnalysisUnit(id=_new_id("unit"), headline=True))
    a.artifacts.append(Artifact(
        id=_new_id("artifact"),
        identifier=Identifier(
            family="accession", raw="gse251671",
            normalized="GSE251671",
        ),
    ))
    b = Audit.from_json(a.to_json())
    assert b.mode == "draft"
    assert b.artifacts[0].identifier.normalized == "GSE251671"


def test_finding_schema():
    f = Finding(
        id=_new_id("finding"), axis="input_binding",
        severity="blocking", mechanism="jev",
        jev_question_id="a4_x", confidence=0.7,
    )
    assert f.record_type == "finding"
    assert f.confidence == 0.7

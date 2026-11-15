import pytest

from reproducibility_bouncer.auditrecord import (
    Document,
    Section,
    SectionName,
    Unit,
)
from reproducibility_bouncer.extract import (
    _cites,
    _merge_proposals,
    _to_records,
    extract_units,
)
from reproducibility_bouncer.jev import JevClient

from fakes import FakeLlm


def _doc():
    return Document(
        id="d1",
        sections=[
            Section(name=SectionName.das, units=[
                Unit(index=1, text="Data are at GSE999999."),
            ]),
            Section(name=SectionName.methods, units=[
                Unit(index=1, text="We mapped reads with STAR v2.7.10a."),
            ]),
            Section(name=SectionName.results, units=[
                Unit(index=1, text="Figure 1 shows the result."),
            ]),
        ],
    )


def test_cites_validates():
    doc = _doc()
    refs = _cites(["0.1", "1.1", "9.9", "bogus", {"section": 2,
                                                 "unit_index": 1}],
                  doc)
    assert len(refs) == 3
    assert (refs[0].section, refs[0].unit_index) == (0, 1)
    assert (refs[2].section, refs[2].unit_index) == (2, 1)


def test_merge_by_result_key():
    props = [
        {"title": "a", "cites": ["1.1"],
         "outputs": [{"result_key": "fig1.a"}]},
        {"title": "a2", "cites": ["2.2"],
         "outputs": [{"result_key": "fig1.a"}]},
        {"title": "b", "cites": ["9.9"],
         "outputs": [{"result_key": "fig2.b"}]},
    ]
    merged = _merge_proposals(props)
    assert len(merged) == 2
    assert set(map(str, merged[0]["cites"])) == {"1.1", "2.2"}


def test_to_records_builds_slots():
    doc = _doc()
    units, slots = _to_records(
        FakeLlm().payload["units"], doc,
    )
    assert len(units) == 1
    u = units[0]
    assert u.outputs[0].result_key == "fig1.a"
    ss = slots[u.id]
    assert len(ss) == 2
    ds = [s for s in ss if s.name == "reads"][0]
    assert ds.slot_type == "dataset"
    assert ds.declared_value == "GSE999999"
    assert u.inputs == [f"slot:{ds.id}"]


def test_extract_units_end_to_end():
    doc = _doc()
    jev = JevClient(mock=True)
    units, slots, findings, cov = extract_units(
        doc, jev=jev, llm=FakeLlm(),
    )
    assert len(units) == 1
    # mock jev choice picks first option -> headline stays True
    assert units[0].headline is True
    assert len(slots) == 2
    assert units[0].id in cov  # a2_extract_coverage recorded


def test_extract_drops_unverifiable_unit():
    class RejectingJev(JevClient):
        def __init__(self):
            super().__init__(mock=True)

        def ask(self, state, questions, stage=""):
            out = super().ask(state, questions, stage=stage)
            for qid in out:
                if qid.startswith("a2_unit_real"):
                    out[qid]["noul"] = 0.1  # unsupported
            return out

    units, slots, findings, cov = extract_units(
        _doc(), jev=RejectingJev(), llm=FakeLlm(),
    )
    assert units == [] and slots == []
    assert any("could not verify" in f.description for f in findings)


def test_bad_json_yields_no_units():
    class BadLlm:
        def complete(self, s, u, temperature=None):
            return "not json"

    units, slots, findings, cov = extract_units(
        _doc(), jev=JevClient(mock=True), llm=BadLlm(),
    )
    assert units == [] and findings == []

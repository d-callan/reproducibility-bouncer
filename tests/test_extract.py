import json

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
    _prompt_blocks,
    _propose,
    _to_records,
    extract_units,
)
from reproducibility_bouncer.jev import JevClient
from reproducibility_bouncer.llm import LlmError

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


def _doc_big(units_per_section=6):
    """Doc sized so a small REPRO_EXTRACT_BLOCK_CHARS forces
    multiple prompt blocks. Includes an abstract (context section)."""
    def units(prefix):
        return [
            Unit(index=i + 1, text=f"{prefix} sentence {i + 1}. " * 4)
            for i in range(units_per_section)
        ]
    return Document(
        id="d2",
        sections=[
            Section(name=SectionName.abstract,
                    units=[Unit(index=1, text="We study X.")]),
            Section(name=SectionName.methods, units=units("Methods")),
            Section(name=SectionName.results, units=units("Results")),
            Section(name=SectionName.das, units=units("Data")),
        ],
    )


def test_prompt_blocks_env_budget(monkeypatch):
    doc = _doc_big()
    assert len(_prompt_blocks(doc)) == 1  # under the 25k default
    monkeypatch.setenv("REPRO_EXTRACT_BLOCK_CHARS", "500")
    blocks = _prompt_blocks(doc)
    assert len(blocks) > 1
    assert "## section -1" in blocks[0]


def test_propose_tolerates_block_failure(monkeypatch):
    """A dead LLM call skips its block — coverage note records the
    gap, other blocks' proposals still merge."""
    monkeypatch.setenv("REPRO_EXTRACT_BLOCK_CHARS", "500")

    class FlakyLlm:
        def __init__(self):
            self.calls = []

        def complete(self, system, user, temperature=None):
            self.calls.append(user)
            if len(self.calls) == 1:
                raise LlmError("gateway down")
            return json.dumps({"units": [{
                "title": "u", "cites": ["2.1"],
                "outputs": [{"result_key": "r1"}]}]})

    doc = _doc_big()
    llm = FlakyLlm()
    merged = _propose(doc, llm)
    assert len(llm.calls) > 1          # multiple blocks attempted
    assert len(merged) == 1            # later block still proposed
    assert any("skipped prompt block 0" in n
               for n in doc.coverage_notes)


def test_propose_context_not_duplicated(monkeypatch):
    """Block 0 already embeds context as '## section -1'; the
    '## context' header must not be prepended a second time."""
    monkeypatch.setenv("REPRO_EXTRACT_BLOCK_CHARS", "500")

    class RecLlm:
        def __init__(self):
            self.calls = []

        def complete(self, system, user, temperature=None):
            self.calls.append(user)
            return json.dumps({"units": []})

    llm = RecLlm()
    _propose(_doc_big(), llm)
    assert len(llm.calls) > 1
    assert "## context (abstract/discussion)" not in llm.calls[0]
    assert "## context (abstract/discussion)" in llm.calls[1]

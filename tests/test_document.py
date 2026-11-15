import pytest

from reproducibility_bouncer.clients import ClientError
from reproducibility_bouncer.document import (
    _parse_jats,
    _parse_plain_text,
    acquire,
)
from reproducibility_bouncer.auditrecord import Document, SectionName
from reproducibility_bouncer.intake import classify

from fakes import JATS_FIXTURE, transport


def _secs(doc, name):
    return [s for s in doc.sections if s.name == name]


def test_jats_parse_full():
    doc = _parse_jats(JATS_FIXTURE, Document())
    assert doc.title == "A test article"
    assert doc.journal == "Journal of Tests"
    assert doc.year == 2024
    assert doc.authors == ["Jane Doe"]
    assert doc.parse_path == "europepmc_jats"

    assert _secs(doc, SectionName.abstract)
    assert _secs(doc, SectionName.intro)
    assert _secs(doc, SectionName.methods)
    assert _secs(doc, SectionName.results)
    assert _secs(doc, SectionName.das)
    assert _secs(doc, SectionName.refs)
    assert _secs(doc, SectionName.captions)

    das_text = " ".join(
        u.text for s in _secs(doc, SectionName.das) for u in s.units
    )
    assert "GSE999999" in das_text


def test_jats_supplement_manifest():
    doc = _parse_jats(JATS_FIXTURE, Document())
    assert len(doc.supplements) == 1
    assert doc.supplements[0]["filename"] == "supp_table1.xlsx"
    assert doc.supplements[0]["fetched"] is False


def test_jats_inline_stats():
    doc = _parse_jats(JATS_FIXTURE, Document())
    assert len(doc.inline_stats) == 1
    st = doc.inline_stats[0]
    assert st["test"] == "t"
    assert st["statistic"] == 3.21
    assert st["df"] == "18"
    assert "<" in st["p_reported"]


def test_plain_text_fallback():
    text = (
        "INTRODUCTION\n"
        "Some background sentence. Another one.\n"
        "METHODS\n"
        "We used RNA-seq. Reads were mapped with STAR v2.7.\n"
        "RESULTS\n"
        "Genes changed significantly (F(2, 30) = 5.4, p = 0.01).\n"
    )
    doc = _parse_plain_text(text, Document(), parse_path="fallback")
    names = {s.name for s in doc.sections}
    assert SectionName.methods in names
    assert SectionName.results in names
    assert doc.inline_stats[0]["test"] == "F"
    assert any("heading heuristics" in n for n in doc.coverage_notes)


def test_acquire_jats_path():
    intake = classify("doi:10.1234/test.1")
    doc, ev = acquire(intake, transport=transport())
    assert doc.source_used == "pmc_oa"
    assert doc.parse_path == "europepmc_jats"
    assert doc.source["pmcid"] == "PMC9999999"
    assert {e.source for e in ev} >= {
        "europepmc", "crossref",
    }


def test_acquire_pdf_fallback(monkeypatch):
    """No JATS -> unpaywall PDF path (bytes fail PyMuPDF -> note)."""
    monkeypatch.setenv("UNPAYWALL_EMAIL", "test@example.org")
    intake = classify("doi:10.1234/test.1")
    doc, ev = acquire(intake, transport=transport(jats=None))
    assert doc.source_used == "vor"  # publisher host_type
    assert doc.parse_path != "europepmc_jats"
    # fake pdf bytes fail parsing -> coverage note records the failure
    assert any("pdf" in n.lower() or "no full text" in n
               for n in doc.coverage_notes)


def test_acquire_draft(tmp_path):
    f = tmp_path / "draft.txt"
    f.write_text("METHODS\nWe did things.\nRESULTS\nThey worked.\n")
    intake = classify(str(f), draft=True)
    doc, ev = acquire(intake, draft=True)
    assert doc.source_used == "draft"
    assert doc.source["local_path"] == str(f)
    assert _secs(doc, SectionName.methods)
    assert ev == []


def test_acquire_refuses_bad_url():
    intake = classify("https://example.org/not-a-paper")
    with pytest.raises(ClientError):
        acquire(intake, transport=transport())

import io
import zipfile

import pytest

from reproducibility_bouncer.clients import ClientError
from reproducibility_bouncer.document import (
    _fetch_supplements,
    _parse_jats,
    _parse_plain_text,
    _supp_units,
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


def _xlsx_bytes(rows) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "params"
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_supp_units_xlsx_rows():
    data = _xlsx_bytes([["param", "value"], ["alpha", 0.05]])
    units = _supp_units("supp_table1.xlsx", data)
    assert units[0] == "sheet: params"
    assert "row 2: alpha | 0.05" in units


def test_supp_units_csv_and_txt():
    units = _supp_units("t.csv", b"a,b\n1,2\n")
    assert any("row" in u and "1" in u for u in units)
    units = _supp_units("notes.txt", b"first line. second line.")
    assert any("first line" in u for u in units)


def test_supp_units_unsupported():
    assert _supp_units("fig.png", b"\x89PNG") is None


def test_supp_units_truncation():
    data = b"line.\n" * 5000
    units = _supp_units("big.txt", data)
    assert len(units) <= 201
    assert units[-1].startswith("[supplement truncated")


def test_fetch_supplements_matches_manifest():
    doc = _parse_jats(JATS_FIXTURE, Document())
    doc.source["pmcid"] = "PMC9999999"
    n_secs = len(doc.sections)
    ev = _fetch_supplements(
        doc, transport=transport(supp_zip=_zip_bytes({
            "supp_table1.xlsx": _xlsx_bytes([["DESeq", "1.40"]]),
            "extra.pdf": b"%PDF-1.4 fake",   # unmanifested + unparsed
        })))
    assert ev
    sup = doc.supplements[0]
    assert sup["fetched"] is True
    assert sup["units"][0] == "sheet: params"
    assert sup["section"] == n_secs
    assert doc.sections[n_secs].name == SectionName.supplement
    extra = doc.supplements[1]
    assert extra["unmanifested"] is True
    assert extra["fetched"] is True
    assert extra["units"] is None      # fake pdf doesn't parse
    assert any("unparsed" in n for n in doc.coverage_notes)


def test_fetch_supplements_no_zip_member_stays_unfetched():
    doc = _parse_jats(JATS_FIXTURE, Document())
    doc.source["pmcid"] = "PMC9999999"
    _fetch_supplements(
        doc, transport=transport(supp_zip=_zip_bytes({
            "other.txt": b"irrelevant.",
        })))
    sup = doc.supplements[0]
    assert sup["fetched"] is False     # no member matched the manifest
    assert doc.supplements[1]["fetched"] is True


def test_fetch_supplements_fetch_failure_notes():
    doc = _parse_jats(JATS_FIXTURE, Document())
    doc.source["pmcid"] = "PMC9999999"
    ev = _fetch_supplements(doc, transport=transport())  # 404 default
    assert ev == []
    assert doc.supplements[0]["fetched"] is False
    assert any("unfetchable" in n for n in doc.coverage_notes)


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

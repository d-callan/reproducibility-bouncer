"""A1 — document acquisition & parsing.

Published: DOI -> Crossref metadata -> Europe PMC resolution -> JATS
full text when the paper is in the PMC OA subset (structured sections —
no PDF parse needed) -> Unpaywall best-OA PDF -> PyMuPDF fallback.
Draft: local file — PDF via PyMuPDF + heading heuristics, DOCX via
pandoc, TXT/MD directly.

Everything lands in a normalized `Document` (specs/data-model.md):
sections with numbered citable units, DAS/CAS isolated, inline
statistical reports extracted, supplement manifest recorded.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from .auditrecord import (
    Document, Section, SectionName, Unit, _new_id,
)
from .clients import ClientError, get_bytes
from .clients.crossref import get_work
from .clients.europepmc import (
    fetch_jats, fetch_supplementary, resolve_ids,
)
from .clients.unpaywall import get_oa
from .evidence import Evidence
from .intake import InputKind, Intake
from .units import _SENT_SPLIT

log = logging.getLogger(__name__)

_ARXIV_PDF = "https://arxiv.org/pdf/{}"

# title keyword -> canonical section. Order matters: checked in
# sequence, so "materials and methods" wins over a bare "results".
_SEC_RULES: list[tuple[re.Pattern[str], SectionName]] = [
    (re.compile(r"data.*availab|availab.*data|data access", re.I),
     SectionName.das),
    (re.compile(r"(code|software|tool).{0,20}availab", re.I),
     SectionName.cas),
    (re.compile(r"abstract|summary$", re.I), SectionName.abstract),
    (re.compile(r"introduct|background", re.I), SectionName.intro),
    (re.compile(r"method|materials|procedure|protocol|"
                r"experimental design", re.I), SectionName.methods),
    (re.compile(r"result|finding", re.I), SectionName.results),
    (re.compile(r"discuss|conclusion", re.I), SectionName.discussion),
    (re.compile(r"reference|bibliograph|cited", re.I),
     SectionName.refs),
    (re.compile(r"supplement|supporting", re.I), SectionName.supplement),
]

# minimal APA-style inline-report patterns: test(df[, df]) = stat, p …
_STATS = [
    (re.compile(
        r"\bt\(\s*([\d.]+)\s*\)\s*=\s*([-.\d]+)"
        r"[^.\n]{0,40}?p\s*([<=>≤≥])\s*([.\d]+)", re.I), "t"),
    (re.compile(
        r"\bF\(\s*([\d.,\s]+?)\s*\)\s*=\s*([-.\d]+)"
        r"[^.\n]{0,40}?p\s*([<=>≤≥])\s*([.\d]+)", re.I), "F"),
    (re.compile(
        r"\br\(\s*([\d.]+)\s*\)\s*=\s*([-.\d]+)"
        r"[^.\n]{0,40}?p\s*([<=>≤≥])\s*([.\d]+)", re.I), "r"),
    (re.compile(
        r"(?:χ²|X2|χ2)\s*\(\s*([\d.]+)[^)]*\)\s*=\s*([-.\d]+)"
        r"[^.\n]{0,40}?p\s*([<=>≤≥])\s*([.\d]+)"), "chi2"),
]


def _sec_name(title: str) -> SectionName:
    for pat, name in _SEC_RULES:
        if pat.search(title):
            return name
    return SectionName.other


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]


def _mk_section(
    name: SectionName, sentences: list[str],
) -> Section:
    return Section(
        name=name,
        units=[
            Unit(index=i + 1, text=s)
            for i, s in enumerate(sentences)
        ],
    )


def _inline_stats(doc: Document) -> None:
    """Scan section units for APA-style reports -> inline_stats[]."""
    for si, sec in enumerate(doc.sections):
        for u in sec.units:
            for pat, test in _STATS:
                for m in pat.finditer(u.text):
                    doc.inline_stats.append({
                        "test": test,
                        "statistic": float(m.group(2)),
                        "df": m.group(1).strip(),
                        "p_reported": f"{m.group(3)} {m.group(4)}",
                        "p_recomputed": None,
                        "unit_ref": {"section": si, "unit_index": u.index},
                    })


# ------------------------------------------------------------ JATS


def _tag(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _text(el: ET.Element) -> str:
    return " ".join("".join(el.itertext()).split())


def _parse_jats(
    xml: str, doc: Document, *,
    section_notes: bool = True,
) -> Document:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise ClientError(f"JATS parse failed: {e}") from e

    for el in root.iter():
        t = _tag(el)
        if t == "article-title" and not doc.title:
            doc.title = _text(el)
        elif t == "journal-title" and not doc.journal:
            doc.journal = _text(el)
        elif t == "year" and doc.year is None:
            try:
                doc.year = int(_text(el))
            except ValueError:
                pass
        elif t == "contrib" and el.get("contrib-type", "author") == "author":
            surname = next(
                (_text(s) for s in el.iter() if _tag(s) == "surname"),
                "",
            )
            given = next(
                (_text(s) for s in el.iter()
                 if _tag(s) == "given-names"),
                "",
            )
            name = f"{given} {surname}".strip()
            if name and name not in doc.authors:
                doc.authors.append(name)

    body = next(
        (e for e in root.iter() if _tag(e) == "body"), None,
    )
    back = next(
        (e for e in root.iter() if _tag(e) == "back"), None,
    )
    abstract = next(
        (e for e in root.iter() if _tag(e) == "abstract"), None,
    )

    sections: list[Section] = []
    if abstract is not None:
        paras = [
            _text(p) for p in abstract.iter()
            if _tag(p) == "p" and _text(p)
        ]
        sections.append(
            _mk_section(SectionName.abstract,
                        [s for para in paras for s in _sentences(para)])
        )

    if body is not None:
        for sec in body:
            if _tag(sec) != "sec":
                continue
            title_el = next(
                (c for c in sec if _tag(c) == "title"), None,
            )
            title = _text(title_el) if title_el is not None else ""
            name = _sec_name(title)
            paras = [
                _text(p) for p in sec.iter()
                if _tag(p) == "p" and _text(p)
            ]
            units = [s for para in paras for s in _sentences(para)]
            cap_sentences: list[str] = []
            for cap in sec.iter():
                if _tag(cap) == "caption":
                    cap_sentences.extend(_sentences(_text(cap)))
            if units:
                sections.append(_mk_section(name, units))
            if cap_sentences:
                sections.append(
                    _mk_section(SectionName.captions, cap_sentences)
                )

    if back is not None:
        ref_units = [
            _text(r) for r in back.iter() if _tag(r) == "ref"
        ]
        if ref_units:
            sections.append(_mk_section(SectionName.refs, ref_units))
        for sec in back.iter():
            if _tag(sec) != "sec":
                continue
            title_el = next(
                (c for c in sec if _tag(c) == "title"), None,
            )
            title = _text(title_el) if title_el is not None else ""
            name = _sec_name(title)
            if name in (SectionName.das, SectionName.cas):
                paras = [
                    _text(p) for p in sec.iter()
                    if _tag(p) == "p" and _text(p)
                ]
                sections.append(
                    _mk_section(
                        name,
                        [s for para in paras
                         for s in _sentences(para)],
                    )
                )
        for sm in back.iter():
            if _tag(sm) == "supplementary-material":
                href = ""
                for m in sm.iter():
                    if _tag(m) in ("media", "graphic", "inline-graphic"):
                        href = next(
                            (v for k, v in m.attrib.items()
                             if k.endswith("href")), "",
                        )
                        if href:
                            break
                fname = href or (_text(sm)[:80] or "supplement")
                # the same file may be declared several times
                # (inline + back-matter); dedupe by basename
                base = Path(fname).name.lower()
                if not any(
                    Path(s["filename"]).name.lower() == base
                    for s in doc.supplements
                ):
                    doc.supplements.append({
                        "filename": fname,
                        "fetched": False,
                        "units": None,
                    })

    doc.sections = sections
    if section_notes and not sections:
        doc.coverage_notes.append(
            "JATS parsed but no body sections found"
        )
    doc.parse_path = "europepmc_jats"
    _inline_stats(doc)
    return doc


# ------------------------------------------------------------ fallbacks


_HEADING_LINE = re.compile(
    r"^(abstract|summary|introduction|background|"
    r"(materials?\s+and\s+|research\s+)?methods?|"
    r"experimental procedures?|results?|"
    r"discussion|conclusions?|references?|bibliography|"
    r"data (availability|access)|code availability|"
    r"supplementary (material|information)|"
    r"acknowledge?ments?|author contributions?|funding|"
    r"conflict of interest)\b[\s:\.\-]*$",
    re.I,
)


def _parse_plain_text(
    text: str, doc: Document, *, parse_path: str,
) -> Document:
    """Heading-heuristic sectionizer for extracted text (PDF/pandoc/txt)."""
    current: list[str] = []
    name = SectionName.other
    buckets: dict[SectionName, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if _HEADING_LINE.match(line) and len(line.split()) <= 8:
            buckets.setdefault(name, []).extend(current)
            current = []
            name = _sec_name(line)
            continue
        current.append(line)
    buckets.setdefault(name, []).extend(current)

    doc.sections = [
        _mk_section(n, [s for para in lines for s in _sentences(para)])
        for n, lines in buckets.items() if lines
    ]
    doc.parse_path = parse_path  # type: ignore[assignment]
    doc.coverage_notes.append(
        "fallback parse: section tags are heading heuristics — "
        "DAS/CAS detection degrades to keyword scan"
    )
    _inline_stats(doc)
    return doc


def _parse_pdf_bytes(data: bytes, doc: Document) -> Document:
    try:
        import pymupdf
    except ImportError as e:  # pragma: no cover
        raise ClientError(
            "pymupdf is required for PDF parsing: pip install pymupdf"
        ) from e
    try:
        pdf = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        raise ClientError(f"unreadable PDF ({e}) — refusing") from e
    if pdf.needs_pass:
        raise ClientError("encrypted PDF — refusing")
    text = "\n".join(page.get_text() for page in pdf)
    return _parse_plain_text(text, doc, parse_path="fallback")


def _docx_text(path: Path) -> str:
    try:
        out = subprocess.run(
            ["pandoc", "-t", "plain", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError as e:
        raise ClientError(
            "DOCX input needs pandoc — install it or convert to PDF"
        ) from e
    if out.returncode != 0:
        raise ClientError(f"pandoc failed: {out.stderr[:300]}")
    return out.stdout


# ------------------------------------------------------------ supplements

# Supplements can be arbitrarily large — cap what enters the citable
# unit stream (eval-driver lesson: monolithic blobs get inhaled whole
# into context). Truncated files get a terminal marker unit so the
# audit can see coverage stopped.
_SUPP_MAX_UNITS = 200
_SUPP_MAX_ROWS = 500        # spreadsheet rows per sheet
_SUPP_MAX_CELL = 500        # chars per spreadsheet cell
_SUPP_TRUNC = "[supplement truncated — first units only]"


def _sheet_units(name: str, rows) -> list[str]:
    """Spreadsheet rows -> 'row n: v1 | v2' unit lines, capped."""
    out = [f"sheet: {name}"]
    for i, row in enumerate(rows):
        if i >= _SUPP_MAX_ROWS:
            out.append(f"sheet {name}: "
                       f"truncated at {_SUPP_MAX_ROWS} rows")
            break
        vals = [str(c)[:_SUPP_MAX_CELL] for c in row
                if c is not None and str(c).strip()]
        if vals:
            out.append(f"row {i + 1}: " + " | ".join(vals))
    return out


def _supp_units(filename: str, data: bytes) -> list[str] | None:
    """Parse one supplement's bytes -> unit-text list (None = type
    we can't turn into text — images, zip, media)."""
    ext = Path(filename).suffix.lower()
    units: list[str]
    try:
        if ext == ".pdf":
            try:
                import pymupdf
                pdf = pymupdf.open(stream=data, filetype="pdf")
                text = "\n".join(p.get_text() for p in pdf)
            except Exception:
                return None
            units = _sentences(text)
        elif ext == ".docx":
            with tempfile.NamedTemporaryFile(
                    suffix=".docx", delete=False) as tmp:
                tmp.write(data)
            try:
                units = _sentences(_docx_text(Path(tmp.name)))
            except ClientError:
                return None
            finally:
                Path(tmp.name).unlink(missing_ok=True)
        elif ext == ".xlsx":
            try:
                import openpyxl
                wb = openpyxl.load_workbook(
                    io.BytesIO(data), read_only=True, data_only=True)
            except Exception:
                return None
            units = []
            for ws in wb.worksheets:
                units += _sheet_units(
                    ws.title, (r for r in ws.iter_rows(
                        values_only=True)))
            wb.close()
        elif ext in (".csv", ".tsv"):
            try:
                text = data.decode("utf-8", "replace")
                dialect = ("excel-tab" if ext == ".tsv"
                           else "excel")
                rows = csv.reader(io.StringIO(text), dialect=dialect)
                units = _sheet_units("data", rows)
            except csv.Error:
                return None
        elif ext in (".txt", ".md", ".tsv.txt"):
            units = _sentences(data.decode("utf-8", "replace"))
        else:
            return None
    except (KeyError, AttributeError, TypeError):
        return None
    truncated = len(units) > _SUPP_MAX_UNITS
    units = units[:_SUPP_MAX_UNITS]
    if truncated:
        units.append(_SUPP_TRUNC)
    return units


def _fetch_supplements(
    doc: Document,
    *,
    transport: httpx.BaseTransport | None = None,
) -> list[Evidence]:
    """Populate doc.supplements manifest from the EuropePMC
    supplementaryFiles ZIP and append parsed content as citable
    `supplement` sections so slot binding and the JEV questions
    (methods-completeness asks about 'text + supplement') see it.
    Manifest entries with no matching ZIP member stay unfetched —
    the 'declared but not fetched' finding then honestly reports
    the gap."""
    if not doc.supplements or not doc.source.get("pmcid"):
        return []
    try:
        blob, ev = fetch_supplementary(
            doc.source["pmcid"], transport=transport)
    except ClientError as err:
        doc.coverage_notes.append(f"supplements unfetchable: {err}")
        return []
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        doc.coverage_notes.append("supplementaryFiles: not a ZIP")
        return [ev]
    # manifest 'filename' is an href basename or a label; match on
    # basename, case-insensitive
    by_manifest = {
        Path(s.get("filename", "")).name.lower(): s
        for s in doc.supplements
    }
    for member in zf.infolist():
        if member.is_dir():
            continue
        base = Path(member.filename).name.lower()
        sup = by_manifest.get(base)
        if sup is None:
            sup = {"filename": member.filename, "fetched": False,
                   "units": None, "unmanifested": True}
            doc.supplements.append(sup)
        try:
            data = zf.read(member)
        except (OSError, RuntimeError) as err:
            doc.coverage_notes.append(
                f"supplement {member.filename}: unreadable ({err})")
            continue
        units = _supp_units(member.filename, data)
        sup["fetched"] = True
        sup["units"] = units
        if units:
            sup["section"] = len(doc.sections)
            doc.sections.append(Section(
                name=SectionName.supplement,
                units=[Unit(index=i + 1, text=t)
                       for i, t in enumerate(units)],
            ))
        else:
            doc.coverage_notes.append(
                f"supplement {member.filename}: fetched but "
                "unparsed (unsupported type)")
    return [ev]


# ------------------------------------------------------------ driver


def acquire(
    intake: Intake,
    *,
    draft: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> tuple[Document, list[Evidence]]:
    """Input -> normalized Document + the Evidence trail."""
    doc = Document(
        id=_new_id("document", intake.normalized or intake.raw),
        retrieved_at=datetime.now(timezone.utc),
    )
    ev: list[Evidence] = []

    if draft or intake.kind == InputKind.local_file:
        path = Path(intake.normalized or intake.raw).expanduser()
        doc.source = {"local_path": str(path)}
        doc.source_used = "draft"
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            doc = _parse_pdf_bytes(path.read_bytes(), doc)
        elif suffix == ".docx":
            doc = _parse_plain_text(
                _docx_text(path), doc, parse_path="pandoc",
            )
        else:
            doc = _parse_plain_text(
                path.read_text(errors="replace"), doc,
                parse_path="pandoc",
            )
        doc.title = doc.title or path.name
        return doc, ev

    doc.source = {k: None for k in
                  ("doi", "pmid", "pmcid", "arxiv_id", "url",
                   "local_path")}

    # --- normalize ids ---------------------------------------------------
    doi = intake.normalized if intake.kind == InputKind.doi else None
    pmid = intake.normalized if intake.kind == InputKind.pmid else None
    pmcid = (
        intake.normalized if intake.kind == InputKind.pmcid else None
    )
    arxiv = (
        intake.normalized if intake.kind == InputKind.arxiv else None
    )

    if intake.kind == InputKind.url:
        raise ClientError(
            f"unrecognized URL {intake.raw!r} — pass a DOI/PMID/PMCID "
            "or a local file with --draft"
        )

    if doi or pmid or pmcid:
        ids, e = resolve_ids(
            doi=doi, pmid=pmid, pmcid=pmcid, transport=transport,
        )
        ev.append(e)
        doi = doi or ids.get("doi")
        pmid = pmid or ids.get("pmid")
        pmcid = pmcid or ids.get("pmcid")
    doc.source.update(
        {"doi": doi, "pmid": pmid, "pmcid": pmcid, "arxiv_id": arxiv}
    )

    # --- metadata ----------------------------------------------------------
    if doi:
        try:
            work, e = get_work(doi, transport=transport)
            ev.append(e)
            doc.title = work["title"]
            doc.authors = work["authors"]
            doc.journal = work["journal"]
            doc.year = work["year"]
        except ClientError as err:
            doc.coverage_notes.append(f"crossref failed: {err}")

    # --- full text: JATS path first ------------------------------------
    if pmcid:
        try:
            xml, e = fetch_jats(pmcid, transport=transport)
            ev.append(e)
            doc = _parse_jats(xml, doc)
            ev.extend(_fetch_supplements(doc, transport=transport))
            doc.source_used = "pmc_oa"
            doc.retrieval_url = e.endpoint
            return doc, ev
        except ClientError as err:
            doc.coverage_notes.append(
                f"europepmc JATS unavailable: {err}"
            )

    # --- PDF fallback ----------------------------------------------------
    pdf_url = None
    if arxiv:
        pdf_url = _ARXIV_PDF.format(arxiv)
        doc.source_used = "preprint"
    elif doi:
        try:
            oa, e = get_oa(doi, transport=transport)
            ev.append(e)
            loc = oa["best_oa_location"]
            pdf_url = loc.get("url_for_pdf")
            doc.source_used = (
                "preprint"
                if loc.get("host_type") == "repository"
                else "vor"
            )
            if not oa["is_oa"] and not pdf_url:
                doc.coverage_notes.append(
                    "closed access per unpaywall — metadata only"
                )
        except ClientError as err:
            doc.coverage_notes.append(f"unpaywall failed: {err}")

    if pdf_url:
        try:
            data, e = get_bytes(pdf_url, source="publisher-pdf",
                                transport=transport)
            ev.append(e)
            doc.retrieval_url = pdf_url
            doc = _parse_pdf_bytes(data, doc)
        except ClientError as err:
            doc.coverage_notes.append(f"pdf fetch/parse failed: {err}")

    if not doc.sections:
        doc.coverage_notes.append(
            "no full text acquired — Document is metadata-only; "
            "downstream stages will have nothing to extract from"
        )
    return doc, ev

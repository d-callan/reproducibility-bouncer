"""GEO probe via NCBI E-utilities — GSE/GSM existence + visibility.

esearch db=gds on `ACC[ACCN]` gives existence; esummary gives the
public status + sample/run counts for subset-vs-study checks.
Set NCBI_API_KEY to raise the rate limit.
"""

from __future__ import annotations

import gzip
import os
from typing import Any

import httpx

from ..evidence import Evidence
from . import ClientError, get_bytes, get_json

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
FTP = "https://ftp.ncbi.nlm.nih.gov/geo"
# hard cap on parsed samples — the matrix file is downloaded whole
# regardless; the cap only bounds pathological series. Kept high
# enough that group matching sees full coverage on typical papers.
MAX_SHEET_SAMPLES = 5000


def _params() -> dict[str, str]:
    p: dict[str, str] = {"retmode": "json"}
    if k := os.environ.get("NCBI_API_KEY"):
        p["api_key"] = k
    return p


def probe(
    acc: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[dict[str, Any] | None, Evidence | None]:
    """GSE/GSM accession -> record dict or None (not found)."""
    acc = acc.upper()
    term = f"{acc}[ACCN]"
    try:
        found, ev = get_json(
            f"{EUTILS}/esearch.fcgi",
            params={**_params(), "db": "gds", "term": term},
            source="ncbi-eutils", transport=transport,
        )
        ids = (found.get("esearchresult") or {}).get("idlist") or []
        if not ids:
            ev.summary = f"ncbi gds {acc}: not found"
            return None, ev
        summary, ev2 = get_json(
            f"{EUTILS}/esummary.fcgi",
            params={**_params(), "db": "gds", "id": ids[0]},
            source="ncbi-eutils", transport=transport,
        )
    except ClientError:
        raise
    rec = (summary.get("result") or {}).get(ids[0]) or {}
    ev2.summary = f"ncbi gds {acc} (uid {ids[0]})"
    return {
        "repo": "geo",
        "object_id": acc,
        "uid": ids[0],
        "title": rec.get("title", ""),
        "accession": rec.get("accession", acc),
        "n_samples": rec.get("n_samples"),
        "public": str(rec.get("public", "")).lower()
        not in ("suppressed", "private"),
        "url": f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={acc}",
    }, ev2



def _matrix_series_dir(gse: str) -> str:
    """GSE251671 -> 'GSE251nnn' (GEO's thousand-bucket series dir)."""
    return f"GSE{gse[3:][:-3]}nnn"


def _unquote(v: str) -> str:
    v = v.strip()
    if v.startswith('"') and v.endswith('"'):
        v = v[1:-1].replace('""', '"')
    return v


def sample_sheet(
    gse: str,
    *,
    transport: httpx.BaseTransport | None = None,
    max_samples: int = MAX_SHEET_SAMPLES,
) -> tuple[dict[str, Any], Evidence]:
    """GSE -> {samples: [{accession, title, source, characteristics}],
    n_series}. characteristics = {key: [values]} merged across ch1+.

    Fetches the gzipped series-matrix file — the column-oriented
    companion to SOFT, served from the FTP mirror (the acc.cgi text
    view is reCAPTCHA-gated for non-browser clients). A response that
    decompresses but lacks !Sample_geo_accession is NOT a sheet —
    ClientError, so callers see 'unverifiable', never a phantom
    empty series."""
    gse = gse.upper()
    url = (f"{FTP}/series/{_matrix_series_dir(gse)}/{gse}"
           f"/matrix/{gse}_series_matrix.txt.gz")
    raw, ev = get_bytes(url, source="geo-matrix", transport=transport)
    try:
        text = gzip.decompress(raw).decode("utf-8", "replace")
    except OSError:
        text = raw.decode("utf-8", "replace")
    rows: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line.startswith("!Sample_"):
            continue
        field, _, rest = line.partition("\t")
        rows.setdefault(field, []).append(rest)
    accessions = [
        _unquote(v) for v in
        (rows.get("!Sample_geo_accession") or [""])[0].split("\t")]
    if not accessions or not all(a.startswith("GSM")
                                 for a in accessions):
        raise ClientError(
            f"series matrix for {gse} returned no sample rows — "
            "response is not a parseable sheet")
    n_series = len(accessions)
    samples = [
        {"accession": a, "title": "", "source": "",
         "characteristics": {}} for a in accessions[:max_samples]]

    def col(field: str, idx: int) -> str:
        vals = (rows.get(field) or [""])[0].split("\t")
        return _unquote(vals[idx]) if idx < len(vals) else ""

    for i, s in enumerate(samples):
        s["title"] = col("!Sample_title", i)
        s["source"] = col("!Sample_source_name_ch1", i)
    for vals in rows.get("!Sample_characteristics_ch1") or []:
        row_vals = vals.split("\t")
        for i, s in enumerate(samples):
            val = _unquote(row_vals[i]) if i < len(row_vals) else ""
            if not val:
                continue
            if ":" in val:
                k, v = val.split(":", 1)
                s["characteristics"].setdefault(
                    k.strip().lower(), []).append(v.strip())
            else:
                s["characteristics"].setdefault("_", []).append(val)
    ev.summary = (f"geo matrix {gse}: {len(samples)}/{n_series} "
                  "samples parsed")
    return {"samples": samples, "n_series": n_series}, ev

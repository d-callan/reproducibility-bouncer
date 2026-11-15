"""ENA portal probe — SRA/INSDC accessions (SRP/SRR/PRJNA/PRJEB/...).

The filereport endpoint answers both "does this accession exist" and
"what does it contain" (run list for subset-vs-study checks). An empty
result table = not found.
"""

from __future__ import annotations

import io
import re
from typing import Any

import httpx

from ..evidence import Evidence
from . import ClientError, get_text

API = "https://www.ebi.ac.uk/ena/portal/api"

_RESULT = {
    "SRR": "read_run", "ERR": "read_run", "DRR": "read_run",
    "SRP": "read_run", "ERP": "read_run", "DRP": "read_run",
    "SRS": "sample", "ERS": "sample", "DRS": "sample",
    "PRJNA": "study", "PRJEB": "study", "PRJDB": "study",
    "SRX": "read_experiment", "ERX": "read_experiment",
    "DRX": "read_experiment",
}


def probe(
    acc: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[dict[str, Any] | None, Evidence]:
    """INSDC accession -> record or None."""
    acc = acc.upper()
    result = _RESULT.get(re.match(r"[A-Z]+", acc).group(0), "read_run")
    fields = ("run_accession,study_accession,experiment_accession,"
              "sample_accession,scientific_name,fastq_ftp"
              if result == "read_run"
              else "study_accession,scientific_name,study_title")
    tsv, ev = get_text(
        f"{API}/filereport",
        params={
            "accession": acc, "result": result,
            "fields": fields, "format": "tsv", "limit": "0",
        },
        source="ena", transport=transport,
    )
    lines = [ln for ln in tsv.splitlines() if ln.strip()]
    if len(lines) <= 1:  # header only / empty = not found
        ev.summary = f"ena {acc}: not found"
        return None, ev
    header = lines[0].split("\t")
    rows = [dict(zip(header, ln.split("\t"))) for ln in lines[1:]]
    ev.summary = f"ena {acc}: {len(rows)} {result} row(s)"
    runs = [r.get("run_accession") for r in rows
            if r.get("run_accession")]
    return {
        "repo": "ena/sra",
        "object_id": acc,
        "result_type": result,
        "n_runs": len(runs) or len(rows),
        "run_accessions": runs[:50],
        "organism": next(
            (r["scientific_name"] for r in rows
             if r.get("scientific_name")), "",
        ),
        "title": next(
            (r["study_title"] for r in rows if r.get("study_title")),
            "",
        ),
        "public": True,  # filereport only serves public records
        "url": f"https://www.ebi.ac.uk/ena/browser/view/{acc}",
    }, ev

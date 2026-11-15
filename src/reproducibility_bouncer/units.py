"""Numbered citable units — the citation-by-number mechanism.

Ported from BioNym `proposals.py` (issue #8): the source is presented to
the generator as numbered units (sentences for prose, lines for tables
and serialized records); the generator emits citation *numbers*, and the
quote is assembled verbatim from the cited units — it can select the
wrong units but can never paraphrase, splice, or fabricate text. A
freeform `quote` field is rejected as unverifiable.

The sentence split skips boundaries before a lowercase token so
initials, 'e.g.'/'i.e.', and binomials ('P. falciparum') stay one unit —
verbatim correctness doesn't need perfect boundaries, but fragment units
make useless quotes. Residual: an abbreviation followed by a capitalized
word ('et al. Johnson') still splits.
"""

from __future__ import annotations

import re
from typing import Any

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[^\sa-z])|\n+")


def _numbered(units: list[str]) -> tuple[str, list[str]]:
    display = "\n".join(f"[{i}] {u}" for i, u in enumerate(units, 1))
    return display, units


def numbered_sentences(text: str) -> tuple[str, list[str]]:
    """Prose -> ('[1] s1\\n[2] s2', [s1, s2]). Source for text claims."""
    return _numbered([s.strip() for s in _SENT_SPLIT.split(text) if s.strip()])


def numbered_lines(text: str) -> tuple[str, list[str]]:
    """Line-structured source (data rows, serialized records) -> same."""
    return _numbered([ln for ln in text.splitlines() if ln.strip()])


def valid_citations(raw: Any, n_units: int) -> list[int] | None:
    """Citation field -> 0-based unit indices that exist.

    None = no citation field at all; [] = the list was empty or every
    index missed the unit range. Callers drop the claim either way —
    citations are required. Nested groups like [[1,2],5] are flattened:
    they carry no information the indices don't (adjacency is
    derivable), and small models improvise them.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        return []
    out = []
    for n in raw:
        if isinstance(n, list):  # flatten improvised grouping
            for m in n:
                try:
                    i = int(m) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= i < n_units:
                    out.append(i)
            continue
        try:
            i = int(n) - 1  # the display numbers are 1-based
        except (TypeError, ValueError):
            continue
        if 0 <= i < n_units:
            out.append(i)
    return out


def verbatim_quote(units: list[str], citations: list[int]) -> str:
    """Assemble the quote from cited units in SOURCE order: sorted and
    deduped (the model may cite in any order or repeat), adjacent
    indices joined with a space, gaps marked with ' … '.
    """
    quote = ""
    prev = -2
    for i in sorted(set(citations)):
        if 0 <= i < len(units):
            if quote:
                quote += " " if i == prev + 1 else " … "
            quote += units[i]
            prev = i
    return quote.strip()

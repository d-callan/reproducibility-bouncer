from reproducibility_bouncer.units import (
    _SENT_SPLIT,
    numbered_lines,
    numbered_sentences,
    valid_citations,
    verbatim_quote,
)


def test_sentences_keep_abbreviations():
    display, units = numbered_sentences(
        "P. falciparum is a parasite. It causes malaria, e.g. fever."
    )
    assert units == [
        "P. falciparum is a parasite.",
        "It causes malaria, e.g. fever.",
    ]
    assert display.startswith("[1]")


def test_lines():
    _, units = numbered_lines("row1\nrow2\n\nrow3")
    assert units == ["row1", "row2", "row3"]


def test_valid_citations():
    assert valid_citations([1, 3], 3) == [0, 2]
    assert valid_citations([[1, 2], 3], 3) == [0, 1, 2]  # flattened
    assert valid_citations(None, 3) is None
    assert valid_citations([9], 3) == []


def test_verbatim_quote():
    units = ["alpha.", "beta.", "gamma.", "delta."]
    assert verbatim_quote(units, [0, 1]) == "alpha. beta."
    assert verbatim_quote(units, [3, 0]) == "alpha. … delta."


def test_split_regex_handles_i_e():
    parts = [s for s in _SENT_SPLIT.split("one, i.e. two. Three.")]
    assert parts[0] == "one, i.e. two."

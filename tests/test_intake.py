from reproducibility_bouncer.intake import InputKind, classify


def test_bare_doi():
    r = classify("10.1073/pnas.2409747121")
    assert r.kind == InputKind.doi
    assert r.normalized == "10.1073/pnas.2409747121"


def test_prefixed_doi():
    r = classify("doi:10.3389/fmicb.2021.626715")
    assert r.kind == InputKind.doi
    assert r.normalized == "10.3389/fmicb.2021.626715"


def test_pmid_and_pmcid():
    assert classify("39123456").kind == InputKind.pmid
    assert classify("PMC11551328").kind == InputKind.pmcid


def test_doi_in_url():
    r = classify("https://doi.org/10.1073/pnas.2409747121")
    assert r.kind == InputKind.doi
    assert r.normalized == "10.1073/pnas.2409747121"


def test_pubmed_url():
    r = classify("https://pubmed.ncbi.nlm.nih.gov/39123456/")
    assert r.kind == InputKind.pmid
    assert r.normalized == "39123456"


def test_arxiv_url_and_id():
    assert classify("2405.12345").kind == InputKind.arxiv
    r = classify("https://arxiv.org/abs/2405.12345")
    assert r.kind == InputKind.arxiv
    assert r.normalized == "2405.12345"


def test_local_file(tmp_path):
    f = tmp_path / "paper.pdf"
    f.write_bytes(b"%PDF-1.4 x")
    r = classify(str(f))
    assert r.kind == InputKind.local_file
    r2 = classify("./whatever.pdf", draft=True)
    assert r2.kind == InputKind.local_file and r2.draft


def test_unknown_url_and_text():
    r = classify("https://example.org/random-page")
    assert r.kind == InputKind.url and r.notes
    assert classify("not a paper").kind == InputKind.unknown

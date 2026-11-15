from reproducibility_bouncer.auditrecord import (
    Document,
    Section,
    SectionName,
    Unit,
)
from reproducibility_bouncer.resolve import (
    extract_identifiers,
    resolve_artifacts,
)

from fakes import transport


def _doc():
    return Document(
        id="d1",
        sections=[
            Section(name=SectionName.das, units=[
                Unit(index=1, text="Data are at GSE999999 and "
                                   "10.5281/zenodo.1111."),
                Unit(index=2, text="Code: github.com/user/repo."),
                Unit(index=3, text="Runs under PRJNA123456 (2 runs)."),
            ]),
            Section(name=SectionName.other, units=[
                Unit(index=1, text="Data available upon reasonable "
                                   "request from the author."),
            ]),
        ],
    )


def test_extract_identifiers():
    found = extract_identifiers(_doc())
    assert "GSE999999" in found
    assert "10.5281/zenodo.1111" in found
    assert "user/repo" in found
    assert "PRJNA123456" in found


def test_resolve_artifacts():
    doc = _doc()
    arts, findings, ev = resolve_artifacts(doc, transport=transport())
    by_id = {
        a.identifier.normalized: a for a in arts if a.identifier
    }

    assert by_id["GSE999999"].resolved.repo == "geo"
    assert by_id["GSE999999"].availability == "public"
    assert by_id["10.5281/zenodo.1111"].resolved.repo == "zenodo"
    assert (by_id["10.5281/zenodo.1111"].mutability_flags
            == ["concept DOI resolves to latest version"])
    assert by_id["user/repo"].resolved.repo == "github"
    assert "personal unarchived repo" in \
        by_id["user/repo"].mutability_flags[0]
    assert by_id["PRJNA123456"].resolved.repo == "ena/sra"
    assert by_id["PRJNA123456"].observed["n_runs"] == 2

    on_req = [a for a in arts if a.availability == "on-request"]
    assert len(on_req) == 1
    assert any(
        f.severity == "blocking" and "on request" in f.description
        for f in findings
    )
    assert len(ev) >= 4  # one Evidence per probed id


def test_absent_is_blocking():
    """identifier resolves to nothing -> absent + blocking finding."""
    import httpx

    def handler(req):  # repo reachable, accession unknown
        if "esearch" in req.url.path:
            return httpx.Response(200, json={
                "esearchresult": {"idlist": []}})
        return httpx.Response(404, text="nope")

    doc = Document(id="d2", sections=[Section(
        name=SectionName.das,
        units=[Unit(index=1, text="At GSE1.")],
    )])
    arts, findings, ev = resolve_artifacts(
        doc, transport=httpx.MockTransport(handler),
    )
    assert arts[0].availability == "absent"
    assert any(f.severity == "blocking" for f in findings)


def test_probe_failure_is_warning_not_absent():
    """unreachable repo -> warning + availability unset, not 'absent'
    (we couldn't check, so we don't claim absence)."""
    import httpx

    def handler(req):
        return httpx.Response(500, text="server dead")

    doc = Document(id="d2", sections=[Section(
        name=SectionName.das,
        units=[Unit(index=1, text="At GSE1.")],
    )])
    arts, findings, ev = resolve_artifacts(
        doc, transport=httpx.MockTransport(handler),
    )
    assert arts[0].availability is None
    assert any(f.severity == "warning" for f in findings)
    assert not any(f.severity == "blocking" for f in findings)


def test_unregistered_family_recorded():
    doc = Document(id="d3", sections=[Section(
        name=SectionName.das,
        units=[Unit(index=1, text="Proteomics at PXD001234.")],
    )])
    arts, findings, ev = resolve_artifacts(doc, transport=transport())
    art = arts[0]
    assert art.resolved is None
    assert art.availability == "absent"
    assert any("not implemented" in f.description or
               "unregistered" in f.description for f in findings)

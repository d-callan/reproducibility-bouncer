"""Canned API payloads + MockTransport for offline tests.

Fakes the same shape as real responses (bionym tests/fakes.py pattern):
every client entry point takes `transport=` so tests never hit the
network.
"""

from __future__ import annotations

import json

import httpx

JATS_FIXTURE = """<?xml version="1.0" ?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front>
    <journal-meta><journal-title-group>
      <journal-title>Journal of Tests</journal-title>
    </journal-title-group></journal-meta>
    <article-meta>
      <article-id pub-id-type="doi">10.1234/test.1</article-id>
      <title-group><article-title>A test article</article-title></title-group>
      <contrib-group>
        <contrib contrib-type="author"><name>
          <surname>Doe</surname><given-names>Jane</given-names>
        </name></contrib>
      </contrib-group>
      <pub-date><year>2024</year></pub-date>
      <abstract><p>We tested things. Results were significant.</p></abstract>
    </article-meta>
  </front>
  <body>
    <sec><title>Introduction</title>
      <p>Antibiotics are important. Resistance is bad.</p>
    </sec>
    <sec><title>Materials and Methods</title>
      <p>We grew P. aeruginosa in broth. The difference was
      significant (t(18) = 3.21, p &lt; 0.01).</p>
    </sec>
    <sec><title>Results</title>
      <p>Treatment reduced growth. Figure 1 shows it.
      <fig><caption><p>Figure 1. Growth curves.</p></caption></fig>
      </p>
    </sec>
  </body>
  <back>
    <sec><title>Data Availability</title>
      <p>Data are at GEO accession GSE999999.</p>
    </sec>
    <ref-list>
      <ref><mixed-citation>Doe J (2020) Earlier work.</mixed-citation></ref>
    </ref-list>
    <supplementary-material>
      <media xlink:href="supp_table1.xlsx"/>
    </supplementary-material>
  </back>
</article>
"""

CROSSREF = {
    "status": "ok",
    "message": {
        "DOI": "10.1234/test.1",
        "title": ["A test article"],
        "author": [{"given": "Jane", "family": "Doe"}],
        "container-title": ["Journal of Tests"],
        "issued": {"date-parts": [[2024]]},
        "type": "journal-article",
        "publisher": "Test Pub",
        "resource": {"primary": {"URL": "https://example.org/a"}},
    },
}

UNPAYWALL = {
    "DOI": "10.1234/test.1",
    "is_oa": True,
    "oa_status": "gold",
    "genre": "journal-article",
    "best_oa_location": {
        "host_type": "publisher",
        "url_for_pdf": "https://example.org/test.pdf",
        "url_for_landing_page": "https://example.org/a",
        "version": "publishedVersion",
        "license": "cc-by",
        "pmh_id": None,
    },
    "oa_locations": [],
}

EPMC_SEARCH = {
    "resultList": {"result": [{
        "pmid": "99123456",
        "pmcid": "PMC9999999",
        "doi": "10.1234/test.1",
        "inEPMC": "y",
        "isOpenAccess": "y",
        "hasFullText": "y",
    }]},
}


GEO_SUMMARY = {
    "result": {"12345678": {
        "uid": "12345678", "accession": "GSE999999",
        "title": "Test expression set", "n_samples": 12,
        "public": "public",
    }},
}
ENA_REPORT = (
    "run_accession\tstudy_accession\tscientific_name\n"
    "SRR111\tSRP111\tPlasmodium falciparum\n"
    "SRR112\tSRP111\tPlasmodium falciparum\n"
)
ZENODO_REC = {
    "doi": "10.5281/zenodo.1111", "conceptdoi": "10.5281/zenodo.1000",
    "metadata": {"title": "Test archive", "license": {"id": "CC-BY-4.0"},
                 "version": "v1.0", "publication_date": "2024-01-01",
                 "access_right": "open"},
    "files": [{"key": "data.csv", "size": 1000}],
    "revision": 1,
}
GITHUB_REPO = {
    "full_name": "user/repo", "default_branch": "main",
    "license": {"spdx_id": "MIT"}, "archived": False,
    "private": False, "pushed_at": "2024-01-01T00:00:00Z",
    "owner": {"type": "User"},
    "html_url": "https://github.com/user/repo",
    "stargazers_count": 5,
}
DRYAD_REC = {
    "title": "Dryad dataset", "versionNumber": 2, "license": "CC0",
    "publicationStatus": "published",
}
OSF_NODE = {"data": {"attributes": {
    "title": "OSF project", "public": True, "registration": False,
    "date_modified": "2024-01-01",
}}}
DATACITE_REC = {"data": {"attributes": {
    "titles": [{"title": "Figshare thing"}], "publisher": "figshare",
    "types": {"resourceTypeGeneral": "Dataset"}, "version": "1",
    "state": "findable", "url": "https://figshare.com/x",
}}}


def transport(
    *,
    jats: str | None = JATS_FIXTURE,
    pdf: bytes | None = None,
) -> httpx.MockTransport:
    """MockTransport serving canned responses by host + path."""

    def handler(req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        if host == "api.crossref.org":
            return httpx.Response(200, json=CROSSREF)
        if host == "api.unpaywall.org":
            return httpx.Response(200, json=UNPAYWALL)
        if host == "www.ebi.ac.uk":
            if path.endswith("/search"):
                return httpx.Response(200, json=EPMC_SEARCH)
            if path.endswith("/fullTextXML"):
                if jats is None:
                    return httpx.Response(404, text="not in OA subset")
                return httpx.Response(200, text=jats)
            if path.endswith("/filereport"):
                return httpx.Response(200, text=ENA_REPORT)
        if host == "eutils.ncbi.nlm.nih.gov":
            if path.endswith("esearch.fcgi"):
                return httpx.Response(200, json={
                    "esearchresult": {"idlist": ["12345678"]}})
            if path.endswith("esummary.fcgi"):
                return httpx.Response(200, json=GEO_SUMMARY)
        if host == "zenodo.org":
            return httpx.Response(200, json=ZENODO_REC)
        if host == "api.github.com":
            return httpx.Response(200, json=GITHUB_REPO)
        if host == "datadryad.org":
            return httpx.Response(200, json=DRYAD_REC)
        if host == "api.osf.io":
            return httpx.Response(200, json=OSF_NODE)
        if host == "api.datacite.org":
            return httpx.Response(200, json=DATACITE_REC)
        if path.endswith(".pdf"):
            return httpx.Response(
                200, content=pdf or b"%PDF-1.4 fake",
            )
        return httpx.Response(404, text=f"unmocked {host}{path}")

    return httpx.MockTransport(handler)


class FakeLlm:
    """Canned A2 generator — returns one analysis unit citing the
    given unit ids (sec.unit strings)."""

    def __init__(self, payload: dict | None = None):
        self.payload = payload or {
            "units": [{
                "title": "main analysis", "headline": True,
                "cites": ["0.1", "1.1"],
                "outputs": [{"result_key": "fig1.a", "kind": "figure",
                             "label": "Fig 1", "cites": ["1.1"]}],
                "inputs": [{"name": "reads",
                            "slot_type": "dataset",
                            "declared_value": "GSE999999",
                            "cites": ["0.1"]}],
                "methods": [{"name": "aligner",
                             "slot_type": "software",
                             "declared_value": "STAR v2.7.10a",
                             "cites": ["1.1"]}],
                "claims": [{"text": "it worked", "cites": ["1.1"]}],
            }],
        }

    def complete(self, system: str, user: str,
                 temperature: float | None = None) -> str:
        import json as _json
        return _json.dumps(self.payload)

    @property
    def enabled(self) -> bool:
        return True

    def total_usage(self) -> dict:
        return {}

from reproducibility_bouncer.intake import classify
from reproducibility_bouncer.jev import JevClient
from reproducibility_bouncer.pipeline import run_static

from fakes import FakeLlm, transport


def test_run_static_end_to_end():
    """A0..A7 on the JATS fixture: propose -> resolve -> bind ->
    static checks -> READINESS report."""
    intake = classify("doi:10.1234/test.1")
    audit, report = run_static(
        intake, jev=JevClient(mock=True), llm=FakeLlm(),
        transport=transport(), input_str="doi:10.1234/test.1",
    )
    assert audit.document is not None
    assert audit.units and audit.slots
    assert audit.artifacts  # GSE999999 in fixture DAS + methods text
    assert "a5" in audit.meta["stages_run"]
    assert "binding_check_coverage" in audit.meta
    assert report["verdict"]["label"] == "READINESS"
    assert set(report["axes"]) == {
        "artifact_access", "input_binding", "sample_metadata",
        "methods_placement", "executability", "result_verifiability",
    }
    assert report["counts"]["analysis_units"] >= 1


def test_run_static_metadata_only_doc():
    """closed-access paper -> no sections -> extraction empty,
    report still produced with gate flavor."""
    class EmptyLlm:
        def complete(self, s, u, temperature=None):
            return '{"units": []}'

    intake = classify("doi:10.1234/test.1")
    audit, report = run_static(
        intake, jev=JevClient(mock=True), llm=EmptyLlm(),
        transport=transport(jats=None), input_str="x",
    )
    assert audit.units == []
    assert report["verdict"]["label"] == "READINESS"
    assert report["counts"]["analysis_units"] == 0

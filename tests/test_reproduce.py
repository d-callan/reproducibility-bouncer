import json

import pytest

from reproducibility_bouncer import reproduce
from reproducibility_bouncer.auditrecord import (
    AnalysisUnit,
    Artifact,
    Audit,
    BoundInstance,
    ClarificationRequest,
    Identifier,
    Milestone,
    OutputSpec,
    Resolved,
    Slot,
)
from reproducibility_bouncer.jev import JevClient
from reproducibility_bouncer.report import build_report


def _artifact(avail="public", ident="GSE1"):
    return Artifact(
        id="a1", kind="dataset",
        identifier=Identifier(family="accession", raw=ident,
                              normalized=ident),
        resolved=Resolved(repo="geo", object_id=ident, version="",
                          url="u"),
        availability=avail,
    )


def _slot(stype="dataset", value="GSE1", bound=True):
    s = Slot(id="s1", unit_id="analysis_unit:u1", name="reads",
             slot_type=stype, declared_value=value)
    if bound:
        s.bound_instance = BoundInstance(
            artifact_id="artifact:a1", instance="")
    return s


def _audit(avail="public", blocking_q=False):
    a = Audit(id="aud1", mode="published")
    a.units = [AnalysisUnit(
        id="u1", headline=True,
        outputs=[OutputSpec(result_key="fig1.a", kind="scalar",
                            expected={"value": 2.0})],
        inputs=["slot:s1"],
    )]
    a.slots = [_slot()]
    a.artifacts = [_artifact(avail)]
    if blocking_q:
        a.questions = [ClarificationRequest(
            id="q1", blocking=True, question="which batch?",
            context_refs=["slot:s1"],
            resolves_unknown="cannot_determine")]
    return a


# ---- gate -------------------------------------------------------------


def test_gate_passes_on_public_inputs():
    g = reproduce.gate(_audit())
    assert g["passed"] and not g["failed_criteria"]


def test_gate_blocks_on_nonpublic_artifact():
    g = reproduce.gate(_audit(avail="controlled-access"))
    assert not g["passed"]
    assert g["failed_criteria"] == ["public-inputs"]
    assert g["expected_stall"]["public-inputs"] == "M1"


def test_gate_blocks_on_blocking_question():
    g = reproduce.gate(_audit(blocking_q=True))
    assert not g["passed"]
    assert g["failed_criteria"] == ["open-questions"]
    # input slot -> predicted M1 stall
    assert g["expected_stall"]["open-questions"] == "M1"


def test_gate_ignores_nonblocking_questions():
    a = _audit()
    a.questions = [ClarificationRequest(
        id="q1", blocking=False, question="fyi",
        resolves_unknown="not_yet_checked")]
    assert reproduce.gate(a)["passed"]


# ---- staging + prompt -------------------------------------------------


def test_stage_writes_line_oriented_inputs(tmp_path):
    a = _audit()
    a.questions = [ClarificationRequest(id="q1", question="x")]
    reproduce.stage(a, tmp_path)
    units = (tmp_path / "units.jsonl").read_text().splitlines()
    man = (tmp_path / "manifest.jsonl").read_text().splitlines()
    assert json.loads(units[0])["unit_id"] == "u1"
    assert json.loads(man[0])["identifier"] == "GSE1"
    assert (tmp_path / "questions.jsonl").exists()


def test_prompt_has_marker_contract_and_skeptic_policy():
    p = reproduce.build_prompt(_audit())
    assert "REPRO_RESULT:" in p
    assert "M4" in p and "not_yet_checked" in p
    assert "units.jsonl" in p and "manifest.jsonl" in p


# ---- milestone mapping + status ---------------------------------------


def _run_result(ms, produced=None, stall=None, clar=None):
    return {"status": "ok", "wall_s": 1.0, "result": {
        "milestones": ms, "produced": produced or {},
        "stall": stall, "clarifications": clar or [],
    }}


def test_milestone_records_default_unchecked():
    recs = reproduce._milestone_records({"M1": "reached",
                                         "M2": "bogus"})
    assert recs[0].status == "reached"
    assert recs[1].status == "unchecked"  # invalid -> unchecked
    assert recs[3].status == "unchecked"  # missing -> unchecked


def test_run_status_pass_fail_indeterminate():
    a = _audit()
    for ms, want in [
        ({"M1": "reached", "M2": "reached", "M3": "reached",
          "M4": "reached"}, "pass"),
        ({"M1": "failed"}, "fail"),
        ({"M1": "reached", "M2": "reached", "M3": "reached"},
         "indeterminate"),
        ({"M1": "reached", "M2": "failed"}, "fail"),
        ({"M1": "M1-blocked:access"}, "fail"),
    ]:
        runs = _run(a, ms)
        assert runs[0].status == want, ms


# ---- concordance --------------------------------------------------------


def test_concordance_numeric_tolerance():
    a = _audit()
    res = reproduce.concordance(
        a.units, {"fig1.a": 2.09})  # rel .05 of 2.0 = .1 -> match
    assert res[0].verdict == "match"
    res = reproduce.concordance(a.units, {"fig1.a": 2.2})
    assert res[0].verdict == "mismatch"


def test_concordance_missing_key_unchecked():
    res = reproduce.concordance(_audit().units, {})
    assert res[0].verdict == "unchecked"


def test_concordance_qualitative_judged():
    a = _audit()
    a.units[0].outputs[0].expected = {"value": "increased"}
    res = reproduce.concordance(
        a.units, {"fig1.a": "increased ~2x"}, jev=JevClient(mock=True))
    assert res[0].verdict == "match"  # mock noul=0.5


# ---- stall forensics ----------------------------------------------------


def test_stall_maps_to_finding():
    a = _audit()
    f = reproduce.Finding(
        id="f1", axis="input_binding", severity="warning",
        subject=["slot:s1"], mechanism="deterministic",
        confidence=1.0,
        description="'UCBPP-PA14' names multiple alternatives")
    runs = _run(a, {"M1": "reached", "M2": "failed"},
                stall={"milestone": "M2",
                       "proximate_cause":
                       "UCBPP-PA14 strain ambiguous, cannot proceed"})
    # findings list is audit's — attach and check mapping
    a.findings.append(f)
    runs = _run(a, {"M1": "reached", "M2": "failed"},
                stall={"milestone": "M2",
                       "proximate_cause":
                       "UCBPP-PA14 strain ambiguous, cannot proceed"})
    assert runs[0].stall.mapped_finding_id == "finding:f1"


# ---- run orchestration ---------------------------------------------------


def _invoke_ok(ms, produced=None):
    def inv(prompt, workdir, session_dir, *, argv, timeout_s):
        return _run_result(ms, produced or {"fig1.a": 2.05})
    return inv


def _run(a, ms, stall=None, **kw):
    import os
    os.environ.setdefault("REPRO_RUNS_DIR", "/tmp/repro_test_runs")
    def inv(prompt, workdir, session_dir, *, argv, timeout_s):
        return _run_result(ms, {"fig1.a": 2.05}, stall=stall)
    return reproduce.run_full(
        a, k=1, jev=JevClient(mock=True),
        invoke=inv, argv=[], **kw)


def test_run_full_gate_blocked_no_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("REPRO_RUNS_DIR", str(tmp_path))
    runs = reproduce.run_full(
        _audit(avail="absent"), k=3,
        jev=JevClient(mock=True), invoke=_invoke_ok({}), argv=[])
    assert len(runs) == 3
    assert all(r.status == "gate_blocked" for r in runs)
    assert runs[0].gate["failed_criteria"] == ["public-inputs"]


def test_run_full_force_overrides_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("REPRO_RUNS_DIR", str(tmp_path))
    runs = reproduce.run_full(
        _audit(avail="absent"), k=1, force=True,
        jev=JevClient(mock=True), argv=[],
        invoke=_invoke_ok({"M1": "reached", "M2": "reached",
                           "M3": "reached", "M4": "reached"}))
    assert runs[0].status == "pass"
    assert runs[0].concordance[0].verdict == "match"


def test_run_full_orbit_error_run(tmp_path, monkeypatch):
    monkeypatch.setenv("REPRO_RUNS_DIR", str(tmp_path))
    def inv(prompt, workdir, session_dir, *, argv, timeout_s):
        return {"status": "timeout", "wall_s": 1200.0}
    runs = reproduce.run_full(_audit(), k=1,
                              jev=JevClient(mock=True),
                              invoke=inv, argv=[])
    assert runs[0].status == "error"
    assert "timeout" in runs[0].stall.proximate_cause


def test_run_clarifications_become_questions(tmp_path, monkeypatch):
    monkeypatch.setenv("REPRO_RUNS_DIR", str(tmp_path))
    a = _audit()

    def inv(prompt, workdir, session_dir, *, argv, timeout_s):
        return _run_result(
            {"M1": "reached", "M2": "reached", "M3": "failed"},
            clar=[{"slot": "batch", "question": "which batch?",
                   "candidates": ["A", "B"],
                   "resolves_unknown": "cannot_determine"}])

    runs = reproduce.run_full(a, k=1, jev=JevClient(mock=True),
                              invoke=inv, argv=[])
    assert runs[0].clarifications
    q = a.questions[-1]
    assert q.blocking  # cannot_determine -> blocking


# ---- verdict stats + report --------------------------------------------


def test_verdict_stats_counts():
    a = _audit()
    a.runs = _run(a, {"M1": "reached", "M2": "reached",
                      "M3": "reached", "M4": "reached"})
    stats = reproduce.verdict_stats(a.runs)
    assert stats["passing_runs"] == 1 and stats["checked_runs"] == 1


def test_report_full_verdict_pass_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("REPRO_RUNS_DIR", str(tmp_path))
    a = _audit()
    a.runs = _run(a, {"M1": "reached", "M2": "reached",
                      "M3": "reached", "M4": "reached"})
    rep = build_report(a, full=True, tau=1, k=1)
    assert rep["verdict"]["label"] == "PASS"
    assert rep["verdict"]["passing_runs"] == 1
    assert rep["runs"][0]["status"] == "pass"

    a.runs = _run(a, {"M1": "failed"})
    rep = build_report(a, full=True, tau=1, k=1)
    assert rep["verdict"]["label"] == "FAIL"


def test_report_full_gate_blocked_flavor():
    a = _audit(avail="absent")
    a.runs = reproduce.run_full(
        a, k=2, jev=JevClient(mock=True),
        invoke=_invoke_ok({}), argv=[])
    rep = build_report(a, full=True, tau=1, k=2)
    assert rep["verdict"]["label"] == "FAIL"
    assert rep["verdict"]["flavor"] == "gate-blocked:public-inputs"

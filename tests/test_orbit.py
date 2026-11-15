import subprocess
from types import SimpleNamespace

import pytest

from reproducibility_bouncer import orbit


def _proc(rc, stdout="", stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


def _run_cmds(monkeypatch, outcomes):
    """Patch subprocess.run with scripted outcomes; returns cmd list."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = outcomes[len(calls) - 1]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(orbit.subprocess, "run", fake_run)
    monkeypatch.setattr(orbit.time, "sleep", lambda s: None)
    return calls


def test_transient_504_retries_with_continue(tmp_path, monkeypatch):
    calls = _run_cmds(monkeypatch, [
        _proc(1, stderr="504 Gateway Time-out\n"),
        _proc(0, stdout='noise REPRO_RESULT: {"ok": true}\n'),
    ])
    res = orbit.run_orbit(
        "p", tmp_path / "w", tmp_path / "s",
        argv=["node", "orbit.js"], retry_delay_s=0,
    )
    assert res["status"] == "ok"
    assert res["result"] == {"ok": True}
    assert res["attempts"] == 2
    assert len(calls) == 2
    assert "--continue" in calls[1]
    assert calls[1][-1] == orbit._RESUME_MSG
    assert calls[0][-1] == "p"


def test_non_transient_rc_not_retried(tmp_path, monkeypatch):
    calls = _run_cmds(monkeypatch, [
        _proc(1, stderr="segmentation fault\n"),
        _proc(0, stdout='REPRO_RESULT: {"ok": true}\n'),
    ])
    res = orbit.run_orbit(
        "p", tmp_path / "w", tmp_path / "s",
        argv=["node", "orbit.js"], retry_delay_s=0,
    )
    assert res["status"] == "rc=1"
    assert res["attempts"] == 1
    assert len(calls) == 1


def test_transient_retries_exhausted(tmp_path, monkeypatch):
    calls = _run_cmds(monkeypatch, [
        _proc(1, stderr="503 Service Unavailable\n"),
        _proc(1, stderr="504 Gateway Time-out\n"),
        _proc(1, stderr="504 Gateway Time-out\n"),
    ])
    res = orbit.run_orbit(
        "p", tmp_path / "w", tmp_path / "s",
        argv=["node", "orbit.js"], retry_delay_s=0,
    )
    assert res["status"] == "rc=1"
    assert res["attempts"] == 3
    assert len(calls) == 3


def test_timeout_budget_shared_across_attempts(tmp_path, monkeypatch):
    calls = _run_cmds(monkeypatch, [
        _proc(1, stderr="504 Gateway Time-out\n"),
        subprocess.TimeoutExpired(cmd="orbit", timeout=5),
    ])
    res = orbit.run_orbit(
        "p", tmp_path / "w", tmp_path / "s",
        argv=["node", "orbit.js"], timeout_s=10, retry_delay_s=0,
    )
    assert res["status"] == "timeout"
    assert res["attempts"] == 2
    # second attempt got the remaining budget, not a fresh timeout_s
    assert len(calls) == 2


def test_env_configures_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("REPRO_ORBIT_RETRIES", "0")
    calls = _run_cmds(monkeypatch, [
        _proc(1, stderr="504 Gateway Time-out\n"),
    ])
    res = orbit.run_orbit(
        "p", tmp_path / "w", tmp_path / "s", argv=["node", "orbit.js"],
    )
    assert res["status"] == "rc=1"
    assert len(calls) == 1

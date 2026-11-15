"""A6 — reproduction harness. Only in --full mode.

Gate first (PLAN §A6): runs only start when every required input
artifact is `public` and no *blocking* clarification questions are
outstanding — burning K Orbit runs to rediscover a known stall is
waste. A failed gate is a *predicted* stall recorded as
`gate-blocked:<criterion>` runs; --force-run overrides so the
calibration corpus can compare predicted-vs-observed stalls.

Each of K runs = one fresh Orbit invocation in an isolated workdir
attempting the whole headline-unit set. Milestones M1..M4 are parsed
from the run's REPRO_RESULT marker; concordance matches produced
values to expected by result_key under a per-output tolerance.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .auditrecord import (
    AnalysisUnit,
    Artifact,
    Audit,
    ClarificationRequest,
    Concordance,
    Finding,
    Milestone,
    ReproRun,
    Slot,
    Stall,
    _new_id,
)
from .clients import ClientError
from . import orbit

log = logging.getLogger(__name__)

_INPUT_SLOT_TYPES = {"dataset", "reference_genome",
                     "versioned_identifier"}
_DEFAULT_TOL = {"rel": 0.05, "abs": 0.0}
_MILESTONES = ("M1", "M2", "M3", "M4")


# ---------------------------------------------------------------- gate


def gate(audit: Audit) -> dict[str, Any]:
    """Pre-flight gate -> {passed, failed_criteria, expected_stall}.

    Criteria (PLAN §A6):
      public-inputs  — every artifact bound to an input slot has
                       availability == 'public'
      open-questions — no blocking ClarificationRequest outstanding
    expected_stall: milestone where each failed criterion predicts a
    stall (access → M1; open questions on inputs → M1, else M3).
    """
    failed: list[str] = []
    stall_at: dict[str, str] = {}

    arts = {a.ref(): a for a in audit.artifacts}
    slots = {s.ref(): s for s in audit.slots}
    bad = []
    for slot in audit.slots:
        if slot.slot_type not in _INPUT_SLOT_TYPES:
            continue
        if not slot.bound_instance:
            continue
        art = arts.get(slot.bound_instance.artifact_id)
        if art is not None and art.availability != "public":
            bad.append((slot, art))
    if bad:
        failed.append("public-inputs")
        stall_at["public-inputs"] = "M1"

    blocking = [q for q in audit.questions if q.blocking]
    if blocking:
        failed.append("open-questions")
        # stall where the blocked slot would bite: input slot → M1,
        # anything else → M3 execution
        worst = "M3"
        for q in blocking:
            for ref in q.context_refs:
                s = slots.get(ref)
                if s is not None and s.slot_type in _INPUT_SLOT_TYPES:
                    worst = "M1"
        stall_at["open-questions"] = worst

    return {
        "passed": not failed,
        "failed_criteria": failed,
        "expected_stall": stall_at,
        "detail": {
            "non_public": [
                {"slot": s.id, "artifact": a.id,
                 "availability": a.availability}
                for s, a in bad
            ],
            "blocking_questions": [q.id for q in blocking],
        },
    }


# ------------------------------------------------------------ staging


def _units_jsonl(units: list[AnalysisUnit], slots: dict[str, Slot]
                 ) -> list[str]:
    rows = []
    for u in units:
        rows.append(json.dumps({
            "unit_id": u.id, "headline": u.headline, "rank": u.rank,
            "outputs": [
                {"result_key": o.result_key, "kind": o.kind,
                 "label": o.label, "expected": o.expected}
                for o in u.outputs
            ],
            "inputs": [
                {"slot": s.name, "type": s.slot_type,
                 "value": s.declared_value}
                for r in u.inputs
                if (s := slots.get(r)) is not None
            ],
            "methods": [
                {"slot": s.name, "type": s.slot_type,
                 "value": s.declared_value}
                for r in u.methods
                if (s := slots.get(r)) is not None
            ],
            "claims": [c.text for c in u.claims],
        }))
    return rows


def _manifest_jsonl(artifacts: list[Artifact]) -> list[str]:
    rows = []
    for a in artifacts:
        ident = a.identifier.normalized if a.identifier else None
        rows.append(json.dumps({
            "artifact_id": a.id, "kind": a.kind,
            "identifier": ident, "availability": a.availability,
            "resolved": (
                {"repo": a.resolved.repo, "object_id": a.resolved.object_id,
                 "version": a.resolved.version, "url": a.resolved.url}
                if a.resolved else None
            ),
            "mutability_flags": a.mutability_flags,
        }))
    return rows


def stage(audit: Audit, workdir: Path) -> None:
    """Line-oriented staged inputs — monolithic JSON gets inhaled
    whole into the agent's context; the prompt tells the agent to
    grep instead."""
    workdir.mkdir(parents=True, exist_ok=True)
    slots = {s.ref(): s for s in audit.slots}
    units = [u for u in audit.units if u.headline] or audit.units
    (workdir / "units.jsonl").write_text(
        "\n".join(_units_jsonl(units, slots)) + "\n")
    (workdir / "manifest.jsonl").write_text(
        "\n".join(_manifest_jsonl(audit.artifacts)) + "\n")
    if audit.questions:
        (workdir / "questions.jsonl").write_text("\n".join(
            json.dumps(q.model_dump(mode="json"))
            for q in audit.questions) + "\n")


def build_prompt(audit: Audit) -> str:
    doc = audit.document
    title = (doc.title if doc else "") or "the target paper"
    return f"""You are a reproduction agent auditing \
"{title}" for computational reproducibility.

Staged inputs in this workdir (grep them, do NOT read whole files —
the headless exec-guard DENIES pipes, `>`/`2>` redirects, and compound
commands with `;` `&&` `||` — run one plain command at a time):
- `units.jsonl`: analysis units to reproduce, one JSON per line —
  inputs, methods, and expected outputs keyed by `result_key`.
- `manifest.jsonl`: declared artifacts with resolved repo URLs and
  availability. Fetch each declared input.
- `questions.jsonl` (if present): open questions from the static
  audit — treat as known unknowns.

Milestones to report: M1 artifacts obtained matching the manifest;
M2 runnable environment built; M3 pipeline executes to outputs;
M4 produced outputs concordant with reported results.

Policy: NEVER guess to fill a gap. Unknowns are typed
not_yet_checked | cannot_determine | needs_human_judgement. On the
last two emit a clarification entry instead of silently picking an
interpretation. Inconclusive checks stay inconclusive. No circular
validation — an ID regenerated by the same formula tests nothing;
validate against independent records.

Work through the milestones, then END with a final message starting
exactly with 'REPRO_RESULT:' followed by ONE JSON object:
{{"milestones": {{"M1": "reached|failed|M1-blocked:access", ...}},
 "produced": {{"<result_key>": <value-or-null>, ...}},
 "stall": {{"milestone": "<M>", "proximate_cause": "<why>"}},
 "clarifications": [{{"slot": "<name>", "question": "...",
   "candidates": [...], "resolves_unknown": "<kind>"}}]}}
"""


# ---------------------------------------------------- result mapping


def _milestone_records(raw_ms: dict[str, str]) -> list[Milestone]:
    out = []
    for m in _MILESTONES:
        st = raw_ms.get(m, "unchecked")
        if st not in ("reached", "failed", "M1-blocked:access",
                      "unchecked"):
            st = "unchecked"
        out.append(Milestone(m=m, status=st,
                             ts=datetime.now(timezone.utc)))
    return out


def _numeric_match(expected: float, produced: float,
                   tol: dict[str, float]) -> bool:
    rel = tol.get("rel", _DEFAULT_TOL["rel"])
    abs_ = tol.get("abs", _DEFAULT_TOL["abs"])
    return abs(produced - expected) <= max(abs_, rel * abs(expected))


def concordance(units: list[AnalysisUnit], produced: dict[str, Any],
                *, jev=None) -> list[Concordance]:
    """Match produced outputs to expected by result_key. A key with
    no produced counterpart records `unchecked`, not mismatch."""
    out: list[Concordance] = []
    judged: list[tuple[Concordance, Any, Any]] = []
    for u in units:
        for o in u.outputs:
            exp = o.expected or {}
            tol = exp.get("tolerance") or dict(_DEFAULT_TOL)
            if o.result_key not in produced:
                out.append(Concordance(result_key=o.result_key,
                                       expected=exp.get("value"),
                                       verdict="unchecked"))
                continue
            prod = produced[o.result_key]
            c = Concordance(result_key=o.result_key,
                            expected=exp.get("value"),
                            produced=prod, tolerance_used=tol)
            ev, pv = exp.get("value"), prod
            if isinstance(ev, (int, float)) and \
                    isinstance(pv, (int, float)):
                c.verdict = ("match" if _numeric_match(ev, pv, tol)
                             else "mismatch")
            elif ev is None:
                c.verdict = "unchecked"
            else:
                judged.append((c, ev, pv))
            out.append(c)
    # qualitative comparisons (e.g. figure shape, effect sign) judged
    if judged and jev is not None:
        q = {
            f"m4_concordance:{c.result_key}": {
                "type": "noul",
                "instructions": (
                    f"Expected result {c.result_key} = "
                    f"{ev!r}; the reproduction produced {pv!r}. "
                    "Do they agree within qualitative tolerance "
                    "(direction, shape, conclusion)?"
                ),
            }
            for c, ev, pv in judged
        }
        ans = jev.ask({"concordance": [
            {"key": c.result_key, "expected": ev, "produced": pv}
            for c, ev, pv in judged]}, q, stage="a6")
        for c, ev, pv in judged:
            a = ans.get(f"m4_concordance:{c.result_key}", {})
            c.verdict = ("match" if a.get("noul", 0.0) >= 0.5
                         else "mismatch")
    return out


def _map_stall(stall_raw: dict[str, Any] | None,
               milestones: list[Milestone],
               findings: list[Finding]) -> Stall | None:
    """Map a stall back to the A4/A5 finding it instantiates —
    calibrated signal for the binding audit."""
    cause = ""
    m = None
    if stall_raw:
        m, cause = stall_raw.get("milestone"), str(
            stall_raw.get("proximate_cause", ""))
    else:
        for ms in milestones:
            if ms.status in ("failed", "M1-blocked:access"):
                m = ms.m
                break
    if m is None:
        return None
    mapped = None
    if cause:

        def _toks(text):
            return {t.lower() for t in re.findall(r"[\w.-]{4,}", text)}

        def _distinctive(t):
            # accession/id-shaped tokens — a single shared one is
            # already strong mapping signal, unlike generic words
            return any(c.isdigit() for c in t) or "-" in t \
                or len(t) >= 10

        toks = _toks(cause)
        best, best_score = None, 0
        for f in findings:
            shared = toks & _toks(f.description)
            n_distinctive = sum(1 for t in shared if _distinctive(t))
            score = n_distinctive * 2 + len(shared)
            if score > best_score:
                best, best_score = f, score
        # one shared accession-shaped token (score 3) already maps;
        # two+ generic words (score 2) don't
        if best is not None and best_score >= 3:
            mapped = f"finding:{best.id}"
    return Stall(milestone=m, proximate_cause=cause,
                 mapped_finding_id=mapped)


def _run_status(run: ReproRun) -> str:
    ms = {m.m: m.status for m in run.milestones}
    if ms.get("M4") == "reached":
        return "pass"
    if any(v in ("failed", "M1-blocked:access") for v in ms.values()):
        return "fail"
    if ms.get("M3") == "reached":
        return "indeterminate"  # ran, but M4 uncheckable
    return "fail"


# ------------------------------------------------------------ runs


def _models() -> list[dict[str, str]]:
    """Heterogeneous ensemble preferred; homogeneous Kx1 is a legal
    fallback. Composition lands in every ReproRun + verdict meta."""
    raw = os.environ.get("REPRO_MODELS", "")
    if raw.strip():
        return [{"provider": "orbit", "model": m.strip()}
                for m in raw.split(",") if m.strip()]
    return [{"provider": "orbit",
             "model": os.environ.get("LLM_MODEL", "default")}]


def run_full(
    audit: Audit,
    *,
    k: int = 5,
    jev=None,
    force: bool = False,
    invoke=orbit.run_orbit,
    argv: list[str] | None = None,
    orbit_version: str = "",
    pi_version: str = "",
    timeout_s: int = orbit.RUN_TIMEOUT_S,
    keep_workdirs: bool = True,
) -> list[ReproRun]:
    """Gate -> K sequential Orbit runs -> ReproRun records."""
    g = gate(audit)
    models = _models()
    base = orbit.runs_dir() / f"audit-{audit.id}"
    runs: list[ReproRun] = []

    if not g["passed"] and not force:
        for i in range(k):
            runs.append(ReproRun(
                id=_new_id("repro_run", f"{audit.id}:{i}"),
                k_index=i, model=models[i % len(models)],
                gate={"passed": False,
                      "failed_criteria": g["failed_criteria"],
                      "expected_stall": g["expected_stall"]},
                status="gate_blocked",
            ))
        return runs

    prompt = build_prompt(audit)
    for i in range(k):
        workdir = base / f"run{i}"
        session_dir = workdir / "sessions"
        stage(audit, workdir)
        run = ReproRun(
            id=_new_id("repro_run", f"{audit.id}:{i}"),
            k_index=i, model=models[i % len(models)],
            orbit_version=orbit_version, pi_version=pi_version,
            workdir=str(workdir), session_dir=str(session_dir),
            gate={"passed": g["passed"],
                  "failed_criteria": g["failed_criteria"],
                  "expected_stall": g["expected_stall"]},
        )
        try:
            out = invoke(prompt, workdir, session_dir,
                         argv=argv or [], timeout_s=timeout_s)
        except ClientError as e:
            run.status = "error"
            run.stall = Stall(milestone="M1", proximate_cause=str(e))
            runs.append(run)
            continue
        run.wall_s = float(out.get("wall_s") or 0.0)
        res = out.get("result")
        if out.get("status") != "ok" or res is None:
            run.status = "error"
            tail = (out.get("raw") or "")[-400:]
            cause = f"orbit: {out.get('status')}"
            if tail:
                cause += f" — {tail}"
            run.stall = Stall(milestone="M1", proximate_cause=cause)
            runs.append(run)
            continue
        run.milestones = _milestone_records(res.get("milestones", {}))
        produced = res.get("produced") or {}
        headline = [u for u in audit.units if u.headline] or audit.units
        run.concordance = concordance(headline, produced, jev=jev)
        run.stall = _map_stall(res.get("stall"), run.milestones,
                               audit.findings)
        for c in res.get("clarifications") or []:
            q = ClarificationRequest(
                id=_new_id("clarification_request",
                           f"{run.id}:{len(run.clarifications)}"),
                blocking=c.get("resolves_unknown")
                in ("cannot_determine", "needs_human_judgement"),
                question=str(c.get("question", "")),
                context_refs=[f"slot:{c.get('slot', '')}",
                              f"repro_run:{run.id}"],
                resolves_unknown=c.get("resolves_unknown",
                                       "not_yet_checked"),
            )
            audit.questions.append(q)
            run.clarifications.append(q.ref())
        run.status = _run_status(run)
        runs.append(run)
    return runs


def verdict_stats(runs: list[ReproRun]) -> dict[str, Any]:
    """passing = M4 reached; checked = run actually executed (not
    gate_blocked, not error)."""
    passing = sum(1 for r in runs if r.status == "pass")
    checked = sum(1 for r in runs
                  if r.status in ("pass", "fail", "indeterminate"))
    gate_blocked = [r for r in runs if r.status == "gate_blocked"]
    flavor = None
    if gate_blocked:
        crit = gate_blocked[0].gate.get("failed_criteria") or []
        flavor = f"gate-blocked:{','.join(crit)}" if crit else \
            "gate-blocked"
    return {"passing_runs": passing, "checked_runs": checked,
            "k": len(runs), "flavor": flavor}

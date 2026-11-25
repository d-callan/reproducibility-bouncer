"""A7b — human-readable audit report.

Renders the audit record + report dict (the same payload the CLI emits
as JSON) to a single self-contained HTML file. No dependencies, no JS:
collapsible sections use native <details>. Works both live (after
pipeline) and offline from a previously-saved audit JSON.
"""

from __future__ import annotations

import html
import json
from typing import Any

_LEVEL_CLASS = {
    "ok": "ok", "warning": "warn", "blocking": "block",
    "unchecked": "mute", "na": "mute",
}
_SEV_CLASS = {
    "blocking": "block", "warning": "warn", "informational": "info",
}


def _e(s: Any) -> str:
    return html.escape("" if s is None else str(s))


def _ref(rec: dict) -> str:
    return f"{rec.get('record_type', '?')}:{rec.get('id', '')}"


def _unit_text(document: dict | None, ref: dict) -> str:
    """Resolve a UnitRef {section, unit_index} to the unit's text."""
    try:
        units = (
            document or {}).get("sections", [])[ref["section"]].get(
            "units", [])
    except (KeyError, IndexError, TypeError):
        return ""
    for u in units:  # match declared index, fall back to position
        if u.get("index") == ref.get("unit_index"):
            return u.get("text", "")
    i = (ref.get("unit_index") or 0) - 1
    return units[i].get("text", "") if 0 <= i < len(units) else ""


def _quotes(document: dict | None, refs: list[dict], n: int = 3) -> str:
    qs = [_unit_text(document, r) for r in (refs or [])[:n]]
    qs = [q for q in qs if q]
    if not qs:
        return ""
    items = "".join(f"<li><q>{_e(q)}</q></li>" for q in qs)
    return f'<ul class="quotes">{items}</ul>'


def _badge(text: str, cls: str) -> str:
    return f'<span class="badge {cls}">{_e(text)}</span>'


def _findings_html(finds: list[dict], document) -> str:
    if not finds:
        return '<p class="mute">no findings</p>'
    rows = []
    for f in finds:
        sev = f.get("severity", "informational")
        sug = (f'<div class="sug">&#8618; {_e(f["suggestion"])}</div>'
               if f.get("suggestion") else "")
        mech = f.get("mechanism", "")
        conf = f.get("confidence")
        conf_s = (f' conf {conf:.2f}' if isinstance(
            conf, (int, float)) else "")
        rows.append(
            f'<li>{_badge(sev, _SEV_CLASS.get(sev, "info"))} '
            f'{_e(f.get("axis"))} &middot; {_e(mech)}{_e(conf_s)}'
            f'<div>{_e(f.get("description"))}</div>{sug}</li>')
    return f'<ul class="findings">{"".join(rows)}</ul>'


def _slot_html(slot: dict, ctx: dict) -> str:
    doc = ctx["document"]
    bound = slot.get("bound_instance") or {}
    art = ctx["arts"].get(
        (bound.get("artifact_id") or "").split(":")[-1])
    art_s = ""
    if art:
        ident = (art.get("identifier") or {}).get("raw", "")
        res = art.get("resolved") or {}
        avail = art.get("availability") or "?"
        cls = ("ok" if avail == "public" else
               "warn" if avail in ("controlled-access", "on-request")
               else "block")
        art_s = (
            f'<div class="art">&rarr; {_e(ident)}'
            f'{_badge(avail, cls)}'
            f' <span class="mute">{_e(res.get("repo"))} '
            f'{_e(res.get("version"))}</span>'
            + (f' <a href="{_e(res["url"])}">link</a>'
               if res.get("url") else "") + "</div>")
    score = slot.get("spec_score")
    score_s = (
        "" if score is None else
        f' <span class="mute">score {_e(score)} '
        f'{_e(slot.get("spec_basis"))}</span>')
    spec = slot.get("spec") or {}
    matched = spec.get("matched_samples") or []
    matched_s = (
        f' <span class="mute">({len(matched)} samples)</span>'
        if matched else "")
    via = spec.get("matched_via") or []
    via_s = "".join(
        f' <code>{_e(v.get("field"))}={_e(v.get("value"))}</code>'
        f'<span class="mute">/{_e(v.get("mechanism"))}</span>'
        for v in via)
    own = [f for f in ctx["findings"]
           if _ref(slot) in (f.get("subject") or [])]
    finds_s = (
        _findings_html(own, doc)
        if own else "")
    return (
        f'<li><b>{_e(slot.get("name"))}</b> '
        f'<span class="mute">{_e(slot.get("slot_type"))}</span><br>'
        f'declared: <code>{_e(slot.get("declared_value"))}</code>'
        f'{score_s}{matched_s}{via_s}{art_s}'
        f'{_quotes(doc, slot.get("evidence"))}'
        f'{finds_s}</li>')


def _unit_section(unit: dict, ctx: dict) -> str:
    slots = [s for s in ctx["slots"].values()
             if s.get("unit_id") == _ref(unit)]
    slots_lis = "".join(_slot_html(s, ctx) for s in slots) or (
        '<li class="mute">no slots</li>')
    outs = "".join(
        f'<li><code>{_e(o.get("result_key"))}</code> '
        f'<span class="mute">{_e(o.get("kind"))} '
        f'{_e(o.get("label"))}</span></li>'
        for o in unit.get("outputs") or [])
    outs_s = (
        f'<details><summary>expected outputs '
        f'({len(unit.get("outputs") or [])})</summary>'
        f'<ul>{outs}</ul></details>' if outs else "")
    head = _badge("headline", "hl") if unit.get("headline") else ""
    claim = (unit.get("claims") or [{}])[0].get("text", "")
    title = claim[:120] or unit.get("id", "")
    return (
        f'<details class="unit"><summary>{head} {_e(title)}'
        f' <span class="mute">[{len(slots)} slots]</span></summary>'
        f'{_quotes(ctx["document"], unit.get("source_units"))}'
        f'{outs_s}<ul class="slots">{slots_lis}</ul></details>')


_RUN_STATUS_CLASS = {
    "pass": "ok", "fail": "block", "error": "block",
    "indeterminate": "warn", "gate_blocked": "mute",
}


def _runs_html(runs: list[dict]) -> str:
    """Per-run reproduction audit trail: what orbit claimed vs. the
    artifacts it left behind (transcript, session, notebook)."""
    if not runs:
        return ""
    parts = []
    for r in runs:
        status = r.get("status") or "?"
        cls = _RUN_STATUS_CLASS.get(status, "mute")
        mdl = r.get("model") or {}
        head = (
            f'<summary>run {r.get("k_index", "?")} '
            f'{_badge(status, cls)} '
            f'<span class="mute">{_e(mdl.get("model"))} '
            f'orbit {_e(r.get("orbit_version"))} &middot; '
            f'{_e(r.get("wall_s"))}s</span></summary>')
        rows = ""
        for m in r.get("milestones") or []:
            mst = m.get("status") or "?"
            mcls = {"reached": "ok", "failed": "block",
                    "unchecked": "mute"}.get(mst, "warn")
            rows += (f'<tr><td>{_e(m.get("m"))}</td>'
                     f'<td>{_badge(mst, mcls)}</td></tr>')
        ms_s = (f'<table><tr><th>milestone</th><th>status</th></tr>'
                f'{rows}</table>' if rows else
                '<p class="mute">no milestones reported</p>')
        stall = r.get("stall") or {}
        stall_s = ""
        if stall.get("milestone") or stall.get("proximate_cause"):
            stall_s = (
                f'<p><b>Stall @{_e(stall.get("milestone"))}:</b> '
                f'{_e(stall.get("proximate_cause"))}</p>')
        conc = ""
        for c in r.get("concordance") or []:
            cv = c.get("verdict") or "?"
            ccls = {"match": "ok", "mismatch": "block"}.get(cv, "mute")
            conc += (f'<tr><td>{_e(c.get("result_key"))}</td>'
                     f'<td>{_e(c.get("expected"))}</td>'
                     f'<td>{_e(c.get("produced"))}</td>'
                     f'<td>{_badge(cv, ccls)}</td></tr>')
        conc_s = (f'<table><tr><th>result</th><th>expected</th>'
                  f'<th>produced</th><th>verdict</th></tr>'
                  f'{conc}</table>' if conc else "")
        gate = r.get("gate") or {}
        gate_s = ""
        if gate.get("failed_criteria"):
            gate_s = (f'<p class="mute">gate failed: '
                      f'{_e(", ".join(gate["failed_criteria"]))}</p>')
        wd, sd = r.get("workdir", ""), r.get("session_dir", "")
        trail_s = (
            f'<p class="mute">audit trail — workdir '
            f'<code>{_e(wd)}</code> (notebook.md, activity.jsonl, '
            f'orbit_stdout.log), session <code>{_e(sd)}</code></p>'
            if wd else "")
        parts.append(f'<details class="unit">{head}{gate_s}{ms_s}'
                     f'{stall_s}{conc_s}{trail_s}</details>')
    return (f'<section><h2>Reproduction runs ({len(runs)})</h2>'
            + "".join(parts) + "</section>")


def render_html(audit: dict, report: dict) -> str:
    """Render {audit, report} (the CLI's JSON payload) to HTML."""
    doc = audit.get("document") or {}
    verdict = report.get("verdict") or {}
    axes = report.get("axes") or {}
    counts = report.get("counts") or {}
    meta = report.get("meta") or {}
    findings = audit.get("findings") or []
    questions = audit.get("questions") or []
    ctx = {
        "document": doc,
        "arts": {a.get("id"): a for a in audit.get("artifacts") or []},
        "slots": {s.get("id"): s for s in audit.get("slots") or []},
        "findings": findings,
    }

    label = verdict.get("label", "?")
    vcls = {"PASS": "ok", "NA": "info", "FAIL": "block",
            "INDETERMINATE": "warn"}.get(label, "mute")
    flavor = verdict.get("flavor")
    flavor_s = (f' {_badge(flavor, "warn")}' if flavor else "")
    runs_s = ""
    if verdict.get("k"):
        runs_s = (
            f' <span class="mute">{verdict.get("passing_runs")}/'
            f'{verdict.get("k")} passing (tau '
            f'{verdict.get("tau")})</span>')

    axis_rows = ""
    for a, v in axes.items():
        cls = _LEVEL_CLASS.get(v.get("level"), "mute")
        nf = len(v.get("finding_ids") or [])
        axis_rows += (
            f'<tr><td>{_e(a)}</td>'
            f'<td>{_badge(v.get("level"), cls)}</td>'
            f'<td class="num">{_e(v.get("confidence"))}</td>'
            f'<td class="num">{nf}</td></tr>')

    blocking = [f for f in findings if f.get("severity") == "blocking"]
    runs_sec = _runs_html(report.get("runs") or [])
    open_q = [q for q in questions
              if q.get("blocking") and q.get("answer") is None]
    gate_lis = "".join(
        f'<li>{_e(q.get("question"))}</li>' for q in open_q)
    gate_s = (
        f'<section class="gate"><h2>Blocking</h2>'
        f'{_findings_html(blocking, doc)}'
        + (f'<ol class="questions">{gate_lis}</ol>' if gate_lis else "")
        + "</section>") if (blocking or open_q) else ""

    units = sorted(
        audit.get("units") or [],
        key=lambda u: (not u.get("headline"), u.get("rank", 0)))
    units_s = "".join(_unit_section(u, ctx) for u in units)

    all_q = "".join(
        f'<li>{"&#9873; " if q.get("blocking") else ""}'
        f'{_e(q.get("question"))}</li>' for q in questions)
    questions_s = (
        f'<details><summary><h2>Questions '
        f'({len(questions)})</h2></summary>'
        f'<ol class="questions">{all_q}</ol></details>'
        if questions else "")

    by_axis: dict[str, list] = {}
    for f in findings:
        by_axis.setdefault(f.get("axis", "other"), []).append(f)
    finds_by_axis = "".join(
        f'<details><summary>{_e(ax)} ({len(fs)})</summary>'
        f'{_findings_html(fs, doc)}</details>'
        for ax, fs in sorted(by_axis.items()))

    arts = audit.get("artifacts") or []
    avail_cls = {"public": "ok", "absent": "block"}
    art_rows = ""
    for a in arts:
        avail = a.get("availability") or "?"
        res = a.get("resolved") or {}
        ident = (a.get("identifier") or {}).get("raw")
        art_rows += (
            f'<tr><td>{_e(a.get("kind"))}</td>'
            f'<td>{_e(ident)}</td>'
            f'<td>{_e(res.get("repo"))} {_e(res.get("version"))}</td>'
            f'<td>{_badge(avail, avail_cls.get(avail, "warn"))}</td>'
            f'</tr>')
    arts_s = (
        f'<details><summary><h2>Artifacts ({len(arts)})</h2></summary>'
        f'<table><tr><th>kind</th><th>identifier</th>'
        f'<th>resolved</th><th>access</th></tr>{art_rows}</table>'
        f'</details>' if arts else "")

    costs = report.get("costs") or {}
    cost_bits = " &middot; ".join(
        f'{k}: {v.get("model")}, {v.get("calls")} calls'
        for k, v in costs.items()
        if k != "total_usd" and isinstance(v, dict))
    if costs.get("total_usd") is not None:
        cost_bits += f' &middot; total ${costs["total_usd"]}'

    title = doc.get("title") or meta.get("input") or "audit report"
    byline = ", ".join((doc.get("authors") or [])[:3])
    if len(doc.get("authors") or []) > 3:
        byline += " et al."
    src = doc.get("source") or {}
    ids = " &middot; ".join(
        _e(v) for k, v in src.items() if v and k != "local_path")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>reprobouncer — {_e(title[:80])}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;
 padding:0 1rem;color:#222;line-height:1.45}}
h1{{font-size:1.4rem}} h2{{display:inline;font-size:1.05rem}}
section{{margin:1rem 0}}
.badge{{display:inline-block;padding:.1rem .45rem;border-radius:.6rem;
 font-size:.78rem;font-weight:600;margin:0 .3rem;vertical-align:middle}}
.ok{{background:#d4edda;color:#155724}}
.info{{background:#d1ecf1;color:#0c5460}}
.warn{{background:#fff3cd;color:#856404}}
.block{{background:#f8d7da;color:#721c24}}
.hl{{background:#e2d9f3;color:#4527a0}}
.mute{{color:#888;font-size:.85rem}}
table{{border-collapse:collapse;margin:.5rem 0}}
td,th{{border:1px solid #ddd;padding:.3rem .7rem;text-align:left}}
td.num{{text-align:right}}
details{{margin:.4rem 0}}
details.unit{{border:1px solid #e3e3e3;border-radius:.4rem;
 padding:.4rem .7rem;background:#fafafa}}
summary{{cursor:pointer}}
ul.findings{{list-style:none;padding-left:.5rem}}
ul.findings li{{margin:.35rem 0}}
ul.slots{{list-style:none;padding-left:1rem}}
ul.slots>li{{border-left:3px solid #ddd;padding-left:.6rem;
 margin:.5rem 0}}
ul.quotes{{list-style:none;padding-left:1rem;color:#555;
 font-size:.85rem}}
ul.quotes q{{font-style:italic}}
.sug{{color:#0c5460;font-size:.85rem}}
.gate{{border:1px solid #f5c6cb;background:#fff5f5;
 border-radius:.4rem;padding:.6rem .9rem}}
code{{background:#eee;border-radius:.2rem;padding:0 .25rem;
 font-size:.85em}}
footer{{margin-top:2rem;border-top:1px solid #ddd;padding-top:.5rem}}
</style></head><body>
<h1>{_e(title)}</h1>
<p class="mute">{_e(byline)} {_e(doc.get("journal"))}
 {_e(doc.get("year"))}<br>{ids}</p>
<section class="verdict"><h2>Verdict</h2>
 {_badge(label, vcls)}{flavor_s}{runs_s}</section>
<section><h2>Axes</h2>
<table><tr><th>axis</th><th>level</th><th>confidence</th>
<th>findings</th></tr>{axis_rows}</table></section>
{gate_s}
{runs_sec}
<section><h2>Analysis units ({counts.get("analysis_units", 0)})</h2>
{units_s}</section>
{arts_s}
<details><summary><h2>All findings ({len(findings)})</h2></summary>
{finds_by_axis}</details>
{questions_s}
<footer class="mute">
{_e(meta.get("input"))} &middot; {_e(meta.get("mode"))} mode
 &middot; {_e(meta.get("timestamp"))}<br>{cost_bits}
</footer></body></html>"""


def render_markdown(audit: dict, report: dict) -> str:
    """Plain-markdown fallback renderer (same sections, no styling)."""
    doc = audit.get("document") or {}
    verdict = report.get("verdict") or {}
    axes = report.get("axes") or {}
    findings = audit.get("findings") or []
    questions = audit.get("questions") or []
    lines = [f"# {doc.get('title') or 'audit report'}", ""]
    flavor = verdict.get("flavor")
    lines.append(
        f"**Verdict: {verdict.get('label')}**"
        + (f" ({flavor})" if flavor else ""))
    lines += ["", "## Axes", "",
              "| axis | level | confidence | findings |",
              "|---|---|---|---|"]
    for a, v in axes.items():
        lines.append(
            f"| {a} | {v.get('level')} | {v.get('confidence')} "
            f"| {len(v.get('finding_ids') or [])} |")
    open_q = [q for q in questions
              if q.get("blocking") and q.get("answer") is None]
    blocking = [f for f in findings if f.get("severity") == "blocking"]
    if blocking or open_q:
        lines += ["", "## Blocking"]
        lines += [f"- {f.get('description')}" for f in blocking]
        lines += [f"- Q: {q.get('question')}" for q in open_q]
    runs = report.get("runs") or []
    if runs:
        lines += ["", "## Reproduction runs"]
        for r in runs:
            mdl = r.get("model") or {}
            lines.append(
                f"\n### run {r.get('k_index', '?')} — "
                f"{r.get('status', '?')} "
                f"({mdl.get('model', '')}, {r.get('wall_s')}s)")
            gate = r.get("gate") or {}
            if gate.get("failed_criteria"):
                lines.append(
                    f"- gate failed: "
                    f"{', '.join(gate['failed_criteria'])}")
            for m in r.get("milestones") or []:
                lines.append(
                    f"- {m.get('m')}: {m.get('status')}")
            stall = r.get("stall") or {}
            if stall.get("milestone") or stall.get("proximate_cause"):
                lines.append(
                    f"- **stall @{stall.get('milestone')}**: "
                    f"{stall.get('proximate_cause')}")
            for c in r.get("concordance") or []:
                lines.append(
                    f"- {c.get('result_key')}: expected "
                    f"`{c.get('expected')}` produced "
                    f"`{c.get('produced')}` — {c.get('verdict')}")
            if r.get("workdir"):
                lines.append(
                    f"- audit trail: `{r.get('workdir')}` "
                    f"(notebook.md, activity.jsonl, "
                    f"orbit_stdout.log); session "
                    f"`{r.get('session_dir')}`")
    slots = {s.get("id"): s for s in audit.get("slots") or []}
    arts = {a.get("id"): a for a in audit.get("artifacts") or []}
    lines += ["", "## Analysis units"]
    for u in audit.get("units") or []:
        claim = (u.get("claims") or [{}])[0].get("text", "")
        lines.append(
            f"\n### {'★ ' if u.get('headline') else ''}"
            f"{claim[:100] or u.get('id')}")
        for s in slots.values():
            if s.get("unit_id") != _ref(u):
                continue
            bound = s.get("bound_instance") or {}
            art = arts.get(
                (bound.get("artifact_id") or "").split(":")[-1])
            ident = (art or {}).get("identifier") or {}
            score = s.get("spec_score")
            lines.append(
                f"- **{s.get('name')}** ({s.get('slot_type')}): "
                f"`{s.get('declared_value')}`"
                + ("" if score is None else f" — score {score}")
                + (f" → {ident.get('raw', '')}"
                   f" ({(art or {}).get('availability', '?')})"
                   if art else ""))
            for q in (s.get("evidence") or [])[:2]:
                t = _unit_text(doc, q)
                if t:
                    lines.append(f"  > {t}")
        own_refs = {_ref(s) for s in slots.values()
                    if s.get("unit_id") == _ref(u)}
        own = [f for f in findings
               if own_refs & set(f.get("subject") or [])]
        for f in own:
            lines.append(
                f"  - [{f.get('severity')}] {f.get('description')}"
                + (f" — *{f.get('suggestion')}*"
                   if f.get("suggestion") else ""))
    if questions:
        lines += ["", "## Questions"]
        lines += [f"- {q.get('question')}" for q in questions]
    return "\n".join(lines) + "\n"


def render(payload: dict, fmt: str) -> str:
    """Dispatch on the CLI payload {'audit':…,'report':…}."""
    audit, report = payload.get("audit") or {}, payload.get(
        "report") or {}
    if fmt == "html":
        return render_html(audit, report)
    if fmt == "markdown":
        return render_markdown(audit, report)
    return json.dumps(payload, indent=2, default=str)

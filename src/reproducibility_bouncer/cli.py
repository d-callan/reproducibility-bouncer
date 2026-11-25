"""reprobouncer CLI.

M0 surface: `document` acquires + parses a paper into the normalized
Document record. `audit` is the pipeline entrypoint (A2+ — stub).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv

from .clients import ClientError
from .document import acquire
from .intake import classify

app = typer.Typer(
    name="reprobouncer",
    help="Audit a paper's computational reproducibility.",
    no_args_is_help=True,
)


def _emit(payload: dict, out: Optional[Path]) -> None:
    _emit_text(json.dumps(payload, indent=2, default=str), out)


def _emit_text(text: str, out: Optional[Path]) -> None:
    if out:
        out.write_text(text)
        typer.echo(f"wrote {out}", err=True)
    else:
        typer.echo(text)


@app.command()
def document(
    input_: str = typer.Argument(
        ..., help="doi:… / bare DOI / PMID / PMCID / arXiv id / URL / "
                  "local file (with --draft)",
    ),
    draft: bool = typer.Option(
        False, "--draft", help="treat input as a local draft file",
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="write JSON here instead of stdout",
    ),
    mock_jev: bool = typer.Option(
        False, "--mock-jev",
        help="offline dev: no JEV calls (accepted for forward-compat; "
             "A1 makes none anyway)",
    ),
) -> None:
    """Acquire + parse a paper -> normalized Document JSON (A0+A1)."""
    intake = classify(input_, draft=draft)
    try:
        doc, evidence = acquire(intake, draft=draft)
    except ClientError as e:
        typer.echo(f"refused: {e}", err=True)
        raise typer.Exit(2)
    _emit(
        {
            "document": json.loads(doc.model_dump_json()),
            "evidence": [
                json.loads(e.model_dump_json()) for e in evidence
            ],
        },
        out,
    )


@app.command()
def audit(
    input_: str = typer.Argument(..., help="same forms as `document`"),
    draft: bool = typer.Option(False, "--draft"),
    full: bool = typer.Option(
        False, "--full", help="run the A6 reproduction harness (Orbit)",
    ),
    k: int = typer.Option(5, "--k", help="independent runs (--full)"),
    tau: int = typer.Option(
        3, "--tau", help="min passing runs for a PASS verdict"),
    run_timeout: int = typer.Option(
        20, "--run-timeout",
        envvar="REPRO_RUN_TIMEOUT_MIN",
        help="per-run wall budget in minutes (--full); complex studies "
             "need more than the 20 min default"),
    force_run: bool = typer.Option(
        False, "--force-run",
        help="override the A6 gate (calibration only)"),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="write report here (JSON or --format)",
    ),
    format_: str = typer.Option(
        "json", "--format", "-f",
        help="output format: json | html | markdown",
    ),
    mock_jev: bool = typer.Option(False, "--mock-jev",
                                  help="offline dev: canned judge answers"),
    mock_llm: bool = typer.Option(False, "--mock-llm",
                                  help="offline dev: canned generator"),
) -> None:
    """Run the audit pipeline and emit the A7 report. Static mode
    gives an NA verdict (reproduction not attempted); --full adds A6
    reproduction runs and a PASS/FAIL/INDETERMINATE verdict."""
    from .jev import JevClient
    from .llm import LlmClient
    from .pipeline import run_full, run_static

    load_dotenv()
    try:
        jev = JevClient(mock=mock_jev)
        llm = LlmClient(mock=mock_llm)
    except Exception as e:  # missing credentials
        typer.echo(f"client init failed: {e}", err=True)
        raise typer.Exit(2)
    if not llm.enabled:
        typer.echo(
            "A2 needs a generator: set LLM_* env or use --mock-llm",
            err=True,
        )
        raise typer.Exit(2)
    intake = classify(input_, draft=draft)
    try:
        if full:
            audit_rec, report = run_full(
                intake, draft=draft, k=k, tau=tau, force=force_run,
                run_timeout_s=run_timeout * 60,
                jev=jev, llm=llm, input_str=input_,
            )
        else:
            audit_rec, report = run_static(
                intake, draft=draft, jev=jev, llm=llm, input_str=input_,
            )
    except ClientError as e:
        typer.echo(f"refused: {e}", err=True)
        raise typer.Exit(2)
    payload = {"audit": json.loads(audit_rec.to_json()),
               "report": report}
    if format_ == "json":
        _emit(payload, out)
    else:
        from .render import render
        _emit_text(render(payload, format_), out)


@app.command()
def render(
    report_json: Path = typer.Argument(
        ..., help="a saved audit JSON (the -o payload from `audit`)"),
    format_: str = typer.Option(
        "html", "--format", "-f",
        help="output format: html | markdown | json",
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="write here instead of stdout",
    ),
) -> None:
    """Re-render a saved audit JSON as a human-readable report."""
    from .render import render as _render
    payload = json.loads(report_json.read_text())
    _emit_text(_render(payload, format_), out)


def main() -> None:
    app()


if __name__ == "__main__":
    main()

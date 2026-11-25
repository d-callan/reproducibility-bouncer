# reproducibility-bouncer

Audit a scientific paper for **reproducibility, robustness, and
methodological/statistical soundness** — BioNym-style deterministic
evidence gathering + typed JEV judging + LLM proposal, with an
Orbit-backed reproduction harness that actually tries to re-run the
analysis.

Design: [PLAN.md](PLAN.md) · specs: [specs/](specs/)

## Install

```bash
pip install -e .        # or: pip install -e '.[dev]' for tests
```

## Configuration (env vars)

| var | purpose |
|---|---|
| `TYPESAFE_API_KEY` | JEV judge key (required for judged stages) |
| `JEV_BASE_URL`, `JEV_MODEL` | judge endpoint/model overrides |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | generator (OpenAI-compatible; dev default OpenRouter + MiniMax-M2.7) |
| `UNPAYWALL_EMAIL` | required by the Unpaywall API |
| `NCBI_API_KEY`, `GITHUB_TOKEN` | optional; raise rate limits |
| `GALAXY_URL`, `GALAXY_API_KEY` | Orbit-side only |

Offline dev: pass `--mock-jev` — no key needed.

## Usage

```bash
# M0: acquire + parse a paper into the normalized Document record
reprobouncer document doi:10.1073/pnas.2409747121
reprobouncer document 10.3389/fmicb.2021.626715 --out doc.json

# draft mode
reprobouncer document ./paper.pdf --draft

# static audit (A0–A5) -> A7 report (verdict NA: reproduction not attempted)
reprobouncer audit doi:10.1073/pnas.2409747121 --out audit.json
reprobouncer audit ./paper.pdf --draft

# offline dev: canned judge + generator, no API keys needed
reprobouncer audit doi:10.1073/pnas.2409747121 --mock-jev --mock-llm

# full mode: A6 reproduction harness (needs Orbit/loom 0.9.x)
reprobouncer audit doi:10.1073/pnas.2409747121 --full --k 5 --tau 3
reprobouncer audit doi:...  --full --force-run   # calibration only
```

`document` emits `{document, evidence}`; `audit` emits `{audit,
report}` — verdict (`NA`/`gate-blocked:<criterion>` static,
`PASS`/`FAIL`/`INDETERMINATE` full), per-axis levels, findings,
clarification questions, run records, counts, cost logs.

`--full` needs Orbit 0.9.x installed the usual way —
`npm i -g @galaxyproject/loom@0.9` (published name pending the
orbit rename; bin is `loom`, we detect `orbit` or `loom` on PATH)
under node >= 22.19. `ORBIT_BIN` overrides detection with a launcher
path; `NODE_BIN` prepends a node>=22.19 bin dir when the default
node is older. Runs use your normal `~/.orbit` provider config;
`REPRO_ORBIT_HOME` isolates for evals. Each of K sequential Orbit
attempts runs in `$REPRO_RUNS_DIR` (default
`~/.cache/reprobouncer_runs`); a failed pre-flight gate records
`gate-blocked:<criterion>` instead of burning runs. Each run gets a
wall-clock budget — `--run-timeout MIN` (env `REPRO_RUN_TIMEOUT_MIN`,
default 20); raise it for complex studies (big downloads, long
analyses). If Orbit dies on a transient LLM-endpoint error (5xx,
connection reset, rate limit) the run resumes its session with
`--continue` instead of failing — `REPRO_ORBIT_RETRIES` (default 2)
and `REPRO_ORBIT_RETRY_DELAY_S` (default 60) tune that; the wall
budget is shared across attempts.

## Containerized runs (recommended for `--full`)

The `Dockerfile` bakes the whole stack — python package, node 22,
Orbit 0.9.x (pinned `d06b334d`) — so a `--full` audit executes the
reproduction agent's shell commands **inside the container**, not on
your host. That is the containment boundary: the agent can only touch
container filesystem.

```bash
docker build -t reprobouncer .

# static audit
docker run --rm --env-file .env -v "$PWD/out:/out" \
  reprobouncer audit doi:10.1073/pnas.2409747121 -o /out/audit.json

# full mode — K Orbit attempts run in /runs (mount it to inspect
# agent workdirs / notebooks afterwards)
docker run --rm --env-file .env \
  -v "$PWD/out:/out" -v reprobouncer_runs:/runs \
  reprobouncer audit doi:10.1073/pnas.2409747121 --full -k 1 -o /out/full.json
```

Orbit's LLM provider is env-keyed in the container
(`ORBIT_ACTIVE_LLM_API_KEY`, auto-forwarded from `LLM_API_KEY`) — no
`~/.orbit` mount needed. Endpoint/model default to TACC Tejas +
MiniMax-M3; override with `ORBIT_LLM_BASE_URL` / `ORBIT_LLM_MODEL`.

## Layout

`src/reproducibility_bouncer/` — `jev.py`, `llm.py`, `evidence.py`
(vendored from BioNym), `auditrecord.py` (thin container, Decision 9),
`units.py` (numbered citable units / citation-by-number),
`intake.py` (A0), `document.py` (A1), `extract.py` (A2),
`resolve.py` (A3), `bind.py` (A4), `static_checks.py` (A5),
`report.py` (A7), `pipeline.py` (orchestration), `cli.py`, and
`clients/` (crossreorb,t. y` (A6 ruanyr), `reproducewall, A6 gate +
europepmc, geo, `pipeline.py`, ena, zenodo,  dryad, osf, datacite 
 `registry.py` identifier dispatch). Build order + milestones: 
LAN.md §10. 

# reproducibility-bouncer — design plan

- **Name**: `reproducibility-bouncer` — PyPI `reproducibility-bouncer`,
  import `reproducibility_bouncer`, CLI `reprobouncer`
- **Repo**: `github.com/d-callan/reproducibility-bouncer` (create when
  scaffold lands)
- **License**: MIT
- **First domain**: computational pathogen genomics (schema kept
  domain-generic — see Decisions)

## 1. Concept

A standalone tool that audits a scientific paper (published) or manuscript
(draft) for **reproducibility, robustness, and methodological/statistical
soundness**, combining the BioNym architecture — deterministic evidence
gathering + typed JEV judging + LLM proposal — with an **agentic
reproduction harness** (Orbit-backed, §3/A6) that actually tries to
re-run the analysis.

Outputs:

- **Per-axis scores** (see §6), each backed by evidence and, where judged,
  calibrated JEV confidences.
- **Structured suggestions** — actionable findings classified
  `blocking | warning | informational` and ranked by severity and
  effort (e.g. "SRA accession SRP12345 resolves but only 3/12 samples
  are public"; "no random seed is set in the reported DE pipeline").
- **An overall PASS / FAIL mark** whose semantics are operational:
  PASS = most reproduction-agent runs, given the paper plus its declared
  public artifacts, reach a "meaningful result" milestone without any
  human intervention. FAIL otherwise. Threshold calibrated against a
  ground-truth corpus (§8).

Two input modes:

- **Published paper** — input is a DOI / PMID / PMCID / arXiv|bioRxiv id /
  URL. Tool locates full text and hunts the public artifacts (data
  accessions, code repos) declared or implied by the paper.
- **Draft manuscript** — input is a file (PDF/DOCX/TeX/markdown) plus an
  optional user-supplied manifest (`data:` and `code:` locations — which
  may be public URLs, accessions, *or local paths*). Verdict is reframed
  as **readiness**: can it survive agent reproduction *as submitted*,
  and what must be fixed before submission.

## 2. What carries over from BioNym (reuse map)

The following patterns/components lift over nearly verbatim. Vendor them
rather than depend on `bionym` — the repos will diverge.

| BioNym (`src/bionym/`) | bouncer analog | Notes |
|---|---|---|
| `jev.py` `JevClient` | `jev.py` unchanged | typed questions (choice/score/noul), `JevQuotaError`, diskcache, usage_log, `/v1/systemone`-compatible backends (Laya/open judges via `JEV_BASE_URL`) |
| `llm.py` `LlmClient` | `llm.py` | proposal/summarization model; add `temperature: float = 0.0` |
| `resolver.py` staged pipeline | `auditor.py` | same stage-orchestration pattern: gather evidence → build compact `state` → batch JEV ask → materialize nodes/edges |
| `questions/` per-stage modules | `questions/` per-stage | `build_state` / `build_questions` split, questions always Python-templated upstream; JEV judges evidence, never enumerates |
| `graph.py` `KnowledgeGraph` | `auditrecord.py` | schema + mechanics reused (node/edge/evidence/confidence); container reimplemented thin — see §4 representation decision |
| `evidence.py` `Evidence` | unchanged | source / endpoint / retrieved_at / summary / payload |
| two-band confidence contract | same | `1.0` = API-asserted or deterministic check; `<1.0` = JEV-judged (`jev_question_id` recorded) |
| `cli.py` + `report.py` | same roles | `reprobouncer audit <input>`, `reprobouncer audit --draft <file> --manifest <yml>` |
| `evals/` replay harness | `evals/` | diskcache-backed call capture, refill-and-resume, `--mock-jev` offline path |
| refusal discipline | same | intake refuses inputs it cannot confidently map to an auditable document |

Non-obvious lessons to carry over:

- **Never swallow `JevQuotaError`** — degrade-by-skip fabricates wrong
  answers and burns remaining budget. Propagate; eval runners treat it as
  stop-and-checkpoint.
- **Slice `usage_log` per audit call**, not cumulative across reused clients.
- **Isolate `cache_dir` per replicate** in any multi-run evaluation.
- **Record harness/model versions in every verdict** (see §7 — agent
  capability is a moving baseline).

## 3. Pipeline (stage architecture)

```
A0 intake      — classify input kind; published vs draft; refuse non-auditable
A1 document    — acquire + parse full text into structured sections
A2 audit graph — LLM-propose analysis units; JEV verifies each extraction
A3 artifacts   — resolve declared/implied data & code to concrete objects
A4 binding     — is each analysis-unit slot bound to a unique instance?
A5 static audit— deterministic + judged checks, no execution
A6 reproduce   — K Orbit agent runs attempt re-execution (full mode)
A7 score+report— per-axis scores, suggestions, PASS/FAIL verdict
```

### A0 — intake/classify

- Input forms: `doi:...`, bare DOI, PMID/PMCID, arXiv/bioRxiv id, URL,
  local file + `--draft`.
- Deterministic regex hints first (same style as `s0_classify`); JEV
  `choice` question only when ambiguous.
- Refusals: non-paper URLs, unsupported formats, encrypted PDFs. Better to
  refuse than silently audit garbage — same discipline as BioNym's S0/S1.

### A1 — document acquisition & parsing

- **Published**: DOI → Crossref metadata → Unpaywall for OA status →
  source priority: version-of-record > PMC OA > preprint. Record which
  was used — a revised preprint may not match the published text.
  **Supplementary files**: fetch via PMC supplementary-material links
  first, publisher page second; unreachable supplements are recorded
  as a finding — methods and parameters frequently live there and
  their absence limits A2/A4 completeness.
- **Draft**: local file. DOCX via pandoc; PDF/TeX via GROBID (structured
  XML with section/citation markup). Fallback when GROBID is
  unavailable or fails: PyMuPDF text extraction + section-heading
  heuristics — downstream stages must tolerate coarser section
  tagging (DAS/CAS detection degrades to a keyword scan).
- Emit normalized `Document`: sections (intro/methods/results), captions,
  Data/Code Availability Statements (DAS/CAS) located and isolated,
  references, inline statistical reports (test name, statistic, df, p).

### A2 — analysis-unit extraction

LLM proposes; JEV verifies. The key structural move: decompose the paper
into **analysis units** — each is a (figure/table/claim → pipeline) pair:

```
AnalysisUnit
  ├── outputs     : Fig 2B, Table 1, "HR=0.62 (95% CI 0.41–0.93)"
  ├── inputs      : dataset(s), samples, filters/exclusions
  ├── methods     : named test/model, software + version, parameters, seed
  └── claims      : the conclusions drawn from the output
```

- LLM proposes units + fills slots; every extracted element is re-judged
  by JEV against verbatim source passages — the `_verbatim_quote`
  discipline from BioNym proposals, which is citation-by-number, not
  quote generation: the source is presented as **numbered citable
  units** (sentences for prose — split with a regex that keeps
  initials/`e.g.`/binomials together — lines for tables and
  serialized records), the generator emits citation *numbers*, and
  the quote is assembled verbatim from the cited units. It can
  select the wrong units, but it can never paraphrase, splice, or
  fabricate text — a freeform `quote` field is rejected as
  unverifiable. The judge sees the assembled text plus its unit
  span, not the model's paraphrase.
- Papers exceeding model context: **section-wise map+merge** — extract
  per section, merge unit sets with dedup; never truncate methods.
- Generator marks `headline` units (the paper's key claims); JEV
  confirms. A6 attempts only headline units, capped at 3 per audit —
  reproduction budget shouldn't chase every figure.
- Unextractable/underspecified slots are *kept as explicit nulls*, not
  filled from the model's prior — a null slot is itself a finding
  ("methods omit the multiple-testing correction").
- Every slot value carries a **provenance label** (borrowed from the
  codeathon metadata skills — see Decision 7): `reported` (stated in
  text/supplement), `observed` (read from the artifact itself),
  `inferred` (derived, e.g. from filenames — never presented as
  confirmed), `confirmed` (two independent sources agree), or
  `conflicting` (sources disagree — record both). Every unknown is
  typed `not_yet_checked` / `cannot_determine` /
  `needs_human_judgement` — the last class is never resolved by the
  model (see the A6 skeptic policy).

### A3 — artifact resolution

Resolve every declared or implied artifact to a concrete, probed object.
All resolution outcomes are **deterministic → confidence 1.0** edges.
Existence is necessary but not sufficient — whether the paper's
reference *identifies* the right instance is A4's job.

- **Data**: parse DAS + accession-pattern regex over full text (GSE/SRP/
  PRJNA/PXD/E-MTAB/Zenodo DOI/OSF/Dataverse DOI/…). Probe each via repo
  APIs — resolves? public? file count/size vs. what the paper implies?
  license? embargoed?
- **Code**: GitHub/GitLab/Zenodo-software-DOI/Code Ocean/institutional
  GitLab. Probe: reachable? archived? license? tagged release matching
  the paper's version claim? README/run instructions? dependency files?
- **Identifier routing**: DataCite + identifiers.org for DOI/curi
  routing; per-repo API clients for depth.
- **Mutability risk flagging**: "available on request", supplementary
  files on publisher sites, personal GitHub (unarchived) vs Zenodo DOI —
  ranked by durability, not just presence. (Silent-divergence risk is
  real: a versionless URL can change under a "reproducible" paper.)
- **Cross-reference both directions**: paper → declared accessions →
  resolve, *and* accession → full record → did the paper use the whole
  study? A BioProject often contains more samples/runs than the
  analysis used, and one GSM may map to multiple SRRs (technical
  replicates — not biological samples). Record
  `provided_subset_of_larger_study` + `missing_records` rather than
  assuming the declared set is complete.
- **Availability is a class, not a boolean**: `public` |
  `controlled-access` (dbGaP/EGA — exists, but credential-gated) |
  `on-request` | `absent`. Each has different audit semantics:
  controlled-access and on-request mean an agent cannot fetch the
  data in `--full` at all — a predictable `M1-blocked:access` stall
  class, recorded distinctly from `M1-failed:not-found`, and a
  different suggestion ("deposit at a repository with an access
  mechanism the field can use" vs. "data is simply not public").
  `absent` is a blocking `artifact_access` finding on its own.

New client modules (`clients/`): `crossref.py`, `unpaywall.py`,
`europepmc.py`, `biorxiv.py`, `datacite.py`, `zenodo.py`, `dryad.py`,
`figshare.py`, `osf.py`, `dataverse.py`, `github.py` (+gitlab),
`geo.py`, `sra.py`/`ena.py`, plus a `registry.py` resolver that maps an
identifier string → the right client. Repo coverage is incremental —
start with the N repositories that dominate the target domain.

### A4 — specification binding

**Existence ≠ identification.** For every input/method slot of every
analysis unit from A2, ask: does the paper's reference pick out *one*
concrete value? A slot pointing at an existing-but-ambiguous artifact is
a distinct failure class from a missing artifact — this is the stage
where "the devil in the details" gets audited, because the details are
usually *metadata about artifacts*, not artifacts themselves.

- **Slots are open-world; checks are layered, not enumerated per
  artifact.** A2 proposes whatever slots *this* analysis consumes —
  no artifact-type enumeration in advance. A4 assigns each a coarse
  `slot_type` (`versioned_identifier`, `dataset`, `software`,
  `parameter`, `sample_grouping`, `reference_genome`, `other`), then
  checks in three layers:
  - *generic invariants* — format-agnostic tests on the slot record:
    `version` field non-null and parseable as *some* version shape
    (semver / calver / commit-digest / release-tag), `declared_value`
    non-empty, single bound instance. No per-format parsers — "we
    used DESeq2" fails here regardless of where DESeq2 lives.
  - *registry-mediated probes* — reuse A3's identifier→client
    dispatch table: each registered family carries a `version_pattern`
    plus a uniqueness/existence probe (does this GCA resolve to
    exactly one assembly? does the git ref name a commit, not a
    floating branch? does `deseq2 1.34` exist on Bioconductor?). The
    enumeration burden is the same incremental registry A3 already
    maintains — not a second list.
  - *judged fallback* — slots matching neither (unrecognized ID
    families already failed A3 resolution anyway) get JEV-judged
    binding with enumerated candidate sets — degraded, never skipped.
  The deterministic-vs-judged check fraction is reported per audit
  (`binding_check_coverage`) so coverage is measured, not assumed.
- **Slot-binding checks** (deterministic where possible, judged where
  semantics matter):
  - identifier carries a version/release — `GCA_000001405.15` vs. bare
    `GCA_000001405`; versioned DOI vs. concept DOI; repo tag/commit vs.
    floating branch; `deseq2=1.34` vs. "we used DESeq2"
  - resolved instance is *unique* — e.g. "GRCh38" without patch level:
    enumerate the candidate releases, flag with the candidate count
  - **sample-level bindings**: fetch the repo's sample metadata (SRA
    run table, GEO sample attributes); the paper's grouping variables
    (DESeq formula terms, batch labels, exclusion criteria) must map
    onto actual metadata columns. Column existence is deterministic;
    whether a column's semantics match the paper's group names is a
    JEV `noul`/`choice` question with the column values in evidence
  - **reconstruct before declaring unbound**: raw reads public but
    sample metadata thin/absent is a common distinct failure class —
    "data public, design not reconstructible". When needed columns
    are missing, first attempt reconstruction from secondary evidence
    (filenames, BioSample attributes, supplementary tables, FASTQ
    headers — the metadata-construction skill's workflow), labeling
    each recovered value `observed`/`inferred` per the A2 provenance
    labels and never silently promoting it. What survives
    un-reconstructed is the true `cannot_determine`/
    `needs_human_judgement` set that becomes `clarification_request`s
    in A6
  - parameters/software: stated values vs. silently-assumed defaults;
    version pinned?
- **Ambiguity is an enumerated finding, not a yes/no**: each unbound or
  multiply-bound slot records its candidate set ("GRCh38 primary vs.
  complete vs. T2T, 14 patch releases") so the suggestion can say
  exactly what to pin.
- **Feeds downstream twice**: (a) findings → suggestions and the
  `input_binding` axis; (b) predicted reproduction blockers — A6 stall
  forensics maps actual run failures back to these findings, validating
  the binding audit empirically.

### A5 — static audit (no execution)

Two flavors of checks per axis (§6):

- **Deterministic** (`1.0`): accession resolves, repo reachable, license
  detected, dep file present, checksum/size vs. declared. `statcheck`
  scope = APA-style inline reports (t, F, r, χ² statistic + df →
  recomputed p); GRIM/SPRITE on integer-rounded means/SDs where the
  sample sizes allow.
- **Judged** (`<1.0` typed score/choice/noul questions over evidence
  bundles): methods completeness (are all pipeline parameters recoverable
  from text+supplement?), test–design match (paired test on independent
  groups? parametric on clearly non-normal tiny-n?), robustness
  reporting (sensitivity analyses, seeds, exclusion
  criteria pre-specification). (Reporting-standard compliance is
  deferred — Decision 12; when added, the checklist is
  user-specified, never auto-selected.)

### A6 — agentic reproduction harness

**Only in `--full` mode**; this is what makes PASS/FAIL meaningful.

- **Pre-flight gate**: runs only start when (a) every required input
  artifact resolved with availability `public` — `controlled-access`,
  `on-request`, and `absent` inputs mean runs can only stall at M1 —
  and (b) no *blocking* clarification questions are outstanding
  (`cannot_determine`/`needs_human_judgement` on slots the analysis
  consumes; informational questions don't gate). Burning K Orbit
  runs to rediscover a known stall is waste. A failed gate is a
  *predicted* stall, not silence: the report records which criterion
  failed and the expected stall milestone, and gate-blocked runs
  count as non-passing in τ. `--force-run` overrides the gate — it
  exists so the calibration corpus can validate predicted-vs-observed
  stalls empirically (the stall-forensics feedback loop). In draft
  mode the gate is the fix list's enforcement point: answer the
  blocking questions, gate clears, audit proceeds.
- **Execution backend = Orbit** (optional dependency): after the gate
  clears, each of K runs is an `orbit` CLI invocation; Orbit owns
  execution semantics — sandboxing, environment construction, tool
  selection via UDTs, and whether tools run locally or via Galaxy is
  Orbit configuration (point users at Orbit docs; it is not our
  surface). This removes the container-runtime and analysis-language
  scoping questions entirely — Orbit figures out execution.
  No local Orbit install → `--full` refuses with an install pointer.
  Orbit output parsing is pinned to a declared set of Orbit versions
  via a compat table — formats change, so supported-version pinning is
  explicit and new versions get validated before being added.
  The arbitrary-code-execution risk doesn't disappear — it moves to
  Orbit's execution/config; document the expectation that users run
  Orbit in a suitably isolated context.
- **Orbit mechanics** (from the galaxyproject/orbit README): Orbit is a
  pi-based interactive agent — `notebook.md` in the run workdir is
  the authoritative run state (plan-step checkboxes +
  `orbit-invocation` YAML blocks with status + `activity.jsonl`
  decisions), which is what our milestone mapping and stall forensics
  parse. Run input = the workdir itself plus an `ORBIT.md`
  standing-instructions file carrying the task and the
  resolved-artifact bundle; draft-mode local files go in the workdir
  (the workspace jail confines writes there). Per-run model comes
  from Orbit's provider config — OpenAI-compatible endpoints are
  supported, so OpenRouter serves the heterogeneous ensemble. Orbit's
  approval prompts (plan, parameters, exec-guard) auto-deny in
  headless mode; fully unattended runs need
  `--dangerously-bypass-permissions` plus task-level instruction to
  proceed without plan approval (the approval gate is prompt-level,
  not a runtime lock). Cost: per-run spend is readable from the pi
  session's `usage.cost` under `~/.pi/agent/sessions/`.
- **Invocation** (resolved — proven in bionym `evals/eval_orbit_grounded.py`):
  `node bin/orbit.js --print --session-dir <dir> "<prompt>"` with
  cwd = the run workdir, an isolated `HOME` (controls which Galaxy
  profile/creds the run sees — absent → local routing), and the run
  model's key injected as `ORBIT_ACTIVE_LLM_API_KEY`. `bin/orbit.js`
  also notes a structured event output for evals — verify the exact
  flag at integration; it beats scraping the TUI for telemetry.
  Initial compat-table pins: `@galaxyproject/orbit` **0.9.x**
  (npm; checkout d06b334d), `pi-coding-agent` **1.4.x**, node
  **>=22.19**. Install detection for `--full`: `orbit` resolvable
  on PATH (or `ORBIT_BIN` env override) and `orbit --version`
  parses to a compat-table entry — missing or unparseable →
  refuse with an install pointer.
- **Run-input + workdir discipline** (lessons from bionym
  `eval_orbit_grounded.py`): run workdirs live *outside* the repo
  and any IDE-watched path — ~300 session dirs under a workspace
  root once exhausted the 65k inotify limit and Orbit's chokidar
  watcher died ENOSPC mid-run (`ORBIT_RUNS_DIR`-style override,
  default e.g. `~/.cache/reprobouncer_runs`). Inputs are staged as
  **line-oriented files** (document units as `units.jsonl`,
  artifacts as `manifest.jsonl`) — monolithic JSON gets inhaled
  whole into the agent's context; the task prompt tells the agent
  to grep them. The prompt must also warn that headless
  exec-guard blocks pipes and `>` redirects. A terminal marker
  convention (`REPRO_RESULT:` line) makes `--print` stdout
  parseable the way the eval's `ANSWER:` did. Node is pinned via
  a `NODE_BIN` env prepend; starting wall-clock budget 20 min/run.
  Post-run, the session-JSONL tool-call scan the eval's `--audit`
  mode uses is our isolation check *and* forensics input.
- **Budget + instrumentation**: step/time/token budgets live in Orbit
  config where Orbit supports them; our harness records K run
  outcomes, wall time, and whatever telemetry Orbit surfaces.
  Milestone mapping and stall forensics parse Orbit's run
  status/logs — the piece most coupled to Orbit's output format
  (see open questions).
- **Milestones** (graded partial credit under the binary verdict):

  | Milestone | Definition |
  |---|---|
  | `M1 artifacts` | data + code obtained matching declared manifests — gated/on-request access records `M1-blocked:access` |
  | `M2 environment` | runnable env constructed (deps resolved) |
  | `M3 execution` | pipeline runs to completion, outputs produced |
  | `M4 concordance` | headline results reproduced within tolerance |

- **Success criteria for `M4`**: extracted expected values from A2 vs.
  produced outputs. Each expected value carries a `result_key`
  (figure/table/statistic id — `fig2b.hr`, `table1.n`); produced
  outputs are matched by key, not by position — a value with no
  produced counterpart records `M4-unchecked` for that unit, not a
  mismatch. Numeric concordance uses a tolerance table per expected
  value (`rel` default 0.05, `abs` default 0), overridable per-unit
  in the schema and globally via `--tolerance`/config; qualitative
  match (e.g. figure shape, sign of effect) judged by JEV with both
  artifacts in evidence. "Meaningful results" = M4, or M3 +
  judged-partial if the paper's own reporting is too vague to check
  (recorded as `M4-unchecked`, a distinct outcome — a paper that
  can't be checked isn't a pass).
- **Agent inputs**: the agent sees the *whole* paper (full text +
  supplement + resolved artifacts) — the same evidence bundle a human
  reproducer would work from. PASS/FAIL certifies the paper as a whole.
- **Skeptic policy** — vendored/adapted from the codeathon
  `sample-metadata-galaxy` skills (`analysis-readiness`,
  `metadata-construction`; same author as this project, so the text
  can be vendored verbatim where useful):
  the agent *never guesses to fill a gap*. Unknowns are typed
  `not_yet_checked` (go check), `cannot_determine` (evidence
  exhausted), or `needs_human_judgement` (a biological/experimental
  call — which condition is the control, whether replicates are
  biological, whether a batch effect is acceptable). On the last two
  the agent emits a structured `clarification_request` — the slot,
  the candidate interpretations, and what would resolve it — instead
  of silently picking one. Inconclusive checks stay inconclusive; no
  rounding toward the convenient answer. And **no circular
  validation**: an ID regenerated by the same formula that produced
  it tests nothing — validate against independent records; the
  misses are the finding.
- **Clarification instrumentation**: every `clarification_request` is
  logged with its stall point and candidate set, and mapped back to
  the A4/A5 finding it instantiates (same feedback loop as stall
  forensics). **The question list is always emitted as a report
  artifact, in both modes** — in published mode it is the concrete
  list of questions a reproducer would have to ask the authors (the
  paper's spec gaps made legible, useful even for runs that pass);
  in draft mode it is answerable by the user (`unresolved_questions_
  for_user`, §5). In published mode there is no answerer, so each
  blocking request is an *unanswered question* and the run cannot
  satisfy "without human intervention". Per-run question counts feed
  the `self_sufficiency` axis (§6) and the verdict gate (§7). The
  metric only works under the no-guessing policy — an agent that
  asks nothing but guesses silently is the worst case, not the best.
  (How a mid-run clarification request is *expressed* depends on
  Orbit's agent interface — see "Orbit mechanics" below: headless
  auto-denies approval prompts, so in published mode a blocking
  question becomes a denied prompt → stall → forensics; in draft
  mode the run can stay interactive and the user is the answerer.)
- **K independent runs**: each run = one fresh Orbit invocation in a
  per-run workdir attempting the *whole* headline unit set — the
  verdict is on the paper, not per-unit. K=5 default; runs are
  **sequential** — `--parallel N` (≤K) is opt-in for users whose
  Orbit config can sustain it. **Heterogeneous model ensemble
  preferred** (e.g. 2–3 generator families) so the verdict measures
  the paper's clarity, not one model's quirks — but a **homogeneous
  K×1 ensemble is a supported fallback**, since many users will only
  have credentials for one model.
  Verdict metadata records the ensemble composition so a same-model
  verdict is legible as such. The §8 model-benchmarking suite exists to
  say which models are fit for the harness role at all.
- **Reproduction bundle** per run: env digest, artifact checksums, full
  command/action log, outputs, diff-vs-reported. Persisted — the bundle
  is itself auditable evidence and feeds evals replay.
- **Stall forensics**: when a run stalls or fails, the stall point and
  proximate cause are logged and mapped back to A4 binding findings.
  This both explains the failure in the report and empirically
  validates the binding audit — a flagged ambiguity that actually
  stalls runs is calibrated signal; one agents routinely route around
  is noise to retune.
- **Why Orbit**: the in-sandbox agent loop delegates to Orbit; what
  stays ours is the orchestration layer — gate, K-run scheduling,
  milestone mapping, stall forensics, verdict accounting, evals
  replay. The LLM↔JEV tuning surface that motivated a custom loop
  doesn't disappear — it applies to *our* stages (A2/A4 extraction,
  A7 scoring) and to whatever hooks Orbit exposes; a custom
  `agent.py` loop remains the fallback if Orbit proves unfit.
  Framed longer-term: this experiment doubles as a prototype for a
  possible Orbit-native reproduction-audit feature — the milestone
  taxonomy, stall forensics, and gate semantics are the parts a
  future Orbit feature would absorb.

### A7 — scoring & report

- Per-axis `score` questions over assembled evidence bundles, with rubric
  levels in `criteria` (JEV score type already supports this).
- **Verdict**: `PASS` iff ≥ τ of K runs reach `M4`; τ calibrated (§8),
  initial working value 3/5. Document-mode-only audits (no `--full`, or
  draft without runnable artifacts) emit `READINESS` verdicts instead of
  PASS/FAIL, so a cheap scan never masquerades as a reproduction verdict.
- Report: JSON (machine) + HTML (human). Every score links to its
  evidence; every suggestion cites the check that produced it.

## 4. Audit-graph model

Same mechanics as `KnowledgeGraph` (merge, evidence union, confidence
max), domain-typed nodes:

```python
class NodeType(str, Enum):
    PUBLICATION = "Publication"
    MANUSCRIPT = "Manuscript"
    ANALYSIS_UNIT = "AnalysisUnit"
    DATASET = "Dataset"          # resolved artifact w/ repo attrs
    CODE_ARTIFACT = "CodeArtifact"
    SOFTWARE = "Software"        # name+version extracted
    METHOD = "Method"
    RESULT = "Result"            # reported number/figure
    CLAIM = "Claim"
    CHECK = "Check"              # an audit check + its finding
    REPRO_RUN = "ReproRun"       # an agent reproduction attempt
    SUGGESTION = "Suggestion"
```

Predicates: `produced_by`, `uses_dataset`, `implemented_in`,
`declares_param`, `resolves_to`, `binds_to` (slot→unique instance),
`ambiguously_binds` (slot→candidate set), `requires_clarification`
(run→slot/finding — a logged `clarification_request`), `fails_check`,
`reproduces` (run→result), `suggests`. Every `CHECK`/`SUGGESTION` edge carries the evidence bundle
that triggered it — "why do we believe this" stays answerable.

Confidence semantics inherit BioNym's exactly, including the caveat that
confidence is *audit-finding confidence* (how sure we are the finding is
correct), not how good the paper is — a low axis score can sit at
confidence 1.0.

**Confidence composes.** A finding edge's confidence is the min over its
input edges and the mechanism that produced it — a deterministic check
on a model-extracted slot value inherits the extraction's JEV-scored
confidence, not `1.0`. "Deterministic" describes the *mechanism* (no
model call at check time), not the finding's certainty. Negative
findings ("no version declared") additionally depend on extraction
*completeness* — the generator may have missed a mention in the
supplement — so they carry an extraction-coverage caveat alongside the
composed confidence.

**Representation decision.** The *schema* is graph-shaped — typed
nodes, typed edges, evidence on every assertion — but the v0
*container* is thin: Pydantic records + typed refs in flat lists, not
a vendored `KnowledgeGraph`. Merge/dedup (`key(node)` lookups) and the
one-hop ref-following helpers are written explicitly (~30 lines).
Rationale: an audit is an evidence tree, not a knowledge network —
every access pattern is one-hop, and BioNym's merge/confidence-max
machinery was built for accumulating knowledge across entities, a
problem this tool mostly doesn't have. (It may even be wrong here:
conflicting extractions should surface `conflicting`, not silently
confidence-max.) **Promotion path**: the schema already *is* a graph,
so swapping the container for a real graph or a duckdb index over the
JSON artifacts is mechanical. What would warrant it: cross-audit
analysis becoming first-class — "which findings recur across a
corpus" — which only makes sense once a corpus exists, i.e. once the
experiment has proven out and been adopted somewhere that audits at
scale; or per-audit graphs outgrowing flat lists + dict lookups.

### Who does what — deterministic vs JEV vs generator

The BioNym pattern holds throughout: the **generator proposes**
(expensive, few calls), **JEV filters/scores/verifies** (cheap typed
questions, many), and **deterministic code handles whatever needs no
model at all** — deterministic mechanisms contribute confidence `1.0`,
but the resulting edge inherits the min over its inputs (see Confidence
semantics above).

| Stage | Deterministic | JEV | Generator LLM |
|---|---|---|---|
| A0 intake | identifier regex, URL/mode classification | — | — |
| A1 document | section parsing, statcheck recompute, table extraction | span validation (`noul`: does this span support the claim?) | structure recovery on malformed PDFs/supplements (fallback only) |
| A2 extraction | — | scores candidate units, assigns provenance label per slot (`choice`) | proposes analysis units, fills slots |
| A3 resolution | accession resolution, manifest fetch, availability class, checksum, license detect | — | accession/URL extraction from prose where regex fails |
| A4 binding | generic slot invariants + registry-mediated probes (version parse, column existence, ontology lookup) | `noul`/`choice`: column↔group-name semantics, candidate-instance selection, binding for unregistered families | proposes bindings, assigns slot_type, drafts reconstruction values from secondary sources |
| A5 static audit | license/dep checks, statcheck/GRIM, repo reachability | methods completeness, test–design match, checklist compliance | assembles rubric-specific evidence bundles for JEV to judge |
| A6 harness | pre-flight gate, K-run orchestration, milestone mapping, logging | qualitative M4 checks (figure shape, effect sign), stall-forensics classification | the reproduction agent (Orbit's loop — external; custom `agent.py` is the fallback) |
| A7 scoring | axis aggregation, verdict rule, milestone histogram | validates quoted evidence in report | report narrative + suggestion wording |

## 5. Draft-manuscript specifics

- Optional `--manifest draft.yml`: `data:` / `code:` entries may be local
  paths, URLs, or accessions; `environment:` hints; `headline_results:`.
- **Local data stays local.** Only schema-level summaries (file names,
  formats, row/col counts, column types) enter JEV/LLM state — never raw
  data. Document this prominently; it's a trust boundary.
- **Self-reproduction** (`--full` with local artifacts): the harness runs
  the user's own pipeline through Orbit in a clean execution environment —
  catches "works-on-my-machine" before reviewers do.
- Verdict becomes readiness-focused: predicted pass probability +
  blocking findings (missing accession, unwritten DAS, unpinned deps),
  plus a generated DAS/CAS draft as a suggestion artifact. Status
  names borrow the codeathon readiness record: `ready | blocked |
  ready_with_warnings`.
- **Clarification is the fix list**: the always-emitted question
  list (§3/A6) is answerable here — each user answer upgrades the
  slot's provenance to `confirmed_by_user` and unblocks a re-run.
  This is the mode where the question metric becomes a
  collaboration instrument rather than a gate — answer N questions,
  get a reproducible manuscript.

## 6. Audit axes (v0 taxonomy)

| Axis | What it measures | Mostly |
|---|---|---|
| `artifact_access` | data/code present, resolvable, public, licensed, durable | deterministic |
| `input_binding` | every analysis-unit input bound to a unique versioned instance — assembly release, sample→group metadata map, tool version | deterministic + judged |
| `sample_metadata` | does sample-level metadata exist, how complete is it, how much had to be reconstructed (provenance mix), are field values unambiguous, and do they attach to formal ontologies (NCBITaxon/EFO/MIxS) | deterministic + judged |
| `methods_placement` | critical analysis details buried in main text/supplement that belong in Methods — **warning, not score-blocking** | judged |
| `executability` | env specified, deps pinned, entry point exists | deterministic + run |
| `methods_completeness` | pipeline reconstructable from text alone | judged |
| `statistical_rigor` | test–design match, multiplicity, effect sizes, power | judged + statcheck |
| `robustness` | sensitivity analyses, seeds, stochasticity handling | judged + run variance |
| `reporting_compliance` | domain checklist adherence | judged |
| `self_sufficiency` | how much the agent must ask to proceed — clarification requests per run, weighted by blocking unknown type | run |
| `result_verifiability` | recomputed/reproduced values match reported | run + deterministic |

Axes are **named, versioned, and additive** — v0 ships
`artifact_access`, `input_binding`, `sample_metadata`,
`methods_placement`, `executability`, plus `result_verifiability`;
`self_sufficiency` lands with the
harness in M2. `statistical_rigor`/`robustness` rubrics grow with
calibration data; `reporting_compliance` is deferred — when it
lands, the checklist is user-specified (`--checklist`), not an
auto-selected domain default.

**How axes relate to the verdict**: an axis *score* never fails a
paper directly — the gate is finding *severity*. A `blocking`
finding enters the question/gate machinery: it emits a blocking
`clarification_request` when an answer could resolve it, or gates
A6 directly when it can't (e.g. `absent` data). The
question-generation step sees the complete A4/A5 finding set plus
slot unknowns — so "blocking" is defined by what it produces:
blocking ⇒ question or gate-block ⇒ `gate-blocked` FAIL flavor.
`warning`/`informational` findings only lower axis scores.

`sample_metadata` scoring detail (all evidence already gathered by
A3/A4 — no new fetches):

- **presence/completeness**: fraction of samples with values for every
  field the analysis actually consumes (group/condition, batch,
  replicate structure, organism/strain, assay-relevant fields); empty,
  placeholder, and defaulted values count as missing
- **provenance mix**: existed in-repo (`reported`/`observed`) vs. had
  to be reconstructed (`inferred`) vs. `confirmed` against a second
  source — reconstruction-heavy metadata scores lower but still beats
  absent
- **unambiguity**: can a field's values be interpreted without
  biological judgment calls — "water" and "DMSO" as distinct control
  candidates is unambiguous *data*; "against control" in the paper
  with two control-like values is an ambiguous *mapping* (that part
  is `input_binding`/`clarification_burden`'s job — this axis measures
  the metadata itself)
- **ontology attachment**: values resolvable to controlled-vocabulary
  terms (NCBITaxon for organism, EFO/CL for cell type, UBERON tissue,
  MIxS checklist fields) — deterministic via OLS/ontology lookup, vs.
  free-text-only fields that a reproducer must re-interpret

## 7. Verdict semantics & caveats

- `PASS` = ≥ τ of K independent agent runs reach `M4` within budget.
  Formalized, but it measures **agentic reproducibility specifically** —
  it conflates paper quality with harness capability. Mitigations:
  pin harness+model+Orbit versions; stamp every verdict (`reprobouncer
  0.3.2, orbit 1.4, harness minimax-m3 @ 2026-11-14`); re-run the
  calibration corpus on every harness or Orbit-compat bump; report
  milestone histogram, not just the binary.
- `FAIL` via the A6 gate is a distinct flavor: `gate-blocked:<criterion>`
  is recorded with the predicted stall milestone, separating
  "reproduction attempted and observed failing" from "reproduction
  provably can't start" — the latter is usually author-fixable in ways
  the former isn't.
- `M4-unchecked` runs are excluded from the τ denominator entirely;
  if more than K/2 of the K runs are unchecked the verdict is
  `INDETERMINATE`, not a coin flip.
- `--max-cost` is an opt-in hard cap on the audit's JEV+LLM spend;
  without it, spend warns at a config threshold but proceeds.
- A run that issues a blocking `clarification_request` in published
  mode cannot count toward τ — PASS certifies "without human
  intervention", and a needed judgment call is an intervention by
  definition. `clarification_burden` (count + unknown-type mix per
  run) is reported alongside the milestone histogram: a paper that
  passes only when agents guess is a FAIL wearing a PASS costume, and
  question volume is how we tell the difference.
- Budget sensitivity must be tested in evals — PASS at 2× the step
  budget that FAILs at 1× is a budget artifact, not a paper property.

## 8. Calibration & evaluation plan

The PASS/FAIL mark is meaningless until calibrated against papers with
known reproduction outcomes.

- **Ground-truth sources** (verify availability before committing):
  CODECHECK register (papers with confirmed code-execution outcomes),
  ReproHack archives, *Reproducibility Project: Cancer Biology*,
  **PaperBench** (OpenAI's agentic ML-paper replication benchmark with
  rubric trees — closest prior art to our M-milestones), **CORE-Bench**
  (computational reproducibility agent benchmark). Both benchmarks also
  offer runnable tasks that can stress-test the harness itself.
- **Fixture corpus** (dev/test papers): first two = the codeathon case
  studies — **Romano et al., PNAS 2024** (PMC11551328 / GSE251671;
  'livny', ground truth known) and **Cianciulli Sesso et al., Front.
  Microbiol. 2021** (10.3389/fmicb.2021.626715; 'blasi', no ground
  truth) — same organism/context, opposite knowledge conditions.
  Cached client responses per fixture so M0/M1 develop offline.
- **Calibration suite**: sweep τ and milestone weighting on the
  ground-truth set; maximize agreement with known labels; ECE on
  sub-1.0 judged checks vs. a small human-audit set (the BioNym
  50-edge-audit pattern).
- **Adversarial set**: papers that *look* complete but fail (mutable
  artifacts, underspecified parameters, dead links), papers that look
  sparse but pass (a one-file script with clean deps), wrong-entity
  artifacts (similar accession, different study).
- **Harness model benchmark** (run *before* calibration, like BioNym's
  §3.1 judge replay serving both as deployment guidance and stack-
  selection rationale): replay a fixed reproduction task set across
  candidate harness models — milestone attainment, tool-call efficiency,
  budget burn, failure modes (infinite loops, credential fishing,
  fabrication). Output: a documented model tier list (fit-for-harness /
  marginal / avoid) + the production ensemble default. Homogeneous
  ensembles are benchmarked too, so the fallback's reliability is
  quantified rather than assumed.
- **Cross-run stability**: K-run variance report; harness determinism at
  `temperature=0` vs `0.7` (same replicate-isolation rules as BioNym).
- **Cost/time accounting**: per-audit JEV+LLM token usage and wall time
  per stage, recorded in graph metadata like `jev_usage`/`llm_usage`.

## 9. Proposed repo layout

```
src/reproducibility_bouncer/
  cli.py            — reprobouncer audit <input> [--draft] [--manifest] [--full] [--force-run] [-K N]
  auditor.py        — stage orchestration (the Resolver analog)
  auditrecord.py    — typed nodes/edges + evidence + confidence (graph-shaped schema, thin container; promote to real graph/duckdb if corpus analysis earns it)
  evidence.py       — vendored from bionym
  jev.py            — vendored from bionym
  llm.py            — vendored from bionym (+temperature)
  document.py       — acquisition + GROBID/pandoc parsing → Document
  manifest.py       — draft-mode manifest parsing + local-data introspection
  extractor.py      — analysis-unit proposal + slot extraction
  checks/           — deterministic checks (statcheck, license, dep-file, …)
  repro/
    harness.py      — K-run orchestration, gate, milestone detection
    orbit.py        — Orbit CLI wrapper + output parser + version-compat table
    compare.py      — output-vs-reported concordance
  questions/        — a0_intake … a7_score build_state/build_questions
  clients/          — crossref, unpaywall, europepmc, biorxiv, datacite,
                      zenodo, github, geo, sra, osf, dryad, figshare, registry
  report.py         — JSON + HTML
evals/              — calibration corpus, replay harness, human-audit sets
tests/              — fakes/mocks pattern from bionym tests
pyproject.toml
```

Runtime configuration (env vars):

- `TYPESAFE_API_KEY` (required), `JEV_BASE_URL`, `JEV_MODEL` — judge
- `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` — generator (dev default:
  OpenRouter + MiniMax-M2.7); per-run Orbit provider config consumes
  `ORBIT_ACTIVE_LLM_API_KEY` (see A6)
- `NCBI_API_KEY`, `GITHUB_TOKEN` — optional; raise rate limits
- `GALAXY_URL`, `GALAXY_API_KEY` — Orbit-side only; absent → local
  routing (Orbit config surface, not ours)
- `--max-cost` / config threshold — audit spend ceiling (§7)

## 10. Build order

- **M0 — scaffold**: vendored jev/llm/evidence, auditrecord, A0+A1 for
  published inputs, `--mock-jev` path. Ship a `document` subcommand that
  just dumps the parsed Document — useful immediately.
- **M1 — static audit** (shippable v0.1): A2 extraction, A3 for the top
  ~6 repos (GitHub, Zenodo, GEO, SRA, OSF, Dryad), A4 binding + A5
  checks, A7 READINESS report. No execution yet — still valuable.
- **M2 — harness**: A6 gate + Orbit backend (optional dep, pinned
  versions), K runs, milestone mapping, stall forensics → first
  PASS/FAIL. No custom agent loop unless Orbit proves unfit — the
  experiment doubles as a prototype for an Orbit-native
  reproduction feature.
- **M3 — draft mode**: manifest, local-data introspection,
  self-reproduction, DAS generation.
- **M4 — calibration**: ground-truth corpus, τ fitting, ECE, stability
  sweeps. Only after this does the verdict mean what it claims.
- **M5+ — distribution**: Galaxy tool / hosted service / browser report —
  same downstream shape as BioNym's BRC integration, decide later.

## 11. Decisions (2026-11-14)

1. **Domain**: computational pathogen genomics first. During build,
   keep a `docs/domain-specificity.md` ledger logging which components
   ended up domain-specific vs. accidentally generic — the honest
   genericity boundary is an empirical finding of the project, not a
   guess. Repo clients and checklists are expected to be the domain-
   coupled parts; extraction/judging/harness should not be.
2. **Harness**: custom orchestration layer (gate, K runs, milestone
   mapping, stall forensics) — chosen for the LLM↔JEV interaction
   surface and budget/telemetry control (§3/A6). The in-sandbox
   agent loop itself delegates to Orbit (Decision 10).
3. **"Most agents"**: heterogeneous ensemble preferred; homogeneous
   K×one-model supported fallback (users may only hold one set of
   credentials). Model-benchmark suite (§8) defines fitness tiers.
4. **Agent visibility**: whole paper + supplement + artifacts; PASS/FAIL
   is on the whole paper. `methods_placement` check emits a warning
   when critical info lives outside the Methods section.
5. **Name/home/license**: `reproducibility-bouncer`,
   github.com/d-callan, MIT. CLI `reprobouncer`.
6. **Specification binding stage** (added 2026-11-14): artifact
   *existence* and *identifiability* are separate questions — new stage
   A4 audits whether each analysis-unit slot pins a unique concrete
   instance (assembly release, sample→group metadata map, tool version),
   not merely whether a resolvable artifact exists somewhere. Harness
   stall forensics feed back to validate binding findings empirically.
   Availability is classified `public | controlled-access | on-request
   | absent` rather than boolean, and missing sample metadata triggers
   a reconstruction attempt from secondary sources before a slot is
   declared unbound.
7. **Skeptic-agent policy + clarification metric** (added 2026-11-14):
   agent behavior and unknown-typing adapted from the NIAID-BRC
   codeathon `sample-metadata-galaxy` skills
   (github.com/NIAID-BRC-Codeathons/sample-metadata-galaxy/tree/main/skills)
   — motivated by a real group reproduction attempt where the paper
   said "against control" but the sample metadata contained both water
   and DMSO controls; a skill-less agent guessed, a skilled one stopped
   and asked. Provenance labels land in A2, the no-guessing/typed-
   unknown policy in A6, and the count of clarification requests a run
   must issue becomes `clarification_burden`, feeding the
   `self_sufficiency` axis and gating the verdict. The question list
   is emitted as a report artifact in *both* modes — published mode
   gets "questions a reproducer would have to ask the authors",
   draft mode gets an answerable fix list. Licensing is a non-issue
   (skill author = project owner), so skill text may be vendored
   verbatim where useful.
8. **A6 pre-flight gate + explicit model-role split** (added 2026-11-14):
   the harness runs only when all required inputs are publicly
   fetchable and no blocking clarification questions are outstanding;
   gate failure is a predicted stall (recorded, non-passing), and
   `--force-run` exists for calibration validation. A per-stage table
   fixes deterministic/JEV/generator responsibilities, preserving the
   BioNym pattern: generator proposes, JEV filters, deterministic code
   does whatever needs no model.
9. **Graph-shaped schema, thin container** (added 2026-11-14): keep
   BioNym's typed nodes/edges/evidence/confidence schema but implement
   v0 as flat Pydantic records with explicit merge + ref-following,
   not a vendored KnowledgeGraph — audits are evidence trees with
   one-hop access patterns, and merge/confidence-max semantics were
   built for a problem this tool doesn't have. Promotion to a real
   graph or duckdb index is mechanical (schema already graph-shaped)
   and warranted when corpus-scale cross-audit analysis becomes
   first-class — contingent on the experiment proving out and gaining
   adoption at scale.
10. **Orbit as reproduction backend** (added 2026-11-14): A6 delegates
    in-sandbox execution to Orbit — K `orbit` CLI invocations after
    the pre-flight gate, outputs parsed into milestones/stalls via a
    pinned-version compat table. Orbit is an optional dependency:
    `--full` without a local Orbit install refuses with an install
    pointer. Sandbox/environment/tool-selection semantics (UDTs,
    Galaxy-vs-local tools) are Orbit config, not our surface — the
    Python-vs-R and container-runtime scoping questions drop out.
    The experiment doubles as a prototype for a potential
    Orbit-native reproduction feature — and upstream asks are
    viable since it's the same team (e.g. a dedicated
    headless/audit mode if `--print` proves limiting). A custom
    agent loop is the fallback if Orbit proves unfit.
11. **Model stack** (added 2026-11-14): JEV (TypeSafe) is the judge
    default, as in BioNym; the generator/proposal model is
    configurable and starts on OpenRouter for dev/testing — the §8
    model-fitness benchmark picks the production default.
    Homogeneous K×1 ensemble remains the supported fallback.
    Dev default: MiniMax-M2.7 — continuity with the BioNym eval
    stack and existing usage data. Benchmark candidates should
    include long-context strong-extraction models (Kimi K2,
    Qwen3, GLM); the extraction replay set scores citation
    fidelity (right unit selection) alongside proposal quality.
12. **Axes gate via severity, not score** (added 2026-11-14): axis
    scores are a scorecard — no numeric score fails a paper.
    Blocking findings emit clarification questions (or gate A6
    directly when unanswerable); the question-generation step sees
    the full finding set, and `gate-blocked` is the FAIL flavor.
    `warning`/`informational` findings only lower scores.
    `reporting_compliance` is deferred; when added, the checklist
    is user-specified (`--checklist`).

## 12. Open questions (as of 2026-11-14)

Remaining underspecified items — each blocks a specific build step;
working defaults noted where one exists.

- **Data-model schemas** — drafted in `specs/data-model.md`
  (Document, AnalysisUnit, Slot, Artifact, Finding,
  ClarificationRequest, ReproRun, Report). Open: review against the
  §3 stage descriptions before M0 — fields will shift on the first
  real audit, which is expected.
- **JEV question inventory** — drafted in `specs/jev-questions.md`
  (per-stage ids, types, templates, build_state). Open: calibrate
  template wording on the fixture corpus before trusting
  confidences.
- **Orbit residuals** — resolved: invocation surface (A6
  "Invocation" bullet), compat-table initial pins + install
  detection (0.9.x / pi 1.4.x / node ≥22.19), and upstream
  feature requests are viable (same team — see Decision 10).

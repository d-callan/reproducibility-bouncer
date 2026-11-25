# Data model — v0 record specs

Thin container (Decision 9): flat Pydantic records + typed refs in
`auditrecord.py`. Every record carries `id`, `record_type`,
`schema_version`. Refs are `{record_type}:{id}` strings. Evidence is
always refs into Document citable units or Artifact objects — never
freeform text.

Confidence contract (two-band): `1.0` = API-asserted or deterministic;
`<1.0` = JEV-judged (`jev_question_id` recorded). Composed confidence
= min(input confidences, mechanism). Negative findings carry an
extraction-coverage caveat (`coverage_caveat: true`).

## Evidence (vendored `evidence.py`, unchanged from BioNym)

- `source` (client/endpoint name), `endpoint` (URL/operation),
  `retrieved_at`, `summary`, `payload` — one per external API/
  repository call; `Artifact.resolved` and client-derived fields cite
  Evidence refs, not raw payloads. Document citable-unit refs cover
  *textual* evidence; `Evidence` covers *endpoint* evidence.

## Audit (container — `auditrecord.py`)

- `id`, `mode` (`published|draft`), `created`
- `document_id` → Document
- `units[]`, `slots[]`, `artifacts[]`, `evidence[]`, `findings[]`,
  `questions[]`, `runs[]` — flat lists, typed refs between them
- `meta`: {`reprobouncer`, `orbit`, model, `schema_version`}
- Merge is explicit (~30 lines): dedup by (type, natural key); on
  conflict keep both and mark `conflicting` — never silent max
  (Decision 9).

## Document (A1)

- `source`: {`doi`, `pmid`, `pmcid`, `arxiv_id`, `url`, `local_path`}
- `source_used`: `vor|pmc_oa|preprint|draft` + `retrieval_url`,
  `retrieved_at` — a preprint may not match the published text
- `title`, `authors[]`, `journal`, `year`
- `sections[]`: {`name` (`abstract|intro|methods|results|discussion|
  captions|das|cas|refs|supplement|other`), `units[]`}.
  Units are the numbered citable units (sentence granularity for
  prose, line granularity for tables/serialized records — the
  `_verbatim_quote` mechanism). Every downstream evidence ref is a
  `{section, unit_index}` pair into this structure.
- `inline_stats[]`: {`test`, `statistic`, `df`, `p_reported`,
  `p_recomputed`, `unit_ref`}
- `supplements[]`: {`filename`, `fetched`, `units[]|null`}; unfetched
  supplements are a finding, not silence
- `parse_path`: `grobid|fallback|pandoc`; `coverage_notes[]`

## AnalysisUnit (A2)

- `headline`: bool — generator marks, JEV confirms; ≤3 audited at A6
- `rank`: int — presentation order
- `outputs[]`: {`result_key` (`fig2b.hr`, `table1.n`), `kind`
  (`figure|table|scalar|other`), `label` ("Fig 2B"), `expected`
  {`value`, `ci`, `tolerance` {`rel`, `abs`} | null}, `evidence`
  (unit_refs)}
- `inputs[]`, `methods[]`: refs → Slot
- `claims[]`: {`text`, `evidence` (unit_refs)}
- `source_units`: unit_refs the generator cited per element

## Slot (created A2, bound A4)

- `unit_id` → AnalysisUnit
- `name`, `slot_type`: `versioned_identifier|dataset|software|
  parameter|sample_grouping|reference_genome|other`
- `declared_value`: str | null — as stated in the paper
- `bound_instance`: {`artifact_id`, `instance` (release/version/
  rowset)} | null — must be unique; ambiguity records `candidates[]`
- `candidates[]`: enumerated ambiguity set
- `provenance`: `reported|observed|inferred|confirmed|conflicting|
  confirmed_by_user`
- `unknown`: null | `not_yet_checked|cannot_determine|
  needs_human_judgement` — never guessed (skeptic policy)
- `check_path`: `generic_invariant|registry_probe|judged|unchecked` —
  feeds `binding_check_coverage`
- `evidence`: unit_refs (+ artifact_refs for `observed`)

## Artifact (A3)

- `kind`: `dataset|code|container|reference|other`
- `identifier`: {`family` (`accession|doi|url|path`), `raw`,
  `normalized`}
- `resolved`: {`repo`, `object_id`, `version`, `url`,
  `evidence` (Evidence refs)} | null
- `availability`: `public|controlled-access|on-request|absent` —
  non-`public` on required inputs gate-blocks A6
- `declared` vs `observed`: {`checksum`, `size`} — mismatch = finding
- `license`: str | null
- `mutability_flags[]` ("on request", supplementary-only,
  latest-release link)
- `source_units`: where declared

## Finding (A4/A5/forensics)

- `axis`: `artifact_access|input_binding|sample_metadata|
  methods_placement|executability|result_verifiability|self_sufficiency`
- `severity`: `blocking|warning|informational` — blocking emits a
  ClarificationRequest (if answerable) or gate-blocks A6 (if not)
- `subject`: refs[] — slots, artifacts, units, or document-level
- `mechanism`: `deterministic|jev|generator`; `jev_question_id` when
  judged
- `confidence`: composed (min of input edges, per contract)
- `coverage_caveat`: bool — negative findings only
- `derived_from`: finding/edge ids[]
- `description`, `suggestion` (draft-mode fix text)
- `question_id` → ClarificationRequest | null

## ClarificationRequest (always emitted, both modes)

- `blocking`: bool — gates A6; in draft mode = fix-list item
- `question`, `context_refs[]` (slot/finding)
- `resolves_unknown`: `not_yet_checked|cannot_determine|
  needs_human_judgement`
- `answer`: {`text`, `answered_by` (`user`), `at`} | null
- `mid_run`: bool — emitted mid-run via Orbit vs. statically at
  A4/A5. Published-mode blocking + unanswered = τ-disqualifying for
  that run.

## ReproRun (A6)

- `k_index`, `model`: {`provider`, `model`}, `orbit_version`,
  `pi_version` — verdict stamps all of them
- `workdir`, `session_dir`
- `gate`: {`passed`, `failed_criteria[]` (`public-inputs`,
  `open-questions`)}
- `milestones[]`: {`m` (`M1|M2|M3|M4`), `status` (`reached|failed|
  M1-blocked:access|unchecked`), `ts`} — parsed from notebook.md +
  orbit-invocation YAML + activity.jsonl
- `stall`: {`milestone`, `proximate_cause`, `mapped_finding_id`|null}
- `clarifications[]` → ClarificationRequest — counts feed
  `clarification_burden`
- `concordance[]`: {`result_key`, `expected`, `produced`,
  `tolerance_used`, `verdict` (`match|mismatch|unchecked`)}
- `cost`: {`jev_usd`, `llm_usd`, `orbit_usd`}, `wall_s`
- `status`: `pass|fail|gate_blocked|indeterminate|error`

## Report (A7 top level)

`{verdict, axes, findings, questions, runs, costs, meta}`

- `verdict`: {`label` (`PASS|FAIL|NA|INDETERMINATE`),
  `flavor` (`gate-blocked:<criterion>` | null), `tau`, `k`,
  `passing_runs`, `checked_runs`}
- `axes`: {axis → {`score`, `confidence`, `finding_ids[]`}}
- `findings`, `questions`: full records (published = for-authors
  question list; draft = answerable fix list)
- `runs`: ReproRun[] | null (null in static-only mode)
- `costs`: {`jev`, `llm`, `orbit`, `total_usd`, `by_stage`}
- `meta`: {`reprobouncer` + `orbit` + model versions, `schema_version`,
  `timestamp`, `input`, `mode` (`published|draft`), `full`: bool}

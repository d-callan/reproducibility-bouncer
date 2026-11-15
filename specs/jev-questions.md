# JEV question inventory — v0

Per-stage `build_state`/`build_questions` contracts, following the
BioNym `questions/` pattern: questions are Python-templated upstream,
JEV judges presented evidence, never enumerates or invents items.
Types: `score` (0–1), `choice` (enum), `noul` (supported /
unsupported / unclear binary judgment).

Question templates are starting text — calibrate wording on the
fixture corpus before trusting confidences.

## A0 — intake (rare; regex first)

| id | type | fires when | template | state |
|---|---|---|---|---|
| `a0_input_kind` | choice {`paper_pdf`,`paper_url`,`non_paper`,`ambiguous`} | identifier regex ambiguous | "Classify this input: {snippet}" | input string, regex hints |

## A1 — document

| id | type | fires when | template | state |
|---|---|---|---|---|
| `a1_span_supports` | noul | every extracted span claim | "Source unit {n}: '{text}'. Does it state '{claim}'?" | unit text + claim |

## A2 — extraction (the bulk of judge load)

| id | type | fires when | template | state |
|---|---|---|---|---|
| `a2_unit_real` | noul | per proposed AnalysisUnit | "Units {i..j} — is this a distinct analysis producing an output?" | cited unit texts |
| `a2_slot_value` | noul | per filled slot | "Do units {i..j} support '{slot}' = '{declared_value}'?" | unit texts + slot |
| `a2_provenance` | choice {`reported`,`observed`,`inferred`} | per slot | "How was this value obtained: stated in text / read from an artifact / derived?" | unit texts + artifact summary |
| `a2_headline` | choice {`headline`,`secondary`} | per unit | "Is this unit one of the paper's key results (named in abstract/discussion)?" | unit + abstract/discussion units |
| `a2_extract_coverage` | score | per unit | "0–1: fraction of slots a reproducer needs that are stated" | unit slot manifest. *Coverage of our extraction — distinct from `a5_methods_complete` (reproducer-recoverability).* |

## A4 — binding

| id | type | fires when | template | state |
|---|---|---|---|---|
| `a4_column_semantics` | choice {`matches`,`differs`,`unclear`} | `sample_grouping` slots | "Column '{c}' values vs group names in units {i}: same partition?" | column values + unit texts |
| `a4_instance_select` | choice {c1..cn,`ambiguous`} | enumerated candidate sets | "Which instance do units {i..j} most plausibly mean?" | candidates + unit texts |
| `a4_binding_unique` | noul | unregistered identifier families | "Does this evidence uniquely identify one versioned instance?" | slot + evidence bundle |

## A5 — static audit

| id | type | fires when | template | state |
|---|---|---|---|---|
| `a5_methods_complete` | score | per unit | "Are all pipeline parameters recoverable from text + supplement?" | slot manifest vs doc units |
| `a5_test_design` | choice {`matched`,`mismatch`,`unclear`} | per stat test | "Does '{test}' match the described design (paired vs independent, n, distribution)?" | methods units + test claim |
| `a5_robustness` | score | per unit | "Sensitivity analyses / seeds / pre-specified exclusions reported?" | unit slot manifest |
| `a5_methods_placement` | choice {`proper`,`buried`,`absent`} | per unit | "Are analysis-critical details in Methods, buried in main text/supplement, or absent?" | detail list + locations |

## A6 — harness (per run)

| id | type | fires when | template | state |
|---|---|---|---|---|
| `a6_m4_qualitative` | noul | qualitative concordance items | "Reported: '{desc}'. Produced: '{artifact_summary}'. Same result (shape/sign)?" | reported desc + produced artifact summary/image |
| `a6_stall_class` | choice {`input_access`,`env`,`exec`,`ambiguity`,`agent_error`} | per stalled/failed run | "Classify the proximate cause of the stall from this log tail" | log tail + milestone state |

## A7 — report

| id | type | fires when | template | state |
|---|---|---|---|---|
| `a7_quote_valid` | noul | every quoted evidence line in the report | "Does unit {n} contain exactly this quoted text?" | quote + source unit |

## `build_state` per stage

- **A0**: input string + regex hint table
- **A1**: document sections + numbered units (as emitted by the parser)
- **A2**: document sections/units + prior stage's artifact summary
- **A4**: slot record + candidate artifacts + the unit texts the
  generator cited
- **A5**: per-unit slot manifest + relevant methods/results units
- **A6**: produced-vs-reported `result_key` pairs + Orbit run logs
- **A7**: quote ↔ source-unit pairs assembled for the report

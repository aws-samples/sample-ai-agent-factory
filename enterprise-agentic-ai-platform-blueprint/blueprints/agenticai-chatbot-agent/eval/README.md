# Chatbot blueprint — evaluation corpus

**Z7-I (BLUEPRINT_GAP_ANALYSIS Partial-1):** golden-prompt regression
suite + promptfoo-compatible YAML config.

## Files

- `evaluation.config.yaml` — promptfoo schema. Run with `promptfoo eval -c evaluation.config.yaml`.
- `golden_corpus.jsonl` — 8 reference cases (3 factual, 4 adversarial-refusal, 1 latency).

## Pipeline integration

`scripts/evaluation_gate.py` reads the corpus + thresholds and exits
non-zero if any of the 7 scoring categories breach. Wired into the
WorkloadPipelineStack `EvaluationGate` CodeBuild step.

## Adding new cases

Append rows to `golden_corpus.jsonl`. Append matching `tests:` blocks to
`evaluation.config.yaml` so both runners stay in sync. Then bump the
`thresholdsHash` in `buildAgentManifest()` callers.

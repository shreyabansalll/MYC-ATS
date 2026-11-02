# Evaluation Guide

## What this evaluates

`evaluation/run_synthetic_evaluation.py` is a deterministic, offline regression harness. It creates fictional PDF and DOCX resumes in a temporary directory, runs the existing extract/parse/generic-score paths, checks selected expected text and fields, tests malformed/empty/unsupported inputs, and checks one controlled unsupported-numeric-claim example.

Run it from the repository root:

```bash
venv/bin/python -m evaluation.run_synthetic_evaluation
```

The script prints JSON with the synthetic checks and score values. Its fixture names and `.test` email domain are fictional. It does not read local candidate resumes, call Anthropic, or claim results on real candidates. CI runs the same command after installing the dependencies and spaCy model.

## Interpreting results

- A failed assertion means a deterministic behavior expected by the synthetic fixture changed or regressed.
- Text-marker coverage and the listed field checks describe only these generated examples. They are not estimates of resume parsing accuracy.
- Generic score and keyword-match values are recorded only for the synthetic fixtures; they are not quality targets and should not be compared to candidate populations.
- The numeric-claim example validates one narrow heuristic. It does not measure overall factuality or prove rewritten content true.
- Ordinary tests and this harness do not call the live LLM. No live-provider evaluation suite is currently provided.

## Evidence still needed for a real quality evaluation

The repository does not include an authorized, de-identified, field-labeled benchmark with adjudicated extraction ground truth, job descriptions, and human-rated factuality. Establishing production accuracy or unsupported-claim rates requires such a dataset, an evaluation protocol, and separately controlled live-provider runs. Historical 269-resume figures are described in [CLAUDE.md](./CLAUDE.md) and [CODEBASE_AUDIT.md](./CODEBASE_AUDIT.md); they are not current two-layer rewrite-improvement results.

Do not use synthetic results as evidence of hiring outcomes, ATS vendor compatibility, regulatory compliance, or production performance.

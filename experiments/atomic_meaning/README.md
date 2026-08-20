# Atomic Meaning bounded experiment

This experiment evaluates the proposed capability-neutral LLM0.5 graph,
registry-owned fact binding, and deterministic `MeaningPlan` compilation. It
uses the real Product 4 capability registry, checker, and executable validator.
It does not project, freeze, package, deploy, or run Engines 1–3.

The runner loads encrypted credentials from an explicitly selected local test
data directory. It fails before a provider call unless the expected project ID
and API-key suffix match. The API key is never written to an artifact.

Required environment variables:

```text
PRODUCT4_EXPERIMENT_DATA_ROOT
PRODUCT4_EXPERIMENT_PROJECT_ID
PRODUCT4_EXPERIMENT_KEY_SUFFIX
```

Optional output directory:

```text
PRODUCT4_EXPERIMENT_OUTPUT_ROOT=/tmp
```

Run the long depth-and-breadth case:

```text
PYTHONPATH=. .venv/bin/python experiments/atomic_meaning/runner.py
```

Run the two unseen medium cases:

```text
PYTHONPATH=. .venv/bin/python experiments/atomic_meaning/two_medium_cases.py
```

Every provider result is checkpointed before subsequent validation. A case is
successful only when the real Meaning Plan contract validates with zero
missing fact fields, zero active configuration findings, and zero executable
errors.

Probe the existing downstream boundary without provider calls:

```text
PYTHONPATH=. .venv/bin/python experiments/atomic_meaning/downstream_probe.py \
  /tmp/autoglific-atomic-course_registration-result.json
```

The probe invokes the atomic deterministic projection boundary, renders the
authored Mermaid, prepares `authoring-package-1.0`, and validates its canonical
hash. It stops before session freeze and Engines 1–3.

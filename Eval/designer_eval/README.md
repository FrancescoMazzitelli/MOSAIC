# Designer evaluation harness

This directory contains the harness for evaluating the **Designer** fallback used
when the planner returns an empty plan. It is a separate, new setup:

- **45 diagnostic queries**, balanced 15/15/15:
  - `OUT_OF_DOMAIN`: the catalog cannot satisfy the request;
  - `AMBIGUOUS`: the request is under-specified or has multiple intents;
  - `INVALID`: the request is logically or semantically invalid.
- **100 in-domain smart-city queries** to measure Designer false positives.
- Two independent annotators, with a separate adjudication sheet.
- A runner that uses `dry_run` (planning + validation, no execution).
- An analyzer that produces the confusion matrix, reach metrics, silent wrong
  plan, contract yield/validity, reuse, plausibility, and false positives.

The labels in `queries/designer_queries.csv` are **proposals** and are not
adjudicated. Do not use them as final results in the paper before adjudication.

## Layout

```
designer_eval/
├── queries/
│   ├── designer_queries.csv          # seed + proposed labels (then adjudicated)
│   └── smart_city_100.csv            # normalized copy of the 100 in-domain queries
├── annotations/
│   ├── annotator_A.csv               # blind: only query_id, query, category to fill in
│   ├── annotator_B.csv               # blind
│   └── adjudicated.csv               # proposed category visible + adjudicated category
├── contracts/                        # (optional) scoring material
├── results/                          # run_*/
├── run_designer_eval.py
├── analyze_designer_eval.py
├── prepare_annotation.py
├── apply_adjudication.py
├── fetch_catalog_snapshot.py
├── common.py
└── README.md
```

## Prerequisites

1. MOSAIC stack running (control unit, gateway, catalog, Ollama).
2. Control unit updated with `dry_run` support and the routing fields
   (`route_reason`, `planner_status`, `planner_tasks_count`,
   `validated_tasks_count`, `designer_attempted`). The fields are additive.
3. A snapshot of the full catalog, for HC-1 and exact endpoint reuse:

```bash
python3 -m Eval.designer_eval.fetch_catalog_snapshot \
  --catalog-url http://localhost:5000 \
  --out Eval/designer_eval/catalog_snapshot.json
```

If the snapshot is missing, the runner still works, but HC-1 and reuse are
computed only on the retrieved services; the analysis reports this.

## Annotation and adjudication

The blind sheets have already been generated. To regenerate them:

```bash
python3 -m Eval.designer_eval.prepare_annotation --force
```

Protocol:

1. `annotator_A.csv` and `annotator_B.csv` contain only `query_id`, `query` and
   empty columns. Each annotator assigns one of the three categories and a
   confidence from 1 to 5, without seeing the proposed labels.
2. `adjudicated.csv` also contains `proposed_category`; the two annotators
   discuss disagreements and fill in `adjudicated_category`.
3. Copy the final labels into `queries/designer_queries.csv`, updating
   `gold_category` and setting `label_status=adjudicated` for all 45 rows. The
   runner warns if the file is not adjudicated.

   Alternatively, after filling `annotations/adjudicated.csv`:

   ```bash
   python3 -m Eval.designer_eval.apply_adjudication
   # -> queries/designer_queries_adjudicated.csv
   ```

   Then pass `--queries queries/designer_queries_adjudicated.csv` to the runner
   and to the analysis. If `queries/designer_queries_adjudicated.csv` exists,
   the runner and the analysis use it automatically as the default. Use
   `--in-place` only when the adjudication is final.

Do not use the 30 queries in `test_validation/out_of_plan_test_requests.csv` as a
test set: they were used for debugging and have labels from a single annotator.
They remain useful only as a pilot.

## Running

Dry run (recommended; it does not perform write-side calls):

```bash
# For a reproducible run (optional, but recommended for the paper)
DESIGNER_TEMPERATURE=0.0 DESIGNER_SEED=42 docker compose -f Compose.yaml up -d control-unit

python3 -m Eval.designer_eval.run_designer_eval \
  --control-url http://localhost:5500/api/control/invoke \
  --catalog Eval/designer_eval/catalog_snapshot.json \
  --dry-run \
  --out-dir Eval/designer_eval/results/run_manual
```

`--dry-run` is the default. Use `--no-dry-run` only if you want to execute the
plans and accept side effects on the mocks.

For a quick test:

```bash
python3 -m Eval.designer_eval.run_designer_eval --limit 5 --allow-proposed
```

The runner saves:

- `raw_responses.jsonl`: full response, one line per query;
- `per_query.csv`: flat row with routing, contract, and retrieval data;
- `run_manifest.json`: configuration, commit, model, prompt hashes, timestamp.

## Analysis

```bash
python3 -m Eval.designer_eval.analyze_designer_eval \
  --run-dir Eval/designer_eval/results/run_manual
```

Main outputs:

- `summary.md`: human-readable tables;
- `summary.json`: all metrics, including Wilson CIs;
- `confusion_matrix.csv`;
- `failure_breakdown.csv` (route reason per class);
- `per_query_enriched.csv`;
- `contracts_to_review_A.csv` and `_B.csv`: contracts for OOD queries only,
  with `rationale` removed, to be scored blind.

### Contract scoring

Each evaluator fills in a copy of `contracts_to_review_A.csv` /
`_B.csv` with:

- `score`: contract plausibility, integer 1–5;
- `reuse_semantic`: `yes`/`no` if the contract reuses an operation or capability
  from the catalog (semantic reuse, distinct from an exact match);
- `notes`: brief justification.

1–5 rubric:

| Score | Meaning |
|---:|---|
| 1 | Unusable contract: inconsistent or out-of-domain service/endpoint. |
| 2 | Weak idea: the endpoint or schema is not enough to satisfy the request. |
| 3 | Usable with moderate fixes: correct structure, details to be settled. |
| 4 | Good: plausible service and endpoint, schema almost complete. |
| 5 | Directly implementable: coherent service, endpoint, and schema. |

Then:

```bash
python3 -m Eval.designer_eval.analyze_designer_eval \
  --run-dir ... \
  --scores-a .../contract_scores_A.csv \
  --scores-b .../contract_scores_B.csv
```

The analysis computes mean/median, the share of contracts with mean ≥ 3, exact
agreement, agreement within 1 point, quadratic weighted kappa, and ICC(2,1).

## Metrics

For the 45 queries:

- **Reach rate**: share of queries in which the Designer is activated.
- **Classification accuracy (reached)**: category accuracy among reached
  queries.
- **End-to-end accuracy**: accuracy counting non-empty plans as errors too.
- **Silent wrong plan (pre)**: share of queries with at least one planner task
  before validation. This is the most important routing failure: the Designer is
  not called.
- **Silent wrong plan (post)**: share of queries with executable tasks after
  validation/auto-fix.
- **Contract yield**: share of OOD queries with a produced contract.
- **Contract hard validity**: compliance with HC-1, HC-2, HC-3, HC-4, HC-5.
- **Contract leakage**: contracts produced for non-OOD queries (must be 0).

For the 100 smart-city queries:

- **Designer activation**: share of in-domain queries in which the Designer is
  activated (routing false positive).
- **Predicted OOD**: share classified as `OUT_OF_DOMAIN`.
- **FP | gold op in Ret**: false positives conditioned on retrieval coverage.
  Separates Designer errors from retrieval errors.
- **FP attributable to retrieval**: activations in which the gold endpoint was
  not among the retrieved services.

Overall:

- **Abstention precision/recall/F1**: the Designer as an "abstain" classifier on
  the 45 + 100 queries.

## Harness tests

```bash
python3 -m unittest discover -s Eval/designer_eval/tests -v
```

The tests cover dataset balancing, endpoint normalization, HC-1/HC-5,
`ret_has_oracle`, and reading boolean flags from the CSV.

## Important caveats

- **Prompt contamination**: the Designer prompt was cleaned of the
  `smart-library-mock` example and the "energy" example, replaced with a neutral
  museum-loan example. The planner examples remain contaminated
  (`smart-library-mock`, `smart-hospital-mock`, `smart-campus-mock`,
  `smart-logistics-mock`, `smart-hospitality-mock`, `smart-acoustics-mock`,
  `smart-real-estate-mock`), and those services are not in the smart-city
  catalog. Decontaminating the planner is still necessary before the final
  version and requires a planner rerun.
- **Designer input**: the Designer receives only the retrieved services
  (`discovered["services"]`), not the full catalog. The `ret_has_gold` metric
  separates a Designer error from a retrieval error. A "full catalog" condition
  is not implemented in this harness.
- **Model**: the current system results use
  `Qwen3-Embedding-0.6B + bge-reranker-base`; do not attribute the metrics to
  `BGE-small-en-v1.5` without a rerun.
- **Designer temperature**: hard-coded to `0.2` in the original code; it is now
  possible to set `DESIGNER_TEMPERATURE` and `DESIGNER_SEED`. For a reproducible
  evaluation, use `DESIGNER_SEED` (and, to remove residual variability,
  `DESIGNER_TEMPERATURE=0.0`). Compose already passes them to the `control-unit`
  container.
- **Routing**: the Designer is activated only if `tasks == []`. Malformed JSON
  or a non-empty plan do not activate it; the analysis exposes both cases via
  `route_reason`.
- **Old June run**: 26/30 correct, but contaminated by the prompt examples and
  not paper-ready. `Q21`, `Q29`, `Q30` bypass the Designer; `Q25` is classified
  as `OUT_OF_DOMAIN` instead of `INVALID`.

## Useful commands

```bash
# Quick dataset check
python3 - <<'PY'
from Eval.designer_eval.common import read_csv
from collections import Counter
rows = read_csv('Eval/designer_eval/queries/designer_queries.csv')
print(Counter(r['gold_category'] for r in rows))
PY

# Analyze the latest run
python3 -m Eval.designer_eval.analyze_designer_eval
```

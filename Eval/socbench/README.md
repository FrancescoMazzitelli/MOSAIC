# MOSAIC on SOCBench-D

Standalone, CPU-only harness that measures MOSAIC's retrieval stage on
SOCBench-D and fills the last row of the comparison table with Pesl et al.
(IEEE TSC 2026).

It does **not** modify the paper, the benchmark, or `db-gateway/`.

## What is measured

SOCBench-D: 5 instances x 11 GICS domains x 5 services x 10 endpoints, with
10 domain-level queries per domain (550 queries). Each query has a gold set
`E` of `METHOD /path` endpoints (mean `|E| = 4.53`).

The protocol is the one used by the authors' notebook
(`socbench-d/code/src/analysis/cross-domain-average.ipynb`):

```
recall@k    = |retrieved_k INTERSECT E| / |E|
precision@k = |retrieved_k INTERSECT E| / |retrieved_k|
```

`retrieved_k` is the list of unique operation ids in the top-k. Duplicate
`METHOD /path` strings exposed by two services in the same domain are
collapsed, keeping the best score, as in Pesl's `_get_endpoints_from`.
The reported numbers are the **mean over the 550 query-level values**
(the notebook does `groupby(Model, Strategy, K).mean()` over `entries`).

## MOSAIC pipeline (as implemented)

1. **Stage 1** - bi-encoder over service descriptions; top-`K1=7` services.
   In-domain there are only 5 services, so all of them are always selected.
2. **Stage 2** - cross-encoder (`BAAI/bge-reranker-base`) over every endpoint
   of the selected services, using the same enriched text as
   `_build_enriched_text`: capability description + parameter names + response
   property names (+ request property names for non-GET).
3. **RRF (Eq. 1)** applied per endpoint:

   ```
   F(o) = 1/(60 + rank_1(service(o))) + 1/(60 + rank_2(o))
   ```

   where `rank_1` is the stage-1 rank of the owning service and `rank_2` is
   the cross-encoder rank of the endpoint.

   This is exactly Eq. 1 of the paper, which is defined per operation:
   `ρ1(o)` is the rank of the service declaring `o`, `ρ2(o)` is the rank of
   `o` in stage 2. The public `db-gateway` implementation computes the fusion
   at service level (using each service's best endpoint score); this harness
   follows the paper's per-operation definition, because the metric is
   endpoint-level.

Two ablations are computed for diagnosis, not for the paper row:

* `reranker` - cross-encoder score only (no RRF, no stage-1 signal);
* `flat` - one bi-encoder over the same enriched endpoint texts.

## Deliberate deviations from production

* **No token budget (5000) and no `TOP_ENDPOINTS_PER_SERVICE=6` cap.** These
  are prompt-packing parameters, not retrieval-quality parameters. At `k=20`
  a cap of 6 per service would make the comparison structurally impossible.
  The harness ranks every candidate and cuts at `k`.
* **Stage-1 text is the service description.** SOCBench-D has no
  `generated_description`, so this is exactly what `select_stage1_text`
  would return.
* **In-domain only.** The pooled 55-service / 550-endpoint condition is a
  different experiment and is not comparable to the published table. It is
  not implemented.
* The cross-encoder is truncated at 256 tokens; endpoint texts in SOCBench-D
  are short.

## Models

| Flag | Model | Why |
|---|---|---|
| `--embedder bge` | `BAAI/bge-small-en-v1.5` | Same embedder as the BGE block of Pesl's table; CPU-friendly; this is the fair row. |
| `--embedder qwen` | `Qwen/Qwen3-Embedding-0.6B` | MOSAIC's production embedder; "as published" row, not like-for-like with Pesl. |

The reranker is always `BAAI/bge-reranker-base` (MOSAIC's stage 2).

## Run

### Single automated script (recommended)

`run.sh` runs everything in sequence: start the stack, import services, register
in Consul, reindex Stage 1, run the benchmark (MOSAIC only) and build the LaTeX
table.

```bash
cd Eval/socbench

# full run (~10 minutes + import/reindex time)
./run.sh

# smoke test: 1 domain, 10 queries
./run.sh --max-cases 1 --tag smoke

# stack already started and services already registered
./run.sh --skip-stack --skip-register

# second row with MOSAIC's production embedder
./run.sh --embedder qwen --tag qwen
```

The baselines (BGE / OpenAI / NVIDIA, 8 chunking strategies) are **not
recomputed**: they are already reported in the paper and hardcoded in
`make_table.py`. This script measures only the MOSAIC row.

Flags:
- `--skip-stack`    Docker stack already started
- `--skip-register` services already in Mongo/Qdrant (Consul and reindex still run)
- `--max-cases N`   only the first N domains (smoke test)
- `--tag NAME`      output file prefix
- `--embedder x`    `bge` (default, comparable to the BGE block) or `qwen` (Qwen3-Embedding-0.6B, MOSAIC's production embedder)
- `--k 5 10 20`     k values (default)

### Individual steps

```bash
# benchmark only (stack already started and services already registered + reindexed)
python eval_gateway.py --tag gateway

# LaTeX table only
python make_table.py
```

The virtualenv defaults to `/tmp/opencode/mosaic-socbench/.venv`
(override with `VENV=...`). Models are cached by Hugging Face under
`~/.cache/huggingface`.

## Full-stack run (Docker: Mongo + Qdrant + db-gateway)

This is the end-to-end measurement: services are registered through the same
path as the smart-city demo (`api-importer` -> Consul + Redis +
`catalog-gateway` -> MongoDB + Qdrant), and retrieval is performed by the
gateway itself, without LLM generation.

```bash
# 1. start the core stack (no control-unit, no LLM generation)
cd /home/francesco/Desktop/MOSAIC
docker compose -f Compose.yaml up -d --build catalog-gateway api-importer healthcheck-service

# 2. register all 275 SOCBench-D services (resumable: existing ids are skipped)
cd Eval/socbench
python register_gateway.py --workers 2

# 3. evaluate the 550 queries through POST /index/search
python eval_gateway.py --tag gateway

# 4. rebuild the table (adds the full-stack row)
python make_table.py
```

Notes:

* `USE_LLM_DECOMPOSITION=false` is set in `Compose.yaml`: the retrieval-only
  benchmark must not call an external Ollama instance.
* The gateway accepts two evaluation-only fields on `/index/search`:
  `service_ids` (Stage 1 filter restricting the catalog to the five services
  of the query's domain) and `return_endpoints: true` (returns the flat
  Eq. 1 ranking instead of the token-budgeted prompt payload).
* `POST /register` and `GET /health` are excluded from the ranking; `PATCH`
  operations are included (69 of the 2,750 SOCBench-D endpoints are PATCH).
* The Microcks import is separate and is not used by retrieval. To populate
  the mock server:
  `docker run --rm --network mosaic_default -v "$PWD/socbench-d/benchmark:/specs:ro" quay.io/microcks/microcks-cli:latest microcks import "/specs/<path>/openapi.json:true" --microcksURL=http://mock-server:8080/api --keycloakClientId=foo --keycloakClientSecret=bar`.

## Outputs

```
results/
  bge_per_query.csv      # 550 queries x variants x k
  bge_summary.csv        # mean recall/precision per variant and k
  bge_per_domain.csv     # per instance/domain breakdown
  bge_run.log
  gateway_summary.csv    # full-stack run: mean recall/precision per k
  gateway_per_query.csv
  gateway_per_domain.csv
  gateway_registered.json
  table_socbench.tex     # complete table, MOSAIC rows filled
```

## How to read the result

* The row is directly comparable to the **BGE block** only (same embedder,
  same queries, same gold, same `k`, same averaging).
* In-domain, stage 1 is nearly trivial (5 services, `K1=7`): the measured
  difference is mostly the contribution of the **cross-encoder reranker**,
  not of the bi-encoder. Say this explicitly in the paper.
* `P@20` is bounded by `|E|/20 ~ 0.226`; the low precision at `k=20` is
  structural (many endpoints are returned because `k` is large), not a
  retrieval failure.
* The `reranker` and `flat` ablations are there to attribute the effect. If
  the reranker-only variant beats the full RRF at `k=20`, report it: it means
  the service-rank term helps at small `k` but can displace high-scoring
  endpoints at large `k`.

## Caveats for the paper

* Baseline values are **as reported** by Pesl et al. (authors' artifact);
  the MOSAIC row is **measured here**. Same protocol, not the same code.
* Pesl retrieves *chunks*; MOSAIC retrieves *endpoints*. The comparison is
  only meaningful against the endpoint-level strategies (`Endpoint split *`),
  not against `Whole document` or multi-endpoint `JSON` chunking.
* The `Whole Document (200,0)` and `JSON (150,0)` rows are absent from the
  table because the authors' notebook does not contain all nine values for
  them (16 of 18 cells are missing).

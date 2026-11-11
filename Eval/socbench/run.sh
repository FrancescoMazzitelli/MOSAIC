#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# MOSAIC on SOCBench-D — full automatic run
#
#   1. (optional) start the Docker stack for retrieval (Mongo + Qdrant +
#      db-gateway + api-importer + registry + redis + mock-server)
#   2. register the 275 SOCBench-D services in the catalog (resumable)
#   3. run the retrieval benchmark on the 550 queries (mosaic variant only)
#   4. rebuild the LaTeX table with the measured MOSAIC rows + the Pesl et al.
#      baselines already computed by the paper's authors
#
# The baselines (BGE / OpenAI / NVIDIA, 8 chunking strategies) are not
# recomputed: they are already reported in the paper and hardcoded in make_table.py.
# This script measures ONLY the MOSAIC row.
#
# Usage:
#   ./run.sh                        # full run (~10 minutes + import time)
#   ./run.sh --skip-stack           # stack already started
#   ./run.sh --skip-register        # services already registered
#   ./run.sh --max-cases 1          # smoke test: 1 domain, 10 queries
#   ./run.sh --embedder qwen        # use Qwen3-Embedding instead of BGE
#   ./run.sh --tag bge             # output file name (default: tag from args)
# ============================================================================

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$ROOT/../.." && pwd)"
COMPOSE_FILE="$PROJECT_ROOT/Compose.yaml"

# ── Control flags ──────────────────────────────────────────────────────────
SKIP_STACK=false
SKIP_REGISTER=false
EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-stack)     SKIP_STACK=true; shift ;;
    --skip-register)  SKIP_REGISTER=true; shift ;;
    *)                EXTRA_ARGS+=("$1"); shift ;;
  esac
done

# ── 1. Start the Docker stack (retrieval services only) ────────────────────
if ! $SKIP_STACK; then
  echo ""
  echo "═══════════════════════════════════════════════════════════════"
  echo "  STEP 1/4 — Starting Docker stack (retrieval-only, no control-unit)"
  echo "═══════════════════════════════════════════════════════════════"
  cd "$PROJECT_ROOT"
  docker compose -f "$COMPOSE_FILE" up -d --build \
    registry catalog-data catalog-vector catalog-gateway \
    api-importer healthcheck-service mock-server
  echo "  Waiting for catalog-gateway to become ready..."
  for i in $(seq 1 60); do
    if curl -sf http://localhost:5000/health >/dev/null 2>&1; then
      echo "  catalog-gateway ready."
      break
    fi
    sleep 2
  done
  if ! curl -sf http://localhost:5000/health >/dev/null 2>&1; then
    echo "  [ERROR] catalog-gateway did not respond after 120s."
    exit 1
  fi
fi

# ── 2. Registering the SOCBench-D services ─────────────────────────────────
if ! $SKIP_REGISTER; then
  echo ""
  echo "═══════════════════════════════════════════════════════════════"
  echo "  STEP 2/4 — Registering 275 SOCBench-D services (Mongo + Qdrant)"
  echo "═══════════════════════════════════════════════════════════════"
  cd "$ROOT"
  python register_gateway.py --workers 2 "${EXTRA_ARGS[@]}"
fi

# ── 2b. Registration in Consul (registry) ──────────────────────────────────
# register_gateway.py only writes to Mongo/Qdrant via api-importer; the services
# must also be in the registry (Consul) so that the control-unit can find them.
# The /api/importer/register_existing endpoint re-registers without re-downloading.
# URLs on localhost: the script runs from the host, not inside Docker.
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  STEP 2b/4 — Registering in Consul (registry)"
echo "═══════════════════════════════════════════════════════════════"
CONSUL_RESULT=$(curl -sS -X POST http://localhost:7500/api/importer/register_existing)
echo "  $CONSUL_RESULT"
echo "$CONSUL_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"  registered={d.get('registered')} skipped={d.get('skipped')} errors={d.get('errors')}\")" 2>/dev/null || true

# ── 2c. Reindex Stage 1 (services_index in Qdrant) ─────────────────────────
# create_or_update_service() writes to services_index ONLY for services
# loaded via POST /service (not for those via /import/file). The 275 SOCBench-D
# services went to Mongo/Qdrant but services_index is empty.
# /index/reindex populates services_index from the documents already in Mongo.
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  STEP 2c/4 — Reindexing Stage 1 (services_index in Qdrant)"
echo "═══════════════════════════════════════════════════════════════"
REINDEX_OUTPUT=$(curl -sS -X POST --max-time 900 http://localhost:5000/index/reindex)
echo "  $REINDEX_OUTPUT"

# ── 3. Retrieval benchmark (MOSAIC only) ───────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  STEP 3/4 — Retrieval benchmark (550 queries, mosaic variant)"
echo "═══════════════════════════════════════════════════════════════"
cd "$ROOT"
python eval_gateway.py "${EXTRA_ARGS[@]}"

# ── 4. Rebuilding the LaTeX table ──────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  STEP 4/4 — LaTeX table (MOSAIC + Pesl et al. baselines)"
echo "═══════════════════════════════════════════════════════════════"
cd "$ROOT"
python make_table.py

echo ""
echo "✅ Done. Results in $ROOT/results/"
ls -la "$ROOT/results/"*.{csv,tex,json,log} 2>/dev/null || true
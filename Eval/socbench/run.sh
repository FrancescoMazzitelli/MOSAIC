#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# MOSAIC on SOCBench-D — full automatic run
#
#   1. (opzionale) avvia lo stack Docker per il retrieval (Mongo + Qdrant +
#      db-gateway + api-importer + registry + redis + mock-server)
#   2. registra i 275 servizi SOCBench-D nel catalogo (riprendibile)
#   3. esegue il benchmark di retrieval sui 550 query (solo variante mosaic)
#   4. ricostruisce la tabella LaTeX con le righe MOSAIC misurate + i baseline
#      Pesl et al. già calcolati dagli autori del paper
#
# I baseline (BGE / OpenAI / NVIDIA, 8 chunking strategy) non vengono
# ricalcolati: sono già riportati nel paper e hardcoded in make_table.py.
# Questo script misura SOLO la riga MOSAIC.
#
# Uso:
#   ./run.sh                        # full run (~10 minuti + tempo di import)
#   ./run.sh --skip-stack           # stack già avviato
#   ./run.sh --skip-register        # servizi già registrati
#   ./run.sh --max-cases 1          # smoke test: 1 dominio, 10 query
#   ./run.sh --embedder qwen        # usa Qwen3-Embedding invece di BGE
#   ./run.sh --tag bge             # nome file di output (default: tag from args)
# ============================================================================

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$ROOT/../.." && pwd)"
COMPOSE_FILE="$PROJECT_ROOT/Compose.yaml"

# ── Flag di controllo ──────────────────────────────────────────────────────
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

# ── 1. Avvio dello stack Docker (solo servizi di retrieval) ────────────────
if ! $SKIP_STACK; then
  echo ""
  echo "═══════════════════════════════════════════════════════════════"
  echo "  STEP 1/4 — Avvio stack Docker (retrieval-only, no control-unit)"
  echo "═══════════════════════════════════════════════════════════════"
  cd "$PROJECT_ROOT"
  docker compose -f "$COMPOSE_FILE" up -d --build \
    registry catalog-data catalog-vector catalog-gateway \
    api-importer healthcheck-service mock-server
  echo "  Attendo che il catalog-gateway sia ready..."
  for i in $(seq 1 60); do
    if curl -sf http://localhost:5000/health >/dev/null 2>&1; then
      echo "  catalog-gateway ready."
      break
    fi
    sleep 2
  done
  if ! curl -sf http://localhost:5000/health >/dev/null 2>&1; then
    echo "  [ERROR] catalog-gateway non risponde dopo 120s."
    exit 1
  fi
fi

# ── 2. Registrazione dei servizi SOCBench-D ─────────────────────────────────
if ! $SKIP_REGISTER; then
  echo ""
  echo "═══════════════════════════════════════════════════════════════"
  echo "  STEP 2/4 — Registrazione 275 servizi SOCBench-D (Mongo + Qdrant)"
  echo "═══════════════════════════════════════════════════════════════"
  cd "$ROOT"
  python register_gateway.py --workers 2 "${EXTRA_ARGS[@]}"
fi

# ── 2b. Registrazione in Consul (registry) ──────────────────────────────────
# register_gateway.py scrive solo in Mongo/Qdrant via api-importer; i servizi
# devono essere anche nel registry (Consul) perché la control-unit li trovi.
# L'endpoint /api/importer/register_existing ri-registra senza riscaricare.
# URL su localhost: lo script viene eseguito dall'host, non dentro Docker.
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  STEP 2b/4 — Registrazione in Consul (registry)"
echo "═══════════════════════════════════════════════════════════════"
CONSUL_RESULT=$(curl -sS -X POST http://localhost:7500/api/importer/register_existing)
echo "  $CONSUL_RESULT"
echo "$CONSUL_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"  registered={d.get('registered')} skipped={d.get('skipped')} errors={d.get('errors')}\")" 2>/dev/null || true

# ── 2c. Reindicizzazione Stage 1 (services_index in Qdrant) ─────────────────
# create_or_update_service() scrive in services_index SOLO per i servizi
# caricati via POST /service (non per quelli via /import/file). I 275 servizi
# SOCBench-D sono andati su Mongo/Qdrant ma services_index è vuoto.
# /index/reindex popola services_index dai documenti già in Mongo.
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  STEP 2c/4 — Reindicizzazione Stage 1 (services_index in Qdrant)"
echo "═══════════════════════════════════════════════════════════════"
REINDEX_OUTPUT=$(curl -sS -X POST --max-time 900 http://localhost:5000/index/reindex)
echo "  $REINDEX_OUTPUT"

# ── 3. Benchmark di retrieval (solo MOSAIC) ─────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  STEP 3/4 — Benchmark retrieval (550 query, variante mosaic)"
echo "═══════════════════════════════════════════════════════════════"
cd "$ROOT"
python eval_gateway.py "${EXTRA_ARGS[@]}"

# ── 4. Ricostruzione della tabella LaTeX ────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  STEP 4/4 — Tabella LaTeX (MOSAIC + baseline Pesl et al.)"
echo "═══════════════════════════════════════════════════════════════"
cd "$ROOT"
python make_table.py

echo ""
echo "✅ Fatto. Risultati in $ROOT/results/"
ls -la "$ROOT/results/"*.{csv,tex,json,log} 2>/dev/null || true
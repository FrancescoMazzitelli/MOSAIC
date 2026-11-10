#!/bin/sh
# Import every SOCBench-D OpenAPI into the Microcks mock server.
#
# This is the "pass the OpenAPI to the mock server" step. It is independent
# from the retrieval catalog (MongoDB/Qdrant) and is not used by
# eval_gateway.py: mocks are only needed if the planner/generation stage is
# executed, which this retrieval-only benchmark deliberately skips.
#
# Requires the stack to be up:
#   docker compose -f Compose.yaml up -d mock-server registry catalog-data \
#       catalog-vector catalog-gateway api-importer healthcheck-service
#
# Usage:
#   ./import_microcks.sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
SPECS_DIR="$ROOT/socbench-d/benchmark"
LIST_FILE="$SCRIPT_DIR/results/microcks_specs.txt"
mkdir -p "$SCRIPT_DIR/results"

# The microcks-cli image is minimal: it has no `find`, so build the list on
# the host and mount it inside the container.
if command -v find >/dev/null 2>&1; then
  find "$SPECS_DIR" -name openapi.json | sort |
    sed "s|^$SPECS_DIR|/specs|" > "$LIST_FILE"
else
  # Fallback for environments without `find` (e.g. inside the CLI image).
  printf '%s\n' /specs/*/*/*/openapi.json > "$LIST_FILE"
fi

total=$(wc -l < "$LIST_FILE")
echo "importing $total OpenAPI files into Microcks"

docker run --rm --network mosaic_default \
  -v "$ROOT/socbench-d/benchmark:/specs:ro" \
  -v "$LIST_FILE:/specs.list:ro" \
  quay.io/microcks/microcks-cli:latest \
  sh -c '
    i=0
    while IFS= read -r f; do
      i=$((i+1))
      printf "[%s] %s\n" "$i" "$f"
      microcks import "$f:true" \
        --microcksURL=http://mock-server:8080/api \
        --keycloakClientId=foo --keycloakClientSecret=bar 2>&1 | tail -1 \
        || echo "FAILED: $f"
    done < /specs.list
  '

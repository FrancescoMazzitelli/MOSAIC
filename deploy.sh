#!/bin/sh
set -e

APIS_DIR="/apis"
SCRIPTS_DIR="/scripts"
MICROCKS_URL="${MICROCKS_URL:-http://mock-server:8080/api}"
TOKEN="${TOKEN:-dummy}"

echo "| Starting automatic import of APIs and dispatcher patching..."

for api_file in "$APIS_DIR"/*.yaml; do
  api_filename=${api_file##*/}
  base_name=${api_filename%.yaml}

  raw_title=$(grep -m 1 "^[[:space:]]*title:" "$api_file" \
    | sed 's/^[[:space:]]*title:[[:space:]]*//' \
    | tr -d "'\"" | tr -d '\r')
  service_name="$raw_title"

  register_script="${SCRIPTS_DIR}/${base_name}.groovy"
  get_script="${SCRIPTS_DIR}/${base_name}-get.groovy"

  echo "| Importing API: $api_filename ($service_name)"
  microcks import "${api_file}:true" \
    --microcksURL="${MICROCKS_URL}" \
    --keycloakClientId=foo --keycloakClientSecret=bar
  echo "| Imported $api_filename."

  # ── Fetch service_id with polling ────────────────────────────────────────
  MAX_RETRIES=15
  RETRY_COUNT=0
  service_id=""

  while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    service_id=$(curl -s "${MICROCKS_URL}/services" \
      -H "Authorization: Bearer $TOKEN" | \
      /tmp/jq -r --arg term "$service_name" \
        '.[] | select((.name | ascii_downcase) | contains($term | ascii_downcase)) | .id' \
      | head -n 1)

    if [ -n "$service_id" ] && [ "$service_id" != "null" ]; then
      break
    fi

    echo "⏳ Waiting for Microcks DB... (Attempt $((RETRY_COUNT+1))/$MAX_RETRIES)"
    sleep 2
    RETRY_COUNT=$((RETRY_COUNT + 1))
  done

  if [ -z "$service_id" ] || [ "$service_id" = "null" ]; then
    echo "⚠️  Service ID not found for $service_name — skipping."
    echo "-----------------------------------"
    continue
  fi

  echo "| Service ID: $service_id"

  # ── Patch POST /register ─────────────────────────────────────────────────
  if [ -f "$register_script" ]; then
    echo "| Patching POST /register..."
    SCRIPT=$(cat "$register_script" | tr -d '\r')
    REGISTER_OP_ENC=$(printf '%s' "POST /register" | /tmp/jq -sRr @uri)
    PAYLOAD=$(/tmp/jq -n \
      --arg dispatcher "SCRIPT" \
      --arg dispatcherRules "$SCRIPT" \
      '{dispatcher: $dispatcher, dispatcherRules: $dispatcherRules}')

    if curl --fail-with-body -sS \
        -X PUT "${MICROCKS_URL}/services/${service_id}/operation?operationName=${REGISTER_OP_ENC}" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" > /tmp/patch.json 2>&1; then
      echo "✅ POST /register → SCRIPT"
    else
      echo "❌ POST /register patch failed"; cat /tmp/patch.json
    fi
  fi

  # ── Per-operation dispatcher patching ────────────────────────────────────
  # The get-script (<service>-get.groovy) is the COLLECTION GET dispatcher:
  # it routes by query param (status, zoneId, type, ...) and, as a fallback,
  # returns a specific example from the list (e.g. 'example_list').
  # It must be applied ONLY to the collection GET. Applying it also to
  # POST/PUT/DELETE/GET-by-id (as it used to) makes the fallback
  # return an example name that does not exist on those operations
  # (e.g. 'example_list' on a POST) -> HTTP 400 "response ... does not exist".
  # All other operations get a static 'return mock' dispatcher
  # (the 'mock' example is present on every response after fix_yaml.py).
  MOCK_PAYLOAD=$(/tmp/jq -n \
    --arg dispatcher "SCRIPT" \
    --arg dispatcherRules "return 'mock'" \
    '{dispatcher: $dispatcher, dispatcherRules: $dispatcherRules}')

  GET_PAYLOAD=""
  if [ -f "$get_script" ]; then
    SCRIPT=$(cat "$get_script" | tr -d '\r')
    GET_PAYLOAD=$(/tmp/jq -n \
      --arg dispatcher "SCRIPT" \
      --arg dispatcherRules "$SCRIPT" \
      '{dispatcher: $dispatcher, dispatcherRules: $dispatcherRules}')
  fi

  grep "^  /[a-zA-Z{]" "$api_file" \
    | grep -v "/health:\|/register:" \
    | sed 's/^  //' | tr -d ':' | tr -d '\r' \
    | while read -r path; do
        rel=${path#/}
        case "$rel" in
          */*) is_collection=no ;;
          *)   is_collection=yes ;;
        esac
        for method in GET POST PUT DELETE PATCH; do
          if [ "$method" = "GET" ] && [ "$is_collection" = "yes" ] && [ -n "$GET_PAYLOAD" ]; then
            PAYLOAD="$GET_PAYLOAD"; label="get-script"
          else
            PAYLOAD="$MOCK_PAYLOAD"; label="mock"
          fi
          OP="${method} ${path}"
          OP_ENC=$(printf '%s' "$OP" | /tmp/jq -sRr @uri)
          if curl --fail-with-body -sS \
              -X PUT "${MICROCKS_URL}/services/${service_id}/operation?operationName=${OP_ENC}" \
              -H "Authorization: Bearer $TOKEN" \
              -H "Content-Type: application/json" \
              -d "$PAYLOAD" > /tmp/patch_op.json 2>&1; then
            echo "✅ ${OP} → SCRIPT (${label})"
          fi
        done
      done

  echo "-----------------------------------"
done

echo "✅ Done: APIs imported and dispatchers patched."

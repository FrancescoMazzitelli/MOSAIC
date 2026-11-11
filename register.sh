#!/bin/sh
set -e

echo "| Starting POST /register invocations..."

APIS_DIR="/apis"
MOCK_BASE_URL="http://mock-server:8080/rest"
VERSION="1.0"

for api_file in "$APIS_DIR"/*.yaml; do
  echo "Debug: Processing file '$api_file'"

  # Reads the title, removing both \r (Windows CRLF) and residual \n
  raw_title=$(grep -m 1 "^[[:space:]]*title:" "$api_file" \
    | sed 's/^[[:space:]]*title:[[:space:]]*//' \
    | tr -d "'\"" \
    | tr -d '\r' \
    | tr -d '\n')

  echo "Debug: Real service_name read from YAML = '$raw_title'"

  # Encode spaces and & in the URL
  service_url_part=$(echo "$raw_title" | sed 's/ /+/g' | sed 's/&/%26/g')

  register_url="${MOCK_BASE_URL}/${service_url_part}/${VERSION}/register"
  echo "Debug: register_url='$register_url'"

  response_body_file="/tmp/register_response.json"
  response=$(curl -sS -o "$response_body_file" -w "%{http_code}" -X POST "$register_url")

  if [ "$response" -eq 200 ] || [ "$response" -eq 201 ]; then
    echo "✅ POST /register successful for $raw_title"
  else
    echo "❌ POST /register failed for $raw_title — HTTP $response"
    if [ -s "$response_body_file" ]; then
      echo "Response body:"
      cat "$response_body_file"
    fi
  fi

  echo "-----------------------------------"
  sleep 0.5
done

echo "✅ Done: All /register endpoints invoked."
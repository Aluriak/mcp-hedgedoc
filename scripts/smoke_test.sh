#!/usr/bin/env bash
# Smoke-tests a built mcp-hedgedoc image: runs it in read-only mode
# against a bogus HEDGEDOC_URL (never touches the real instance) and
# checks that the read-only gate genuinely holds.
#
# Usage: scripts/smoke_test.sh <image-ref>
set -euo pipefail

IMAGE="${1:?Usage: scripts/smoke_test.sh <image-ref>}"
CONTAINER_NAME="mcp-hedgedoc-smoketest-$$"
HOST_PORT="18765"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cleanup() {
  podman rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Starting $IMAGE as $CONTAINER_NAME on port $HOST_PORT (read-only mode, fake HEDGEDOC_URL)..."
podman run -d --name "$CONTAINER_NAME" \
  -p "${HOST_PORT}:8080" \
  -e HEDGEDOC_URL="http://localhost:1" \
  -e HEDGEDOC_EMAIL="smoketest@example.org" \
  -e HEDGEDOC_PASSWORD="unused" \
  -e HEDGEDOC_READ_ONLY="true" \
  "$IMAGE" >/dev/null

echo "Waiting for the server to accept connections..."
for _ in $(seq 1 30); do
  if curl -sS -o /dev/null "http://127.0.0.1:${HOST_PORT}/mcp" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

if ! curl -sS -o /dev/null "http://127.0.0.1:${HOST_PORT}/mcp" 2>/dev/null; then
  echo "FAIL: server never became reachable. Container logs:"
  podman logs "$CONTAINER_NAME" || true
  exit 1
fi

python3 "${SCRIPT_DIR}/mcp_check.py" "http://127.0.0.1:${HOST_PORT}/mcp"
RESULT=$?

if [ "$RESULT" -ne 0 ]; then
  echo "--- container logs (for context) ---"
  podman logs "$CONTAINER_NAME" || true
fi

exit "$RESULT"

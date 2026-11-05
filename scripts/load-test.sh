#!/usr/bin/env bash
# Runs the k6 load profile (tests/load/all.js) against a disposable Compose stack with the fake model.
#
# Like scripts/test.sh: unique `bbd-os-test-<hex>` project, docker-compose.test.yml overrides, owner bootstrapped
# through the E2E_SETUP_TOKEN flow (done by the k6 setup() itself), everything removed on exit. k6 runs from the pinned
# grafana/k6 image on the stack's Compose network, so the API is reached as http://api:8000 and the fake model as
# http://fake-model:8000/v1.
#
# Environment:
#   LOAD_OUT_DIR           output directory (default test-results/load/<UTC timestamp>); summary.json, compose-ps.txt
#   LOAD_SCENARIO          entry script under tests/load (default all.js; also read_mix.js, chat.js, ingest.js)
#   LOAD_DURATION_SCALE    scenario length multiplier (default 1; 0.1 for a smoke run)
#   SLO_LATENCY_SCALE      multiplier on every latency threshold (default 1)
#   SEED_DOCS              documents uploaded before the run (default 8)
#   K6_IMAGE               override the pinned image
# The run fails (exit 99, k6's threshold-failure code) when any SLO threshold is crossed.
set -euo pipefail

K6_IMAGE="${K6_IMAGE:-grafana/k6:0.57.0}"
scenario="${LOAD_SCENARIO:-all.js}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
[[ "$scenario" =~ ^[a-z_]+\.js$ && -f "tests/load/$scenario" ]] || { echo "Unknown LOAD_SCENARIO: $scenario" >&2; exit 2; }
out_dir="${LOAD_OUT_DIR:-test-results/load/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$out_dir"
out_dir="$(cd "$out_dir" && pwd)"
# The k6 image runs as a non-root user; it must be able to write the summary into the bind mount.
chmod 777 "$out_dir"

project="bbd-os-test-$(python3 -c 'import secrets; print(secrets.token_hex(5))')"
[[ "$project" =~ ^bbd-os-test-[0-9a-f]{10}$ ]]
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }
export WEB_PORT="$(free_port)"
export API_TEST_PORT="$(free_port)"
while [[ "$API_TEST_PORT" == "$WEB_PORT" ]]; do export API_TEST_PORT="$(free_port)"; done
export TEST_POSTGRES_PORT="$(free_port)"
while [[ "$TEST_POSTGRES_PORT" == "$WEB_PORT" || "$TEST_POSTGRES_PORT" == "$API_TEST_PORT" ]]; do export TEST_POSTGRES_PORT="$(free_port)"; done
export TEST_PUBLIC_ORIGIN="http://localhost:${WEB_PORT}"

compose=(docker compose -p "$project" -f docker-compose.yml -f docker-compose.test.yml)
cleanup() {
  # Keep the stack's state next to the results: `down` destroys the only evidence of a container that died mid-run.
  "${compose[@]}" ps -a >"$out_dir/compose-ps.txt" 2>&1 || true
  "${compose[@]}" logs --no-color --timestamps --tail 2000 >"$out_dir/compose-logs.txt" 2>&1 || true
  "${compose[@]}" down --volumes --remove-orphans
}
trap cleanup EXIT

"${compose[@]}" up -d --build
network="$(docker network ls --filter "label=com.docker.compose.project=$project" --format '{{.Name}}' | head -n 1)"
[[ -n "$network" ]] || { echo "Compose network for $project not found" >&2; exit 1; }

# Record the host the numbers came from; the SLOs are defined for 2 vCPU / 8 GiB (docs/slo.md).
{
  echo "k6_image=$K6_IMAGE"
  echo "scenario=$scenario"
  echo "project=$project"
  echo "host_cpus=$(nproc 2>/dev/null || echo unknown)"
  echo "host_mem_kib=$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo unknown)"
  echo "source_revision=$(git rev-parse HEAD 2>/dev/null || echo unknown)"
} >"$out_dir/environment.txt"

status=0
docker run --rm --network "$network" \
  -v "$repo_root/tests/load:/scripts:ro" -v "$out_dir:/out" \
  -e API_URL=http://api:8000 \
  -e PUBLIC_ORIGIN="$TEST_PUBLIC_ORIGIN" \
  -e E2E_SETUP_TOKEN=bbd-os-disposable-test-token \
  -e FAKE_MODEL_URL=http://fake-model:8000/v1 \
  -e LOAD_DURATION_SCALE="${LOAD_DURATION_SCALE:-1}" \
  -e SLO_LATENCY_SCALE="${SLO_LATENCY_SCALE:-1}" \
  -e SEED_DOCS="${SEED_DOCS:-8}" \
  "$K6_IMAGE" run --summary-export /out/summary.json "/scripts/$scenario" || status=$?
echo "k6 exit status: $status (0 = all SLO thresholds met, 99 = threshold crossed). Results: $out_dir"
exit "$status"

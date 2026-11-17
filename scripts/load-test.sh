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
#   LOAD_TEST_LIMITS       1 layers docker-compose.limits.yml (2 CPU / 8 GiB split, docs/performance-report.md)
#   K6_IMAGE               override the pinned image
# The run fails (exit 99, k6's threshold-failure code) when any SLO threshold is crossed.
set -euo pipefail

# Git Bash would rewrite "/scripts:ro"-style docker arguments into Windows paths; keep them verbatim.
export MSYS_NO_PATHCONV=1

# `python3` on Windows is often the Microsoft Store stub (exists, but does not run); take the first that executes.
py=""
for cand in python3 python; do
  if "$cand" -c 'import sys' >/dev/null 2>&1; then py="$cand"; break; fi
done
[[ -n "$py" ]] || { echo "No working python3/python found on PATH" >&2; exit 2; }

# Under MSYS/Cygwin, Docker Desktop needs bind-mount sources in Windows form (D:/...); plain `pwd` gives /d/...
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) pwd_host() { pwd -W; } ;;
  *) pwd_host() { pwd; } ;;
esac

K6_IMAGE="${K6_IMAGE:-grafana/k6:0.57.0}"
scenario="${LOAD_SCENARIO:-all.js}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd_host)"
cd "$repo_root"
[[ "$scenario" =~ ^[a-z_]+\.js$ && -f "tests/load/$scenario" ]] || { echo "Unknown LOAD_SCENARIO: $scenario" >&2; exit 2; }
out_dir="${LOAD_OUT_DIR:-test-results/load/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$out_dir"
out_dir="$(cd "$out_dir" && pwd_host)"
# The k6 image runs as a non-root user; it must be able to write the summary into the bind mount.
chmod 777 "$out_dir"

project="bbd-os-test-$("$py" -c 'import secrets; print(secrets.token_hex(5))')"
[[ "$project" =~ ^bbd-os-test-[0-9a-f]{10}$ ]]
free_port() { "$py" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }
export WEB_PORT="$(free_port)"
export API_TEST_PORT="$(free_port)"
while [[ "$API_TEST_PORT" == "$WEB_PORT" ]]; do export API_TEST_PORT="$(free_port)"; done
export TEST_POSTGRES_PORT="$(free_port)"
while [[ "$TEST_POSTGRES_PORT" == "$WEB_PORT" || "$TEST_POSTGRES_PORT" == "$API_TEST_PORT" ]]; do export TEST_POSTGRES_PORT="$(free_port)"; done
export TEST_PUBLIC_ORIGIN="http://localhost:${WEB_PORT}"

compose=(docker compose -p "$project" -f docker-compose.yml -f docker-compose.test.yml)
[[ "${LOAD_TEST_LIMITS:-0}" == "1" ]] && compose+=(-f docker-compose.limits.yml)
sampler_pid=""
cleanup() {
  [[ -z "$sampler_pid" ]] || kill "$sampler_pid" 2>/dev/null || true
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

# Resource sampling: one JSON line per container of this project every ~5s (docker stats itself takes ~2s per call).
(
  while true; do
    docker stats --no-stream --format '{{json .}}' 2>/dev/null | grep -F "\"Name\":\"$project-" | sed "s/^{/{\"ts\":$(date +%s),/" || true
    sleep 3
  done
) >"$out_dir/docker-stats.jsonl" &
sampler_pid=$!

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
kill "$sampler_pid" 2>/dev/null || true
wait "$sampler_pid" 2>/dev/null || true
sampler_pid=""
# Per-service max/avg CPU% and max memory (stdlib only).
"$py" - "$out_dir/docker-stats.jsonl" "$project" <<'PY' | tee "$out_dir/docker-stats-summary.txt"
import json, re, sys
units = {"b": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3, "kb": 1e3, "mb": 1e6, "gb": 1e9}
def size(t):
    m = re.match(r"([\d.]+)\s*([A-Za-z]+)", t.strip())
    return float(m[1]) * units.get(m[2].lower(), 1) if m else 0.0
rows = {}
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    try:
        d = json.loads(line)
        used, limit = d["MemUsage"].split("/")
        r = rows.setdefault(d["Name"].removeprefix(sys.argv[2] + "-"), {"cpu": [], "mem": 0.0, "limit": ""})
        r["cpu"].append(float(d["CPUPerc"].rstrip("%")))
        r["mem"] = max(r["mem"], size(used))
        r["limit"] = limit.strip()
    except (ValueError, KeyError):
        continue
print(f"{'service':<28}{'samples':>8}{'cpu_max%':>10}{'cpu_avg%':>10}{'mem_max_MiB':>13}  mem_limit")
for n, r in sorted(rows.items()):
    c = r["cpu"]
    print(f"{n:<28}{len(c):>8}{max(c):>10.1f}{sum(c)/len(c):>10.1f}{r['mem']/1024**2:>13.1f}  {r['limit']}")
PY
echo "k6 exit status: $status (0 = all SLO thresholds met, 99 = threshold crossed). Results: $out_dir"
exit "$status"

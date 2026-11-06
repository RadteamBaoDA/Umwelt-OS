#!/usr/bin/env bash
# Synthetic end-to-end alert delivery proof. Starts the pinned Alertmanager with the real routing config, points both
# receivers' URL files at a throwaway stub webhook sink, fires one severity=ticket and one severity=page alert, and fails
# unless each reaches the sink under the expected receiver within TIMEOUT seconds. Throwaway containers/network only.
# Usage: scripts/alert-delivery-smoke.sh   (ALERTMANAGER_CONFIG overrides the config, used to prove failure detection)
set -euo pipefail
export MSYS_NO_PATHCONV=1
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${1:-${ALERTMANAGER_CONFIG:-$ROOT/infrastructure/monitoring/alertmanager.yml}}"
IMAGE="prom/alertmanager:v0.28.1"   # keep in sync with docker-compose.monitoring.yml and ci.yml
TIMEOUT="${TIMEOUT:-90}"            # page group_wait is 10s, ticket group_wait is 30s
RUN="alert-smoke-$$"
WORK="$(mktemp -d)"
cleanup() { docker rm -f "$RUN-am" "$RUN-sink" >/dev/null 2>&1 || true; docker network rm "$RUN" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT
# Docker Desktop on Windows (git-bash) needs native paths for bind mounts; a no-op on Linux/macOS.
native() { if command -v cygpath >/dev/null; then cygpath -m "$1"; else printf %s "$1"; fi; }
DWORK="$(native "$WORK")"; DCONFIG="$(native "$CONFIG")"

# Stub sink: appends one JSON line {path, receiver, status} per webhook POST (Alertmanager payload carries "receiver").
cat > "$WORK/sink.py" <<'PY'
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        with open("/out/received.jsonl", "a") as f:
            f.write(json.dumps({"path": self.path, "receiver": body.get("receiver"), "status": body.get("status")}) + "\n")
        self.send_response(200); self.end_headers()
    def log_message(self, *a): pass
http.server.HTTPServer(("0.0.0.0", 8080), H).serve_forever()
PY
printf 'http://sink:8080/ticket' > "$WORK/ticket_url"
printf 'http://sink:8080/page' > "$WORK/page_url"
: > "$WORK/received.jsonl"
chmod -R a+rX "$WORK"; chmod a+rw "$WORK/received.jsonl"

docker network create "$RUN" >/dev/null
docker run -d --name "$RUN-sink" --network "$RUN" --network-alias sink -v "$DWORK/sink.py:/sink.py:ro" -v "$DWORK:/out" \
  python:3.12-slim python /sink.py >/dev/null
docker run -d --name "$RUN-am" --network "$RUN" \
  -v "$DCONFIG:/etc/alertmanager/alertmanager.yml:ro" \
  -v "$DWORK/ticket_url:/run/secrets/alert_webhook_url:ro" -v "$DWORK/page_url:/run/secrets/alert_page_webhook_url:ro" \
  "$IMAGE" --config.file=/etc/alertmanager/alertmanager.yml --storage.path=/tmp/am >/dev/null

for _ in $(seq 30); do docker exec "$RUN-am" wget -qO- http://localhost:9093/-/ready >/dev/null 2>&1 && break; sleep 1; done
docker exec "$RUN-am" amtool alert add --alertmanager.url=http://localhost:9093 alertname=SmokeTicket severity=ticket --annotation=summary=smoke >/dev/null
docker exec "$RUN-am" amtool alert add --alertmanager.url=http://localhost:9093 alertname=SmokePage severity=page --annotation=summary=smoke >/dev/null

want() { grep -q "\"path\": \"/$1\", \"receiver\": \"$2\", \"status\": \"firing\"" "$WORK/received.jsonl"; }
for ((t = 0; t < TIMEOUT; t++)); do
  if want ticket owner-webhook && want page owner-page; then
    echo "PASS: ticket -> owner-webhook and page -> owner-page delivered after ${t}s"; cat "$WORK/received.jsonl"; exit 0
  fi
  sleep 1
done
echo "FAIL: alerts not delivered with the expected receivers within ${TIMEOUT}s. Received:" >&2
cat "$WORK/received.jsonl" >&2
docker logs "$RUN-am" 2>&1 | tail -15 >&2
exit 1

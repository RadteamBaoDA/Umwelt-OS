#!/usr/bin/env bash
set -euo pipefail

pytest_target="${PYTEST_TARGET:-}"
e2e_target="${E2E_TARGET:-}"
if [[ -n "$pytest_target" && -n "$e2e_target" ]]; then
  echo 'Choose only one of PYTEST_TARGET or E2E_TARGET.' >&2
  exit 2
fi

if [[ -n "$pytest_target" && "$pytest_target" != tests/integration/* ]]; then
  uv run pytest "$pytest_target" -q
  exit
fi

if [[ -z "$pytest_target" && -z "$e2e_target" ]]; then
  uv run pytest -q
fi

project="bbd-os-test-$(python -c 'import secrets; print(secrets.token_hex(5))')"
free_port() { python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'; }
export WEB_PORT="$(free_port)"
export API_TEST_PORT="$(free_port)"
while [[ "$API_TEST_PORT" == "$WEB_PORT" ]]; do export API_TEST_PORT="$(free_port)"; done
export TEST_POSTGRES_PORT="$(free_port)"
while [[ "$TEST_POSTGRES_PORT" == "$WEB_PORT" || "$TEST_POSTGRES_PORT" == "$API_TEST_PORT" ]]; do export TEST_POSTGRES_PORT="$(free_port)"; done
export TEST_PUBLIC_ORIGIN="http://localhost:${WEB_PORT}"
export TEST_DATABASE_URL="postgresql+asyncpg://bbd_test:bbd-os-test-db-only@127.0.0.1:${TEST_POSTGRES_PORT}/bbd_test"
compose=(docker compose -p "$project" -f docker-compose.yml -f docker-compose.test.yml)
cleanup() { "${compose[@]}" down --volumes --remove-orphans; }
trap cleanup EXIT
"${compose[@]}" up -d --build
# `up` waits for migration completion; this second run proves the revision is idempotent.
"${compose[@]}" run --rm migrate
"${compose[@]}" run --rm --no-deps api python -c 'import apps.api.main, core.auth.dependencies, modules'

export BBD_INTEGRATION=1
export BBD_API_URL="http://localhost:${API_TEST_PORT}"
"${compose[@]}" exec -T postgres psql -U bbd_test -d bbd_test -At -c "SELECT current_database() || '|' || current_user" | grep -Fx 'bbd_test|bbd_test' >/dev/null
uv run pytest tests/integration/test_auth_race.py -q
if [[ -n "$pytest_target" ]]; then
  if [[ "$pytest_target" != tests/integration/test_auth_race.py ]]; then
    uv run pytest "$pytest_target" -q
  fi
else
  uv run pytest tests/integration -q --ignore=tests/integration/test_auth_race.py
fi

if [[ -z "$pytest_target" || -n "$e2e_target" ]]; then
  # Only this uniquely named disposable project and verified test identity may be reset.
  [[ "$project" =~ ^bbd-os-test-[0-9a-f]{10}$ ]]
  "${compose[@]}" exec -T postgres psql -U bbd_test -d bbd_test -v ON_ERROR_STOP=1 -c "$(tr -d '\r' < "$(dirname "${BASH_SOURCE[0]}")/sql/e2e-reset.sql" | tr '\n' ' ')"
  export E2E_SETUP_TOKEN=bbd-os-disposable-test-token
  export PLAYWRIGHT_BASE_URL="http://localhost:${WEB_PORT}"
  export PLAYWRIGHT_EXTERNAL_SERVER=1
  if [[ -n "$e2e_target" ]]; then
    npm run test:e2e -- "$e2e_target"
  else
    npm run test:e2e
  fi
fi

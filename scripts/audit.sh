#!/usr/bin/env bash
# Dependency vulnerability audit (see docs/security.md, "Dependency policy").
# npm: fails on high/critical advisories (including dev dependencies).
# Python: audits the exact locked production set from uv.lock with pip-audit.
set -euo pipefail

cd "$(dirname "$0")/.."

npm audit --audit-level=high

requirements="$(mktemp)"
trap 'rm -f "$requirements"' EXIT
uv export --frozen --no-hashes --no-dev --no-emit-project -o "$requirements" >/dev/null
# --no-deps/--disable-pip: the export is already fully resolved; no env is built.
uvx pip-audit --no-deps --disable-pip -r "$requirements"

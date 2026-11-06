#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
docker compose -p tvg-v252-test -f compose.yaml up -d --build
docker compose -p tvg-v252-test -f compose.yaml ps

echo
echo "test URL: http://<NAS>:${TGDL_TEST_PORT:-18787}"
echo "bootstrap token:"
echo "  docker compose -p tvg-v252-test -f compose.yaml exec tvg-v2-5-2-test cat /data/.setup-token"

#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

docker compose up -d --build --force-recreate telegram-asr-bot
docker compose logs -f --tail=100 telegram-asr-bot

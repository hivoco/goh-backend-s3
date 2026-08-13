#!/bin/bash

# Start the Grains of Hope API.
#
# Port 8000 by default — that's what admin/.env.local and frontend/.env.local
# point at, and NEXT_PUBLIC_* is baked in at build time, so the two must agree
# or every request fails with ERR_CONNECTION_REFUSED.
#
# Override when 8000 is taken (the hero campaign's API also uses it):
#     PORT=8001 ./start_server.sh
# and change NEXT_PUBLIC_API_BASE_URL in both .env.local files to match.

cd "$(dirname "$0")"

PORT="${PORT:-8000}"
echo "Grains of Hope API -> http://127.0.0.1:${PORT}  (docs at /docs)"

.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port "${PORT}"

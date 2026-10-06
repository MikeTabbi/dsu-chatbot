#!/usr/bin/env bash
# Runs the chat assistant demo: the backend (http://localhost:8000) and the demo page
# (http://localhost:8080/demo.html). Ctrl+C stops both.
#
# Answers come from Claude when .env has CLAUDE_CLIENT=anthropic and ANTHROPIC_API_KEY set.
# With CLAUDE_CLIENT=fake (the default in .env.example) answers start with "[fake answer]".
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "No .env file. Copy .env.example to .env, then set CLAUDE_CLIENT=anthropic and ANTHROPIC_API_KEY."
  exit 1
fi
if [ ! -s data/index/chunks.json ]; then
  echo "Warning: no data/index/chunks.json, so every question gets the \"couldn't find anything\" answer."
  echo "         Build it with python -m ingestion.pipeline (see the main README, \"Retrieval\")."
fi

PYTHON=python3
[ -x .venv/bin/python ] && PYTHON=.venv/bin/python
client=$(grep -E '^CLAUDE_CLIENT=' .env | tail -1 | cut -d= -f2 | tr -d '[:space:]"')
echo "Claude client: ${client:-fake} (from .env)"

"$PYTHON" -m uvicorn api.app.main:app --port 8000 &
api=$!
"$PYTHON" -m http.server 8080 --bind 127.0.0.1 --directory widget >/dev/null 2>&1 &
web=$!
trap 'kill $api $web 2>/dev/null' EXIT INT TERM

sleep 2
echo
echo "Demo page: http://localhost:8080/demo.html   (Ctrl+C to stop)"
command -v open >/dev/null && open "http://localhost:8080/demo.html"
wait

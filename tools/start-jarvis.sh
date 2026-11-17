#!/usr/bin/env bash
# Start Jarvis: Postgres, then the API server, then open the UI in the Windows browser.
# Used by the "Jarvis" desktop shortcut. Closing the window stops the server.
set -euo pipefail

unset PYTHONPATH   # ROS (sourced in ~/.bashrc) sets it and breaks the venv's Python

cd "$(dirname "$0")/.."
URL="http://localhost:8000"

if curl -s -m 2 "$URL/health" >/dev/null; then
  echo "Jarvis is already running: opening $URL"
  cmd.exe /c start "" "$URL" >/dev/null 2>&1
  sleep 2
  exit 0
fi

echo "Starting Postgres..."
docker compose up -d --wait db

if ! curl -s -m 2 localhost:11434 >/dev/null; then
  echo "WARNING: Ollama is not responding on :11434 (try: sudo systemctl start ollama)"
fi

# Open the browser once the server answers its health check.
(
  for _ in $(seq 1 90); do
    if curl -s -m 2 "$URL/health" >/dev/null; then
      cmd.exe /c start "" "$URL" >/dev/null 2>&1
      exit 0
    fi
    sleep 1
  done
  echo "Jarvis did not become healthy within 90s; check the log above."
) &

echo "Starting Jarvis on $URL  (close this window or press Ctrl+C to stop)"
# Run from the project root so .env is found.
exec .venv/bin/uvicorn jarvis.api:app --app-dir src

#!/bin/bash
# Double-clickable launcher. Starts the agent and opens it in your browser.
# Close this window (or press Ctrl-C) to stop it.

cd "$(dirname "$0")" || exit 1

PORT="$(grep -E '^LOG_AGENT_PORT=' .env 2>/dev/null | cut -d= -f2 | tr -d ' "'"'"'')"
PORT="${PORT:-3000}"

# Reuse an instance that is already up rather than failing on a bound port.
if lsof -ti:"$PORT" >/dev/null 2>&1; then
  echo "Log Agent is already running on port $PORT - opening it."
  open "http://127.0.0.1:$PORT"
  exit 0
fi

PY="python3"
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"

echo "Starting Log Agent…"
"$PY" run.py &
AGENT=$!

# Wait for the server to answer before opening the browser, so the first load is
# never an error page.
for _ in $(seq 1 40); do
  curl -s -o /dev/null "http://127.0.0.1:$PORT/api/state" 2>/dev/null && break
  sleep 0.25
done

open "http://127.0.0.1:$PORT"
echo
echo "Log Agent is running at http://127.0.0.1:$PORT"
echo "Close this window to stop it."
trap 'kill $AGENT 2>/dev/null' EXIT
wait $AGENT

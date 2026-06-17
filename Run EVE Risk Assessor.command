#!/bin/zsh
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
URL="http://127.0.0.1:8731"
PORT="8731"

cd "$APP_DIR"

if lsof -iTCP:"$PORT" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  echo "EVE Risk Assessor is already running."
  open "$URL"
  exit 0
fi

if [ ! -x "$APP_DIR/venv/bin/uvicorn" ]; then
  echo "Could not find venv/bin/uvicorn."
  echo "Open Terminal in this folder and run:"
  echo "  python3 -m venv venv"
  echo "  venv/bin/pip install -r requirements.txt"
  echo
  read "?Press Return to close."
  exit 1
fi

echo "Starting EVE Risk Assessor..."
echo "Close this Terminal window to stop the app."
echo

(
  sleep 2
  open "$URL"
) &

"$APP_DIR/venv/bin/uvicorn" backend.api:app --host 127.0.0.1 --port "$PORT"

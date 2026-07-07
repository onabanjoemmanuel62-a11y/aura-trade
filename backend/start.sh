#!/bin/bash
set -e

echo "Starting Python Brain..."
uvicorn aura_brain.brain:app --host 0.0.0.0 --port 8000 &
PYTHON_PID=$!

echo "Waiting for Python to be ready..."
READY=0
for i in $(seq 1 30); do
  # If the python process has died, stop waiting immediately — no point polling a dead process
  if ! kill -0 $PYTHON_PID 2>/dev/null; then
    echo "❌ Python Brain process died before becoming ready. Aborting deploy."
    wait $PYTHON_PID   # re-raise its exit code / print any final output
    exit 1
  fi

  if curl -s http://127.0.0.1:8000/docs > /dev/null 2>&1; then
    echo "✅ Python Brain is ready!"
    READY=1
    break
  fi
  echo "  attempt $i/30..."
  sleep 2
done

if [ "$READY" -ne 1 ]; then
  echo "❌ Python Brain did not become ready after 30 attempts (60s). Aborting deploy."
  kill $PYTHON_PID 2>/dev/null || true
  exit 1
fi

echo "Starting Node Server..."
exec npm start
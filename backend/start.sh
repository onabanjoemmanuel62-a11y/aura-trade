#!/bin/bash
echo "Starting Python Brain..."
uvicorn aura_brain.brain:app --host 0.0.0.0 --port 8000 &

echo "Waiting for Python to be ready..."
for i in $(seq 1 30); do
  if curl -s http://127.0.0.1:8000/docs > /dev/null 2>&1; then
    echo "Python Brain is ready!"
    break
  fi
  echo "  attempt $i/30..."
  sleep 2
done

echo "Starting Node Server..."
exec npm start
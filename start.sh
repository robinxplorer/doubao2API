#/bin/bash

function start() {
  echo "Starting..."
  python -m uvicorn backend.main:app --host 0.0.0.0 --port 7861
}

start

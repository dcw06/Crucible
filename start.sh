#!/bin/bash
# Render: runs uvicorn internally on 8000, streamlit publicly on $PORT
# Local: not used — run uvicorn and streamlit separately as before

set -e

# Start uvicorn API in background on fixed internal port
uvicorn main:app --host 0.0.0.0 --port 8000 &

# Start streamlit on the public port Render assigns
streamlit run dashboard.py \
  --server.port "${PORT:-8501}" \
  --server.address 0.0.0.0 \
  --server.headless true

# If streamlit exits, kill the background uvicorn too
kill %1 2>/dev/null || true

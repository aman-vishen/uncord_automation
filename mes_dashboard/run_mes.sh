#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
: "${INGEST_API_KEY:=local-development-key}"
: "${DASHBOARD_USERNAME:=admin}"
: "${DASHBOARD_PASSWORD:=admin}"
: "${PORT:=8080}"
export INGEST_API_KEY DASHBOARD_USERNAME DASHBOARD_PASSWORD PORT
python3 app.py

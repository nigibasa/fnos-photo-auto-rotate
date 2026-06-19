#!/bin/sh
set -eu

mkdir -p "${DATA_DIR:-/data}/logs" "${DATA_DIR:-/data}/backups"
exec python3 /app/server.py

#!/bin/bash
# Stops ONLY the OpenLease process — scoped by port, not by module path. OpenProp uses the
# identical "uvicorn app.app:app" module path, so `pkill -f` would kill both; port 8788 is
# what distinguishes OpenLease (OpenProp holds 8787), so that's what we kill by.
PORT="${OPENLEASE_PORT:-8788}"
PIDS="$(lsof -ti ":$PORT")"
if [ -n "$PIDS" ]; then
  kill $PIDS
  echo "OpenLease (port $PORT) stopped. You can close this window."
else
  echo "OpenLease (port $PORT) was not running."
fi

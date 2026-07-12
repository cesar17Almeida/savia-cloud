#!/bin/sh
# Hourly tick for the daily inference; the backend gates per station by local hour.
set -eu
. /etc/savia-cloud/env
exec curl -fsS -X POST -H "X-Cron-Token: ${CRON_SECRET}" "http://127.0.0.1:${PORT:-8000}/cron/daily"

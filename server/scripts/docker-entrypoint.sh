#!/bin/sh
set -eu

if [ "${CCM_ENV:-}" != "production" ]; then
    echo "FATAL: CCM_ENV must be explicitly set to 'production' in the container runtime environment." >&2
    exit 1
fi
case "${CCM_AUTH_MODE:-}" in
    session|token) ;;
    *)
        echo "FATAL: CCM_AUTH_MODE must be explicitly set to 'session' or 'token' in the container runtime environment." >&2
        exit 1
        ;;
esac

mkdir -p /data/logs /data/backups
rm -rf -- /data/backups/local

python scripts/manage_db.py apply-pending-restore-if-any
alembic upgrade head
python scripts/manage_db.py verify

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1

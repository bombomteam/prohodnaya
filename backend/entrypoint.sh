#!/bin/sh
set -e

# alembic upgrade head   # включается в Ф2, когда появятся миграции

exec uvicorn app.main:app --host 0.0.0.0 --port 8000

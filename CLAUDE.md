# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Project rules

- For any codebase search (files, symbols, usages, "where/how is X"), delegate to the `haiku-scout` subagent instead of searching yourself. Use its report to act.
- Also delegate cheap bulk work (classification, extraction, summarizing files) to `haiku-scout`.
- Backend work goes phase by phase per `PRD-backend.md` §10: `backend-coder` (sonnet) writes, `phase-reviewer` (opus) accepts, then tests in the container and one commit per phase with a Russian message (`бэк: ...`).
- Do not pull Docker images yourself; tell the user which ones are needed.

# Commands

Stack (frontend + db + backend) from the repo root; `.env` from `.env.example` with `POSTGRES_*`/`DATABASE_URL` filled. `COMPOSE_PROFILES=backend` enables db and backend; without it only frontend starts.

```
docker compose up -d --build db backend
docker compose exec -T backend python -m pytest -q -p no:cacheprovider tests
docker compose exec -T backend python -m pytest -q -p no:cacheprovider tests/test_main.py::test_health
curl localhost:8000/health        # API docs: localhost:8000/docs
```

There is no Python 3.12 on the host (system python3 is 3.9): run backend tests inside the container.

# Backend architecture (`backend/app`)

- FastAPI + **sync** SQLAlchemy 2 (PRD-mandated; `face.analyze` is CPU-bound sync). Only assistant routes are `async` (the `llm` contract is async).
- Layers: `api/` (routes, no SQL/logic) → `services/` (business logic) → `repositories/` (DB queries; `repositories/queries.py` is the single source of numbers for analytics, reports and the assistant). `schemas/` pydantic, `models/` ORM, `core/` config/exceptions/handlers/logging/auth/crypto, `dependency.py` holds `XDep = Annotated[...]` wrappers.
- All §8 routes are under `/api` (`api/router.py`); `/health` stays at the root.
- Error format everywhere: `{code, message}`, Russian `message`; raise `core.exceptions.ApiError`. Handlers in `core/handlers.py`.
- `core/auth.py` is temporary (Keycloak owner replaces it; keep `current_user`/`require_role` signatures). `AUTH_DEV_BYPASS=true` gives a dev user with all roles.
- `app/face/` and `app/llm/` are interfaces owned by other teammates — don't implement them, never add a fake "match"/canned answer. Tests monkeypatch `face`.
- Route bodies still marked `# STUB` are placeholders from Ф1; `tests/test_no_stubs.py` (Ф8) must find none.
- The pass decision is server-side only: nonce is one-shot and consumed by atomic `UPDATE ... WHERE status=... RETURNING`; every deny writes an `access_events` row.

Frontend: see `frontend/AGENTS.md` before touching `frontend/`.

@AGENTS.md

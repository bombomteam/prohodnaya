# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Project rules

- For any codebase search (files, symbols, usages, "where/how is X"), delegate to the `haiku-scout` subagent instead of searching yourself. Use its report to act.
- Also delegate cheap bulk work (classification, extraction, summarizing files) to `haiku-scout`.
- Backend work goes phase by phase per `PRD-backend.md` §10: `backend-coder` (sonnet) writes, `phase-reviewer` (opus) accepts, then tests in the container and one commit per phase with a Russian message (`бэк: ...`).
- Do not pull Docker images yourself; tell the user which ones are needed.

# Repo ownership and git (bombomteam/prohodnaya)

- Folder owners: `backend/` — goldslugger762 (this user), `frontend/` and `keycloak/` — scuf77, `face/` — DevOps. Edit only `backend/`. Never change other folders or the shared `compose.yaml`, `.env.example`, `.github/`, `deploy/` — write the needed change as a request for the owner instead.
- Before work: `git pull --rebase`. Work in your own branch; into `main` only via PR. Never `git push --force`. A push to `main` deploys to the stand, so run backend tests first.
- Never commit `.env`, passwords, tokens, keys, photos/videos (except `case_prerekvizity/`), models, archives, DB dumps, files > 1 MB. Check `git status` and `git add` files by name, not `-A` or a whole directory.
- Small commits: `бэк: …` (`фронт: …`, `keycloak: …` for others).
- CI runs on every push and PR (tests of your part, secret and junk scan); the result is the `ci-ok` check. Into `main` only via a PR with green `ci-ok`. Red CI: open Actions, find the failed step, fix it in your branch. DB-backed backend tests skip without Postgres, so a green `ci-ok` alone doesn't prove they ran — also run them in the container.
- Push to `main` deploys to the stand https://hack-detect.duckdns.org (login: https://auth.hack-detect.duckdns.org). A failed deploy rolls back automatically; manual rollback: Actions → Rollback → Run workflow. `git push --force` is forbidden.
- Full rules: section «Git, CI и стенд» in `AGENTS.md`.

# Commands

Stack (frontend + keycloak + db + backend) from the repo root; `.env` from `.env.example` with `POSTGRES_*`/`DATABASE_URL` and the four `KC_*_PASSWORD` filled (without the KC passwords compose refuses to start any service). `COMPOSE_PROFILES=backend` enables db and backend. The `face` service is not in compose yet, so person creation returns 503 until it is.

```
docker compose up -d --build db backend
docker compose exec -T backend python -m pytest -q -p no:cacheprovider tests
docker compose exec -T backend python -m pytest -q -p no:cacheprovider tests/test_main.py::test_health
curl localhost:8000/health        # API docs: localhost:8000/docs
```

There is no Python 3.12 on the host (system python3 is 3.9): run backend tests inside the container.

# Backend architecture (`backend/app`)

- FastAPI + **sync** SQLAlchemy 2 (PRD-mandated; the face client is sync httpx). Only assistant routes are `async` (the `llm` contract is async).
- Layers: `api/` (routes, no SQL/logic) → `services/` (business logic) → `repositories/` (DB queries; `repositories/queries.py` is the single source of numbers for analytics, reports and the assistant). `schemas/` pydantic, `models/` ORM, `core/` config/exceptions/handlers/logging/auth/crypto, `dependency.py` holds `XDep = Annotated[...]` wrappers.
- All §8 routes are under `/api` (`api/router.py`); `/health` stays at the root.
- Error format everywhere: `{code, message}`, Russian `message`; raise `core.exceptions.ApiError`. Handlers in `core/handlers.py`.
- `core/auth.py` is temporary (Keycloak owner replaces it; keep `current_user`/`require_role` signatures). `AUTH_DEV_BYPASS=true` gives a dev user with all roles.
- `app/face/` is the thin HTTP client to the face module (contract `face/docs/api.md` v1); the module itself lives in `/face` and has another owner — don't touch `/face`. `app/llm/` is an interface owned by another teammate — don't implement it. Never add a fake "match"/canned answer; any face failure = deny. Tests monkeypatch `face` functions or the httpx transport.
- Face reason codes map to `FACE_<REASON>` (e.g. `FACE_NO_FACE`, `FACE_FACE_MISMATCH`) with the module's `reason.text`; `FaceRejected` (4xx bad_image/unsupported_format/too_large) → 422 on enrollment; `FaceUnavailable` → `FACE_SERVICE_UNAVAILABLE`. A pass is allowed only on valid phone signature AND `status == "ok"`.
- Route bodies still marked `# STUB` are placeholders from Ф1; `tests/test_no_stubs.py` (Ф8) must find none.
- The pass decision is server-side only: nonce is one-shot and consumed by atomic `UPDATE ... WHERE status=... RETURNING`; every deny writes an `access_events` row.
- Pass flow (`services/passage.py`, Ф4): `/sign` builds the signed message `session_id|nonce|direction` from the DB, never from the client; a bad signature keeps the session `pending`. `/face` checks business rules first (no module call on deny), commits, calls `face.verify_frames` outside any transaction, then decision + shift + event in one transaction (`_finish`). Business deny = 200 `decision=deny`; technical errors: 404 `SESSION_NOT_FOUND`, 403 `DEVICE_INVALID`, 409 `SESSION_EXPIRED`, 403 `SESSION_FOREIGN`.
- Tests needing a DB use the separate `<db>_test` database from `tests/conftest.py` (fixtures `client`, `db`); never point tests at the app DB.

Frontend: see `frontend/AGENTS.md` before touching `frontend/`.

@AGENTS.md
